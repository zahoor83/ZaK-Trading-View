from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.security.permissions import AccountRole
from app.services import paper_trading_bootstrap as bootstrap


class Result:
    def __init__(self, *, rows=None, scalars=None):
        self._rows = rows or []
        self._scalars = scalars or []

    def all(self):
        return self._rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars)


class QueuedSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def execute(self, statement):
        self.calls += 1
        if not self.results:
            raise AssertionError("Unexpected database query.")
        return self.results.pop(0)


def dec(value):
    return Decimal(value)


@pytest.mark.parametrize(
    "futures_leverage",
    ["10", None, "0", "-1", "NaN", True],
    ids=["valid", "missing", "zero", "negative", "not-finite", "boolean"],
)
@pytest.mark.asyncio
async def test_bootstrap_service_validates_spot_and_futures_leverage(
    monkeypatch, futures_leverage
):
    user_id = uuid4()
    account_id = uuid4()
    spot_id, futures_id = uuid4(), uuid4()
    spot_session_id, futures_session_id = uuid4(), uuid4()
    spot_book_id, futures_book_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)

    account = SimpleNamespace(id=account_id, name="Primary Account", owner_user_id=user_id)
    membership = SimpleNamespace(role="owner")
    spot = SimpleNamespace(
        id=spot_id, market_type="spot", status="active", position_mode=None
    )
    futures = SimpleNamespace(
        id=futures_id, market_type="futures", status="active", position_mode="hedge"
    )
    spot_session = SimpleNamespace(
        id=spot_session_id, account_id=account_id, portfolio_id=spot_id,
        session_number=2, status="active",
    )
    futures_session = SimpleNamespace(
        id=futures_session_id, account_id=account_id, portfolio_id=futures_id,
        session_number=1, status="active",
    )

    codes = [
        "cash_available", "spot_position_cost", "futures_margin_locked",
        "realized_pnl", "fee_expense", "contributed_capital",
    ]

    def snapshot_rows(book_id, cash):
        rows = []
        for code in codes:
            ledger_account = SimpleNamespace(code=code)
            balance = dec(cash) if code == "cash_available" else (
                dec("1000.00") if code == "contributed_capital" else dec("0")
            )
            snapshot = SimpleNamespace(balance=balance)
            rows.append((ledger_account, snapshot))
        return rows

    open_position = SimpleNamespace(
        id=uuid4(), symbol="BTCUSDT", side="long", status="open",
        initial_quantity=dec("0.0100"), remaining_quantity=dec("0.0100"),
        entry_price=dec("65000.00"), remaining_cost_basis=dec("650.00"),
        margin_locked=None, leverage_snapshot=None,
        realized_pnl=dec("0"), entry_fee_original=dec("0.650000"),
        entry_fee_posted=dec("0.65"), opened_at=now, closed_at=None,
        market_type="spot",
    )
    closed_position = SimpleNamespace(
        id=uuid4(), symbol="ETHUSDT", side="long", status="closed",
        initial_quantity=dec("1"), remaining_quantity=dec("0"),
        entry_price=dec("3000"), remaining_cost_basis=dec("0"),
        margin_locked=None, leverage_snapshot=None,
        realized_pnl=dec("25.123456"), entry_fee_original=dec("3"),
        entry_fee_posted=dec("3.00"), opened_at=now, closed_at=now,
        market_type="spot",
    )
    futures_position = SimpleNamespace(
        id=uuid4(), symbol="BTCUSDT", side="long", status="open",
        initial_quantity=dec("0.0100"), remaining_quantity=dec("0.0100"),
        entry_price=dec("65000.00"), remaining_cost_basis=None,
        margin_locked=dec("65.00"),
        leverage_snapshot=(
            dec(futures_leverage)
            if isinstance(futures_leverage, str)
            else futures_leverage
        ),
        realized_pnl=dec("0"), entry_fee_original=dec("0.650000"),
        entry_fee_posted=dec("0.65"), opened_at=now, closed_at=None,
        market_type="futures",
    )

    session = QueuedSession([
        Result(rows=[(membership, account)]),
        Result(scalars=[spot, futures]),
        Result(scalars=[spot_session]),
        Result(rows=snapshot_rows(spot_book_id, "349.35")),
        Result(scalars=[open_position, closed_position]),
        Result(scalars=[futures_session]),
        Result(rows=snapshot_rows(futures_book_id, "1000.00")),
        Result(scalars=[futures_position]),
    ])

    async def authorize(*args, **kwargs):
        return SimpleNamespace(
            role=AccountRole.OWNER,
            market_type="spot" if kwargs["portfolio_id"] == spot_id else "futures",
        )

    async def validate(*args, **kwargs):
        return spot_book_id if kwargs["market_type"] == "spot" else futures_book_id

    monkeypatch.setattr(bootstrap, "authorize_portfolio_permission", authorize)
    monkeypatch.setattr(bootstrap, "validate_existing_paper_ledger", validate)

    if futures_leverage != "10":
        with pytest.raises(bootstrap.PaperTradingBootstrapIntegrityError):
            await bootstrap.load_paper_trading_bootstrap(
                session,
                authenticated_user_id=user_id,
            )
        return

    result = await bootstrap.load_paper_trading_bootstrap(
        session,
        authenticated_user_id=user_id,
    )

    assert result["account"] == {
        "id": account_id,
        "name": "Primary Account",
        "role": "owner",
    }
    assert [item["market_type"] for item in result["portfolios"]] == ["spot", "futures"]
    spot_result, futures_result = result["portfolios"]
    assert spot_result["ledger"]["cash_available"] == "349.35"
    assert futures_result["ledger"]["cash_available"] == "1000.00"
    assert spot_result["session"]["session_number"] == 2
    assert [item["status"] for item in spot_result["positions"]] == ["open", "closed"]
    assert spot_result["positions"][0]["initial_quantity"] == "0.0100"
    assert spot_result["positions"][0]["leverage"] is None
    assert spot_result["positions"][1]["leverage"] is None
    assert spot_result["positions"][1]["realized_pnl"] == "25.123456"
    assert futures_result["positions"][0]["leverage"] == "10"
    assert futures_result["positions"][0]["margin_locked"] == "65.00"
    assert futures_result["positions"][0]["remaining_cost_basis"] is None
    assert spot_result["positions"][0]["opened_at"] == now.isoformat()
    assert spot_result["positions"][0]["closed_at"] is None
    assert not session.results
    assert session.calls == 8


