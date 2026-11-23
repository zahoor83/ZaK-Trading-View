"""Read-only paper-ledger market isolation validation tests."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from types import SimpleNamespace

import pytest

from app.models.paper_ledger import (
    PaperLedgerAccount,
    PaperLedgerAccountSnapshot,
    PaperLedgerBook,
    PaperLedgerEntry,
    PaperLedgerTransaction,
)
from app.services.paper_ledger_provisioning import (
    INITIAL_BALANCE,
    INITIAL_FUNDING_KEY,
    MANDATORY_ACCOUNTS,
    _initial_funding_fingerprint,
)
from app.services.paper_ledger_validation import (
    PaperLedgerValidationError,
    validate_existing_paper_ledger,
)


class FakeResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class FakeSession:
    def __init__(self, records):
        self.records = records

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        return FakeResult(self.records.get(entity, []))


def make_valid_ledger(market_type):
    account_id = uuid.uuid4()
    portfolio_id = uuid.uuid4()
    session_id = uuid.uuid4()
    book_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    book = SimpleNamespace(
        id=book_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        session_id=session_id,
        market_type=market_type,
        status="active",
    )

    accounts_by_code = {}
    accounts = []
    for code, normal_side in MANDATORY_ACCOUNTS:
        account = SimpleNamespace(
            id=uuid.uuid4(),
            book_id=book_id,
            market_type=market_type,
            code=code,
            normal_side=normal_side,
        )
        accounts.append(account)
        accounts_by_code[code] = account

    snapshots = []
    for code, account in accounts_by_code.items():
        balance = (
            INITIAL_BALANCE
            if code in {"cash_available", "contributed_capital"}
            else Decimal("0")
        )
        snapshots.append(
            SimpleNamespace(
                id=uuid.uuid4(),
                book_id=book_id,
                ledger_account_id=account.id,
                balance=balance,
            )
        )

    transaction_id = uuid.uuid4()
    transaction = SimpleNamespace(
        id=transaction_id,
        book_id=book_id,
        idempotency_key=INITIAL_FUNDING_KEY,
        request_fingerprint=_initial_funding_fingerprint(
            account_id=account_id,
            portfolio_id=portfolio_id,
            session_id=session_id,
            market_type=market_type,
        ),
        transaction_type="initial_funding",
        status="posted",
        posted_at=now,
    )
    entries = [
        SimpleNamespace(
            id=uuid.uuid4(),
            book_id=book_id,
            transaction_id=transaction_id,
            ledger_account_id=accounts_by_code["cash_available"].id,
            direction="debit",
            amount=INITIAL_BALANCE,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            book_id=book_id,
            transaction_id=transaction_id,
            ledger_account_id=accounts_by_code["contributed_capital"].id,
            direction="credit",
            amount=INITIAL_BALANCE,
        ),
    ]
    records = {
        PaperLedgerBook: [book],
        PaperLedgerAccount: accounts,
        PaperLedgerAccountSnapshot: snapshots,
        PaperLedgerTransaction: [transaction],
        PaperLedgerEntry: entries,
    }
    return {
        "session": FakeSession(records),
        "records": records,
        "account_id": account_id,
        "portfolio_id": portfolio_id,
        "session_id": session_id,
        "book": book,
        "accounts_by_code": accounts_by_code,
    }


async def validate_fixture(fixture, market_type):
    return await validate_existing_paper_ledger(
        fixture["session"],
        account_id=fixture["account_id"],
        portfolio_id=fixture["portfolio_id"],
        session_id=fixture["session_id"],
        market_type=market_type,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["spot", "futures"])
async def test_valid_market_ledger_passes_validation(market_type):
    fixture = make_valid_ledger(market_type)
    assert await validate_fixture(fixture, market_type) == fixture["book"].id


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["spot", "futures"])
async def test_account_market_mismatch_fails_closed(market_type):
    fixture = make_valid_ledger(market_type)
    fixture["records"][PaperLedgerAccount][0].market_type = (
        "futures" if market_type == "spot" else "spot"
    )
    with pytest.raises(
        PaperLedgerValidationError,
        match="account market does not match its book",
    ):
        await validate_fixture(fixture, market_type)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market_type", "prohibited_code"),
    [
        ("spot", "futures_margin_locked"),
        ("futures", "spot_position_cost"),
    ],
)
async def test_cross_market_journal_posting_fails_closed(market_type, prohibited_code):
    fixture = make_valid_ledger(market_type)
    prohibited_account = fixture["accounts_by_code"][prohibited_code]
    cash_account = fixture["accounts_by_code"]["cash_available"]
    transaction_id = uuid.uuid4()
    transaction = SimpleNamespace(
        id=transaction_id,
        book_id=fixture["book"].id,
        idempotency_key="additional-transaction",
        request_fingerprint="a" * 64,
        transaction_type="spot_buy" if market_type == "spot" else "futures_open",
        status="posted",
        posted_at=datetime.now(timezone.utc),
    )
    fixture["records"][PaperLedgerTransaction].append(transaction)
    fixture["records"][PaperLedgerEntry].extend(
        [
            SimpleNamespace(
                id=uuid.uuid4(), book_id=fixture["book"].id,
                transaction_id=transaction_id,
                ledger_account_id=prohibited_account.id,
                direction="debit", amount=Decimal("1"),
            ),
            SimpleNamespace(
                id=uuid.uuid4(), book_id=fixture["book"].id,
                transaction_id=transaction_id,
                ledger_account_id=cash_account.id,
                direction="credit", amount=Decimal("1"),
            ),
        ]
    )
    with pytest.raises(
        PaperLedgerValidationError,
        match="prohibited cross-market account posting",
    ):
        await validate_fixture(fixture, market_type)


def _append_precision_test_transaction(fixture, debit_amounts, credit_amounts):
    transaction_id = uuid.uuid4()
    transaction = SimpleNamespace(
        id=transaction_id,
        book_id=fixture["book"].id,
        idempotency_key="precision-regression",
        request_fingerprint="d" * 64,
        transaction_type="spot_buy",
        status="posted",
        posted_at=datetime.now(timezone.utc),
    )
    fixture["records"][PaperLedgerTransaction].append(transaction)

    cash = fixture["accounts_by_code"]["cash_available"]
    capital = fixture["accounts_by_code"]["contributed_capital"]

    for amount in debit_amounts:
        fixture["records"][PaperLedgerEntry].append(
            SimpleNamespace(
                id=uuid.uuid4(),
                book_id=fixture["book"].id,
                transaction_id=transaction_id,
                ledger_account_id=cash.id,
                direction="debit",
                amount=amount,
            )
        )

    for amount in credit_amounts:
        fixture["records"][PaperLedgerEntry].append(
            SimpleNamespace(
                id=uuid.uuid4(),
                book_id=fixture["book"].id,
                transaction_id=transaction_id,
                ledger_account_id=capital.id,
                direction="credit",
                amount=amount,
            )
        )

    with localcontext() as context:
        context.prec = 120
        debit_total = sum(debit_amounts, Decimal("0"))
        credit_total = sum(credit_amounts, Decimal("0"))
        for snapshot in fixture["records"][PaperLedgerAccountSnapshot]:
            if snapshot.ledger_account_id == cash.id:
                snapshot.balance += debit_total
            elif snapshot.ledger_account_id == capital.id:
                snapshot.balance += credit_total


@pytest.mark.asyncio
async def test_high_precision_balanced_journal_reconciles_exactly():
    fixture = make_valid_ledger("spot")
    amounts = [Decimal("1e35"), Decimal("1e-36")]

    _append_precision_test_transaction(fixture, amounts, amounts)

    assert await validate_fixture(fixture, "spot") == fixture["book"].id


@pytest.mark.asyncio
async def test_high_precision_imbalance_is_rejected():
    fixture = make_valid_ledger("spot")

    _append_precision_test_transaction(
        fixture,
        [Decimal("1e35"), Decimal("1e-36")],
        [Decimal("1e35"), Decimal("2e-36")],
    )

    with pytest.raises(
        PaperLedgerValidationError,
        match="not exactly balanced",
    ):
        await validate_fixture(fixture, "spot")


@pytest.mark.asyncio
async def test_single_transaction_aggregate_above_limit_is_rejected():
    fixture = make_valid_ledger("spot")
    transaction_id = uuid.uuid4()
    amount = Decimal("6e35")

    transaction = SimpleNamespace(
        id=transaction_id,
        book_id=fixture["book"].id,
        idempotency_key="single-transaction-over-limit",
        request_fingerprint="e" * 64,
        transaction_type="spot_buy",
        status="posted",
        posted_at=datetime.now(timezone.utc),
    )
    fixture["records"][PaperLedgerTransaction].append(transaction)

    codes = (
        ("cash_available", "debit"),
        ("spot_position_cost", "debit"),
        ("contributed_capital", "credit"),
        ("realized_pnl", "credit"),
    )

    for code, direction in codes:
        account = fixture["accounts_by_code"][code]
        fixture["records"][PaperLedgerEntry].append(
            SimpleNamespace(
                id=uuid.uuid4(),
                book_id=fixture["book"].id,
                transaction_id=transaction_id,
                ledger_account_id=account.id,
                direction=direction,
                amount=amount,
            )
        )

    # Each account remains below 1e36. The single transaction total
    # is 1.2e36 on both sides and must therefore be rejected.
    with localcontext() as context:
        context.prec = 120
        for snapshot in fixture["records"][PaperLedgerAccountSnapshot]:
            matching_code = next(
                code
                for code, account in fixture["accounts_by_code"].items()
                if account.id == snapshot.ledger_account_id
            )
            if matching_code in {
                "cash_available",
                "spot_position_cost",
                "contributed_capital",
                "realized_pnl",
            }:
                snapshot.balance += amount

    with pytest.raises(
        PaperLedgerValidationError,
        match="invalid transaction aggregate total",
    ):
        await validate_fixture(fixture, "spot")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_amount",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1e36"),
        Decimal("1e-37"),
        1.0,
        True,
    ],
)
async def test_invalid_persisted_entry_decimal_is_rejected(invalid_amount):
    fixture = make_valid_ledger("spot")
    fixture["records"][PaperLedgerEntry][0].amount = invalid_amount

    with pytest.raises(
        PaperLedgerValidationError,
        match="invalid persisted entry amount",
    ):
        await validate_fixture(fixture, "spot")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_balance",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1e36"),
        Decimal("1e-37"),
        1.0,
        True,
    ],
)
async def test_invalid_persisted_snapshot_decimal_is_rejected(invalid_balance):
    fixture = make_valid_ledger("spot")
    fixture["records"][PaperLedgerAccountSnapshot][0].balance = invalid_balance

    with pytest.raises(
        PaperLedgerValidationError,
        match="invalid persisted snapshot balance",
    ):
        await validate_fixture(fixture, "spot")


@pytest.mark.asyncio
async def test_aggregate_account_total_above_limit_is_rejected():
    fixture = make_valid_ledger("spot")

    # Each transaction total is individually valid (< 1e36).
    # The accumulated cash and capital account totals exceed 1e36.
    amount = Decimal("6e35")
    _append_precision_test_transaction(fixture, [amount], [amount])
    _append_precision_test_transaction(fixture, [amount], [amount])

    with pytest.raises(
        PaperLedgerValidationError,
        match="invalid aggregated account total",
    ):
        await validate_fixture(fixture, "spot")
