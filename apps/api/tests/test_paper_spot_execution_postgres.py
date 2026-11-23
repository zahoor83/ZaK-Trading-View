from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401 - ensure all mapped tables are registered
from app.market_data.config import MarketDataConfig
from app.market_data.service import (
    CachedSpotBookTicker,
    MarketDataService,
    MarketResources,
)
from app.market_data.service_models import PipelineState
from app.market_data.symbol_metadata import parse_spot_exchange_info
from app.models.identity import User
from app.models.paper_fee_policy import PaperFeePolicyHead, PaperFeePolicyVersion
from app.models.paper_ledger import (
    PaperExecutionRequest,
    PaperLedgerBook,
    PaperLedgerAccount,
    PaperLedgerAccountSnapshot,
    PaperLedgerEntry,
    PaperLedgerTransaction,
    PaperPosition,
)
from app.services.account_service import ensure_default_account_with_initial_portfolios
from app.services import paper_trade_execution as spot_execution
from app.services.paper_fee_policy_persistence import set_paper_fee_policy


ZERO = Decimal("0")
BUY_PRICE = Decimal("100.01")


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
async def spot_pg_sessions():
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


def _make_spot_market_data(symbol: str = "BTCUSDT") -> MarketDataService:
    service = MarketDataService(
        MarketDataConfig(spot_book_ticker_symbols=(symbol,)),
        monotonic=lambda: 100.0,
    )

    metadata = parse_spot_exchange_info(
        {
            "serverTime": 1_700_000_000_000,
            "symbols": [
                {
                    "symbol": symbol,
                    "status": "TRADING",
                    "baseAsset": symbol[:-4],
                    "quoteAsset": "USDT",
                    "orderTypes": ["LIMIT", "MARKET"],
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "minPrice": "0.01",
                            "maxPrice": "1000000",
                            "tickSize": "0.01",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.00001",
                            "maxQty": "100000",
                            "stepSize": "0.00001",
                        },
                    ],
                }
            ],
        },
        fetched_at_monotonic=95.0,
    )

    service._resources["spot"] = MarketResources(
        market="spot",
        clock=None,
        metadata=metadata,
        freshness_validator=None,
        server_time_provider=None,
        metadata_refresh_provider=lambda: None,
    )

    service._pipeline_states["spot_book_ticker"] = PipelineState.SUBSCRIBING

    expected_stream = f"{symbol.lower()}@bookTicker"
    service._supervisors["spot_book_ticker"] = SimpleNamespace(
        state=SimpleNamespace(value="SUBSCRIPTIONS_READY"),
        active_streams=(expected_stream,),
    )

    payload = {
        "u": 123456,
        "s": symbol,
        "b": "100.00",
        "B": "2.0",
        "a": "100.01",
        "A": "3.0",
    }
    service._spot_book_ticker_quotes[
        ("spot_book_ticker", symbol)
    ] = CachedSpotBookTicker(
        symbol=symbol,
        update_id=payload["u"],
        received_at_monotonic=99.5,
        payload=payload,
    )

    return service


async def _create_owner_account(factory):
    unique = uuid.uuid4().hex
    email = f"spot-execution-{unique}@example.test"

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

    async with factory() as session:
        async with session.begin():
            await set_paper_fee_policy(
                session,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.spot_portfolio_id,
                entry_fee_rate=Decimal("0.001"),
                exit_fee_rate=Decimal("0.001"),
            )

    return user_id, provisioned


