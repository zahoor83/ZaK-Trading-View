from __future__ import annotations

import os
import uuid
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401 - ensure all mapped tables are registered
from app.market_data.config import MarketDataConfig
from app.market_data.futures_order_book import FuturesOrderBookSynchronizer
from app.market_data.service import MarketDataService, MarketResources
from app.market_data.service_models import PipelineState
from app.market_data.symbol_metadata import SymbolMetadata, SymbolMetadataSnapshot
from app.models.identity import User
from app.models.paper_fee_policy import PaperFeePolicyHead, PaperFeePolicyVersion
from app.models.paper_ledger import (
    PaperExecutionRequest,
    PaperLedgerAccount,
    PaperLedgerAccountSnapshot,
    PaperLedgerEntry,
    PaperLedgerTransaction,
    PaperPosition,
)
from app.services.account_service import ensure_default_account_with_initial_portfolios
from app.services import paper_futures_execution as futures_execution
from app.services.paper_fee_policy_persistence import set_paper_fee_policy


BASE_MS = 1_700_000_000_000
ZERO = Decimal("0")
BUY_PRICE = Decimal("100.01")


class FakeClock:
    is_synchronized = True

    def now_exchange_ms(self, *, at_monotonic=None):
        point = 100.0 if at_monotonic is None else at_monotonic
        return BASE_MS + round((point - 100.0) * 1000)


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
async def futures_pg_sessions():
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


def _make_depth_book(symbol: str) -> FuturesOrderBookSynchronizer:
    synchronizer = FuturesOrderBookSynchronizer(
        symbol,
        monotonic=lambda: 99.8,
    )
    event = {
        "e": "depthUpdate",
        "E": BASE_MS - 100,
        "T": BASE_MS - 100,
        "s": symbol,
        "U": 100,
        "u": 101,
        "pu": 99,
        "b": [["100.00", "2.0"]],
        "a": [["100.01", "3.0"]],
    }
    assert synchronizer.ingest_event(event) is True
    synchronizer.apply_snapshot(
        {
            "lastUpdateId": 100,
            "E": BASE_MS - 200,
            "T": BASE_MS - 200,
            "bids": [["100.00", "1.0"]],
            "asks": [["100.01", "1.0"]],
        }
    )
    return synchronizer


def _make_market_data(symbol: str = "BTCUSDT") -> MarketDataService:
    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=(symbol,)),
        monotonic=lambda: 100.0,
    )
    filters = (
        MappingProxyType(
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "1000000",
                "tickSize": "0.01",
            }
        ),
        MappingProxyType(
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "1000",
                "stepSize": "0.001",
            }
        ),
        MappingProxyType(
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "1000",
                "stepSize": "0.001",
            }
        ),
        MappingProxyType(
            {
                "filterType": "MIN_NOTIONAL",
                "notional": "10",
            }
        ),
    )
    record = SymbolMetadata(
        market="usdm_futures",
        symbol=symbol,
        status="TRADING",
        base_asset=symbol[:-4],
        quote_asset="USDT",
        order_types=("LIMIT", "MARKET"),
        filter_types=(
            "PRICE_FILTER",
            "LOT_SIZE",
            "MARKET_LOT_SIZE",
            "MIN_NOTIONAL",
        ),
        pair=symbol,
        contract_type="PERPETUAL",
        margin_asset="USDT",
        filters=filters,
    )
    metadata = SymbolMetadataSnapshot(
        market="usdm_futures",
        server_time_ms=BASE_MS - 500,
        fetched_at_monotonic=99.5,
        max_age_seconds=300.0,
        symbols=MappingProxyType({symbol: record}),
    )
    service._resources["usdm_futures"] = MarketResources(
        market="usdm_futures",
        clock=FakeClock(),
        metadata=metadata,
        freshness_validator=None,
        server_time_provider=None,
        metadata_refresh_provider=lambda: None,
    )
    service._pipeline_states["futures_depth"] = PipelineState.SUBSCRIBING
    expected_stream = f"{symbol.lower()}@depth"
    service._supervisors["futures_depth"] = SimpleNamespace(
        state=SimpleNamespace(value="SUBSCRIPTIONS_READY"),
        active_streams=(expected_stream,),
    )
    service._futures_order_books = {symbol: _make_depth_book(symbol)}
    return service


async def _create_owner_account(factory):
    unique = uuid.uuid4().hex
    email = f"futures-execution-{unique}@example.test"

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
                portfolio_id=provisioned.futures_portfolio_id,
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

    assert len(accounts) == 6
    assert len(snapshots) == 6
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
            entry.amount if entry.direction == positive_direction else -entry.amount
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


def _intent(*, quantity: str, position_id: str | None = None):
    return {
        "symbol": "BTCUSDT",
        "side": "buy",
        "position_side": "long",
        "quantity": quantity,
        "leverage": "10",
        "position_id": position_id,
        "reduce_only": False,
        "order_type": "market",
        "limit_price": None,
    }


