import ast
import re
from pathlib import Path

import app.models
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.dialects import postgresql
from app.database.base import Base


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0007_paper_trading_ledger.py"
)

EXPECTED_TABLES = {
    "paper_ledger_books",
    "paper_ledger_accounts",
    "paper_ledger_transactions",
    "paper_ledger_entries",
    "paper_ledger_account_snapshots",
    "paper_positions",
    "paper_execution_requests",
}


def migration_source():
    return MIGRATION_PATH.read_text(encoding="utf-8-sig")


def migration_tree():
    return ast.parse(migration_source())


def literal_assignments():
    values = {}
    for node in migration_tree().body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        values[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass
    return values


def test_revision_and_parent_are_correct():
    values = literal_assignments()
    assert values["revision"] == "0007_paper_trading_ledger"
    assert values["down_revision"] == "0006_internal_service_nonce_uuid"


def test_only_seven_approved_financial_tables_are_included():
    values = literal_assignments()
    assert set(values["TABLE_NAMES"]) == EXPECTED_TABLES
    assert len(values["TABLE_NAMES"]) == 7


def test_table_ddl_matches_registered_metadata():
    values = literal_assignments()
    dialect = postgresql.dialect()

    expected = {
        table.name: str(CreateTable(table).compile(dialect=dialect)).strip()
        for table in Base.metadata.sorted_tables
        if table.name in EXPECTED_TABLES
    }

    actual = {}
    for ddl in values["TABLE_DDL"]:
        match = re.search(r'CREATE TABLE\s+"?([a-zA-Z0-9_]+)"?', ddl)
        assert match, f"Cannot identify table in DDL: {ddl[:120]}"
        actual[match.group(1)] = ddl.strip()

    assert actual == expected


def test_index_ddl_matches_registered_metadata():
    values = literal_assignments()
    dialect = postgresql.dialect()

    expected = []
    for table in Base.metadata.sorted_tables:
        if table.name not in EXPECTED_TABLES:
            continue
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            expected.append(
                str(CreateIndex(index).compile(dialect=dialect)).strip()
            )

    assert values["INDEX_DDL"] == expected


def test_no_fixed_numeric_precision_or_scale_typmods():
    combined = "\n".join(literal_assignments()["TABLE_DDL"])
    assert not re.search(
        r"\bNUMERIC\s*\(\s*\d+\s*,\s*\d+\s*\)", combined, re.I
    )
    assert not re.search(
        r"\bDECIMAL\s*\(\s*\d+\s*,\s*\d+\s*\)", combined, re.I
    )


def test_financial_foreign_keys_are_restrictive():
    combined = "\n".join(literal_assignments()["TABLE_DDL"]).upper()
    assert "ON DELETE RESTRICT" in combined


def test_downgrade_refuses_to_drop_nonempty_financial_tables():
    source = migration_source()
    assert "Refusing paper-ledger downgrade" in source
    assert "COUNT(*)" in source
    assert "op.drop_table(table_name, schema=schema_name)" in source

def test_initial_funding_unique_index_is_scoped_to_each_book():
    import runpy
    from pathlib import Path

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0007_paper_trading_ledger.py"
    )
    migration = runpy.run_path(str(migration_path))

    matching_indexes = [
        statement
        for statement in migration["INDEX_DDL"]
        if "uq_paper_ledger_transactions_one_initial_funding" in statement
    ]

    assert len(matching_indexes) == 1

    sql = " ".join(matching_indexes[0].split())

    assert "ON paper_ledger_transactions (book_id)" in sql
    assert "WHERE transaction_type = 'initial_funding'" in sql

def test_transaction_lifecycle_trigger_enforces_posting_rules():
    values = literal_assignments()
    definitions = values["INTEGRITY_DDL"]

    lifecycle_functions = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_guard_transaction_lifecycle("
        )
    ]

    assert len(lifecycle_functions) == 1

    lifecycle_sql = " ".join(lifecycle_functions[0].split())

    assert "paper_ledger_guard_transaction_lifecycle" in lifecycle_sql
    assert "New ledger transactions must start as draft" in lifecycle_sql
    assert "Only draft-to-posted transition is allowed" in lifecycle_sql
    assert "Ledger transaction identity is immutable" in lifecycle_sql
    assert "clock_timestamp()" in lifecycle_sql
    assert "SECURITY DEFINER" not in lifecycle_sql
    assert "SET search_path = pg_catalog, __SCHEMA__" in lifecycle_sql