async def _snapshot_balances(session, book_id):
    accounts = list(
        (
            await session.scalars(
                select(PaperLedgerAccount).where(
                    PaperLedgerAccount.book_id == book_id
                )
            )
        ).all()
    )
    snapshots = list(
        (
            await session.scalars(
                select(PaperLedgerAccountSnapshot).where(
                    PaperLedgerAccountSnapshot.book_id == book_id
                )
            )
        ).all()
    )
    entries = list(
        (
            await session.scalars(
                select(PaperLedgerEntry).where(
                    PaperLedgerEntry.book_id == book_id
                )
            )
        ).all()
    )

    assert accounts
    assert len(accounts) == len(snapshots)

    account_by_id = {item.id: item for item in accounts}
    snapshot_by_account = {
        item.ledger_account_id: item for item in snapshots
    }

    independently_reconciled = {item.id: ZERO for item in accounts}

    for entry in entries:
        account = account_by_id[entry.ledger_account_id]
        positive_direction = (
            "debit" if account.normal_side == "debit" else "credit"
        )
        signed_amount = (
            entry.amount
            if entry.direction == positive_direction
            else -entry.amount
        )
        independently_reconciled[account.id] += signed_amount

    result = {}
    for account in accounts:
        snapshot = snapshot_by_account[account.id]
        expected = independently_reconciled[account.id]
        assert snapshot.balance == expected, (
            f"Snapshot mismatch for {account.code}: "
            f"stored={snapshot.balance}, independently_reconciled={expected}"
        )
        result[account.code] = snapshot.balance

    return result


def _buy_intent():
    return {
        "symbol": "BTCUSDT",
        "side": "buy",
        "position_side": None,
        "quantity": Decimal("0.1"),
        "leverage": None,
        "position_id": None,
        "reduce_only": False,
        "order_type": "market",
        "limit_price": None,
    }


@pytest.mark.asyncio
async def test_postgres_spot_buy_persists_position_journal_and_reconciled_ledger(
    spot_pg_sessions,
):
    user_id, provisioned = await _create_owner_account(spot_pg_sessions)
    market_data = _make_spot_market_data()
    idempotency_key = str(uuid.uuid4())

    async with spot_pg_sessions() as session:
        result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=idempotency_key,
            trade_intent=_buy_intent(),
        )

    assert result.replayed is False
    response = result.response_data

    assert response["market_type"] == "spot"
    assert response["status"] == "posted"
    assert response["transaction_type"] == "spot_buy"
    assert response["symbol"] == "BTCUSDT"
    assert response["side"] == "buy"
    assert Decimal(response["quantity"]) == Decimal("0.1")
    assert Decimal(response["execution_price"]) == BUY_PRICE
    assert response["quote_source"] == "spot_book_ticker"
    assert response["quote_update_id"] == 123456

    position_id = uuid.UUID(response["position_id"])

    async with spot_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)

        assert position is not None
        assert position.market_type == "spot"
        assert position.symbol == "BTCUSDT"
        assert position.side == "long"
        assert position.status == "open"
        assert position.initial_quantity == Decimal("0.1")
        assert position.remaining_quantity == Decimal("0.1")
        assert position.entry_price == BUY_PRICE
        assert position.remaining_cost_basis == Decimal("10.001")
        assert position.entry_fee_original == Decimal("0.010001")
        assert position.entry_fee_posted == Decimal("0.01")
        assert position.fee_config_snapshot["market_type"] == "spot"
        assert position.symbol_metadata_snapshot["market"] == "spot"

        book_id = position.book_id
        balances = await _snapshot_balances(session, book_id)

        transaction = await session.scalar(
            select(PaperLedgerTransaction).where(
                PaperLedgerTransaction.book_id == book_id,
                PaperLedgerTransaction.id == uuid.UUID(response["transaction_id"]),
                PaperLedgerTransaction.transaction_type == "spot_buy",
                PaperLedgerTransaction.status == "posted",
            )
        )
        assert transaction is not None

        requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )
        assert len(requests) == 1

        policy_versions = list(
            (
                await session.scalars(
                    select(PaperFeePolicyVersion).where(
                        PaperFeePolicyVersion.account_id == provisioned.account_id,
                        PaperFeePolicyVersion.market_type == "spot",
                    )
                )
            ).all()
        )
        policy_heads = list(
            (
                await session.scalars(
                    select(PaperFeePolicyHead).where(
                        PaperFeePolicyHead.account_id == provisioned.account_id,
                        PaperFeePolicyHead.market_type == "spot",
                    )
                )
            ).all()
        )
        assert len(policy_versions) == 1
        assert len(policy_heads) == 1

        assert balances
        assert all(value.is_finite() for value in balances.values())

