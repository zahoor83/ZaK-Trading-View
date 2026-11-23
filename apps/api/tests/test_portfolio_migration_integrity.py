from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0002_portfolios_sessions.py"
)


def migration_source():
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_existing_accounts_are_rejected_before_schema_creation():
    source = migration_source()

    guard = "IF EXISTS (SELECT 1 FROM trading_accounts)"
    error = "Migration 0002 requires an empty trading_accounts table"

    assert guard in source
    assert error in source
    assert source.index(guard) < source.index("op.create_table(")


def test_integrity_check_locks_parent_before_counting():
    source = migration_source()

    start = source.index("CREATE FUNCTION assert_account_portfolio_pair")
    end = source.index("CREATE FUNCTION check_account_portfolio_pair_trigger")

    function = source[start:end]

    assert "FOR UPDATE" in function
    assert function.index("FOR UPDATE") < function.index("SELECT count(*)")


def test_portfolio_mutations_use_before_row_lock_trigger():
    source = migration_source()

    assert "CREATE FUNCTION lock_paper_portfolio_account()" in source
    assert "CREATE TRIGGER trg_lock_paper_portfolio_account" in source
    assert "BEFORE INSERT OR UPDATE OR DELETE ON paper_portfolios" in source


def test_portfolio_account_ownership_cannot_be_reassigned():
    source = migration_source()

    assert "NEW.account_id IS DISTINCT FROM OLD.account_id" in source
    assert "paper_portfolios.account_id is immutable" in source


def test_downgrade_removes_lock_trigger_and_function():
    source = migration_source()
    downgrade = source[source.index("def downgrade()"):]

    assert "DROP TRIGGER IF EXISTS trg_lock_paper_portfolio_account" in downgrade
    assert "DROP FUNCTION IF EXISTS lock_paper_portfolio_account()" in downgrade


def test_existing_deferred_pair_integrity_is_preserved():
    source = migration_source()

    assert "trg_account_portfolio_pair" in source
    assert "trg_portfolio_pair_integrity" in source
    assert "DEFERRABLE INITIALLY DEFERRED" in source