from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.paper_ledger import (
    PaperLedgerAccount,
    PaperLedgerBook,
    PaperLedgerEntry,
    PaperLedgerTransaction,
)
from app.services.paper_ledger_provisioning import (
    INITIAL_BALANCE,
    MANDATORY_ACCOUNTS,
    PaperLedgerProvisioningError,
    provision_initial_paper_ledger,
)


class FakeResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, *, transaction_active, existing_book=None):
        self.transaction_active = transaction_active
        self.existing_book = existing_book
        self.execute_count = 0
        self.added = []
        self.flush_count = 0

    def in_transaction(self):
        return self.transaction_active

    async def execute(self, statement):
        self.execute_count += 1
        return FakeResult(self.existing_book)

    def add(self, instance):
        self.added.append(instance)

    def add_all(self, instances):
        self.added.extend(instances)

    async def flush(self):
        self.flush_count += 1

        for instance in self.added:
            if getattr(instance, "id", None) is None:
                instance.id = uuid.uuid4()


@pytest.mark.asyncio
async def test_provisioning_rejects_missing_caller_transaction_before_database_work():
    session = FakeSession(transaction_active=False)

    with pytest.raises(
        PaperLedgerProvisioningError,
        match="active caller-owned transaction",
    ):
        await provision_initial_paper_ledger(
            session,
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            market_type="spot",
        )

    assert session.execute_count == 0
    assert session.added == []
    assert session.flush_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["spot", "futures"])
async def test_provisioning_creates_six_accounts_and_balanced_initial_funding(
    market_type,
):
    session = FakeSession(transaction_active=True)

    account_id = uuid.uuid4()
    portfolio_id = uuid.uuid4()
    session_id = uuid.uuid4()

    book_id = await provision_initial_paper_ledger(
        session,
        account_id=account_id,
        portfolio_id=portfolio_id,
        session_id=session_id,
        market_type=market_type,
    )

    books = [
        item for item in session.added
        if isinstance(item, PaperLedgerBook)
    ]
    accounts = [
        item for item in session.added
        if isinstance(item, PaperLedgerAccount)
    ]
    transactions = [
        item for item in session.added
        if isinstance(item, PaperLedgerTransaction)
    ]
    entries = [
        item for item in session.added
        if isinstance(item, PaperLedgerEntry)
    ]

    assert len(books) == 1
    assert books[0].id == book_id
    assert books[0].account_id == account_id
    assert books[0].portfolio_id == portfolio_id
    assert books[0].session_id == session_id
    assert books[0].market_type == market_type
    assert books[0].status == "active"

    assert len(accounts) == 6
    assert {
        (item.code, item.normal_side)
        for item in accounts
    } == set(MANDATORY_ACCOUNTS)

    assert all(item.book_id == book_id for item in accounts)
    assert all(item.market_type == market_type for item in accounts)

    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.transaction_type == "initial_funding"
    assert transaction.status == "posted"
    assert transaction.posted_at is None

    assert len(entries) == 2
    assert {
        (item.direction, item.amount)
        for item in entries
    } == {
        ("debit", INITIAL_BALANCE),
        ("credit", INITIAL_BALANCE),
    }

    assert all(item.book_id == book_id for item in entries)
    assert all(item.transaction_id == transaction.id for item in entries)
    assert sum(
        (item.amount for item in entries if item.direction == "debit"),
        Decimal("0"),
    ) == sum(
        (item.amount for item in entries if item.direction == "credit"),
        Decimal("0"),
    )


@pytest.mark.asyncio
async def test_provisioning_rejects_existing_book_without_creating_records():
    existing_book = SimpleNamespace(id=uuid.uuid4())
    session = FakeSession(
        transaction_active=True,
        existing_book=existing_book,
    )

    with pytest.raises(
        PaperLedgerProvisioningError,
        match="already exists",
    ):
        await provision_initial_paper_ledger(
            session,
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            market_type="futures",
        )

    assert session.execute_count == 1
    assert session.added == []
    assert session.flush_count == 0