@pytest.mark.asyncio
async def test_postgres_spot_sell_persists_position_journal_and_reconciled_ledger(
    spot_pg_sessions,
):
    user_id, provisioned = await _create_owner_account(spot_pg_sessions)
    market_data = _make_spot_market_data()

    async with spot_pg_sessions() as session:
        buy_result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_buy_intent(),
        )

    assert buy_result.replayed is False
    buy_response = buy_result.response_data
    position_id = uuid.UUID(buy_response["position_id"])

    sell_intent = {
        "symbol": "BTCUSDT",
        "side": "sell",
        "position_side": None,
        "quantity": Decimal("0.05"),
        "leverage": None,
        "position_id": str(position_id),
        "reduce_only": False,
        "order_type": "market",
        "limit_price": None,
    }

    async with spot_pg_sessions() as session:
        sell_result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=sell_intent,
        )

    assert sell_result.replayed is False
    response = sell_result.response_data

    assert response["market_type"] == "spot"
    assert response["status"] == "posted"
    assert response["transaction_type"] == "spot_sell"
    assert response["position_id"] == str(position_id)
    assert response["symbol"] == "BTCUSDT"
    assert response["side"] == "sell"
    assert Decimal(response["quantity"]) == Decimal("0.05")
    assert Decimal(response["execution_price"]) == Decimal("100.00")
    assert Decimal(response["gross_value"]) == Decimal("5.0000")
    assert Decimal(response["fee_original"]) == Decimal("0.005")
    assert Decimal(response["fee_posted"]) == Decimal("0.01")
    assert Decimal(response["realized_pnl"]) == Decimal("-0.0005")
    assert Decimal(response["remaining_quantity"]) == Decimal("0.05")
    assert Decimal(response["remaining_cost_basis"]) == Decimal("5.0005")
    assert response["position_status"] == "open"
    assert response["quote_source"] == "spot_book_ticker"
    assert response["quote_update_id"] == 123456

    async with spot_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)

        assert position is not None
        assert position.market_type == "spot"
        assert position.symbol == "BTCUSDT"
        assert position.side == "long"
        assert position.status == "open"
        assert position.initial_quantity == Decimal("0.1")
        assert position.remaining_quantity == Decimal("0.05")
        assert position.entry_price == BUY_PRICE
        assert position.remaining_cost_basis == Decimal("5.0005")
        assert position.realized_pnl == Decimal("-0.0005")

        book_id = position.book_id
        balances = await _snapshot_balances(session, book_id)

        transaction = await session.scalar(
            select(PaperLedgerTransaction).where(
                PaperLedgerTransaction.book_id == book_id,
                PaperLedgerTransaction.id == uuid.UUID(response["transaction_id"]),
                PaperLedgerTransaction.transaction_type == "spot_sell",
                PaperLedgerTransaction.status == "posted",
            )
        )
        assert transaction is not None

        requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )
        assert len(requests) == 2

        assert balances
        assert all(value.is_finite() for value in balances.values())


