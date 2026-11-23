"""Run paper ledger lock guards under a restricted non-login owner.

Revision ID: 0010_paper_guard_owner
Revises: 0009_paper_app_role_grants
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

from app.database.config import DatabaseSettings


revision = "0010_paper_guard_owner"
down_revision = "0009_paper_app_role_grants"
branch_labels = None
depends_on = None

HELPER_ROLE = "zak_paper_ledger_guard_owner"

FUNCTIONS = (
    (
        "paper_ledger_guard_transaction_lifecycle",
        "trg_paper_ledger_transaction_lifecycle",
        ("public.paper_ledger_books", "public.paper_ledger_entries"),
        "FOR SHARE",
    ),
    (
        "paper_ledger_guard_entry_insert",
        "trg_paper_ledger_entry_immutable",
        (
            "public.paper_ledger_transactions",
            "public.paper_ledger_books",
            "public.paper_ledger_accounts",
        ),
        "FOR UPDATE",
    ),
)

READ_TABLES = (
    "paper_ledger_books",
    "paper_ledger_transactions",
    "paper_ledger_entries",
    "paper_ledger_accounts",
)


def _resolve_target():
    DatabaseSettings.from_environment()

    app_raw = os.environ.get("DATABASE_URL")
    migration_raw = os.environ.get("MIGRATION_DATABASE_URL")

    if not app_raw or not migration_raw:
        raise RuntimeError(
            "DATABASE_URL and MIGRATION_DATABASE_URL are required."
        )

    app_url = make_url(app_raw)
    migration_url = make_url(migration_raw)

    if (
        not app_url.host
        or (app_url.host or "").casefold()
        != (migration_url.host or "").casefold()
        or (app_url.port or 5432) != (migration_url.port or 5432)
        or app_url.database != migration_url.database
    ):
        raise RuntimeError(
            "Application and migration URLs must target the same host, "
            "port and database."
        )

    app_role = app_url.username
    if not app_role:
        raise RuntimeError("DATABASE_URL must identify the application role.")

    connection = op.get_bind()

    current_user = connection.execute(
        sa.text("SELECT current_user")
    ).scalar_one()

    current_database = connection.execute(
        sa.text("SELECT current_database()")
    ).scalar_one()

    if app_url.database != current_database:
        raise RuntimeError("Migration connection database does not match DATABASE_URL.")

    if app_role == current_user:
        raise RuntimeError("Application and migration roles must be different.")

    if app_url.port is not None:
        current_port = int(
            connection.execute(
                sa.text("SELECT current_setting('port')")
            ).scalar_one()
        )
        if app_url.port != current_port:
            raise RuntimeError("Application and migration ports do not match.")

    schema = connection.execute(
        sa.text("SELECT current_schema()")
    ).scalar_one_or_none()

    if schema != "public":
        raise RuntimeError(
            "This migration requires the existing paper ledger schema to be public."
        )

    roles = connection.execute(
        sa.text("""
            SELECT rolname, rolsuper, rolcreaterole, rolcanlogin
            FROM pg_catalog.pg_roles
            WHERE rolname IN (:app_role, :migration_role)
        """),
        {"app_role": app_role, "migration_role": current_user},
    ).mappings().all()

    role_map = {row["rolname"]: row for row in roles}

    if app_role not in role_map or current_user not in role_map:
        raise RuntimeError("Application or migration database role is missing.")

    if role_map[app_role]["rolsuper"]:
        raise RuntimeError("Application database role must not be a superuser.")

    migration_role = role_map[current_user]
    if not migration_role["rolsuper"] and not migration_role["rolcreaterole"]:
        raise RuntimeError(
            "Migration role needs CREATEROLE or SUPERUSER to establish "
            "the restricted trigger owner."
        )

    if not connection.execute(
        sa.text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
    ).scalar_one():
        raise RuntimeError(
            "Migration role needs CREATE on the target schema for safe ownership transfer."
        )

    preparer = connection.dialect.identifier_preparer
    quoted_schema = preparer.quote_schema(schema)
    quoted_app = preparer.quote(app_role)
    quoted_migration = preparer.quote(current_user)
    quoted_helper = preparer.quote(HELPER_ROLE)

    for table in READ_TABLES:
        if not sa.inspect(connection).has_table(table, schema=schema):
            raise RuntimeError(f"Required table is missing: {table}")

    return (
        connection,
        app_role,
        current_user,
        migration_role["rolsuper"],
        schema,
        quoted_schema,
        quoted_app,
        quoted_migration,
        quoted_helper,
    )


def _validate_functions(connection, schema, expected_owner, expected_definer):
    rows = connection.execute(
        sa.text("""
            SELECT
                p.proname,
                p.prosecdef,
                p.proconfig,
                p.pronargs,
                pg_get_userbyid(p.proowner) AS owner,
                pg_get_functiondef(p.oid) AS definition,
                COALESCE(
                    array_agg(t.tgname)
                        FILTER (WHERE t.tgname IS NOT NULL),
                    ARRAY[]::text[]
                ) AS trigger_names
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            LEFT JOIN pg_trigger t
              ON t.tgfoid = p.oid
             AND NOT t.tgisinternal
            WHERE n.nspname = :schema
              AND p.proname IN (
                'paper_ledger_guard_transaction_lifecycle',
                'paper_ledger_guard_entry_insert'
              )
              AND p.pronargs = 0
            GROUP BY p.oid
        """),
        {"schema": schema},
    ).mappings().all()

    by_name = {row["proname"]: row for row in rows}

    for name, trigger_name, required_tables, lock_clause in FUNCTIONS:
        row = by_name.get(name)
        if row is None:
            raise RuntimeError(f"Required trigger function is missing: {name}")

        if row["owner"] != expected_owner:
            raise RuntimeError(
                f"Unexpected owner for {name}: {row['owner']}"
            )

        if bool(row["prosecdef"]) != expected_definer:
            raise RuntimeError(
                f"Unexpected SECURITY DEFINER state for {name}."
            )

        definition = row["definition"]

        if lock_clause not in definition:
            raise RuntimeError(f"Expected row-lock clause missing in {name}.")

        for table_name in required_tables:
            if table_name not in definition:
                raise RuntimeError(
                    f"Expected qualified table reference {table_name} "
                    f"missing in {name}."
                )

        if trigger_name not in row["trigger_names"]:
            raise RuntimeError(
                f"Expected trigger {trigger_name} is not attached to {name}."
            )

        if expected_definer:
            if row["proconfig"] != ["search_path=pg_catalog"]:
                raise RuntimeError(
                    f"{name} does not have the required restricted search_path."
                )
        elif row["proconfig"] != ["search_path=pg_catalog, public"]:
            raise RuntimeError(
                f"{name} has an unexpected original search_path."
            )


def _validate_helper_role(connection):
    row = connection.execute(
        sa.text("""
            SELECT
                rolsuper,
                rolinherit,
                rolcreaterole,
                rolcreatedb,
                rolcanlogin,
                rolreplication,
                rolbypassrls
            FROM pg_roles
            WHERE rolname = :role
        """),
        {"role": HELPER_ROLE},
    ).mappings().one_or_none()

    if row is None:
        raise RuntimeError("Restricted helper role does not exist.")

    if (
        row["rolsuper"]
        or row["rolinherit"]
        or row["rolcreaterole"]
        or row["rolcreatedb"]
        or row["rolcanlogin"]
        or row["rolreplication"]
        or row["rolbypassrls"]
    ):
        raise RuntimeError(
            "Helper role attributes are unsafe; refusing to use it."
        )

    memberships = connection.execute(
        sa.text("""
            SELECT count(*)
            FROM pg_auth_members m
            JOIN pg_roles r ON r.oid = m.roleid
            JOIN pg_roles member ON member.oid = m.member
            WHERE r.rolname = :role OR member.rolname = :role
        """),
        {"role": HELPER_ROLE},
    ).scalar_one()

    if memberships:
        raise RuntimeError(
            "Helper role has existing memberships; refusing unsafe ownership."
        )


def upgrade():
    (
        connection,
        app_role,
        migration_user,
        migration_is_superuser,
        schema,
        quoted_schema,
        quoted_app,
        quoted_migration,
        quoted_helper,
    ) = _resolve_target()

    _validate_functions(
        connection,
        schema,
        expected_owner=migration_user,
        expected_definer=False,
    )

    # The application role intentionally retains UPDATE on ledger
    # transactions from migration 0009. Only ledger books must remain
    # non-writable by the application role.
    app_has_update = connection.execute(
        sa.text("""
            SELECT has_any_column_privilege(
                :app_role,
                'public.paper_ledger_books',
                'UPDATE'
            )
        """),
        {"app_role": app_role},
    ).scalar_one()

    if app_has_update:
        raise RuntimeError(
            "Application role already has UPDATE on a protected ledger table. "
            "Refusing to change or conceal existing privileges."
        )

    existing = connection.execute(
        sa.text("""
            SELECT 1 FROM pg_roles WHERE rolname = :role
        """),
        {"role": HELPER_ROLE},
    ).scalar_one_or_none()

    if existing is not None:
        raise RuntimeError(
            "Restricted helper role already exists cluster-wide. "
            "Refusing to reuse an existing role."
        )

    connection.exec_driver_sql(
        f"CREATE ROLE {quoted_helper} "
        "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOINHERIT NOREPLICATION NOBYPASSRLS"
    )

    _validate_helper_role(connection)

    # The helper receives only the reads used by the two guard functions.
    connection.exec_driver_sql(
        f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_helper}"
    )
    connection.exec_driver_sql(
        f"GRANT CREATE ON SCHEMA {quoted_schema} TO {quoted_helper}"
    )

    for table in READ_TABLES:
        quoted_table = connection.dialect.identifier_preparer.quote(table)
        connection.exec_driver_sql(
            f"GRANT SELECT ON TABLE {quoted_schema}.{quoted_table} "
            f"TO {quoted_helper}"
        )

    # Column-level UPDATE is sufficient for row locks. No other column
    # becomes writable by the helper role.
    for table in ("paper_ledger_books", "paper_ledger_transactions"):
        quoted_table = connection.dialect.identifier_preparer.quote(table)
        connection.exec_driver_sql(
            f"GRANT UPDATE (id) ON TABLE {quoted_schema}.{quoted_table} "
            f"TO {quoted_helper}"
        )

    if not migration_is_superuser:
        connection.exec_driver_sql(
            f"GRANT {quoted_helper} TO {quoted_migration}"
        )

    for name, _, _, _ in FUNCTIONS:
        quoted_function = connection.dialect.identifier_preparer.quote(name)
        qualified_function = (
            f"{quoted_schema}.{quoted_function}()"
        )

        connection.exec_driver_sql(
            f"ALTER FUNCTION {qualified_function} OWNER TO {quoted_helper}"
        )
        connection.exec_driver_sql(
            f"ALTER FUNCTION {qualified_function} SECURITY DEFINER"
        )
        connection.exec_driver_sql(
            f"ALTER FUNCTION {qualified_function} "
            "SET search_path TO pg_catalog"
        )
        connection.exec_driver_sql(
            f"REVOKE ALL ON FUNCTION {qualified_function} FROM PUBLIC"
        )
        connection.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION {qualified_function} TO {quoted_app}"
        )

    connection.exec_driver_sql(
        f"REVOKE CREATE ON SCHEMA {quoted_schema} FROM {quoted_helper}"
    )

    if not migration_is_superuser:
        connection.exec_driver_sql(
            f"REVOKE {quoted_helper} FROM {quoted_migration}"
        )

    _validate_functions(
        connection,
        schema,
        expected_owner=HELPER_ROLE,
        expected_definer=True,
    )

    _validate_helper_role(connection)

    helper_update_ok = connection.execute(
        sa.text("""
            SELECT
                has_column_privilege(
                    :helper, 'public.paper_ledger_books', 'id', 'UPDATE'
                )
                AND has_column_privilege(
                    :helper,
                    'public.paper_ledger_transactions',
                    'id',
                    'UPDATE'
                )
        """),
        {"helper": HELPER_ROLE},
    ).scalar_one()

    if not helper_update_ok:
        raise RuntimeError("Helper row-lock privileges failed verification.")

    # Preserve the pre-existing application UPDATE permission on
    # paper_ledger_transactions; only books must remain non-writable.
    app_update_after = connection.execute(
        sa.text("""
            SELECT has_any_column_privilege(
                :app, 'public.paper_ledger_books', 'UPDATE'
            )
        """),
        {"app": app_role},
    ).scalar_one()

    if app_update_after:
        raise RuntimeError(
            "Application role unexpectedly has UPDATE on a protected ledger table."
        )


def downgrade():
    (
        connection,
        app_role,
        migration_user,
        migration_is_superuser,
        schema,
        quoted_schema,
        quoted_app,
        quoted_migration,
        quoted_helper,
    ) = _resolve_target()

    _validate_helper_role(connection)

    _validate_functions(
        connection,
        schema,
        expected_owner=HELPER_ROLE,
        expected_definer=True,
    )

    if not migration_is_superuser:
        connection.exec_driver_sql(
            f"GRANT {quoted_helper} TO {quoted_migration}"
        )

    for name, _, _, _ in FUNCTIONS:
        quoted_function = connection.dialect.identifier_preparer.quote(name)
        qualified_function = f"{quoted_schema}.{quoted_function}()"

        connection.exec_driver_sql(
            f"ALTER FUNCTION {qualified_function} OWNER TO {quoted_migration}"
        )
        connection.exec_driver_sql(
            f"ALTER FUNCTION {qualified_function} SECURITY INVOKER"
        )
        connection.exec_driver_sql(
            f"ALTER FUNCTION {qualified_function} "
            "SET search_path TO pg_catalog, public"
        )
        connection.exec_driver_sql(
            f"REVOKE EXECUTE ON FUNCTION {qualified_function} FROM {quoted_app}"
        )
        connection.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION {qualified_function} TO PUBLIC"
        )

    for table in ("paper_ledger_books", "paper_ledger_transactions"):
        quoted_table = connection.dialect.identifier_preparer.quote(table)
        connection.exec_driver_sql(
            f"REVOKE UPDATE (id) ON TABLE {quoted_schema}.{quoted_table} "
            f"FROM {quoted_helper}"
        )

    for table in READ_TABLES:
        quoted_table = connection.dialect.identifier_preparer.quote(table)
        connection.exec_driver_sql(
            f"REVOKE SELECT ON TABLE {quoted_schema}.{quoted_table} "
            f"FROM {quoted_helper}"
        )

    connection.exec_driver_sql(
        f"REVOKE CREATE ON SCHEMA {quoted_schema} FROM {quoted_helper}"
    )
    connection.exec_driver_sql(
        f"REVOKE USAGE ON SCHEMA {quoted_schema} FROM {quoted_helper}"
    )

    if not migration_is_superuser:
        connection.exec_driver_sql(
            f"REVOKE {quoted_helper} FROM {quoted_migration}"
        )

    _validate_functions(
        connection,
        schema,
        expected_owner=migration_user,
        expected_definer=False,
    )