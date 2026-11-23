"""Restricted archive operation for Paper Trading ledger books."""

from __future__ import annotations

import os

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import make_url

revision = "0011_paper_reset_archive"
down_revision = "0010_paper_guard_owner"
branch_labels = None
depends_on = None

HELPER_ROLE = "zak_paper_reset_archive_owner"
FUNCTION_NAME = "archive_paper_ledger_book"
FUNCTION_SIGNATURE = "public.archive_paper_ledger_book(uuid, uuid, uuid, uuid)"


def _context():
    bind = op.get_bind()
    app_value = os.environ.get("DATABASE_URL")
    migration_value = os.environ.get("MIGRATION_DATABASE_URL")

    if not app_value or not migration_value:
        raise RuntimeError("Both database URLs are required.")

    app_url = make_url(app_value)
    migration_url = make_url(migration_value)

    identity = bind.execute(
        text("SELECT current_user, current_database()")
    ).one()

    migration_user, database_name = identity

    if (
        app_url.drivername != "postgresql+asyncpg"
        or migration_url.drivername != "postgresql+asyncpg"
        or app_url.host != migration_url.host
        or app_url.port != migration_url.port
        or app_url.database != migration_url.database
        or app_url.database != database_name
        or migration_url.username != migration_user
        or app_url.username == migration_user
    ):
        raise RuntimeError("Database identity validation failed.")

    roles = bind.execute(
        text("""
            SELECT rolname, rolsuper, rolcreaterole
            FROM pg_catalog.pg_roles
            WHERE rolname IN (:app, :migration)
        """),
        {"app": app_url.username, "migration": migration_user},
    ).mappings().all()

    role_map = {r["rolname"]: r for r in roles}

    if app_url.username not in role_map or migration_user not in role_map:
        raise RuntimeError("Required database role is missing.")

    if role_map[app_url.username]["rolsuper"]:
        raise RuntimeError("Application role must not be superuser.")

    if not (
        role_map[migration_user]["rolsuper"]
        or role_map[migration_user]["rolcreaterole"]
    ):
        raise RuntimeError("Migration role requires CREATEROLE.")

    if not bind.execute(
        text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
    ).scalar_one():
        raise RuntimeError("Migration role requires public schema CREATE.")

    if bind.execute(
        text("SELECT to_regclass('public.paper_ledger_books')")
    ).scalar_one() is None:
        raise RuntimeError("paper_ledger_books does not exist.")

    return bind, app_url.username, migration_user


def _quote(bind, role):
    return bind.execute(
        text("SELECT quote_ident(:role)"), {"role": role}
    ).scalar_one()


def _validate_helper(bind):
    role = bind.execute(
        text("""
            SELECT oid, rolsuper, rolinherit, rolcreaterole,
                   rolcreatedb, rolcanlogin, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role
        """),
        {"role": HELPER_ROLE},
    ).mappings().one_or_none()

    if role is None:
        raise RuntimeError("Archive helper role does not exist.")

    flags = (
        role["rolsuper"], role["rolinherit"], role["rolcreaterole"],
        role["rolcreatedb"], role["rolcanlogin"], role["rolreplication"],
        role["rolbypassrls"],
    )

    if flags != (False, False, False, False, False, False, False):
        raise RuntimeError("Archive helper role has unsafe attributes.")

    memberships = bind.execute(
        text("""
            SELECT count(*)
            FROM pg_catalog.pg_auth_members
            WHERE roleid = :oid OR member = :oid
        """),
        {"oid": role["oid"]},
    ).scalar_one()

    if memberships:
        raise RuntimeError("Archive helper role has memberships.")