@pytest.mark.asyncio
async def test_postgres_spot_full_sell_closes_position_and_reconciles_ledger(
    spot_pg_sessions,
):
    user_id, provisioned = await _create_owner_account(spot_pg_sessions)
    market_data = _make_spot_market_data()

    async with spot_pg_sessions() as session:
        buy_result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_buy_intent(),
        )

    assert buy_result.replayed is False
    position_id = uuid.UUID(buy_result.response_data["position_id"])

    sell_intent = {
        "symbol": "BTCUSDT",
        "side": "sell",
        "position_side": None,
        "quantity": Decimal("0.1"),
        "leverage": None,
        "position_id": str(position_id),
        "reduce_only": False,
        "order_type": "market",
        "limit_price": None,
    }

    async with spot_pg_sessions() as session:
        sell_result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=sell_intent,
        )

    assert sell_result.replayed is False
    response = sell_result.response_data

    assert response["market_type"] == "spot"
    assert response["status"] == "posted"
    assert response["transaction_type"] == "spot_sell"
    assert response["position_id"] == str(position_id)
    assert response["side"] == "sell"
    assert Decimal(response["quantity"]) == Decimal("0.1")
    assert Decimal(response["execution_price"]) == Decimal("100.00")
    assert Decimal(response["gross_value"]) == Decimal("10.000")
    assert Decimal(response["fee_original"]) == Decimal("0.010")
    assert Decimal(response["fee_posted"]) == Decimal("0.01")
    assert Decimal(response["realized_pnl"]) == Decimal("-0.001")
    assert Decimal(response["remaining_quantity"]) == Decimal("0")
    assert Decimal(response["remaining_cost_basis"]) == Decimal("0")
    assert response["position_status"] == "closed"

    async with spot_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)

        assert position is not None
        assert position.market_type == "spot"
        assert position.symbol == "BTCUSDT"
        assert position.side == "long"
        assert position.status == "closed"
        assert position.initial_quantity == Decimal("0.1")
        assert position.remaining_quantity == Decimal("0")
        assert position.remaining_cost_basis == Decimal("0")
        assert position.realized_pnl == Decimal("-0.001")
        assert position.closed_at is not None

        book_id = position.book_id
        balances = await _snapshot_balances(session, book_id)

        transaction = await session.scalar(
            select(PaperLedgerTransaction).where(
                PaperLedgerTransaction.book_id == book_id,
                PaperLedgerTransaction.id == uuid.UUID(response["transaction_id"]),
                PaperLedgerTransaction.transaction_type == "spot_sell",
                PaperLedgerTransaction.status == "posted",
            )
        )
        assert transaction is not None

        requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )
        assert len(requests) == 2

        assert balances
        assert all(value.is_finite() for value in balances.values())


@pytest.mark.asyncio
async def test_postgres_spot_sell_idempotency_replay_does_not_duplicate_posting(
    spot_pg_sessions,
):
    user_id, provisioned = await _create_owner_account(spot_pg_sessions)
    market_data = _make_spot_market_data()

    async with spot_pg_sessions() as session:
        buy_result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_buy_intent(),
        )

    assert buy_result.replayed is False
    position_id = uuid.UUID(buy_result.response_data["position_id"])

    sell_intent = {
        "symbol": "BTCUSDT",
        "side": "sell",
        "position_side": None,
        "quantity": Decimal("0.05"),
        "leverage": None,
        "position_id": str(position_id),
        "reduce_only": False,
        "order_type": "market",
        "limit_price": None,
    }
    sell_idempotency_key = str(uuid.uuid4())

    async with spot_pg_sessions() as session:
        first_sell = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=sell_idempotency_key,
            trade_intent=sell_intent,
        )

    assert first_sell.replayed is False
    original_response = first_sell.response_data
    assert original_response["transaction_type"] == "spot_sell"

    async with spot_pg_sessions() as session:
        position_before = await session.get(PaperPosition, position_id)
        assert position_before is not None

        book_id = position_before.book_id
        balances_before = await _snapshot_balances(session, book_id)

        transactions_before = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        entries_before = list(
            (
                await session.scalars(
                    select(PaperLedgerEntry).where(
                        PaperLedgerEntry.book_id == book_id,
                    )
                )
            ).all()
        )

        transaction_ids_before = {item.id for item in transactions_before}
        entry_ids_before = {item.id for item in entries_before}

        position_state_before = (
            position_before.status,
            position_before.remaining_quantity,
            position_before.remaining_cost_basis,
            position_before.realized_pnl,
        )

    async with spot_pg_sessions() as session:
        replay_result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=sell_idempotency_key,
            trade_intent=sell_intent,
        )

    assert replay_result.replayed is True
    assert replay_result.response_data == original_response

    async with spot_pg_sessions() as session:
        position_after = await session.get(PaperPosition, position_id)
        assert position_after is not None

        position_state_after = (
            position_after.status,
            position_after.remaining_quantity,
            position_after.remaining_cost_basis,
            position_after.realized_pnl,
        )
        assert position_state_after == position_state_before

        balances_after = await _snapshot_balances(session, book_id)
        assert balances_after == balances_before

        transactions_after = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        entries_after = list(
            (
                await session.scalars(
                    select(PaperLedgerEntry).where(
                        PaperLedgerEntry.book_id == book_id,
                    )
                )
            ).all()
        )

        assert {item.id for item in transactions_after} == transaction_ids_before
        assert {item.id for item in entries_after} == entry_ids_before

        requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )
        assert len(requests) == 2