@pytest.mark.asyncio
async def test_postgres_futures_open_increase_idempotency_and_atomic_rollback(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account(futures_pg_sessions)
    market_data = _make_market_data()
    first_key = str(uuid.uuid4())
    second_key = str(uuid.uuid4())
    rollback_key = str(uuid.uuid4())

    # First execution: open a new Futures LONG position.
    async with futures_pg_sessions() as session:
        opened = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=first_key,
            trade_intent=_intent(quantity="0.1"),
        )
    assert opened.replayed is False
    assert opened.response_data["position_action"] == "open"
    assert opened.response_data["quote_source"] == "futures_depth_book"
    position_id = uuid.UUID(opened.response_data["position_id"])

    async with futures_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None
        assert position.status == "open"
        assert position.side == "long"
        assert position.initial_quantity == Decimal("0.1")
        assert position.remaining_quantity == Decimal("0.1")
        assert position.entry_price == BUY_PRICE
        assert position.margin_locked == Decimal("1.0001")
        assert position.entry_fee_original == Decimal("0.010001")
        assert position.entry_fee_posted == Decimal("0.01")
        book_id = position.book_id

        first_balances = await _snapshot_balances(session, book_id)
        assert first_balances["futures_margin_locked"] == Decimal("1.0001")
        assert first_balances["fee_expense"] == Decimal("0.01")
        assert first_balances["cash_available"] == Decimal("998.9899")

    # Second execution: increase the same position using a new idempotency key.
    async with futures_pg_sessions() as session:
        increased = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=second_key,
            trade_intent=_intent(
                quantity="0.2",
                position_id=str(position_id),
            ),
        )
    assert increased.replayed is False
    assert increased.response_data["position_action"] == "increase"
    assert increased.response_data["position_id"] == str(position_id)

    async with futures_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None
        assert position.initial_quantity == Decimal("0.3")
        assert position.remaining_quantity == Decimal("0.3")
        assert position.entry_price == BUY_PRICE
        assert position.margin_locked == Decimal("3.0003")
        assert position.entry_fee_original == Decimal("0.030003")
        assert position.entry_fee_posted == Decimal("0.03")

        balances_before_rollback = await _snapshot_balances(session, book_id)
        assert balances_before_rollback["futures_margin_locked"] == Decimal("3.0003")
        assert balances_before_rollback["fee_expense"] == Decimal("0.03")
        assert balances_before_rollback["cash_available"] == Decimal("996.9697")

        posted_transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.transaction_type == "futures_open",
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        assert len(posted_transactions) == 2

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

    # Idempotent replay must not create another position mutation or journal.
    async with futures_pg_sessions() as session:
        replay = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=second_key,
            trade_intent=_intent(
                quantity="0.2",
                position_id=str(position_id),
            ),
        )
    assert replay.replayed is True
    assert replay.response_data == increased.response_data

    # Reusing that key with a different request must be rejected.
    with pytest.raises(futures_execution.PaperIdempotencyConflict):
        async with futures_pg_sessions() as session:
            await futures_execution.execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=second_key,
                trade_intent=_intent(
                    quantity="0.25",
                    position_id=str(position_id),
                ),
            )

    # Force failure only after the real journal, execution request and trigger
    # updates have flushed. The wrapper transaction must roll all of them back.
    original_post = futures_execution.post_paper_trade_journal_in_transaction

    async def post_then_fail(*args, **kwargs):
        await original_post(*args, **kwargs)
        raise RuntimeError("controlled post-journal rollback probe")

    monkeypatch.setattr(
        futures_execution,
        "post_paper_trade_journal_in_transaction",
        post_then_fail,
    )

    with pytest.raises(RuntimeError, match="controlled post-journal rollback probe"):
        async with futures_pg_sessions() as session:
            await futures_execution.execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=rollback_key,
                trade_intent=_intent(
                    quantity="0.1",
                    position_id=str(position_id),
                ),
            )

    monkeypatch.setattr(
        futures_execution,
        "post_paper_trade_journal_in_transaction",
        original_post,
    )

    async with futures_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None
        assert position.initial_quantity == Decimal("0.3")
        assert position.remaining_quantity == Decimal("0.3")
        assert position.margin_locked == Decimal("3.0003")
        assert position.entry_fee_original == Decimal("0.030003")
        assert position.entry_fee_posted == Decimal("0.03")

        balances_after_rollback = await _snapshot_balances(session, book_id)
        assert balances_after_rollback == balances_before_rollback

        rollback_transactions = await session.scalar(
            select(PaperLedgerTransaction.id).where(
                PaperLedgerTransaction.book_id == book_id,
                PaperLedgerTransaction.idempotency_key == rollback_key,
            )
        )
        assert rollback_transactions is None

        rollback_requests = await session.scalar(
            select(PaperExecutionRequest.id).where(
                PaperExecutionRequest.book_id == book_id,
                PaperExecutionRequest.idempotency_key
                == futures_execution.hash_idempotency_key(rollback_key),
            )
        )
        assert rollback_requests is None

        final_transactions = await session.scalar(
            select(PaperLedgerTransaction.id).where(
                PaperLedgerTransaction.book_id == book_id,
                PaperLedgerTransaction.transaction_type == "futures_open",
                PaperLedgerTransaction.status == "posted",
            )
        )
        assert final_transactions is not None

        all_trade_transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.transaction_type == "futures_open",
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        assert len(all_trade_transactions) == 2

        all_trade_requests = list(
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
        assert len(all_trade_requests) == 2

        # Also verify the immutable fee-policy configuration was persisted.
        policy_versions = list(
            (
                await session.scalars(
                    select(PaperFeePolicyVersion).where(
                        PaperFeePolicyVersion.account_id == provisioned.account_id,
                        PaperFeePolicyVersion.market_type == "futures",
                    )
                )
            ).all()
        )
        policy_heads = list(
            (
                await session.scalars(
                    select(PaperFeePolicyHead).where(
                        PaperFeePolicyHead.account_id == provisioned.account_id,
                        PaperFeePolicyHead.market_type == "futures",
                    )
                )
            ).all()
        )
        assert len(policy_versions) == 1
        assert len(policy_heads) == 1
