import uuid

import pytest

from app.models.identity import (
    AccountMembership,
    TradingAccount,
    User,
)
from app.models.portfolio import PaperPortfolio, PaperSession
from app.services.account_service import (
    InvalidAccountNameError,
    InvalidFuturesPositionModeError,
    TransactionOwnershipError,
    UserNotEligibleError,
    UserNotFoundError,
    create_account_with_initial_portfolios,
)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        if isinstance(self.value, (list, tuple)):
            if len(self.value) == 0:
                return None
            if len(self.value) > 1:
                from sqlalchemy.exc import MultipleResultsFound
                raise MultipleResultsFound()
            return self.value[0]
        return self.value

    def scalars(self):
        return self

    def all(self):
        return list(self.value)


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.transaction_active = True
        self.session.begin_count += 1
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.session.transaction_active = False

        if exc_type is None:
            self.session.committed = True
        else:
            self.session.rolled_back = True

        return False


class FakeSession:
    def __init__(
        self, user=None, fail_on_flush=None, already_active=False, records=None
    ):
        self.user = user
        self.records = records or {}
        self.fail_on_flush = fail_on_flush
        self.transaction_active = already_active
        self.begin_count = 0
        self.flush_count = 0
        self.committed = False
        self.rolled_back = False
        self.added = []
        self.lock_requested = False

    def in_transaction(self):
        return self.transaction_active

    def begin(self):
        return FakeTransaction(self)

    async def execute(self, statement):
        from sqlalchemy.sql import operators

        entity = statement.column_descriptions[0]["entity"]

        if entity is User:
            self.lock_requested = statement._for_update_arg is not None
            return FakeResult(self.user)

        results = list(self.records.get(entity, []))
        whereclause = statement.whereclause

        def matches(instance, expression):
            operator = getattr(expression, "operator", None)

            if operator is operators.and_:
                return all(
                    matches(instance, clause)
                    for clause in expression.clauses
                )

            if operator in (operators.eq, operators.is_):
                field_name = getattr(expression.left, "key", None)
                if not field_name:
                    raise AssertionError(
                        f"Unsupported SQL filter column: {expression.left!r}"
                    )

                right = expression.right
                expected = getattr(right, "value", right)

                if right.__class__.__name__ == "Null":
                    expected = None

                return getattr(instance, field_name) == expected

            if operator is operators.in_op:
                field_name = getattr(expression.left, "key", None)
                if not field_name:
                    raise AssertionError(
                        f"Unsupported SQL IN column: {expression.left!r}"
                    )

                right = expression.right
                expected = getattr(right, "value", right)
                return getattr(instance, field_name) in expected

            raise AssertionError(
                f"FakeSession does not support SQL filter operator: {operator!r}"
            )

        if whereclause is not None:
            results = [
                item for item in results
                if matches(item, whereclause)
            ]

        return FakeResult(results)


    def add(self, instance):
        self.added.append(instance)

    def add_all(self, instances):
        self.added.extend(instances)

    async def flush(self):
        self.flush_count += 1

        if self.fail_on_flush == self.flush_count:
            raise RuntimeError("Simulated database flush failure.")

        for instance in self.added:
            if getattr(instance, "id", None) is None:
                instance.id = uuid.uuid4()


def active_verified_user():
    return User(
        id=uuid.uuid4(),
        email="owner@example.test",
        email_verified=True,
        status="active",
    )


