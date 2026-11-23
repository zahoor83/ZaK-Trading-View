"""Create independent Spot/Futures portfolios and paper sessions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_portfolios_sessions"
down_revision = "0001_identity_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No automatic backfill is safe because Futures position mode
    # requires an explicit user decision.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM trading_accounts) THEN
                RAISE EXCEPTION
                    'Migration 0002 requires an empty trading_accounts table. '
                    'Existing accounts need an explicitly approved portfolio backfill.';
            END IF;
        END;
        $$;
    """)

    op.create_table(
        "paper_portfolios",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trading_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("market_type", sa.String(10), nullable=False),
        sa.Column("position_mode", sa.String(10)),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "account_id",
            "id",
            name="uq_paper_portfolios_account_id_id",
        ),
        sa.UniqueConstraint(
            "account_id",
            "market_type",
            name="uq_paper_portfolios_account_market",
        ),
        sa.CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_paper_portfolios_market_type",
        ),
        sa.CheckConstraint(
            "(market_type = 'spot' AND position_mode IS NULL) OR "
            "(market_type = 'futures' AND "
            "position_mode IN ('one_way', 'hedge'))",
            name="ck_paper_portfolios_position_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_paper_portfolios_status",
        ),
    )

    op.create_index(
        "ix_paper_portfolios_account_status",
        "paper_portfolios",
        ["account_id", "status"],
    )

    op.create_table(
        "paper_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "portfolio_id"],
            ["paper_portfolios.account_id", "paper_portfolios.id"],
            name="fk_paper_sessions_account_portfolio",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "account_id",
            "portfolio_id",
            "session_number",
            name="uq_paper_sessions_portfolio_number",
        ),
        sa.UniqueConstraint(
            "account_id",
            "portfolio_id",
            "id",
            name="uq_paper_sessions_account_portfolio_id",
        ),
        sa.CheckConstraint(
            "session_number > 0",
            name="ck_paper_sessions_number_positive",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND archived_at IS NULL) OR "
            "(status = 'archived' AND archived_at IS NOT NULL)",
            name="ck_paper_sessions_archive_state",
        ),
    )

    op.create_index(
        "uq_paper_sessions_one_active",
        "paper_sessions",
        ["portfolio_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_paper_sessions_account_portfolio",
        "paper_sessions",
        ["account_id", "portfolio_id"],
    )

    # Every committed account must own exactly one Spot and one Futures
    # portfolio. Account creation therefore inserts all three records
    # inside the same transaction.
    op.execute("""
        CREATE FUNCTION assert_account_portfolio_pair(p_account_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            portfolio_count integer;
            spot_count integer;
            futures_count integer;
        BEGIN
            -- Serialize integrity checks for this account.
            PERFORM id
              FROM trading_accounts
             WHERE id = p_account_id
             FOR UPDATE;

            IF NOT FOUND THEN
                RETURN;
            END IF;

            SELECT count(*),
                   count(*) FILTER (WHERE market_type = 'spot'),
                   count(*) FILTER (WHERE market_type = 'futures')
              INTO portfolio_count, spot_count, futures_count
              FROM paper_portfolios
             WHERE account_id = p_account_id;

            IF portfolio_count <> 2
               OR spot_count <> 1
               OR futures_count <> 1 THEN
                RAISE EXCEPTION
                    'Account % must have exactly one Spot and one Futures portfolio',
                    p_account_id
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$;
    """)

    op.execute("""
        CREATE FUNCTION check_account_portfolio_pair_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_TABLE_NAME = 'trading_accounts' THEN
                IF TG_OP <> 'INSERT' THEN
                    PERFORM assert_account_portfolio_pair(OLD.id);
                END IF;

                IF TG_OP <> 'DELETE' THEN
                    PERFORM assert_account_portfolio_pair(NEW.id);
                END IF;
            ELSE
                IF TG_OP <> 'INSERT' THEN
                    PERFORM assert_account_portfolio_pair(OLD.account_id);
                END IF;

                IF TG_OP <> 'DELETE' THEN
                    PERFORM assert_account_portfolio_pair(NEW.account_id);
                END IF;
            END IF;

            RETURN NULL;
        END;
        $$;
    """)

    # Serialize portfolio mutations through the parent account row.
    op.execute("""
        CREATE FUNCTION lock_paper_portfolio_account()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_account_id uuid;
        BEGIN
            IF TG_OP = 'UPDATE'
               AND NEW.account_id IS DISTINCT FROM OLD.account_id THEN
                RAISE EXCEPTION
                    'paper_portfolios.account_id is immutable'
                    USING ERRCODE = '23514';
            END IF;

            IF TG_OP = 'DELETE' THEN
                target_account_id := OLD.account_id;
            ELSE
                target_account_id := NEW.account_id;
            END IF;

            PERFORM id
              FROM trading_accounts
             WHERE id = target_account_id
             FOR UPDATE;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;

            RETURN NEW;
        END;
        $$;
    """)

    op.execute("""
        CREATE TRIGGER trg_lock_paper_portfolio_account
        BEFORE INSERT OR UPDATE OR DELETE ON paper_portfolios
        FOR EACH ROW
        EXECUTE FUNCTION lock_paper_portfolio_account();
    """)

    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_account_portfolio_pair
        AFTER INSERT OR UPDATE OR DELETE ON trading_accounts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_account_portfolio_pair_trigger();
    """)

    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_portfolio_pair_integrity
        AFTER INSERT OR UPDATE OR DELETE ON paper_portfolios
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_account_portfolio_pair_trigger();
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_lock_paper_portfolio_account "
        "ON paper_portfolios"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS lock_paper_portfolio_account()"
    )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_portfolio_pair_integrity "
        "ON paper_portfolios"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_account_portfolio_pair "
        "ON trading_accounts"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS check_account_portfolio_pair_trigger()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS assert_account_portfolio_pair(uuid)"
    )

    op.drop_index(
        "ix_paper_sessions_account_portfolio",
        table_name="paper_sessions",
    )
    op.drop_index(
        "uq_paper_sessions_one_active",
        table_name="paper_sessions",
    )
    op.drop_table("paper_sessions")

    op.drop_index(
        "ix_paper_portfolios_account_status",
        table_name="paper_portfolios",
    )
    op.drop_table("paper_portfolios")