"""Create identity and account membership foundation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_identity_accounts"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(320)),
        sa.Column("email_normalized", sa.String(320), unique=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'disabled')",
            name="ck_users_status",
        ),
    )

    op.create_table(
        "auth_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint(
            "issuer", "subject",
            name="uq_auth_identities_issuer_subject",
        ),
    )
    op.create_index(
        "ix_auth_identities_user_id",
        "auth_identities",
        ["user_id"],
    )

    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_user_sessions_user_id", "user_sessions", ["user_id"]
    )
    op.create_index(
        "ix_user_sessions_expires_at", "user_sessions", ["expires_at"]
    )

    op.create_table(
        "trading_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'deletion_pending', 'deleted')",
            name="ck_trading_accounts_status",
        ),
    )
    op.create_index(
        "ix_trading_accounts_owner_user_id",
        "trading_accounts",
        ["owner_user_id"],
    )

    op.create_table(
        "account_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trading_accounts.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'active'")),
        sa.Column("invited_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint(
            "account_id", "user_id",
            name="uq_account_memberships_account_user",
        ),
        sa.UniqueConstraint(
            "account_id", "id",
            name="uq_account_memberships_account_id_id",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'trader', 'viewer')",
            name="ck_account_memberships_role",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'suspended', 'revoked')",
            name="ck_account_memberships_status",
        ),
    )
    op.create_index(
        "ix_account_memberships_user_id",
        "account_memberships",
        ["user_id"],
    )
    op.create_index(
        "ix_account_memberships_account_status",
        "account_memberships",
        ["account_id", "status"],
    )
    op.create_index(
        "uq_account_memberships_one_active_owner",
        "account_memberships",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner' AND status = 'active'"),
    )

    op.create_table(
        "account_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trading_accounts.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("email_normalized", sa.String(320), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("invited_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "role IN ('trader', 'viewer')",
            name="ck_account_invitations_role",
        ),
    )
    op.create_index(
        "ix_account_invitations_account_email",
        "account_invitations",
        ["account_id", "email_normalized"],
    )

    # Deferred checks permit account and owner membership to be inserted
    # within the same transaction, but reject incomplete ownership at commit.
    op.execute("""
        CREATE FUNCTION assert_account_owner_membership(p_account_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_owner uuid;
            active_owner_count integer;
        BEGIN
            SELECT owner_user_id
              INTO expected_owner
              FROM trading_accounts
             WHERE id = p_account_id;

            IF NOT FOUND THEN
                RETURN;
            END IF;

            SELECT count(*)
              INTO active_owner_count
              FROM account_memberships
             WHERE account_id = p_account_id
               AND role = 'owner'
               AND status = 'active';

            IF active_owner_count <> 1 OR NOT EXISTS (
                SELECT 1
                  FROM account_memberships
                 WHERE account_id = p_account_id
                   AND user_id = expected_owner
                   AND role = 'owner'
                   AND status = 'active'
            ) THEN
                RAISE EXCEPTION
                    'Account % must have exactly one active owner matching owner_user_id',
                    p_account_id
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$;
    """)

    op.execute("""
        CREATE FUNCTION check_account_owner_membership_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_TABLE_NAME = 'trading_accounts' THEN
                IF TG_OP <> 'INSERT' THEN
                    PERFORM assert_account_owner_membership(OLD.id);
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    PERFORM assert_account_owner_membership(NEW.id);
                END IF;
            ELSE
                IF TG_OP <> 'INSERT' THEN
                    PERFORM assert_account_owner_membership(OLD.account_id);
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    PERFORM assert_account_owner_membership(NEW.account_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;
    """)

    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_account_owner_integrity
        AFTER INSERT OR UPDATE OR DELETE ON trading_accounts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_account_owner_membership_trigger();
    """)

    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_membership_owner_integrity
        AFTER INSERT OR UPDATE OR DELETE ON account_memberships
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_account_owner_membership_trigger();
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_membership_owner_integrity "
        "ON account_memberships"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_account_owner_integrity "
        "ON trading_accounts"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS check_account_owner_membership_trigger()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS assert_account_owner_membership(uuid)"
    )

    op.drop_index(
        "ix_account_invitations_account_email",
        table_name="account_invitations",
    )
    op.drop_table("account_invitations")

    op.drop_index(
        "uq_account_memberships_one_active_owner",
        table_name="account_memberships",
    )
    op.drop_index(
        "ix_account_memberships_account_status",
        table_name="account_memberships",
    )
    op.drop_index(
        "ix_account_memberships_user_id",
        table_name="account_memberships",
    )
    op.drop_table("account_memberships")

    op.drop_index(
        "ix_trading_accounts_owner_user_id",
        table_name="trading_accounts",
    )
    op.drop_table("trading_accounts")

    op.drop_index("ix_user_sessions_expires_at", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")

    op.drop_index("ix_auth_identities_user_id", table_name="auth_identities")
    op.drop_table("auth_identities")
    op.drop_table("users")