@pytest.mark.asyncio
async def test_account_creation_builds_complete_atomic_structure():
    user = active_verified_user()
    session = FakeSession(user=user)

    result = await create_account_with_initial_portfolios(
        session,
        owner_user_id=user.id,
        account_name="  Primary Account  ",
        futures_position_mode="hedge",
    )

    accounts = [x for x in session.added if isinstance(x, TradingAccount)]
    memberships = [x for x in session.added if isinstance(x, AccountMembership)]
    portfolios = [x for x in session.added if isinstance(x, PaperPortfolio)]
    paper_sessions = [x for x in session.added if isinstance(x, PaperSession)]

    assert len(accounts) == 1
    assert len(memberships) == 1
    assert len(portfolios) == 2
    assert len(paper_sessions) == 2

    assert accounts[0].name == "Primary Account"
    assert accounts[0].owner_user_id == user.id
    assert accounts[0].status == "active"

    assert memberships[0].user_id == user.id
    assert memberships[0].role == "owner"
    assert memberships[0].status == "active"

    spot = next(x for x in portfolios if x.market_type == "spot")
    futures = next(x for x in portfolios if x.market_type == "futures")

    assert spot.position_mode is None
    assert futures.position_mode == "hedge"

    assert all(x.status == "active" for x in portfolios)
    assert all(x.session_number == 1 for x in paper_sessions)
    assert all(x.status == "active" for x in paper_sessions)
    assert all(x.account_id == accounts[0].id for x in paper_sessions)

    assert result.account_id == accounts[0].id
    assert result.spot_portfolio_id == spot.id
    assert result.futures_portfolio_id == futures.id
    assert result.futures_position_mode == "hedge"

    assert session.flush_count >= 3
    assert session.lock_requested is True
    assert session.committed is True
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_one_way_futures_mode_is_supported():
    user = active_verified_user()
    session = FakeSession(user=user)

    result = await create_account_with_initial_portfolios(
        session,
        owner_user_id=user.id,
        account_name="One Way Account",
        futures_position_mode="one_way",
    )

    futures = next(
        x for x in session.added
        if isinstance(x, PaperPortfolio) and x.market_type == "futures"
    )

    assert futures.position_mode == "one_way"
    assert result.futures_position_mode == "one_way"


@pytest.mark.asyncio
async def test_unverified_user_is_rejected_and_transaction_rolls_back():
    user = active_verified_user()
    user.email_verified = False
    session = FakeSession(user=user)

    with pytest.raises(UserNotEligibleError):
        await create_account_with_initial_portfolios(
            session,
            owner_user_id=user.id,
            account_name="Account",
            futures_position_mode="hedge",
        )

    assert session.rolled_back is True
    assert session.committed is False
    assert session.added == []


@pytest.mark.asyncio
async def test_inactive_user_is_rejected():
    user = active_verified_user()
    user.status = "suspended"
    session = FakeSession(user=user)

    with pytest.raises(UserNotEligibleError):
        await create_account_with_initial_portfolios(
            session,
            owner_user_id=user.id,
            account_name="Account",
            futures_position_mode="hedge",
        )

    assert session.rolled_back is True
    assert session.added == []


@pytest.mark.asyncio
async def test_missing_user_is_rejected():
    session = FakeSession(user=None)

    with pytest.raises(UserNotFoundError):
        await create_account_with_initial_portfolios(
            session,
            owner_user_id=uuid.uuid4(),
            account_name="Account",
            futures_position_mode="hedge",
        )

    assert session.rolled_back is True
    assert session.added == []


@pytest.mark.asyncio
async def test_invalid_position_mode_is_rejected_before_transaction():
    session = FakeSession(user=active_verified_user())

    with pytest.raises(InvalidFuturesPositionModeError):
        await create_account_with_initial_portfolios(
            session,
            owner_user_id=session.user.id,
            account_name="Account",
            futures_position_mode="cross",
        )

    assert session.begin_count == 0
    assert session.added == []


@pytest.mark.asyncio
async def test_invalid_account_names_are_rejected_before_transaction():
    session = FakeSession(user=active_verified_user())

    invalid_names = ("", "   ", "x" * 121, "bad\nname")

    for name in invalid_names:
        with pytest.raises(InvalidAccountNameError):
            await create_account_with_initial_portfolios(
                session,
                owner_user_id=session.user.id,
                account_name=name,
                futures_position_mode="hedge",
            )

    assert session.begin_count == 0
    assert session.added == []


@pytest.mark.asyncio
async def test_existing_transaction_is_not_nested_or_committed():
    user = active_verified_user()
    session = FakeSession(user=user, already_active=True)

    with pytest.raises(TransactionOwnershipError):
        await create_account_with_initial_portfolios(
            session,
            owner_user_id=user.id,
            account_name="Account",
            futures_position_mode="hedge",
        )

    assert session.begin_count == 0
    assert session.committed is False
    assert session.added == []


