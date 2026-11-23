import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.paper_trade_fingerprint import fingerprint_trade_intent_request
from app.security.paper_idempotency_decision import (
    IdempotencyDecision,
    IdempotencyDecisionKind,
)
from app.services import paper_ledger_posting as posting
from app.services.paper_ledger_rules import JournalEntryInput


class FakeResult:
    def __init__(self, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.transaction_started = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.session.committed = True
        else:
            self.session.rolled_back = True
        return False


class FakeSession:
    def __init__(self, results, *, fail_flush_at=None):
        self.results = list(results)
        self.added = []
        self.transaction_started = False
        self.committed = False
        self.rolled_back = False
        self.flush_count = 0
        self.fail_flush_at = fail_flush_at

    def in_transaction(self):
        return self.transaction_started

    def begin(self):
        return FakeTransaction(self)

    async def execute(self, statement):
        if not self.results:
            raise AssertionError("Unexpected database execute call.")
        return self.results.pop(0)

    def add(self, item):
        self.added.append(item)

    def add_all(self, items):
        self.added.extend(items)

    async def flush(self):
        self.flush_count += 1
        if self.fail_flush_at == self.flush_count:
            raise RuntimeError("Simulated database flush failure.")

        for item in self.added:
            if isinstance(item, posting.PaperLedgerTransaction) and item.id is None:
                item.id = uuid.uuid4()


@pytest.fixture
def setup_posting(monkeypatch):
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    portfolio_id = uuid.uuid4()
    book_id = uuid.uuid4()
    session_id = uuid.uuid4()
    request_key = str(uuid.uuid4())

    entries = (
        JournalEntryInput(
            account_code="spot_position_cost",
            direction="debit",
            amount=Decimal("10.00"),
        ),
        JournalEntryInput(
            account_code="cash_available",
            direction="credit",
            amount=Decimal("10.00"),
        ),
    )

    access = SimpleNamespace(
        user_id=user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        session_id=session_id,
        book_id=book_id,
        market_type="spot",
    )

    async def fake_authorize(*args, **kwargs):
        return access

    async def fake_validate(*args, **kwargs):
        return book_id

    monkeypatch.setattr(posting, "authorize_paper_write", fake_authorize)
    monkeypatch.setattr(posting, "validate_existing_paper_ledger", fake_validate)

    trade_intent = {
        "position_id": None,
        "symbol": "BTCUSDT",
        "side": "buy",
        "position_side": None,
        "quantity": Decimal("1"),
        "order_type": "market",
        "leverage": None,
        "limit_price": None,
        "reduce_only": False,
    }

    fingerprint = fingerprint_trade_intent_request(
        transaction_type="spot_buy",
        market_type="spot",
        trade_intent=trade_intent,
    )

    accounts = [
        SimpleNamespace(
            id=uuid.uuid4(),
            book_id=book_id,
            market_type="spot",
            code=code,
        )
        for code in (
            "cash_available",
            "spot_position_cost",
            "futures_margin_locked",
            "realized_pnl",
            "fee_expense",
            "contributed_capital",
        )
    ]

    return SimpleNamespace(
        user_id=user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        book_id=book_id,
        request_key=request_key,
        entries=entries,
        trade_intent=trade_intent,
        access=access,
        fingerprint=fingerprint,
        accounts=accounts,
    )


@pytest.mark.asyncio
async def test_new_posting_persists_transaction_entries_and_execution_request(setup_posting):
    data = setup_posting
    session = FakeSession([
        FakeResult(),       # no existing execution request
        FakeResult(),       # no orphan transaction
        FakeResult(rows=data.accounts),
    ])

    response_extra = {
        "position_id": str(uuid.uuid4()),
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": "1",
        "quote_source": "spot_book_ticker",
    }

    result = await posting.post_paper_trade_journal(
        session,
        authenticated_user_id=data.user_id,
        account_id=data.account_id,
        portfolio_id=data.portfolio_id,
        idempotency_key=data.request_key,
        transaction_type="spot_buy",
        trade_intent=data.trade_intent,
        entries=data.entries,
        response_data_extra=response_extra,
    )

    assert result.replayed is False
    assert result.response_data["transaction_id"]
    assert result.response_data["book_id"] == str(data.book_id)
    assert result.response_data["market_type"] == "spot"
    assert result.response_data["transaction_type"] == "spot_buy"
    assert result.response_data["status"] == "posted"
    assert result.response_data["entry_count"] == 2
    assert result.response_data["debit_total"] == "10.00"
    assert result.response_data["credit_total"] == "10.00"
    for key, value in response_extra.items():
        assert result.response_data[key] == value
    assert session.transaction_started is True
    assert session.committed is True
    assert session.rolled_back is False

    transactions = [
        item for item in session.added
        if isinstance(item, posting.PaperLedgerTransaction)
    ]
    ledger_entries = [
        item for item in session.added
        if isinstance(item, posting.PaperLedgerEntry)
    ]
    requests = [
        item for item in session.added
        if isinstance(item, posting.PaperExecutionRequest)
    ]

    assert len(transactions) == 1
    assert transactions[0].status == "posted"
    assert len(ledger_entries) == 2
    assert len(requests) == 1
    assert requests[0].status == "completed"
    assert requests[0].response_data == result.response_data


@pytest.mark.asyncio
async def test_same_key_and_fingerprint_returns_persisted_replay(setup_posting):
    data = setup_posting
    response = {
        "transaction_id": str(uuid.uuid4()),
        "book_id": str(data.book_id),
        "market_type": "spot",
        "transaction_type": "spot_buy",
        "status": "posted",
        "entry_count": 2,
        "debit_total": "10.00",
        "credit_total": "10.00",
        "position_id": str(uuid.uuid4()),
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": "1",
        "quote_source": "spot_book_ticker",
    }
    existing = SimpleNamespace(
        request_fingerprint=data.fingerprint,
        response_data=response,
    )
    session = FakeSession([FakeResult(scalar=existing)])

    result = await posting.post_paper_trade_journal(
        session,
        authenticated_user_id=data.user_id,
        account_id=data.account_id,
        portfolio_id=data.portfolio_id,
        idempotency_key=data.request_key,
        transaction_type="spot_buy",
        trade_intent=data.trade_intent,
        entries=(
            JournalEntryInput(
                account_code="spot_position_cost",
                direction="debit",
                amount=Decimal("99.00"),
            ),
            JournalEntryInput(
                account_code="cash_available",
                direction="credit",
                amount=Decimal("99.00"),
            ),
        ),
    )

    assert result.replayed is True
    assert result.response_data == response
    assert session.committed is True
    assert session.added == []


@pytest.mark.asyncio
async def test_same_key_with_different_fingerprint_raises_conflict(setup_posting):
    data = setup_posting
    existing = SimpleNamespace(
        request_fingerprint="a" * 64,
        response_data={"status": "posted"},
    )
    session = FakeSession([FakeResult(scalar=existing)])

    with pytest.raises(posting.PaperIdempotencyConflict):
        await posting.post_paper_trade_journal(
            session,
            authenticated_user_id=data.user_id,
            account_id=data.account_id,
            portfolio_id=data.portfolio_id,
            idempotency_key=data.request_key,
            transaction_type="spot_buy",
            trade_intent=data.trade_intent,
            entries=data.entries,
        )

    assert session.rolled_back is True
    assert session.added == []


@pytest.mark.asyncio
async def test_orphan_transaction_fails_closed(setup_posting):
    data = setup_posting
    session = FakeSession([
        FakeResult(),                    # no execution request
        FakeResult(scalar=uuid.uuid4()), # orphan transaction exists
    ])

    with pytest.raises(posting.PaperLedgerPostingError, match="orphaned"):
        await posting.post_paper_trade_journal(
            session,
            authenticated_user_id=data.user_id,
            account_id=data.account_id,
            portfolio_id=data.portfolio_id,
            idempotency_key=data.request_key,
            transaction_type="spot_buy",
            trade_intent=data.trade_intent,
            entries=data.entries,
        )

    assert session.rolled_back is True
    assert session.added == []


@pytest.mark.asyncio
async def test_database_flush_failure_rolls_back_posting(setup_posting):
    data = setup_posting
    session = FakeSession(
        [
            FakeResult(),
            FakeResult(),
            FakeResult(rows=data.accounts),
        ],
        fail_flush_at=1,
    )

    with pytest.raises(posting.PaperLedgerPostingError, match="rolled back"):
        await posting.post_paper_trade_journal(
            session,
            authenticated_user_id=data.user_id,
            account_id=data.account_id,
            portfolio_id=data.portfolio_id,
            idempotency_key=data.request_key,
            transaction_type="spot_buy",
            trade_intent=data.trade_intent,
            entries=data.entries,
        )

    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_transaction_scoped_posting_rejects_missing_transaction():
    class InactiveSession:
        def in_transaction(self):
            return False

    with pytest.raises(
        posting.PaperLedgerPostingError,
        match="active caller-owned transaction",
    ):
        await posting.post_paper_trade_journal_in_transaction(
            InactiveSession(),
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            idempotency_key="invalid",
            transaction_type="invalid",
            trade_intent={},
            entries=(),
        )