@pytest.mark.asyncio
async def test_postgres_spot_concurrent_full_sells_cannot_oversell(
    spot_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account(spot_pg_sessions)
    market_data = _make_spot_market_data()

    async with spot_pg_sessions() as session:
        buy_result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_buy_intent(),
        )

    assert buy_result.replayed is False
    position_id = uuid.UUID(buy_result.response_data["position_id"])

    async with spot_pg_sessions() as session:
        position_before = await session.get(PaperPosition, position_id)
        assert position_before is not None
        book_id = position_before.book_id

        transactions_before = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        transaction_ids_before = {item.id for item in transactions_before}

    original_authorize = spot_execution.authorize_paper_write
    arrivals = 0
    release = asyncio.Event()

    async def synchronized_authorize(session, **kwargs):
        nonlocal arrivals
        arrivals += 1

        if arrivals == 2:
            release.set()

        await asyncio.wait_for(release.wait(), timeout=10)
        return await original_authorize(session, **kwargs)

    monkeypatch.setattr(
        spot_execution,
        "authorize_paper_write",
        synchronized_authorize,
    )

    sell_intent = {
        "symbol": "BTCUSDT",
        "side": "sell",
        "position_side": None,
        "quantity": Decimal("0.1"),
        "leverage": None,
        "position_id": str(position_id),
        "reduce_only": False,
        "order_type": "market",
        "limit_price": None,
    }

    async def execute_one():
        async with spot_pg_sessions() as session:
            return await spot_execution.execute_spot_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.spot_portfolio_id,
                idempotency_key=str(uuid.uuid4()),
                trade_intent=dict(sell_intent),
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            execute_one(),
            execute_one(),
            return_exceptions=True,
        ),
        timeout=30,
    )

    successful = [
        result for result in results
        if not isinstance(result, BaseException)
    ]
    rejected = [
        result for result in results
        if isinstance(result, BaseException)
    ]

    assert len(successful) == 1
    assert len(rejected) == 1
    assert isinstance(rejected[0], spot_execution.PaperTradeExecutionError)

    successful_result = successful[0]
    assert successful_result.replayed is False
    assert successful_result.response_data["transaction_type"] == "spot_sell"
    assert Decimal(successful_result.response_data["quantity"]) == Decimal("0.1")
    assert Decimal(successful_result.response_data["remaining_quantity"]) == ZERO

    async with spot_pg_sessions() as session:
        position_after = await session.get(PaperPosition, position_id)
        assert position_after is not None
        assert position_after.status == "closed"
        assert position_after.remaining_quantity == ZERO
        assert position_after.remaining_cost_basis == ZERO
        assert position_after.closed_at is not None

        balances = await _snapshot_balances(session, book_id)
        assert balances
        assert all(value.is_finite() for value in balances.values())

        transactions_after = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        transaction_ids_after = {item.id for item in transactions_after}

        assert transaction_ids_before.issubset(transaction_ids_after)
        assert len(transaction_ids_after - transaction_ids_before) == 1

        requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )
        assert len(requests) == 2

