from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import paper_trade_execution as execution
from app.services.paper_fee_policy import PaperFeePolicy
from app.services.paper_ledger_posting import PaperPostingResult
from app.services.paper_trade_fingerprint import fingerprint_trade_intent_request


class FakeResult:
    def __init__(self, scalar=None):
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.active = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.session.active = False
        if exc_type is None:
            self.session.committed = True
        else:
            self.session.rolled_back = True
        return False


class FakeSession:
    def __init__(self, *, existing_request=None, position=None):
        self.active = False
        self.committed = False
        self.rolled_back = False
        self.existing_request = existing_request
        self.position = position
        self.added = []
        self.executed = []

    def in_transaction(self):
        return self.active

    def begin(self):
        return FakeTransaction(self)

    async def execute(self, statement):
        sql = str(statement).lower()
        self.executed.append(sql)
        if "paper_execution_requests" in sql:
            return FakeResult(self.existing_request)
        if "paper_positions" in sql:
            return FakeResult(self.position)
        raise AssertionError(f"Unexpected SQL statement: {sql}")

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if getattr(item, "id", None) is None and hasattr(item, "book_id"):
                item.id = uuid.uuid4()


def make_intent(*, side="buy", position_id=None):
    return {
        "position_id": position_id,
        "symbol": "BTCUSDT",
        "side": side,
        "position_side": None,
        "quantity": Decimal("0.1"),
        "order_type": "market",
        "leverage": None,
        "limit_price": None,
        "reduce_only": False,
    }


def install_dependencies(monkeypatch, *, side="buy", existing_request=None, position=None):
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    portfolio_id = uuid.uuid4()
    book_id = uuid.uuid4()
    session_id = uuid.uuid4()
    intent = make_intent(side=side, position_id=(
        str(position.id) if position is not None else None
    ))
    fingerprint = fingerprint_trade_intent_request(
        transaction_type="spot_buy" if side == "buy" else "spot_sell",
        market_type="spot",
        trade_intent=intent,
    )
    request_key = str(uuid.uuid4())
    access = SimpleNamespace(
        user_id=user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        session_id=session_id,
        book_id=book_id,
        market_type="spot",
    )
    monkeypatch.setattr(execution, "MarketDataService", type("FakeMarketData", (), {}))

    async def authorize(*args, **kwargs):
        assert kwargs["permission"].value == "paper_trade.execute"
        return access

    async def validate_ledger(*args, **kwargs):
        return book_id

    policy = PaperFeePolicy(
        policy_id="test-policy",
        version=1,
        market_type="spot",
        entry_fee_rate=Decimal("0.001"),
        exit_fee_rate=Decimal("0.001"),
    )

    async def get_policy(*args, **kwargs):
        return policy

    class FakeAdapter:
        def __init__(self, market_data):
            pass

        def validate_with_context(self, symbol, *, side):
            assert symbol == "BTCUSDT"
            return (
                SimpleNamespace(
                    price=Decimal("100.01") if side == "buy" else Decimal("100.00"),
                    source="spot_book_ticker",
                ),
                SimpleNamespace(
                    metadata_snapshot=object(),
                    symbol_metadata_snapshot={
                        "schema_version": 1,
                        "market": "spot",
                        "symbol": "BTCUSDT",
                        "tick_size": "0.01",
                    },
                    received_at_monotonic=10.0,
                    current_monotonic=10.1,
                    update_id=123,
                ),
            )

    async def post(*args, **kwargs):
        assert kwargs["response_data_extra"]["position_id"]
        assert kwargs["entries"]
        return PaperPostingResult(
            replayed=False,
            response_data={"status": "posted", **kwargs["response_data_extra"]},
        )

    monkeypatch.setattr(execution, "authorize_paper_write", authorize)
    monkeypatch.setattr(execution, "validate_existing_paper_ledger", validate_ledger)
    monkeypatch.setattr(execution, "get_current_paper_fee_policy", get_policy)
    monkeypatch.setattr(execution, "SpotBookTickerExecutionAdapter", FakeAdapter)
    monkeypatch.setattr(execution, "validate_order", lambda *args, **kwargs: None)
    monkeypatch.setattr(execution, "post_paper_trade_journal_in_transaction", post)

    return SimpleNamespace(
        user_id=user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        book_id=book_id,
        intent=intent,
        fingerprint=fingerprint,
        request_key=request_key,
        policy=policy,
    )