def test_posting_trigger_requires_balanced_double_entry():
    values = literal_assignments()
    ddl = "\n".join(values["INTEGRITY_DDL"])

    assert "v_entry_count < 2" in ddl
    assert "v_debit_count = 0" in ddl
    assert "v_credit_count = 0" in ddl
    assert "v_debit_total IS DISTINCT FROM v_credit_total" in ddl
    assert "Cannot post an unbalanced ledger transaction" in ddl


def test_entry_trigger_locks_draft_transaction_and_rejects_mutation():
    values = literal_assignments()
    ddl = "\n".join(values["INTEGRITY_DDL"])

    assert "paper_ledger_guard_entry_insert" in ddl
    assert "TG_OP <> 'INSERT'" in ddl
    assert "FOR UPDATE" in ddl
    assert "v_transaction_status IS DISTINCT FROM 'draft'" in ddl
    assert "Ledger entries are immutable" in ddl


def test_entry_trigger_rejects_cross_market_account_posting():
    definitions = literal_assignments()["INTEGRITY_DDL"]

    functions = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_guard_entry_insert("
        )
    ]

    assert len(functions) == 1
    sql = " ".join(functions[0].split())

    assert "SELECT status, market_type" in sql
    assert "v_book_market_type" in sql
    assert "SELECT code, market_type" in sql
    assert "v_account_code" in sql
    assert "v_account_market_type" in sql
    assert "v_account_market_type IS DISTINCT FROM v_book_market_type" in sql
    assert (
        "v_book_market_type = 'spot' "
        "AND v_account_code = 'futures_margin_locked'"
    ) in sql
    assert (
        "v_book_market_type = 'futures' "
        "AND v_account_code = 'spot_position_cost'"
    ) in sql
    assert "Cross-market ledger account posting is prohibited" in sql


def test_entry_market_validation_runs_after_transaction_and_book_locks():
    definitions = [
        item for item in literal_assignments()["INTEGRITY_DDL"]
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_guard_entry_insert("
        )
    ]

    assert len(definitions) == 1
    sql = " ".join(definitions[0].split())

    transaction_lock = sql.index("FOR UPDATE")
    book_lock = sql.index("FOR SHARE")
    account_lookup = sql.index(
        "FROM __SCHEMA__.paper_ledger_accounts"
    )
    market_rejection = sql.index(
        "Cross-market ledger account posting is prohibited"
    )

    assert transaction_lock < book_lock < account_lookup < market_rejection


def test_ledger_accounts_reject_update_and_delete():
    definitions = literal_assignments()["INTEGRITY_DDL"]

    functions = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_guard_account_immutable("
        )
    ]
    triggers = [
        item for item in definitions
        if item.startswith(
            "CREATE TRIGGER trg_paper_ledger_account_immutable"
        )
    ]

    assert len(functions) == 1
    assert len(triggers) == 1

    function_sql = " ".join(functions[0].split())
    trigger_sql = " ".join(triggers[0].split())

    assert "TG_OP IN ('UPDATE', 'DELETE')" in function_sql
    assert "Ledger account identity and normal side are immutable" in function_sql
    assert "RAISE EXCEPTION" in function_sql
    assert "BEFORE UPDATE OR DELETE" in trigger_sql
    assert "ON __SCHEMA__.paper_ledger_accounts" in trigger_sql
    assert (
        "EXECUTE FUNCTION "
        "__SCHEMA__.paper_ledger_guard_account_immutable()"
    ) in trigger_sql


def test_book_account_completeness_is_deferred_until_commit():
    definitions = literal_assignments()["INTEGRITY_DDL"]

    functions = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_assert_book_has_six_accounts("
        )
    ]
    triggers = [
        item for item in definitions
        if item.startswith(
            "CREATE CONSTRAINT TRIGGER trg_paper_ledger_book_account_completeness"
        )
    ]

    assert len(functions) == 1
    assert len(triggers) == 1

    function_sql = " ".join(functions[0].split())
    trigger_sql = " ".join(triggers[0].split())

    assert "COUNT(*)" in function_sql
    assert "COUNT(DISTINCT code)" in function_sql
    assert "WHERE book_id = NEW.id" in function_sql
    assert "v_account_count <> 6" in function_sql
    assert "v_distinct_code_count <> 6" in function_sql
    assert (
        "Ledger book must have exactly six distinct mandatory accounts"
        in function_sql
    )
    assert "AFTER INSERT" in trigger_sql
    assert "ON __SCHEMA__.paper_ledger_books" in trigger_sql
    assert "DEFERRABLE INITIALLY DEFERRED" in trigger_sql
    assert (
        "EXECUTE FUNCTION "
        "__SCHEMA__.paper_ledger_assert_book_has_six_accounts()"
    ) in trigger_sql