@pytest.mark.asyncio
async def test_postgres_spot_concurrent_partial_sells_serialize_correctly(
    spot_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account(spot_pg_sessions)
    market_data = _make_spot_market_data()

    async with spot_pg_sessions() as session:
        buy_result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_buy_intent(),
        )

    assert buy_result.replayed is False
    position_id = uuid.UUID(buy_result.response_data["position_id"])

    async with spot_pg_sessions() as session:
        position_before = await session.get(PaperPosition, position_id)
        assert position_before is not None
        book_id = position_before.book_id

        transactions_before = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        transaction_ids_before = {item.id for item in transactions_before}

    original_authorize = spot_execution.authorize_paper_write
    arrivals = 0
    release = asyncio.Event()

    async def synchronized_authorize(session, **kwargs):
        nonlocal arrivals
        arrivals += 1

        if arrivals == 2:
            release.set()

        await asyncio.wait_for(release.wait(), timeout=10)
        return await original_authorize(session, **kwargs)

    monkeypatch.setattr(
        spot_execution,
        "authorize_paper_write",
        synchronized_authorize,
    )

    sell_intent = {
        "symbol": "BTCUSDT",
        "side": "sell",
        "position_side": None,
        "quantity": Decimal("0.05"),
        "leverage": None,
        "position_id": str(position_id),
        "reduce_only": False,
        "order_type": "market",
        "limit_price": None,
    }

    async def execute_one():
        async with spot_pg_sessions() as session:
            return await spot_execution.execute_spot_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.spot_portfolio_id,
                idempotency_key=str(uuid.uuid4()),
                trade_intent=dict(sell_intent),
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            execute_one(),
            execute_one(),
            return_exceptions=True,
        ),
        timeout=30,
    )

    assert all(not isinstance(result, BaseException) for result in results)
    assert len(results) == 2

    for result in results:
        assert result.replayed is False
        assert result.response_data["transaction_type"] == "spot_sell"
        assert Decimal(result.response_data["quantity"]) == Decimal("0.05")

    assert {
        Decimal(result.response_data["remaining_quantity"])
        for result in results
    } == {ZERO, Decimal("0.05")}

    async with spot_pg_sessions() as session:
        position_after = await session.get(PaperPosition, position_id)
        assert position_after is not None
        assert position_after.status == "closed"
        assert position_after.remaining_quantity == ZERO
        assert position_after.remaining_cost_basis == ZERO
        assert position_after.closed_at is not None

        balances = await _snapshot_balances(session, book_id)
        assert balances
        assert all(value.is_finite() for value in balances.values())

        transactions_after = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        transaction_ids_after = {item.id for item in transactions_after}

        assert transaction_ids_before.issubset(transaction_ids_after)
        assert len(transaction_ids_after - transaction_ids_before) == 2

        requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )
        assert len(requests) == 3


@pytest.mark.asyncio
async def test_postgres_spot_concurrent_same_key_buy_posts_once(
    spot_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account(spot_pg_sessions)
    market_data = _make_spot_market_data()
    idempotency_key = str(uuid.uuid4())
    trade_intent = _buy_intent()

    original_authorize = spot_execution.authorize_paper_write
    authorization_arrivals = 0
    authorization_barrier = asyncio.Event()

    async def synchronized_authorize(*args, **kwargs):
        nonlocal authorization_arrivals

        authorization_arrivals += 1
        if authorization_arrivals == 2:
            authorization_barrier.set()

        await asyncio.wait_for(authorization_barrier.wait(), timeout=10)
        return await original_authorize(*args, **kwargs)

    monkeypatch.setattr(
        spot_execution,
        "authorize_paper_write",
        synchronized_authorize,
    )

    async def execute_one():
        async with spot_pg_sessions() as session:
            return await spot_execution.execute_spot_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.spot_portfolio_id,
                idempotency_key=idempotency_key,
                trade_intent=dict(trade_intent),
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            execute_one(),
            execute_one(),
            return_exceptions=True,
        ),
        timeout=30,
    )

    assert authorization_arrivals == 2, (
        f"Expected both requests at authorization barrier; got {authorization_arrivals}. "
        f"Results={[repr(result) for result in results]}"
    )

    successful = [
        result for result in results
        if not isinstance(result, BaseException)
    ]
    rejected = [
        result for result in results
        if isinstance(result, BaseException)
    ]

    assert len(successful) == 2, (
        f"Expected two successful results; got {len(successful)}. "
        f"Rejected={[repr(result) for result in rejected]}"
    )
    assert not rejected, f"Concurrent same-key request failed: {rejected!r}"

    original_results = [result for result in successful if result.replayed is False]
    replayed_results = [result for result in successful if result.replayed is True]

    assert len(original_results) == 1
    assert len(replayed_results) == 1

    winning_result = original_results[0]
    concurrent_replay_result = replayed_results[0]
    assert concurrent_replay_result.response_data == winning_result.response_data

    original_response = winning_result.response_data
    assert original_response["transaction_type"] == "spot_buy"
    assert Decimal(original_response["quantity"]) == Decimal("0.1")

    position_id = uuid.UUID(original_response["position_id"])

    async with spot_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None

        book_id = position.book_id
        position_state_before = (
            position.status,
            position.initial_quantity,
            position.remaining_quantity,
            position.remaining_cost_basis,
        )

        positions = list(
            (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.book_id == book_id,
                        PaperPosition.market_type == "spot",
                    )
                )
            ).all()
        )
        assert len(positions) == 1

        transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.transaction_type == "spot_buy",
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        assert len(transactions) == 1

        entries = list(
            (
                await session.scalars(
                    select(PaperLedgerEntry).where(
                        PaperLedgerEntry.book_id == book_id,
                    )
                )
            ).all()
        )
        transaction_ids_before = {item.id for item in transactions}
        entry_ids_before = {item.id for item in entries}

        assert entries
        assert transactions[0].id in {item.transaction_id for item in entries}

        requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )
        assert len(requests) == 1

        balances_before = await _snapshot_balances(session, book_id)
        assert balances_before
        assert all(value.is_finite() for value in balances_before.values())

    async with spot_pg_sessions() as session:
        replay_result = await spot_execution.execute_spot_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.spot_portfolio_id,
            idempotency_key=idempotency_key,
            trade_intent=dict(trade_intent),
        )

    assert replay_result.replayed is True
    assert replay_result.response_data == original_response

    async with spot_pg_sessions() as session:
        position_after = await session.get(PaperPosition, position_id)
        assert position_after is not None

        position_state_after = (
            position_after.status,
            position_after.initial_quantity,
            position_after.remaining_quantity,
            position_after.remaining_cost_basis,
        )
        assert position_state_after == position_state_before

        balances_after = await _snapshot_balances(session, book_id)
        assert balances_after == balances_before

        transactions_after = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.transaction_type == "spot_buy",
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        entries_after = list(
            (
                await session.scalars(
                    select(PaperLedgerEntry).where(
                        PaperLedgerEntry.book_id == book_id,
                    )
                )
            ).all()
        )
        requests_after = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )

        assert {item.id for item in transactions_after} == transaction_ids_before
        assert {item.id for item in entries_after} == entry_ids_before
        assert len(requests_after) == 1