@pytest.mark.asyncio
async def test_spot_buy_creates_position_and_posts_journal_in_same_transaction(monkeypatch):
    data = install_dependencies(monkeypatch)
    session = FakeSession()
    market_data = execution.MarketDataService()

    result = await execution.execute_spot_trade(
        session,
        market_data=market_data,
        authenticated_user_id=data.user_id,
        account_id=data.account_id,
        portfolio_id=data.portfolio_id,
        idempotency_key=data.request_key,
        trade_intent=data.intent,
    )

    assert result.replayed is False
    assert result.response_data["status"] == "posted"
    assert session.committed is True
    assert session.rolled_back is False
    positions = [
        item for item in session.added
        if isinstance(item, execution.PaperPosition)
    ]
    assert len(positions) == 1
    position = positions[0]
    assert position.market_type == "spot"
    assert position.symbol == "BTCUSDT"
    assert position.remaining_quantity == Decimal("0.1")
    assert position.remaining_cost_basis == Decimal("10.001")
    assert position.entry_fee_posted == Decimal("0.01")
    assert position.status == "open"


@pytest.mark.asyncio
async def test_same_key_replay_returns_before_quote_or_position_mutation(monkeypatch):
    data = install_dependencies(monkeypatch)
    existing = SimpleNamespace(
        request_fingerprint=data.fingerprint,
        response_data={"status": "posted", "position_id": str(uuid.uuid4())},
    )
    session = FakeSession(existing_request=existing)
    market_data = execution.MarketDataService()

    class ForbiddenAdapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Replay must return before quote validation.")

    monkeypatch.setattr(execution, "SpotBookTickerExecutionAdapter", ForbiddenAdapter)

    result = await execution.execute_spot_trade(
        session,
        market_data=market_data,
        authenticated_user_id=data.user_id,
        account_id=data.account_id,
        portfolio_id=data.portfolio_id,
        idempotency_key=data.request_key,
        trade_intent=data.intent,
    )

    assert result.replayed is True
    assert result.response_data == existing.response_data
    assert session.committed is True
    assert session.added == []


@pytest.mark.asyncio
async def test_spot_sell_updates_only_matching_open_position(monkeypatch):
    position = SimpleNamespace(
        id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        market_type="spot",
        symbol="BTCUSDT",
        side="long",
        status="open",
        remaining_quantity=Decimal("0.2"),
        remaining_cost_basis=Decimal("20"),
        realized_pnl=Decimal("0"),
        entry_fee_rate=Decimal("0.001"),
        exit_fee_rate=Decimal("0.001"),
        fee_config_snapshot=PaperFeePolicy(
            policy_id="test-policy",
            version=1,
            market_type="spot",
            entry_fee_rate=Decimal("0.001"),
            exit_fee_rate=Decimal("0.001"),
        ).to_snapshot(),
        symbol_metadata_snapshot={
            "schema_version": 1,
            "market": "spot",
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "filters": [{"filterType": "PRICE_FILTER"}],
            "tick_size": "0.01",
        },
        closed_at=None,
    )
    data = install_dependencies(monkeypatch, side="sell", position=position)
    position.book_id = data.book_id
    session = FakeSession(position=position)
    market_data = execution.MarketDataService()

    result = await execution.execute_spot_trade(
        session,
        market_data=market_data,
        authenticated_user_id=data.user_id,
        account_id=data.account_id,
        portfolio_id=data.portfolio_id,
        idempotency_key=data.request_key,
        trade_intent=data.intent,
    )

    assert result.replayed is False
    assert position.remaining_quantity == Decimal("0.1")
    assert position.remaining_cost_basis == Decimal("10")
    assert position.status == "open"
    assert session.committed is True


@pytest.mark.asyncio
async def test_posting_failure_rolls_back_position_mutation(monkeypatch):
    data = install_dependencies(monkeypatch)
    session = FakeSession()
    market_data = execution.MarketDataService()

    async def fail_post(*args, **kwargs):
        raise execution.PaperLedgerPostingError("simulated posting failure")

    monkeypatch.setattr(execution, "post_paper_trade_journal_in_transaction", fail_post)

    with pytest.raises(execution.PaperTradeExecutionError):
        await execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=data.user_id,
            account_id=data.account_id,
            portfolio_id=data.portfolio_id,
            idempotency_key=data.request_key,
            trade_intent=data.intent,
        )

    assert session.rolled_back is True
    assert session.committed is False