def test_account_completeness_uses_existing_allowed_code_constraints():
    values = literal_assignments()
    account_ddl = [
        item for item in values["TABLE_DDL"]
        if "CREATE TABLE paper_ledger_accounts" in item
    ]

    assert len(account_ddl) == 1
    normalized = " ".join(account_ddl[0].split())

    for code in (
        "cash_available",
        "spot_position_cost",
        "futures_margin_locked",
        "realized_pnl",
        "fee_expense",
        "contributed_capital",
    ):
        assert f"'{code}'" in normalized

    assert "UNIQUE (book_id, code)" in normalized


def test_deferred_trigger_rejects_unposted_transactions():
    values = literal_assignments()
    ddl = "\n".join(values["INTEGRITY_DDL"])

    assert "paper_ledger_assert_transaction_posted" in ddl
    assert "CREATE CONSTRAINT TRIGGER trg_paper_ledger_transaction_must_post" in ddl
    assert "DEFERRABLE INITIALLY DEFERRED" in ddl
    assert "Draft ledger transaction cannot survive commit" in ddl


def test_integrity_trigger_downgrade_cleanup_is_complete():
    values = literal_assignments()
    drops = "\n".join(values["INTEGRITY_DROP_DDL"])

    expected = [
        "trg_paper_ledger_snapshot_write_guard",
        "paper_ledger_guard_snapshot_write",
        "trg_paper_ledger_apply_posted_transaction",
        "trg_paper_ledger_account_create_snapshot",
        "paper_ledger_apply_posted_transaction_to_snapshots",
        "paper_ledger_create_account_snapshot",
        "trg_paper_ledger_transaction_must_post",
        "trg_paper_ledger_entry_immutable",
        "trg_paper_ledger_transaction_lifecycle",
        "paper_ledger_assert_transaction_posted",
        "paper_ledger_guard_entry_insert",
        "paper_ledger_guard_transaction_lifecycle",
        "trg_paper_ledger_book_account_completeness",
        "trg_paper_ledger_account_immutable",
        "paper_ledger_assert_book_has_six_accounts",
        "paper_ledger_guard_account_immutable",
        "trg_paper_execution_request_no_truncate",
        "trg_paper_execution_request_immutable",
        "paper_execution_request_immutable",
    ]

    assert len(values["INTEGRITY_DROP_DDL"]) == 19

    for name in expected:
        assert name in drops


def test_book_status_is_locked_during_transaction_and_entry_operations():
    ddl = "\\n".join(literal_assignments()["INTEGRITY_DDL"])

    assert ddl.count("FOR SHARE;") == 2

    expected = (
        "FROM __SCHEMA__.paper_ledger_books"
        " WHERE id = NEW.book_id"
        " FOR SHARE;"
    )
    normalized = " ".join(ddl.split())

    assert normalized.count(expected) == 2


def test_entry_insert_locks_transaction_before_book():
    definitions = [
        item for item in literal_assignments()["INTEGRITY_DDL"]
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_guard_entry_insert("
        )
    ]

    assert len(definitions) == 1
    sql = " ".join(definitions[0].split())

    transaction_lookup = sql.index(
        "FROM __SCHEMA__.paper_ledger_transactions"
    )
    transaction_lock = sql.index("FOR UPDATE")
    book_lookup = sql.index("FROM __SCHEMA__.paper_ledger_books")
    book_lock = sql.index("FOR SHARE")
    transaction_status_check = sql.index(
        "v_transaction_status IS DISTINCT FROM 'draft'"
    )
    book_status_check = sql.index(
        "v_book_status IS DISTINCT FROM 'active'"
    )

    assert transaction_lookup < transaction_lock
    assert transaction_lock < transaction_status_check
    assert transaction_status_check < book_lookup
    assert book_lookup < book_lock < book_status_check


