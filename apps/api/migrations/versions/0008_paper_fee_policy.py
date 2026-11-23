"""Versioned account-level paper fee policies.

Revision ID: 0008_paper_fee_policy
Revises: 0007_paper_trading_ledger
"""

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa

revision = "0008_paper_fee_policy"
down_revision = "0007_paper_trading_ledger"
branch_labels = None
depends_on = None


def _schema_sql():
    connection = op.get_bind()
    schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one_or_none()
    if not isinstance(schema, str) or not schema:
        raise RuntimeError("Cannot resolve fee-policy migration schema.")
    return connection.dialect.identifier_preparer.quote_schema(schema)


def upgrade():
    op.create_table(
        "paper_fee_policy_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("market_type", sa.String(10), nullable=False),
        sa.Column("policy_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("entry_fee_rate", sa.Numeric(), nullable=False),
        sa.Column("exit_fee_rate", sa.Numeric(), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_paper_fee_policy_versions_account", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_paper_fee_policy_versions_creator", ondelete="RESTRICT"),
        sa.UniqueConstraint("account_id", "market_type", "version", name="uq_paper_fee_policy_versions_account_market_version"),
        sa.UniqueConstraint("id", "account_id", "market_type", "version", name="uq_paper_fee_policy_versions_pointer_target"),
        sa.CheckConstraint("market_type IN ('spot', 'futures')", name="ck_paper_fee_policy_versions_market"),
        sa.CheckConstraint("length(policy_id) BETWEEN 1 AND 64", name="ck_paper_fee_policy_versions_policy_id"),
        sa.CheckConstraint("version > 0", name="ck_paper_fee_policy_versions_version"),
        sa.CheckConstraint(
            "entry_fee_rate >= 0 AND entry_fee_rate < 1e36 AND scale(entry_fee_rate) <= 36 "
            "AND exit_fee_rate >= 0 AND exit_fee_rate < 1e36 AND scale(exit_fee_rate) <= 36",
            name="ck_paper_fee_policy_versions_rates",
        ),
    )

    op.create_table(
        "paper_fee_policy_heads",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("market_type", sa.String(10), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "market_type"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], name="fk_paper_fee_policy_heads_account", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["version_id", "account_id", "market_type", "version"],
            ["paper_fee_policy_versions.id", "paper_fee_policy_versions.account_id",
             "paper_fee_policy_versions.market_type", "paper_fee_policy_versions.version"],
            name="fk_paper_fee_policy_heads_current_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("market_type IN ('spot', 'futures')", name="ck_paper_fee_policy_heads_market"),
        sa.CheckConstraint("version > 0", name="ck_paper_fee_policy_heads_version"),
    )

    schema = _schema_sql()
    op.execute(f"""
        CREATE FUNCTION {schema}.paper_fee_policy_version_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {schema}
        AS $function$
        BEGIN
            RAISE EXCEPTION 'Paper fee-policy versions are immutable'
                USING ERRCODE = '23514';
        END;
        $function$
    """)
    op.execute(f"""
        CREATE TRIGGER trg_paper_fee_policy_version_immutable
        BEFORE UPDATE OR DELETE ON {schema}.paper_fee_policy_versions
        FOR EACH ROW
        EXECUTE FUNCTION {schema}.paper_fee_policy_version_immutable()
    """)
    op.execute(f"""
        CREATE TRIGGER trg_paper_fee_policy_version_no_truncate
        BEFORE TRUNCATE ON {schema}.paper_fee_policy_versions
        FOR EACH STATEMENT
        EXECUTE FUNCTION {schema}.paper_fee_policy_version_immutable()
    """)


def downgrade():
    schema_name = op.get_bind().exec_driver_sql(
        "SELECT current_schema()"
    ).scalar_one_or_none()
    if not isinstance(schema_name, str) or not schema_name:
        raise RuntimeError("Cannot resolve fee-policy downgrade schema.")

    op.execute("""
        DO $$
        DECLARE
            version_rows bigint;
            head_rows bigint;
        BEGIN
            SELECT COUNT(*) INTO version_rows FROM paper_fee_policy_versions;
            SELECT COUNT(*) INTO head_rows FROM paper_fee_policy_heads;
            IF version_rows > 0 OR head_rows > 0 THEN
                RAISE EXCEPTION
                    'Refusing fee-policy downgrade: version rows %, head rows %',
                    version_rows, head_rows;
            END IF;
        END
        $$;
    """)

    schema = _schema_sql()
    op.execute(f"DROP TRIGGER IF EXISTS trg_paper_fee_policy_version_no_truncate ON {schema}.paper_fee_policy_versions")
    op.execute(f"DROP TRIGGER IF EXISTS trg_paper_fee_policy_version_immutable ON {schema}.paper_fee_policy_versions")
    op.execute(f"DROP FUNCTION IF EXISTS {schema}.paper_fee_policy_version_immutable()")
    op.drop_table("paper_fee_policy_heads", schema=schema_name)
    op.drop_table("paper_fee_policy_versions", schema=schema_name)
