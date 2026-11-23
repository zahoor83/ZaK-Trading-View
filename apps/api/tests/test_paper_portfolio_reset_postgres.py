from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.models.identity import User
from app.models.paper_ledger import (
    PaperExecutionRequest,
    PaperLedgerAccount,
    PaperLedgerAccountSnapshot,
    PaperLedgerBook,
    PaperLedgerEntry,
    PaperLedgerTransaction,
    PaperPosition,
)
from app.models.portfolio import PaperSession
from app.services.account_service import (
    ensure_default_account_with_initial_portfolios,
)
from app.services import paper_portfolio_reset as portfolio_reset


def _validated_test_url() -> str:
    raw = os.environ.get("DATABASE_URL", "")

    if not raw:
        pytest.fail("DATABASE_URL is not configured.")

    parsed = make_url(raw)

    expected = {
        "drivername": "postgresql+asyncpg",
        "host": "127.0.0.1",
        "port": 55432,
        "database": "zak_oidc_test",
        "username": "zak_oidc_test_app",
    }

    actual = {
        "drivername": parsed.drivername,
        "host": parsed.host,
        "port": parsed.port,
        "database": parsed.database,
        "username": parsed.username,
    }

    if actual != expected:
        pytest.fail(
            "Refusing database connection: target is not the approved "
            "isolated PostgreSQL test database."
        )

    return raw


@pytest.fixture
async def reset_pg_sessions():
    engine = create_async_engine(
        _validated_test_url(),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(
                    text(
                        "SELECT current_user, current_database(), "
                        "current_setting('transaction_isolation')"
                    )
                )
            ).one()

            assert identity[0] == "zak_oidc_test_app"
            assert identity[1] == "zak_oidc_test"
            assert identity[2] == "read committed"

        yield factory

    finally:
        await engine.dispose()


async def _create_owner_account(factory):
    unique = uuid.uuid4().hex
    email = f"paper-reset-{unique}@example.test"

    async with factory() as session:
        user = User(
            email=email,
            email_normalized=email,
            email_verified=True,
            status="active",
        )
        session.add(user)
        await session.flush()
        user_id = user.id
        await session.commit()

    async with factory() as session:
        provisioned = await ensure_default_account_with_initial_portfolios(
            session,
            owner_user_id=user_id,
        )

    return user_id, provisioned


def _portfolio_details(provisioned, market_type):
    if market_type == "spot":
        return (
            provisioned.spot_portfolio_id,
            provisioned.spot_session_id,
        )

    if market_type == "futures":
        return (
            provisioned.futures_portfolio_id,
            provisioned.futures_session_id,
        )

    raise AssertionError(f"Unsupported market type: {market_type}")


async def _get_book_id(session, account_id, portfolio_id, session_id):
    book = await session.scalar(
        select(PaperLedgerBook).where(
            PaperLedgerBook.account_id == account_id,
            PaperLedgerBook.portfolio_id == portfolio_id,
            PaperLedgerBook.session_id == session_id,
        )
    )

    assert book is not None
    return book.id


async def _count_for_portfolio(session, model, portfolio_id):
    statement = select(func.count()).select_from(model)

    if model is PaperSession:
        statement = statement.where(
            PaperSession.portfolio_id == portfolio_id
        )
    elif model is PaperLedgerBook:
        statement = statement.where(
            PaperLedgerBook.portfolio_id == portfolio_id
        )
    elif model is PaperExecutionRequest:
        statement = statement.where(
            PaperExecutionRequest.portfolio_id == portfolio_id,
            PaperExecutionRequest.operation_type == "reset",
        )
    else:
        raise AssertionError(f"Unsupported model: {model}")

    return await session.scalar(statement)