def test_posting_and_entry_operations_lock_active_book():
    definitions = [
        item for item in literal_assignments()["INTEGRITY_DDL"]
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_guard_transaction_lifecycle("
        )
    ]

    assert len(definitions) == 1
    sql = " ".join(definitions[0].split())

    book_lookup = sql.index("FROM __SCHEMA__.paper_ledger_books")
    book_lock = sql.index("FOR SHARE")
    book_status_check = sql.index(
        "v_book_status IS DISTINCT FROM 'active'"
    )
    entry_validation = sql.index(
        "FROM __SCHEMA__.paper_ledger_entries"
    )

    assert book_lookup < book_lock < book_status_check
    assert book_status_check < entry_validation

def test_account_insert_automatically_creates_zero_snapshot():
    ddl = "\n".join(literal_assignments()["INTEGRITY_DDL"])

    assert "paper_ledger_create_account_snapshot" in ddl
    assert "trg_paper_ledger_account_create_snapshot" in ddl
    assert "AFTER INSERT" in ddl
    assert "paper_ledger_account_snapshots" in ddl
    assert "NEW.book_id" in ddl
    assert "NEW.id" in ddl


def test_posting_trigger_updates_snapshots_atomically():
    definitions = literal_assignments()["INTEGRITY_DDL"]

    function = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots("
        )
    ]

    assert len(function) == 1

    sql = " ".join(function[0].split())

    assert "OLD.status" in sql
    assert "NEW.status" in sql
    assert "'draft'" in sql
    assert "'posted'" in sql
    assert "paper_ledger_entries" in sql
    assert "paper_ledger_account_snapshots" in sql
    assert "balance = balance +" in sql


def test_snapshot_delta_uses_each_accounts_normal_side():
    definitions = literal_assignments()["INTEGRITY_DDL"]

    function = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots("
        )
    ]

    assert len(function) == 1

    sql = " ".join(function[0].split())

    assert "normal_side" in sql
    assert "debit" in sql
    assert "credit" in sql
    assert "ledger_account_id" in sql


def test_snapshot_updates_lock_rows_in_deterministic_order():
    definitions = literal_assignments()["INTEGRITY_DDL"]

    function = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots("
        )
    ]

    assert len(function) == 1

    sql = " ".join(function[0].split())

    assert "ORDER BY" in sql
    assert "ledger_account_id" in sql
    assert "FOR UPDATE" in sql


def test_posting_fails_closed_when_snapshot_is_missing():
    definitions = literal_assignments()["INTEGRITY_DDL"]

    function = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots("
        )
    ]

    assert len(function) == 1

    sql = " ".join(function[0].split())

    assert "NOT FOUND" in sql or "v_snapshot" in sql
    assert "RAISE EXCEPTION" in sql
    assert "snapshot" in sql.lower()


def test_both_markets_allow_exactly_the_six_mandatory_account_codes():
    values = literal_assignments()
    dialect = postgresql.dialect()

    account_table = Base.metadata.tables["paper_ledger_accounts"]
    compiled = str(CreateTable(account_table).compile(dialect=dialect))
    normalized = " ".join(compiled.split())

    expected_constraint = (
        "code IN ('cash_available', 'spot_position_cost', "
        "'futures_margin_locked', 'realized_pnl', 'fee_expense', "
        "'contributed_capital')"
    )
    assert expected_constraint in normalized

    migration_accounts = [
        ddl for ddl in values["TABLE_DDL"]
        if "CREATE TABLE paper_ledger_accounts" in ddl
    ]

    assert len(migration_accounts) == 1
    assert migration_accounts[0].strip() == compiled.strip()


def test_snapshot_write_guard_restricts_direct_mutations():
    definitions = literal_assignments()["INTEGRITY_DDL"]

    functions = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_guard_snapshot_write("
        )
    ]

    triggers = [
        item for item in definitions
        if item.startswith(
            "CREATE TRIGGER trg_paper_ledger_snapshot_write_guard"
        )
    ]

    assert len(functions) == 1
    assert len(triggers) == 1

    function_sql = " ".join(functions[0].split())
    trigger_sql = " ".join(triggers[0].split())

    assert "pg_trigger_depth() < 2" in function_sql
    assert "current_user::regrole::oid" in function_sql
    assert "v_create_owner IS DISTINCT FROM v_apply_owner" in function_sql
    assert "writer.prosecdef" in function_sql
    assert "approved writer owner" in function_sql
    assert "TG_OP = 'DELETE'" in function_sql
    assert "RETURN OLD" in function_sql
    assert "RETURN NEW" in function_sql
    assert "RAISE EXCEPTION" in function_sql

    assert "BEFORE INSERT OR UPDATE OR DELETE" in trigger_sql
    assert (
        "ON __SCHEMA__.paper_ledger_account_snapshots"
    ) in trigger_sql
    assert (
        "EXECUTE FUNCTION "
        "__SCHEMA__.paper_ledger_guard_snapshot_write()"
    ) in trigger_sql


