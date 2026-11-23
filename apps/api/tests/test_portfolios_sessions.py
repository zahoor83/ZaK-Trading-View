from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.database.base import Base
import app.models  # noqa: F401


def test_portfolio_and_session_tables_are_registered():
    expected = {"paper_portfolios", "paper_sessions"}
    assert expected.issubset(set(Base.metadata.tables))


def test_account_has_unique_portfolio_per_market_type():
    table = Base.metadata.tables["paper_portfolios"]
    constraints = [
        c for c in table.constraints if isinstance(c, UniqueConstraint)
    ]
    assert any(
        {column.name for column in c.columns}
        == {"account_id", "market_type"}
        for c in constraints
    )


def test_portfolio_market_type_is_restricted():
    table = Base.metadata.tables["paper_portfolios"]
    checks = [
        str(c.sqltext) for c in table.constraints
        if isinstance(c, CheckConstraint)
    ]
    assert any("'spot'" in check and "'futures'" in check for check in checks)


def test_spot_and_futures_position_mode_rules_exist():
    table = Base.metadata.tables["paper_portfolios"]
    checks = [
        str(c.sqltext) for c in table.constraints
        if isinstance(c, CheckConstraint)
    ]
    assert any(
        "one_way" in check and "hedge" in check
        and "position_mode" in check
        for check in checks
    )


def test_session_uses_composite_account_portfolio_foreign_key():
    table = Base.metadata.tables["paper_sessions"]
    constraints = [
        c for c in table.constraints
        if isinstance(c, ForeignKeyConstraint)
    ]
    assert any(
        {element.parent.name for element in c.elements}
        == {"account_id", "portfolio_id"}
        and {element.target_fullname for element in c.elements}
        == {"paper_portfolios.account_id", "paper_portfolios.id"}
        for c in constraints
    )


def test_session_number_must_be_positive():
    table = Base.metadata.tables["paper_sessions"]
    checks = [
        str(c.sqltext) for c in table.constraints
        if isinstance(c, CheckConstraint)
    ]
    assert any("session_number > 0" in check for check in checks)


def test_only_one_active_session_index_exists():
    table = Base.metadata.tables["paper_sessions"]
    indexes = [
        index for index in table.indexes
        if index.name == "uq_paper_sessions_one_active"
    ]
    assert len(indexes) == 1
    assert indexes[0].unique is True
    assert "active" in str(indexes[0].dialect_options["postgresql"]["where"])


def test_archived_session_requires_archived_timestamp():
    table = Base.metadata.tables["paper_sessions"]
    checks = [
        str(c.sqltext) for c in table.constraints
        if isinstance(c, CheckConstraint)
    ]
    assert any(
        "archived_at IS NOT NULL" in check
        and "archived_at IS NULL" in check
        for check in checks
    )


def test_portfolio_schema_does_not_introduce_unapproved_financial_balances():
    table = Base.metadata.tables["paper_portfolios"]
    forbidden = {"balance", "cash_balance", "equity", "available_margin"}
    assert forbidden.isdisjoint(set(table.columns.keys()))