@pytest.mark.asyncio
async def test_postgres_spot_buy_rolls_back_atomically_when_journal_posting_fails(
    spot_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account(spot_pg_sessions)
    market_data = _make_spot_market_data()
    idempotency_key = str(uuid.uuid4())

    async with spot_pg_sessions() as session:
        books = list(
            (
                await session.scalars(
                    select(PaperLedgerBook).where(
                        PaperLedgerBook.account_id == provisioned.account_id,
                        PaperLedgerBook.portfolio_id == provisioned.spot_portfolio_id,
                        PaperLedgerBook.market_type == "spot",
                    )
                )
            ).all()
        )
        assert len(books) == 1
        book_id = books[0].id

        positions_before = list(
            (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.book_id == book_id
                    )
                )
            ).all()
        )
        transactions_before = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id
                    )
                )
            ).all()
        )
        entries_before = list(
            (
                await session.scalars(
                    select(PaperLedgerEntry).where(
                        PaperLedgerEntry.book_id == book_id
                    )
                )
            ).all()
        )
        requests_before = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id
                    )
                )
            ).all()
        )

        position_ids_before = {item.id for item in positions_before}
        transaction_ids_before = {item.id for item in transactions_before}
        entry_ids_before = {item.id for item in entries_before}
        request_ids_before = {item.id for item in requests_before}
        balances_before = await _snapshot_balances(session, book_id)

    async def fail_journal_posting(*args, **kwargs):
        raise spot_execution.PaperLedgerPostingError(
            "Injected journal-posting failure for atomic rollback test."
        )

    monkeypatch.setattr(
        spot_execution,
        "post_paper_trade_journal_in_transaction",
        fail_journal_posting,
    )

    with pytest.raises(
        spot_execution.PaperTradeExecutionError
    ) as exc_info:
        async with spot_pg_sessions() as session:
            await spot_execution.execute_spot_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.spot_portfolio_id,
                idempotency_key=idempotency_key,
                trade_intent=_buy_intent(),
            )

    assert isinstance(
        exc_info.value.__cause__,
        spot_execution.PaperLedgerPostingError,
    )

    async with spot_pg_sessions() as session:
        positions_after = list(
            (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.book_id == book_id
                    )
                )
            ).all()
        )
        transactions_after = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id
                    )
                )
            ).all()
        )
        entries_after = list(
            (
                await session.scalars(
                    select(PaperLedgerEntry).where(
                        PaperLedgerEntry.book_id == book_id
                    )
                )
            ).all()
        )
        requests_after = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id
                    )
                )
            ).all()
        )

        assert {item.id for item in positions_after} == position_ids_before
        assert {item.id for item in transactions_after} == transaction_ids_before
        assert {item.id for item in entries_after} == entry_ids_before
        assert {item.id for item in requests_after} == request_ids_before

        balances_after = await _snapshot_balances(session, book_id)
        assert balances_after == balances_before

    assert len(positions_after) == len(positions_before)
    assert len(transactions_after) == len(transactions_before)
    assert len(entries_after) == len(entries_before)
    assert len(requests_after) == len(requests_before)