def test_snapshot_writer_functions_are_security_definers_and_not_public():
    ddl = "\n".join(literal_assignments()["INTEGRITY_DDL"])

    create_function = [
        item for item in literal_assignments()["INTEGRITY_DDL"]
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_create_account_snapshot("
        )
    ]
    apply_function = [
        item for item in literal_assignments()["INTEGRITY_DDL"]
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots("
        )
    ]

    assert len(create_function) == 1
    assert len(apply_function) == 1

    for definition in (create_function[0], apply_function[0]):
        normalized = " ".join(definition.split())
        assert "SECURITY DEFINER" in normalized
        assert "SET search_path = pg_catalog, __SCHEMA__" in normalized

    assert (
        "REVOKE ALL ON TABLE "
        "__SCHEMA__.paper_ledger_account_snapshots FROM PUBLIC"
    ) in ddl

    assert (
        "REVOKE ALL ON FUNCTION "
        "__SCHEMA__.paper_ledger_create_account_snapshot() FROM PUBLIC"
    ) in ddl

    assert (
        "REVOKE ALL ON FUNCTION "
        "__SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots() FROM PUBLIC"
    ) in ddl

def test_snapshot_guard_is_registered_before_snapshot_creation_paths():
    definitions = literal_assignments()["INTEGRITY_DDL"]

    guard_function = next(
        index for index, item in enumerate(definitions)
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_guard_snapshot_write("
        )
    )

    guard_trigger = next(
        index for index, item in enumerate(definitions)
        if item.startswith(
            "CREATE TRIGGER trg_paper_ledger_snapshot_write_guard"
        )
    )

    account_snapshot_function = next(
        index for index, item in enumerate(definitions)
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_ledger_create_account_snapshot("
        )
    )

    assert guard_function < guard_trigger < account_snapshot_function


def test_downgrade_counts_rows_in_current_schema_only():
    source = migration_source()
    normalized = " ".join(source.split())

    assert "SELECT current_schema()" in source
    assert "target_schema := current_schema()" in normalized
    assert re.search(
        r"format\(\s*'SELECT COUNT\(\*\) FROM %I\.%I'"
        r"\s*,\s*target_schema\s*,\s*table_name\s*\)",
        normalized,
    )


def test_downgrade_drops_tables_in_resolved_schema():
    source = migration_source()
    normalized = " ".join(source.split())

    assert "op.drop_table(table_name, schema=schema_name)" in normalized
    assert "Cannot resolve downgrade target schema." in source


def test_migration_docstring_describes_integrity_triggers():
    source = migration_source()

    assert "Creates the paper-trading financial schema" in source
    assert "integrity triggers" in source.lower()
    assert "Integrity triggers are not included yet" not in source



def test_execution_request_records_are_immutable_and_cleanup_is_complete():
    values = literal_assignments()
    definitions = values["INTEGRITY_DDL"]
    drops = "\\n".join(values["INTEGRITY_DROP_DDL"])

    functions = [
        item for item in definitions
        if item.startswith(
            "CREATE FUNCTION __SCHEMA__.paper_execution_request_immutable("
        )
    ]
    row_triggers = [
        item for item in definitions
        if item.startswith(
            "CREATE TRIGGER trg_paper_execution_request_immutable"
        )
    ]
    truncate_triggers = [
        item for item in definitions
        if item.startswith(
            "CREATE TRIGGER trg_paper_execution_request_no_truncate"
        )
    ]

    assert len(functions) == 1
    assert len(row_triggers) == 1
    assert len(truncate_triggers) == 1

    function_sql = " ".join(functions[0].split())
    row_sql = " ".join(row_triggers[0].split())
    truncate_sql = " ".join(truncate_triggers[0].split())

    assert "RAISE EXCEPTION" in function_sql
    assert "Paper execution requests are immutable" in function_sql
    assert "UPDATE OR DELETE" in row_sql
    assert "BEFORE TRUNCATE" in truncate_sql
    assert "FOR EACH STATEMENT" in truncate_sql

    assert "trg_paper_execution_request_immutable" in drops
    assert "trg_paper_execution_request_no_truncate" in drops
    assert "paper_execution_request_immutable()" in drops