def _validate_privileges(bind, app_role):
    app_update = bind.execute(
        text("""
            SELECT
                has_table_privilege(
                    :app, 'public.paper_ledger_books', 'UPDATE'
                )
                OR EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_attribute
                    WHERE attrelid = 'public.paper_ledger_books'::regclass
                      AND attnum > 0
                      AND NOT attisdropped
                      AND has_column_privilege(
                          :app, 'public.paper_ledger_books',
                          attname, 'UPDATE'
                      )
                )
        """),
        {"app": app_role},
    ).scalar_one()

    if app_update:
        raise RuntimeError("App role has direct ledger book UPDATE privilege.")

    helper = bind.execute(
        text("""
            SELECT
                has_table_privilege(
                    :role, 'public.paper_ledger_books', 'SELECT'
                ) AS can_select,
                has_column_privilege(
                    :role, 'public.paper_ledger_books', 'status', 'UPDATE'
                ) AS can_update_status,
                has_column_privilege(
                    :role, 'public.paper_ledger_books', 'archived_at', 'UPDATE'
                ) AS can_update_archived_at,
                has_table_privilege(
                    :role, 'public.paper_ledger_books', 'UPDATE'
                ) AS can_update_table
        """),
        {"role": HELPER_ROLE},
    ).mappings().one()

    if (
        not helper["can_select"]
        or not helper["can_update_status"]
        or not helper["can_update_archived_at"]
        or helper["can_update_table"]
    ):
        raise RuntimeError("Helper privileges are not restricted correctly.")


def _function_sql():
    return """
        CREATE FUNCTION public.archive_paper_ledger_book(
            p_account_id uuid,
            p_portfolio_id uuid,
            p_session_id uuid,
            p_book_id uuid
        )
        RETURNS timestamptz
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_archived_at timestamptz;
        BEGIN
            SELECT b.archived_at
              INTO v_archived_at
              FROM public.paper_ledger_books AS b
             WHERE b.id = p_book_id
               AND b.account_id = p_account_id
               AND b.portfolio_id = p_portfolio_id
               AND b.session_id = p_session_id
               AND b.status = 'active'
               AND b.archived_at IS NULL
             FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'P0002',
                    MESSAGE = 'Active ledger book not found for requested scope.';
            END IF;

            UPDATE public.paper_ledger_books AS b
               SET status = 'archived',
                   archived_at = pg_catalog.clock_timestamp()
             WHERE b.id = p_book_id
               AND b.account_id = p_account_id
               AND b.portfolio_id = p_portfolio_id
               AND b.session_id = p_session_id
               AND b.status = 'active'
               AND b.archived_at IS NULL
            RETURNING b.archived_at INTO v_archived_at;

            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'P0002',
                    MESSAGE = 'Ledger book archive operation failed.';
            END IF;

            RETURN v_archived_at;
        END;
        $function$
    """