async def _assert_successful_reset_state(
    factory,
    *,
    account_id,
    portfolio_id,
    old_session_id,
    old_book_id,
    response_data,
):
    new_session_id = uuid.UUID(response_data["new_session_id"])
    new_book_id = uuid.UUID(response_data["new_book_id"])

    async with factory() as session:
        old_session = await session.get(PaperSession, old_session_id)
        new_session = await session.get(PaperSession, new_session_id)

        old_book = await session.get(PaperLedgerBook, old_book_id)
        new_book = await session.get(PaperLedgerBook, new_book_id)

        assert old_session is not None
        assert new_session is not None
        assert old_book is not None
        assert new_book is not None

        assert old_session.status == "archived"
        assert old_book.status == "archived"

        assert old_session.archived_at is not None
        assert old_book.archived_at is not None
        assert old_session.archived_at == old_book.archived_at

        assert new_session.status == "active"
        assert new_session.archived_at is None
        assert new_session.session_number == old_session.session_number + 1

        assert new_book.status == "active"
        assert new_book.archived_at is None
        assert new_book.session_id == new_session.id

        assert new_book.account_id == account_id
        assert new_book.portfolio_id == portfolio_id
        assert new_book.market_type == old_book.market_type

        account_snapshot_rows = (
            await session.execute(
                select(
                    PaperLedgerAccount.code,
                    PaperLedgerAccountSnapshot.balance,
                )
                .join(
                    PaperLedgerAccountSnapshot,
                    PaperLedgerAccountSnapshot.ledger_account_id
                    == PaperLedgerAccount.id,
                )
                .where(
                    PaperLedgerAccount.book_id == new_book_id,
                    PaperLedgerAccountSnapshot.book_id == new_book_id,
                )
            )
        ).all()

        balances = {
            code: balance for code, balance in account_snapshot_rows
        }

        assert len(balances) == 6
        assert balances["cash_available"] == Decimal("1000")
        assert balances["contributed_capital"] == Decimal("1000")

        funding_transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == new_book_id,
                        PaperLedgerTransaction.transaction_type
                        == "initial_funding",
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )

        assert len(funding_transactions) == 1

        funding_entries = list(
            (
                await session.scalars(
                    select(PaperLedgerEntry).where(
                        PaperLedgerEntry.book_id == new_book_id,
                        PaperLedgerEntry.transaction_id
                        == funding_transactions[0].id,
                    )
                )
            ).all()
        )

        assert len(funding_entries) == 2

        reset_requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.scope_type == "portfolio",
                        PaperExecutionRequest.account_id == account_id,
                        PaperExecutionRequest.portfolio_id == portfolio_id,
                        PaperExecutionRequest.operation_type == "reset",
                    )
                )
            ).all()
        )

        assert len(reset_requests) == 1
        assert reset_requests[0].book_id is None
        assert reset_requests[0].status == "completed"
        assert reset_requests[0].response_data == response_data


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["spot", "futures"])
async def test_postgres_portfolio_reset_archives_and_reprovisions(
    reset_pg_sessions,
    market_type,
):
    user_id, provisioned = await _create_owner_account(reset_pg_sessions)

    portfolio_id, old_session_id = _portfolio_details(
        provisioned,
        market_type,
    )

    async with reset_pg_sessions() as session:
        old_book_id = await _get_book_id(
            session,
            provisioned.account_id,
            portfolio_id,
            old_session_id,
        )

    idempotency_key = str(uuid.uuid4())

    async with reset_pg_sessions() as session:
        result = await portfolio_reset.reset_paper_portfolio(
            session,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=portfolio_id,
            idempotency_key=idempotency_key,
            confirmed=True,
        )

    assert result.replayed is False

    response = result.response_data

    assert response["operation_type"] == "reset"
    assert response["status"] == "completed"
    assert response["market_type"] == market_type
    assert response["previous_session_id"] == str(old_session_id)
    assert response["previous_book_id"] == str(old_book_id)
    assert response["new_session_number"] == 2
    assert response["initial_balance"] == "1000.00"

    await _assert_successful_reset_state(
        reset_pg_sessions,
        account_id=provisioned.account_id,
        portfolio_id=portfolio_id,
        old_session_id=old_session_id,
        old_book_id=old_book_id,
        response_data=response,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["spot", "futures"])