@pytest.mark.asyncio
async def test_flush_failure_rolls_back_entire_operation():
    user = active_verified_user()
    session = FakeSession(user=user, fail_on_flush=2)

    with pytest.raises(RuntimeError, match="Simulated database flush failure"):
        await create_account_with_initial_portfolios(
            session,
            owner_user_id=user.id,
            account_name="Account",
            futures_position_mode="hedge",
        )

    assert session.rolled_back is True
    assert session.committed is False
    assert session.flush_count == 2


def test_account_creation_result_is_immutable():
    from dataclasses import FrozenInstanceError

    from app.services.account_service import AccountCreationResult

    result = AccountCreationResult(
        account_id=uuid.uuid4(),
        spot_portfolio_id=uuid.uuid4(),
        futures_portfolio_id=uuid.uuid4(),
        spot_session_id=uuid.uuid4(),
        futures_session_id=uuid.uuid4(),
        futures_position_mode="hedge",
    )

    with pytest.raises(FrozenInstanceError):
        result.futures_position_mode = "one_way"

@pytest.mark.asyncio
async def test_default_account_provisioning_creates_hedge_account_once():
    from app.services.account_service import (
        ensure_default_account_with_initial_portfolios,
    )

    user = active_verified_user()
    session = FakeSession(user=user)

    result = await ensure_default_account_with_initial_portfolios(
        session,
        owner_user_id=user.id,
    )

    accounts = [x for x in session.added if isinstance(x, TradingAccount)]
    portfolios = [x for x in session.added if isinstance(x, PaperPortfolio)]
    sessions = [x for x in session.added if isinstance(x, PaperSession)]

    assert len(accounts) == 1
    assert len(portfolios) == 2
    assert len(sessions) == 2
    assert result.account_id == accounts[0].id
    assert result.futures_position_mode == "hedge"
    assert session.lock_requested is True
    assert session.committed is True


@pytest.mark.asyncio
async def test_default_account_provisioning_returns_existing_structure_unchanged():
    from app.services.account_service import (
        ensure_default_account_with_initial_portfolios,
    )

    user = active_verified_user()
    records = _item4_valid_existing_account_records(user)

    account = records[TradingAccount][0]
    spot, futures = records[PaperPortfolio]
    spot_session, futures_session = records[PaperSession]

    # Preserve this test's original one-way Futures scenario.
    futures.position_mode = "one_way"

    session = FakeSession(user=user, records=records)

    result = await ensure_default_account_with_initial_portfolios(
        session,
        owner_user_id=user.id,
    )

    assert result.account_id == account.id
    assert result.spot_portfolio_id == spot.id
    assert result.futures_portfolio_id == futures.id
    assert result.spot_session_id == spot_session.id
    assert result.futures_session_id == futures_session.id
    assert result.futures_position_mode == "one_way"
    assert session.added == []
    assert session.committed is True



@pytest.mark.asyncio
async def test_default_account_provisioning_fails_closed_for_multiple_accounts():
    from app.services.account_service import (
        AccountProvisioningStateError,
        ensure_default_account_with_initial_portfolios,
    )

    user = active_verified_user()
    accounts = [
        TradingAccount(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            name=f"Account {index}",
            status="active",
        )
        for index in range(2)
    ]
    session = FakeSession(user=user, records={TradingAccount: accounts})

    with pytest.raises(AccountProvisioningStateError):
        await ensure_default_account_with_initial_portfolios(
            session,
            owner_user_id=user.id,
        )

    assert session.added == []
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_default_account_provisioning_fails_closed_for_incomplete_account():
    from app.services.account_service import (
        AccountProvisioningStateError,
        ensure_default_account_with_initial_portfolios,
    )

    user = active_verified_user()
    account = TradingAccount(
        id=uuid.uuid4(),
        owner_user_id=user.id,
        name="Incomplete Account",
        status="active",
    )
    session = FakeSession(
        user=user,
        records={TradingAccount: [account]},
    )

    with pytest.raises(AccountProvisioningStateError):
        await ensure_default_account_with_initial_portfolios(
            session,
            owner_user_id=user.id,
        )

    assert session.added == []
    assert session.rolled_back is True