def upgrade():
    bind, app_role, migration_user = _context()

    exists = bind.execute(
        text("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :role"),
        {"role": HELPER_ROLE},
    ).scalar_one_or_none()

    if exists is None:
        bind.exec_driver_sql(
            f"""
            CREATE ROLE {HELPER_ROLE}
                NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                NOINHERIT NOREPLICATION NOBYPASSRLS
            """
        )

    _validate_helper(bind)

    bind.exec_driver_sql(
        f"GRANT USAGE, CREATE ON SCHEMA public TO {HELPER_ROLE}"
    )
    bind.exec_driver_sql(
        f"GRANT SELECT ON public.paper_ledger_books TO {HELPER_ROLE}"
    )
    bind.exec_driver_sql(
        f"""
        GRANT UPDATE (status, archived_at)
        ON public.paper_ledger_books TO {HELPER_ROLE}
        """
    )

    _validate_privileges(bind, app_role)

    migration_ident = _quote(bind, migration_user)
    app_ident = _quote(bind, app_role)

    bind.exec_driver_sql(f"GRANT {HELPER_ROLE} TO {migration_ident}")
    bind.exec_driver_sql(_function_sql())
    bind.exec_driver_sql(
        f"ALTER FUNCTION {FUNCTION_SIGNATURE} OWNER TO {HELPER_ROLE}"
    )
    bind.exec_driver_sql(
        f"REVOKE CREATE ON SCHEMA public FROM {HELPER_ROLE}"
    )
    bind.exec_driver_sql(f"REVOKE {HELPER_ROLE} FROM {migration_ident}")
    bind.exec_driver_sql(
        f"REVOKE ALL ON FUNCTION {FUNCTION_SIGNATURE} FROM PUBLIC"
    )
    bind.exec_driver_sql(
        f"GRANT EXECUTE ON FUNCTION {FUNCTION_SIGNATURE} TO {app_ident}"
    )

    states = bind.execute(
        text("""
            SELECT
                r.rolname,
                p.prosecdef,
                p.proconfig,
                pg_catalog.pg_get_function_identity_arguments(p.oid)
                    AS identity_args
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
            JOIN pg_catalog.pg_roles AS r ON r.oid = p.proowner
            WHERE n.nspname = 'public'
              AND p.proname = :name
        """),
        {"name": FUNCTION_NAME},
    ).mappings().all()

    if len(states) != 1:
        raise RuntimeError(
            "Archive function security validation failed: "
            f"expected one function, found {len(states)}."
        )

    state = states[0]

    if (
        state["rolname"] != HELPER_ROLE
        or state["prosecdef"] is not True
        or "search_path=pg_catalog" not in (state["proconfig"] or [])
        or state["identity_args"] != "p_account_id uuid, p_portfolio_id uuid, p_session_id uuid, p_book_id uuid"
    ):
        raise RuntimeError(
            "Archive function security validation failed: "
            f"owner={state['rolname']!r}, "
            f"security_definer={state['prosecdef']!r}, "
            f"proconfig={state['proconfig']!r}, "
            f"identity_args={state['identity_args']!r}."
        )

    can_execute = bind.execute(
        text("""
            SELECT has_function_privilege(:role, :function, 'EXECUTE')
        """),
        {"role": app_role, "function": FUNCTION_SIGNATURE},
    ).scalar_one()

    if not can_execute:
        raise RuntimeError("App role cannot execute archive function.")

    _validate_helper(bind)
    _validate_privileges(bind, app_role)


def downgrade():
    bind, app_role, migration_user = _context()
    _validate_helper(bind)

    owner = bind.execute(
        text("""
            SELECT r.rolname
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
            JOIN pg_catalog.pg_roles AS r ON r.oid = p.proowner
            WHERE n.nspname = 'public'
              AND p.proname = :name
              AND pg_catalog.pg_get_function_identity_arguments(p.oid)
                  = 'p_account_id uuid, p_portfolio_id uuid, p_session_id uuid, p_book_id uuid'
        """),
        {"name": FUNCTION_NAME},
    ).scalar_one_or_none()

    if owner != HELPER_ROLE:
        raise RuntimeError("Archive function missing or owner unexpected.")

    app_ident = _quote(bind, app_role)
    migration_ident = _quote(bind, migration_user)

    bind.exec_driver_sql(
        f"REVOKE ALL ON FUNCTION {FUNCTION_SIGNATURE} FROM {app_ident}"
    )
    bind.exec_driver_sql(
        f"REVOKE ALL ON FUNCTION {FUNCTION_SIGNATURE} FROM PUBLIC"
    )
    bind.exec_driver_sql(f"GRANT {HELPER_ROLE} TO {migration_ident}")
    bind.exec_driver_sql(
        f"ALTER FUNCTION {FUNCTION_SIGNATURE} OWNER TO {migration_ident}"
    )
    bind.exec_driver_sql(f"REVOKE {HELPER_ROLE} FROM {migration_ident}")
    bind.exec_driver_sql(f"DROP FUNCTION {FUNCTION_SIGNATURE}")

    bind.exec_driver_sql(
        f"""
        REVOKE UPDATE (status, archived_at)
        ON public.paper_ledger_books FROM {HELPER_ROLE}
        """
    )
    bind.exec_driver_sql(
        f"REVOKE SELECT ON public.paper_ledger_books FROM {HELPER_ROLE}"
    )
    bind.exec_driver_sql(
        f"REVOKE USAGE ON SCHEMA public FROM {HELPER_ROLE}"
    )