async def test_postgres_portfolio_reset_idempotent_replay(
    reset_pg_sessions,
    market_type,
):
    user_id, provisioned = await _create_owner_account(reset_pg_sessions)

    portfolio_id, old_session_id = _portfolio_details(
        provisioned,
        market_type,
    )

    idempotency_key = str(uuid.uuid4())

    async with reset_pg_sessions() as session:
        first = await portfolio_reset.reset_paper_portfolio(
            session,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=portfolio_id,
            idempotency_key=idempotency_key,
            confirmed=True,
        )

    assert first.replayed is False

    async with reset_pg_sessions() as session:
        replay = await portfolio_reset.reset_paper_portfolio(
            session,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=portfolio_id,
            idempotency_key=idempotency_key,
            confirmed=True,
        )

    assert replay.replayed is True
    assert replay.response_data == first.response_data

    async with reset_pg_sessions() as session:
        assert await _count_for_portfolio(
            session, PaperSession, portfolio_id
        ) == 2

        assert await _count_for_portfolio(
            session, PaperLedgerBook, portfolio_id
        ) == 2

        assert await _count_for_portfolio(
            session, PaperExecutionRequest, portfolio_id
        ) == 1

        old_session = await session.get(PaperSession, old_session_id)
        assert old_session is not None
        assert old_session.status == "archived"


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["spot", "futures"])
async def test_postgres_portfolio_reset_rejects_open_positions(
    reset_pg_sessions,
    market_type,
):
    user_id, provisioned = await _create_owner_account(reset_pg_sessions)

    portfolio_id, old_session_id = _portfolio_details(
        provisioned,
        market_type,
    )

    async with reset_pg_sessions() as session:
        old_book_id = await _get_book_id(
            session,
            provisioned.account_id,
            portfolio_id,
            old_session_id,
        )

    position_values = {
        "book_id": old_book_id,
        "market_type": market_type,
        "symbol": "BTCUSDT",
        "side": "long",
        "initial_quantity": Decimal("1"),
        "remaining_quantity": Decimal("1"),
        "entry_price": Decimal("100"),
        "remaining_cost_basis": (
            Decimal("100") if market_type == "spot" else None
        ),
        "margin_locked": (
            Decimal("50") if market_type == "futures" else None
        ),
        "leverage_snapshot": (
            Decimal("2") if market_type == "futures" else None
        ),
        "entry_fee_original": Decimal("0"),
        "entry_fee_posted": Decimal("0.00"),
        "entry_fee_rate": Decimal("0"),
        "exit_fee_rate": Decimal("0"),
        "fee_config_snapshot": {"schema_version": 1},
        "symbol_metadata_snapshot": {"symbol": "BTCUSDT"},
    }

    async with reset_pg_sessions() as session:
        async with session.begin():
            session.add(PaperPosition(**position_values))

    with pytest.raises(
        portfolio_reset.PaperPortfolioResetError,
        match="open positions",
    ):
        async with reset_pg_sessions() as session:
            await portfolio_reset.reset_paper_portfolio(
                session,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=portfolio_id,
                idempotency_key=str(uuid.uuid4()),
                confirmed=True,
            )

    async with reset_pg_sessions() as session:
        old_session = await session.get(PaperSession, old_session_id)
        old_book = await session.get(PaperLedgerBook, old_book_id)

        assert old_session is not None
        assert old_book is not None

        assert old_session.status == "active"
        assert old_session.archived_at is None
        assert old_book.status == "active"
        assert old_book.archived_at is None

        assert await _count_for_portfolio(
            session, PaperSession, portfolio_id
        ) == 1

        assert await _count_for_portfolio(
            session, PaperLedgerBook, portfolio_id
        ) == 1

        assert await _count_for_portfolio(
            session, PaperExecutionRequest, portfolio_id
        ) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["spot", "futures"])
async def test_postgres_portfolio_reset_rolls_back_after_new_ledger_provisioning(
    reset_pg_sessions,
    monkeypatch,
    market_type,
):
    user_id, provisioned = await _create_owner_account(reset_pg_sessions)

    portfolio_id, old_session_id = _portfolio_details(
        provisioned,
        market_type,
    )

    async with reset_pg_sessions() as session:
        old_book_id = await _get_book_id(
            session,
            provisioned.account_id,
            portfolio_id,
            old_session_id,
        )

        before_sessions = await _count_for_portfolio(
            session, PaperSession, portfolio_id
        )
        before_books = await _count_for_portfolio(
            session, PaperLedgerBook, portfolio_id
        )

    original_dumps = portfolio_reset.json.dumps

    def fail_reset_response_serialization(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("operation_type") == "reset":
            raise ValueError("Injected reset response serialization failure")

        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(
        portfolio_reset.json,
        "dumps",
        fail_reset_response_serialization,
    )

    with pytest.raises(portfolio_reset.PaperPortfolioResetError):
        async with reset_pg_sessions() as session:
            await portfolio_reset.reset_paper_portfolio(
                session,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=portfolio_id,
                idempotency_key=str(uuid.uuid4()),
                confirmed=True,
            )

    # Verify committed state from a fresh PostgreSQL session after rollback.
    async with reset_pg_sessions() as session:
        old_session = await session.get(PaperSession, old_session_id)
        old_book = await session.get(PaperLedgerBook, old_book_id)

        assert old_session is not None
        assert old_book is not None

        assert old_session.status == "active"
        assert old_session.archived_at is None

        assert old_book.status == "active"
        assert old_book.archived_at is None

        assert await _count_for_portfolio(
            session, PaperSession, portfolio_id
        ) == before_sessions

        assert await _count_for_portfolio(
            session, PaperLedgerBook, portfolio_id
        ) == before_books

        assert await _count_for_portfolio(
            session, PaperExecutionRequest, portfolio_id
        ) == 0

        old_book_accounts = await session.scalar(
            select(func.count()).select_from(PaperLedgerAccount).where(
                PaperLedgerAccount.book_id == old_book_id
            )
        )
        assert old_book_accounts == 6