@pytest.mark.asyncio
async def test_postgres_spot_buy_recovers_after_postgres_statement_failure(
    spot_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account(spot_pg_sessions)
    market_data = _make_spot_market_data()
    idempotency_key = str(uuid.uuid4())

    async with spot_pg_sessions() as session:
        books = list((await session.scalars(
            select(PaperLedgerBook).where(
                PaperLedgerBook.account_id == provisioned.account_id,
                PaperLedgerBook.portfolio_id == provisioned.spot_portfolio_id,
                PaperLedgerBook.market_type == "spot",
            )
        )).all())
        assert len(books) == 1
        book_id = books[0].id

        positions_before = set((await session.scalars(
            select(PaperPosition.id).where(PaperPosition.book_id == book_id)
        )).all())
        transactions_before = set((await session.scalars(
            select(PaperLedgerTransaction.id).where(
                PaperLedgerTransaction.book_id == book_id
            )
        )).all())
        entries_before = set((await session.scalars(
            select(PaperLedgerEntry.id).where(
                PaperLedgerEntry.book_id == book_id
            )
        )).all())
        requests_before = set((await session.scalars(
            select(PaperExecutionRequest.id).where(
                PaperExecutionRequest.book_id == book_id
            )
        )).all())
        balances_before = await _snapshot_balances(session, book_id)

    async def inject_postgres_statement_failure(*args, **kwargs):
        await args[0].execute(text("SELECT 1 / 0"))
        raise AssertionError("Expected PostgreSQL division-by-zero failure.")

    monkeypatch.setattr(
        spot_execution,
        "post_paper_trade_journal_in_transaction",
        inject_postgres_statement_failure,
    )

    with pytest.raises(DBAPIError) as exc_info:
        async with spot_pg_sessions() as session:
            await spot_execution.execute_spot_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.spot_portfolio_id,
                idempotency_key=idempotency_key,
                trade_intent=_buy_intent(),
            )

    assert getattr(exc_info.value.orig, "sqlstate", None) == "22012"

    async with spot_pg_sessions() as session:
        recovery_result = await session.scalar(text("SELECT 1"))
        assert recovery_result == 1

        positions_after = set((await session.scalars(
            select(PaperPosition.id).where(PaperPosition.book_id == book_id)
        )).all())
        transactions_after = set((await session.scalars(
            select(PaperLedgerTransaction.id).where(
                PaperLedgerTransaction.book_id == book_id
            )
        )).all())
        entries_after = set((await session.scalars(
            select(PaperLedgerEntry.id).where(
                PaperLedgerEntry.book_id == book_id
            )
        )).all())
        requests_after = set((await session.scalars(
            select(PaperExecutionRequest.id).where(
                PaperExecutionRequest.book_id == book_id
            )
        )).all())
        balances_after = await _snapshot_balances(session, book_id)

    assert positions_after == positions_before
    assert transactions_after == transactions_before
    assert entries_after == entries_before
    assert requests_after == requests_before
    assert balances_after == balances_before
