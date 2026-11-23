"""Grant least-privilege access to the paper-trading application role.

Revision ID: 0009_paper_app_role_grants
Revises: 0008_paper_fee_policy
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

from app.database.config import DatabaseSettings


revision = "0009_paper_app_role_grants"
down_revision = "0008_paper_fee_policy"
branch_labels = None
depends_on = None


TABLE_PRIVILEGES = {
    "paper_ledger_books": ("SELECT", "INSERT"),
    "paper_ledger_accounts": ("SELECT", "INSERT"),
    "paper_ledger_transactions": ("SELECT", "INSERT", "UPDATE"),
    "paper_ledger_entries": ("SELECT", "INSERT"),
    "paper_ledger_account_snapshots": ("SELECT",),
    "paper_positions": ("SELECT", "INSERT", "UPDATE"),
    "paper_execution_requests": ("SELECT", "INSERT"),
    "paper_fee_policy_versions": ("SELECT", "INSERT"),
    "paper_fee_policy_heads": ("SELECT", "INSERT", "UPDATE"),
}


def _resolve_target():
    # Reuse the application's own validated database configuration.
    DatabaseSettings.from_environment()

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        raise RuntimeError("DATABASE_URL is required to identify the application role.")

    app_url = make_url(raw_url)

    migration_raw_url = os.environ.get("MIGRATION_DATABASE_URL")
    if not migration_raw_url:
        raise RuntimeError("MIGRATION_DATABASE_URL is required.")

    migration_url = make_url(migration_raw_url)

    app_host = (app_url.host or "").casefold()
    migration_host = (migration_url.host or "").casefold()

    if not app_host or app_host != migration_host:
        raise RuntimeError(
            "Application and migration database URLs target different hosts."
        )

    if (app_url.port or 5432) != (migration_url.port or 5432):
        raise RuntimeError(
            "Application and migration database URLs use different ports."
        )

    if app_url.database != migration_url.database:
        raise RuntimeError(
            "Application and migration database URLs target different databases."
        )

    app_role = app_url.username

    if not app_role:
        raise RuntimeError("DATABASE_URL does not identify an application role.")

    connection = op.get_bind()

    current_database = connection.execute(
        sa.text("SELECT current_database()")
    ).scalar_one()

    if app_url.database != current_database:
        raise RuntimeError(
            "Application DATABASE_URL and migration connection target different databases."
        )

    if app_url.port is not None:
        current_port = int(
            connection.execute(
                sa.text("SELECT current_setting('port')")
            ).scalar_one()
        )
        if app_url.port != current_port:
            raise RuntimeError(
                "Application DATABASE_URL and migration connection use different ports."
            )

    current_user = connection.execute(
        sa.text("SELECT current_user")
    ).scalar_one()

    if app_role == current_user:
        raise RuntimeError(
            "Application role must not be the migration connection role."
        )

    role = connection.execute(
        sa.text("""
            SELECT rolname, rolsuper
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
        """),
        {"role_name": app_role},
    ).mappings().one_or_none()

    if role is None:
        raise RuntimeError("Configured application database role does not exist.")

    if role["rolsuper"]:
        raise RuntimeError("Application database role must not be a superuser.")

    schema_name = connection.execute(
        sa.text("SELECT current_schema()")
    ).scalar_one_or_none()

    if not isinstance(schema_name, str) or not schema_name:
        raise RuntimeError("Cannot resolve the migration target schema.")

    preparer = connection.dialect.identifier_preparer
    quoted_schema = preparer.quote_schema(schema_name)
    quoted_role = preparer.quote(app_role)

    for table_name in TABLE_PRIVILEGES:
        if not sa.inspect(connection).has_table(
            table_name,
            schema=schema_name,
        ):
            raise RuntimeError(
                f"Required paper-trading table is missing: {table_name}"
            )

    return connection, schema_name, quoted_schema, quoted_role


def upgrade():
    connection, schema_name, quoted_schema, quoted_role = _resolve_target()

    # Grant schema USAGE only if the application role does not
    # already possess effective USAGE through another grant.
    has_usage = connection.execute(
        sa.text(
            "SELECT has_schema_privilege(:role_name, :schema_name, 'USAGE')"
        ),
        {
            "role_name": make_url(os.environ["DATABASE_URL"]).username,
            "schema_name": schema_name,
        },
    ).scalar_one()

    if not has_usage:
        connection.exec_driver_sql(
            f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_role}"
        )

    preparer = connection.dialect.identifier_preparer

    for table_name, privileges in TABLE_PRIVILEGES.items():
        quoted_table = preparer.quote(table_name)
        privilege_sql = ", ".join(privileges)

        connection.exec_driver_sql(
            f"GRANT {privilege_sql} "
            f"ON TABLE {quoted_schema}.{quoted_table} "
            f"TO {quoted_role}"
        )


def downgrade():
    connection, schema_name, quoted_schema, quoted_role = _resolve_target()
    preparer = connection.dialect.identifier_preparer

    for table_name, privileges in TABLE_PRIVILEGES.items():
        quoted_table = preparer.quote(table_name)
        privilege_sql = ", ".join(privileges)

        connection.exec_driver_sql(
            f"REVOKE {privilege_sql} "
            f"ON TABLE {quoted_schema}.{quoted_table} "
            f"FROM {quoted_role}"
        )

    # Schema USAGE is deliberately retained on downgrade:
    # it may have been inherited or independently granted before
    # this migration. Removing it could revoke unrelated access.