# 5C45E4_ITEM4_STRUCTURAL_EDGE_CASE_TESTS

def _item4_valid_existing_account_records(user):
    from decimal import Decimal
    from datetime import datetime, timezone
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

    account_id = uuid.uuid4()
    spot_id = uuid.uuid4()
    futures_id = uuid.uuid4()

    account = TradingAccount(
        id=account_id,
        owner_user_id=user.id,
        name="Existing Account",
        status="active",
    )

    membership = AccountMembership(
        id=uuid.uuid4(),
        account_id=account_id,
        user_id=user.id,
        role="owner",
        status="active",
    )

    spot = PaperPortfolio(
        id=spot_id,
        account_id=account_id,
        market_type="spot",
        position_mode=None,
        status="active",
    )

    futures = PaperPortfolio(
        id=futures_id,
        account_id=account_id,
        market_type="futures",
        position_mode="hedge",
        status="active",
    )

    spot_session = PaperSession(
        id=uuid.uuid4(),
        account_id=account_id,
        portfolio_id=spot_id,
        session_number=1,
        status="active",
    )

    futures_session = PaperSession(
        id=uuid.uuid4(),
        account_id=account_id,
        portfolio_id=futures_id,
        session_number=1,
        status="active",
    )

    records = {
        TradingAccount: [account],
        AccountMembership: [membership],
        PaperPortfolio: [spot, futures],
        PaperSession: [spot_session, futures_session],
    }

    now = datetime.now(timezone.utc)

    for portfolio, paper_session, market_type in (
        (spot, spot_session, "spot"),
        (futures, futures_session, "futures"),
    ):
        book_id = uuid.uuid4()

        book = PaperLedgerBook(
            id=book_id,
            account_id=account_id,
            portfolio_id=portfolio.id,
            session_id=paper_session.id,
            market_type=market_type,
            status="active",
        )

        accounts_by_code = {}
        snapshots = []
        for code, normal_side in MANDATORY_ACCOUNTS:
            ledger_account = PaperLedgerAccount(
                id=uuid.uuid4(),
                book_id=book_id,
                market_type=market_type,
                code=code,
                normal_side=normal_side,
            )
            accounts_by_code[code] = ledger_account

            balance = (
                INITIAL_BALANCE
                if code in {"cash_available", "contributed_capital"}
                else Decimal("0")
            )

            snapshots.append(
                PaperLedgerAccountSnapshot(
                    id=uuid.uuid4(),
                    book_id=book_id,
                    ledger_account_id=ledger_account.id,
                    balance=balance,
                )
            )

        transaction_id = uuid.uuid4()
        transaction = PaperLedgerTransaction(
            id=transaction_id,
            book_id=book_id,
            idempotency_key=INITIAL_FUNDING_KEY,
            request_fingerprint=_initial_funding_fingerprint(
                account_id=account_id,
                portfolio_id=portfolio.id,
                session_id=paper_session.id,
                market_type=market_type,
            ),
            transaction_type="initial_funding",
            status="posted",
            posted_at=now,
        )

        entries = [
            PaperLedgerEntry(
                id=uuid.uuid4(),
                book_id=book_id,
                transaction_id=transaction_id,
                ledger_account_id=accounts_by_code["cash_available"].id,
                direction="debit",
                amount=INITIAL_BALANCE,
            ),
            PaperLedgerEntry(
                id=uuid.uuid4(),
                book_id=book_id,
                transaction_id=transaction_id,
                ledger_account_id=accounts_by_code["contributed_capital"].id,
                direction="credit",
                amount=INITIAL_BALANCE,
            ),
        ]

        records.setdefault(PaperLedgerBook, []).append(book)
        records.setdefault(PaperLedgerAccount, []).extend(
            accounts_by_code.values()
        )
        records.setdefault(PaperLedgerAccountSnapshot, []).extend(snapshots)
        records.setdefault(PaperLedgerTransaction, []).append(transaction)
        records.setdefault(PaperLedgerEntry, []).extend(entries)

    return records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing_owner_membership",
        "wrong_active_owner",
        "multiple_active_owners",
        "invalid_account_status",
        "wrong_account_owner",
        "invalid_spot_position_mode",
        "invalid_futures_position_mode",
        "invalid_spot_status",
        "unexpected_session_portfolio",
        "nonpositive_session_number",
        "duplicate_session_number",
        "missing_active_session",
        "multiple_active_sessions",
        "unsupported_session_status",
    ],
)
async def test_default_account_provisioning_fails_closed_for_structural_edge_cases(case):
    from app.services.account_service import (
        AccountProvisioningStateError,
        ensure_default_account_with_initial_portfolios,
    )

    user = active_verified_user()
    records = _item4_valid_existing_account_records(user)

    account = records[TradingAccount][0]
    membership = records[AccountMembership][0]
    spot, futures = records[PaperPortfolio]
    spot_session, futures_session = records[PaperSession]

    if case == "missing_owner_membership":
        records[AccountMembership] = []

    elif case == "wrong_active_owner":
        membership.user_id = uuid.uuid4()

    elif case == "multiple_active_owners":
        records[AccountMembership].append(
            AccountMembership(
                id=uuid.uuid4(),
                account_id=account.id,
                user_id=uuid.uuid4(),
                role="owner",
                status="active",
            )
        )

    elif case == "invalid_account_status":
        account.status = "suspended"

    elif case == "wrong_account_owner":
        account.owner_user_id = uuid.uuid4()

    elif case == "invalid_spot_position_mode":
        spot.position_mode = "one_way"

    elif case == "invalid_futures_position_mode":
        futures.position_mode = "invalid"

    elif case == "invalid_spot_status":
        spot.status = "deleted"

    elif case == "unexpected_session_portfolio":
        spot_session.portfolio_id = uuid.uuid4()

    elif case == "nonpositive_session_number":
        spot_session.session_number = 0

    elif case == "duplicate_session_number":
        records[PaperSession].append(
            PaperSession(
                id=uuid.uuid4(),
                account_id=account.id,
                portfolio_id=spot.id,
                session_number=1,
                status="archived",
                archived_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
            )
        )

    elif case == "missing_active_session":
        spot_session.status = "archived"
        spot_session.archived_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )

    elif case == "multiple_active_sessions":
        records[PaperSession].append(
            PaperSession(
                id=uuid.uuid4(),
                account_id=account.id,
                portfolio_id=spot.id,
                session_number=2,
                status="active",
            )
        )

    elif case == "unsupported_session_status":
        spot_session.status = "pending"

    session = FakeSession(user=user, records=records)

    with pytest.raises(AccountProvisioningStateError):
        await ensure_default_account_with_initial_portfolios(
            session,
            owner_user_id=user.id,
        )

    assert session.added == []
    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_default_account_provisioning_preserves_valid_archived_session_history():
    from datetime import datetime, timezone

    from app.services.account_service import (
        ensure_default_account_with_initial_portfolios,
    )

    user = active_verified_user()
    records = _item4_valid_existing_account_records(user)

    account = records[TradingAccount][0]
    spot, futures = records[PaperPortfolio]
    spot_session, futures_session = records[PaperSession]

    archived_at = datetime.now(timezone.utc)

    spot_session.session_number = 2
    futures_session.session_number = 2

    records[PaperSession].extend(
        [
            PaperSession(
                id=uuid.uuid4(),
                account_id=account.id,
                portfolio_id=spot.id,
                session_number=1,
                status="archived",
                archived_at=archived_at,
            ),
            PaperSession(
                id=uuid.uuid4(),
                account_id=account.id,
                portfolio_id=futures.id,
                session_number=1,
                status="archived",
                archived_at=archived_at,
            ),
        ]
    )

    session = FakeSession(user=user, records=records)

    result = await ensure_default_account_with_initial_portfolios(
        session,
        owner_user_id=user.id,
    )

    assert result.account_id == account.id
    assert result.spot_session_id == spot_session.id
    assert result.futures_session_id == futures_session.id
    assert session.added == []
    assert session.committed is True
    assert session.rolled_back is False