@pytest.mark.asyncio
async def test_bootstrap_service_fails_closed_when_multiple_accounts_exist():
    session = QueuedSession([
        Result(rows=[
            (SimpleNamespace(role="owner"), SimpleNamespace(id=uuid4(), name="A")),
            (SimpleNamespace(role="admin"), SimpleNamespace(id=uuid4(), name="B")),
        ])
    ])
    with pytest.raises(bootstrap.PaperTradingBootstrapIntegrityError):
        await bootstrap.load_paper_trading_bootstrap(
            session,
            authenticated_user_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_bootstrap_service_fails_closed_when_active_session_is_missing(monkeypatch):
    user_id = uuid4()
    account_id, portfolio_id, futures_id = uuid4(), uuid4(), uuid4()
    membership = SimpleNamespace(role="owner")
    account = SimpleNamespace(id=account_id, name="Primary", owner_user_id=user_id)
    spot = SimpleNamespace(id=portfolio_id, market_type="spot", status="active", position_mode=None)
    futures = SimpleNamespace(id=futures_id, market_type="futures", status="active", position_mode="hedge")
    session = QueuedSession([
        Result(rows=[(membership, account)]),
        Result(scalars=[spot, futures]),
        Result(scalars=[]),
    ])

    async def authorize(*args, **kwargs):
        return SimpleNamespace(role=AccountRole.OWNER, market_type="spot")

    monkeypatch.setattr(bootstrap, "authorize_portfolio_permission", authorize)
    with pytest.raises(bootstrap.PaperTradingBootstrapIntegrityError):
        await bootstrap.load_paper_trading_bootstrap(
            session,
            authenticated_user_id=user_id,
        )


@pytest.mark.asyncio
async def test_bootstrap_service_fails_closed_when_snapshot_set_is_incomplete(monkeypatch):
    user_id, account_id = uuid4(), uuid4()
    spot_id, futures_id = uuid4(), uuid4()
    spot_book_id, futures_book_id = uuid4(), uuid4()
    membership = SimpleNamespace(role="owner")
    account = SimpleNamespace(id=account_id, name="Primary", owner_user_id=user_id)
    spot = SimpleNamespace(id=spot_id, market_type="spot", status="active", position_mode=None)
    futures = SimpleNamespace(id=futures_id, market_type="futures", status="active", position_mode="hedge")
    spot_session = SimpleNamespace(
        id=uuid4(), account_id=account_id, portfolio_id=spot_id,
        session_number=1, status="active",
    )
    futures_session = SimpleNamespace(
        id=uuid4(), account_id=account_id, portfolio_id=futures_id,
        session_number=1, status="active",
    )
    session = QueuedSession([
        Result(rows=[(membership, account)]),
        Result(scalars=[spot, futures]),
        Result(scalars=[spot_session]),
        Result(rows=[]),
    ])

    async def authorize(*args, **kwargs):
        return SimpleNamespace(
            role=AccountRole.OWNER,
            market_type="spot" if kwargs["portfolio_id"] == spot_id else "futures",
        )

    async def validate(*args, **kwargs):
        return spot_book_id if kwargs["market_type"] == "spot" else futures_book_id

    monkeypatch.setattr(bootstrap, "authorize_portfolio_permission", authorize)
    monkeypatch.setattr(bootstrap, "validate_existing_paper_ledger", validate)

    with pytest.raises(bootstrap.PaperTradingBootstrapIntegrityError):
        await bootstrap.load_paper_trading_bootstrap(
            session,
            authenticated_user_id=user_id,
        )
