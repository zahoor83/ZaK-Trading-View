from __future__ import annotations

import asyncio
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
from app.services.account_service import (
    create_account_with_initial_portfolios,
    ensure_default_account_with_initial_portfolios,
)
from app.services import paper_futures_execution as futures_execution
from app.services import paper_portfolio_operations as portfolio_operations
from app.services.paper_fee_policy_persistence import set_paper_fee_policy


BASE_MS = 1_700_000_000_000
ZERO = Decimal("0")
BUY_PRICE = Decimal("100.01")

def _assert_futures_response_contract(response_data, *, expected_action):
    envelope_fields = {
        "transaction_id", "book_id", "market_type", "transaction_type",
        "status", "entry_count", "debit_total", "credit_total",
    }
    position_fields = {
        "position_id", "symbol", "side", "position_side", "position_action",
        "quantity", "execution_price", "gross_value", "fee_original",
        "fee_posted", "realized_pnl", "position_realized_pnl",
        "remaining_quantity", "remaining_margin", "entry_price", "leverage",
        "position_status", "quote_source", "quote_update_id",
        "fee_config_snapshot", "symbol_metadata_snapshot",
        "fill_method", "fill_levels", "fill_level_count",
    }

    assert isinstance(response_data, dict)
    assert set(response_data) == envelope_fields | position_fields

    assert uuid.UUID(response_data["transaction_id"])
    assert uuid.UUID(response_data["book_id"])
    assert uuid.UUID(response_data["position_id"])

    assert response_data["market_type"] == "futures"
    assert response_data["status"] == "posted"
    assert response_data["transaction_type"] in {
        "futures_open", "futures_close"
    }
    assert type(response_data["entry_count"]) is int
    assert response_data["entry_count"] > 0

    for field in ("debit_total", "credit_total"):
        assert isinstance(response_data[field], str)
        assert Decimal(response_data[field]).is_finite()

    assert response_data["position_action"] == expected_action
    assert response_data["symbol"] == "BTCUSDT"
    assert response_data["side"] in {"buy", "sell"}
    assert response_data["position_side"] in {"long", "short"}
    assert response_data["quote_source"] == "futures_depth_book"
    assert type(response_data["quote_update_id"]) is int
    assert response_data["quote_update_id"] > 0
    assert response_data["fill_method"] == "depth_vwap"
    assert type(response_data["fill_level_count"]) is int
    assert response_data["fill_level_count"] >= 1
    fill_levels = response_data["fill_levels"]
    assert isinstance(fill_levels, list)
    assert len(fill_levels) == response_data["fill_level_count"]
    fill_quantity = Decimal("0")
    fill_notional = Decimal("0")
    for level in fill_levels:
        assert isinstance(level, dict)
        assert set(level) == {"price", "quantity", "notional"}
        for field in ("price", "quantity", "notional"):
            assert isinstance(level[field], str)
            assert Decimal(level[field]).is_finite()
            assert Decimal(level[field]) > 0
        assert (
            Decimal(level["price"]) * Decimal(level["quantity"])
            == Decimal(level["notional"])
        )
        fill_quantity += Decimal(level["quantity"])
        fill_notional += Decimal(level["notional"])
    assert fill_quantity == Decimal(response_data["quantity"])
    assert fill_notional == Decimal(response_data["gross_value"])

    expected_transaction_type = (
        "futures_open"
        if expected_action in {"open", "increase"}
        else "futures_close"
    )
    assert response_data["transaction_type"] == expected_transaction_type

    if expected_action in {"open", "increase"}:
        assert response_data["position_status"] == "open"
    elif expected_action == "close":
        assert response_data["position_status"] == "closed"
    else:
        assert response_data["position_status"] in {"open", "closed"}

    decimal_fields = (
        "quantity", "execution_price", "gross_value", "fee_original",
        "fee_posted", "realized_pnl", "position_realized_pnl",
        "remaining_quantity", "remaining_margin", "entry_price", "leverage",
    )
    for field in decimal_fields:
        assert isinstance(response_data[field], str), field
        assert Decimal(response_data[field]).is_finite(), field

    assert Decimal(response_data["quantity"]) > 0
    assert Decimal(response_data["execution_price"]) > 0
    assert Decimal(response_data["gross_value"]) > 0
    assert Decimal(response_data["remaining_quantity"]) >= 0
    assert Decimal(response_data["remaining_margin"]) >= 0
    assert Decimal(response_data["entry_price"]) > 0
    assert Decimal(response_data["leverage"]) > 0

    fee_snapshot = response_data["fee_config_snapshot"]
    assert isinstance(fee_snapshot, dict)
    assert set(fee_snapshot) == {
        "schema_version", "policy_id", "version", "market_type",
        "entry_fee_rate", "exit_fee_rate",
    }
    assert type(fee_snapshot["schema_version"]) is int
    assert fee_snapshot["schema_version"] == 1
    assert fee_snapshot["market_type"] == "futures"
    assert isinstance(fee_snapshot["policy_id"], str)
    assert fee_snapshot["policy_id"]
    assert type(fee_snapshot["version"]) is int
    assert fee_snapshot["version"] > 0

    for field in ("entry_fee_rate", "exit_fee_rate"):
        assert isinstance(fee_snapshot[field], str)
        assert Decimal(fee_snapshot[field]).is_finite()
        assert Decimal(fee_snapshot[field]) >= 0

    metadata = response_data["symbol_metadata_snapshot"]
    assert isinstance(metadata, dict)
    assert set(metadata) == {
        "schema_version", "market", "symbol", "status", "base_asset",
        "quote_asset", "pair", "contract_type", "margin_asset",
        "delivery_date", "onboard_date", "order_types", "filter_types",
        "price_precision", "quantity_precision", "filters",
        "metadata_server_time_ms", "tick_size",
    }
    assert type(metadata["schema_version"]) is int
    assert metadata["schema_version"] == 1
    assert metadata["market"] == "usdm_futures"
    assert metadata["symbol"] == response_data["symbol"]
    assert metadata["status"] == "TRADING"
    assert metadata["base_asset"] == "BTC"
    assert metadata["quote_asset"] == "USDT"
    assert metadata["pair"] == "BTCUSDT"
    assert metadata["contract_type"] == "PERPETUAL"
    assert metadata["margin_asset"] == "USDT"
    assert isinstance(metadata["order_types"], list)
    assert isinstance(metadata["filter_types"], list)
    assert isinstance(metadata["filters"], list)
    assert metadata["filters"]
    assert isinstance(metadata["tick_size"], str)
    assert Decimal(metadata["tick_size"]) > 0


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
    _assert_futures_response_contract(opened.response_data, expected_action="open")
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
    _assert_futures_response_contract(increased.response_data, expected_action="increase")
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

def _close_intent(*, quantity: str, position_id: str):
    return {
        "symbol": "BTCUSDT",
        "side": "sell",
        "position_side": "long",
        "quantity": quantity,
        "leverage": "10",
        "position_id": position_id,
        "reduce_only": True,
        "order_type": "market",
        "limit_price": None,
    }


@pytest.mark.asyncio
async def test_postgres_futures_partial_reduce_full_close_idempotency_and_rollback(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account(futures_pg_sessions)
    market_data = _make_market_data()

    open_key = str(uuid.uuid4())
    rollback_key = str(uuid.uuid4())
    partial_key = str(uuid.uuid4())
    full_key = str(uuid.uuid4())

    # Open a 0.2 BTC Futures LONG position.
    async with futures_pg_sessions() as session:
        opened = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=open_key,
            trade_intent=_intent(quantity="0.2"),
        )

    assert opened.replayed is False
    _assert_futures_response_contract(opened.response_data, expected_action="open")
    position_id = uuid.UUID(opened.response_data["position_id"])

    async with futures_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None
        assert position.status == "open"
        assert position.remaining_quantity == Decimal("0.2")
        assert position.margin_locked == Decimal("2.0002")
        book_id = position.book_id

        initial_balances = await _snapshot_balances(session, book_id)
        assert initial_balances["cash_available"] == Decimal("997.9798")
        assert initial_balances["futures_margin_locked"] == Decimal("2.0002")
        assert initial_balances["fee_expense"] == Decimal("0.02")
        assert initial_balances["realized_pnl"] == ZERO

    # Force a failure after journal posting during a partial close.
    # Position, journal, execution request and snapshots must all roll back.
    original_post = futures_execution.post_paper_trade_journal_in_transaction

    async def post_then_fail(*args, **kwargs):
        await original_post(*args, **kwargs)
        raise RuntimeError("controlled D2 post-journal rollback probe")

    monkeypatch.setattr(
        futures_execution,
        "post_paper_trade_journal_in_transaction",
        post_then_fail,
    )

    with pytest.raises(
        RuntimeError,
        match="controlled D2 post-journal rollback probe",
    ):
        async with futures_pg_sessions() as session:
            await futures_execution.execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=rollback_key,
                trade_intent=_close_intent(
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
        assert position.status == "open"
        assert position.remaining_quantity == Decimal("0.2")
        assert position.margin_locked == Decimal("2.0002")
        assert position.realized_pnl == ZERO

        balances_after_rollback = await _snapshot_balances(session, book_id)
        assert balances_after_rollback == initial_balances

        rollback_transaction = await session.scalar(
            select(PaperLedgerTransaction.id).where(
                PaperLedgerTransaction.book_id == book_id,
                PaperLedgerTransaction.idempotency_key == rollback_key,
            )
        )
        assert rollback_transaction is None

        rollback_request = await session.scalar(
            select(PaperExecutionRequest.id).where(
                PaperExecutionRequest.book_id == book_id,
                PaperExecutionRequest.idempotency_key
                == futures_execution.hash_idempotency_key(rollback_key),
            )
        )
        assert rollback_request is None

    # Partially reduce 0.1 BTC at the synchronized bid price of 100.00.
    async with futures_pg_sessions() as session:
        partial = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=partial_key,
            trade_intent=_close_intent(
                quantity="0.1",
                position_id=str(position_id),
            ),
        )

    assert partial.replayed is False
    _assert_futures_response_contract(partial.response_data, expected_action="reduce")
    assert partial.response_data["position_status"] == "open"
    assert partial.response_data["position_id"] == str(position_id)
    assert Decimal(partial.response_data["quantity"]) == Decimal("0.1")
    assert Decimal(partial.response_data["execution_price"]) == Decimal("100.00")
    assert Decimal(partial.response_data["fee_original"]) == Decimal("0.01")
    assert Decimal(partial.response_data["fee_posted"]) == Decimal("0.01")
    assert Decimal(partial.response_data["realized_pnl"]) == Decimal("-0.001")
    assert Decimal(partial.response_data["remaining_quantity"]) == Decimal("0.1")
    assert Decimal(partial.response_data["remaining_margin"]) == Decimal("1.0001")

    async with futures_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None
        assert position.status == "open"
        assert position.closed_at is None
        assert position.initial_quantity == Decimal("0.2")
        assert position.remaining_quantity == Decimal("0.1")
        assert position.margin_locked == Decimal("1.0001")
        assert position.realized_pnl == Decimal("-0.001")

        partial_balances = await _snapshot_balances(session, book_id)
        assert partial_balances["cash_available"] == Decimal("998.9689")
        assert partial_balances["futures_margin_locked"] == Decimal("1.0001")
        assert partial_balances["fee_expense"] == Decimal("0.03")
        assert partial_balances["realized_pnl"] == Decimal("-0.001")

    # Replaying the exact partial-close request must not post another journal.
    async with futures_pg_sessions() as session:
        replay = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=partial_key,
            trade_intent=_close_intent(
                quantity="0.1",
                position_id=str(position_id),
            ),
        )

    assert replay.replayed is True
    assert replay.response_data == partial.response_data

    async with futures_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None
        assert position.remaining_quantity == Decimal("0.1")
        assert position.margin_locked == Decimal("1.0001")

        replay_balances = await _snapshot_balances(session, book_id)
        assert replay_balances == partial_balances

        close_transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.transaction_type == "futures_close",
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        assert len(close_transactions) == 1

    # Fully close the remaining 0.1 BTC.
    async with futures_pg_sessions() as session:
        closed = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=full_key,
            trade_intent=_close_intent(
                quantity="0.1",
                position_id=str(position_id),
            ),
        )

    assert closed.replayed is False
    _assert_futures_response_contract(closed.response_data, expected_action="close")
    assert closed.response_data["position_status"] == "closed"
    assert closed.response_data["position_id"] == str(position_id)
    assert Decimal(closed.response_data["quantity"]) == Decimal("0.1")
    assert Decimal(closed.response_data["fee_posted"]) == Decimal("0.01")
    assert Decimal(closed.response_data["realized_pnl"]) == Decimal("-0.001")
    assert Decimal(closed.response_data["position_realized_pnl"]) == Decimal("-0.002")
    assert Decimal(closed.response_data["remaining_quantity"]) == ZERO
    assert Decimal(closed.response_data["remaining_margin"]) == ZERO

    async with futures_pg_sessions() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None
        assert position.status == "closed"
        assert position.closed_at is not None
        assert position.remaining_quantity == ZERO
        assert position.margin_locked == ZERO
        assert position.realized_pnl == Decimal("-0.002")

        final_balances = await _snapshot_balances(session, book_id)
        assert final_balances["cash_available"] == Decimal("999.958")
        assert final_balances["futures_margin_locked"] == ZERO
        assert final_balances["fee_expense"] == Decimal("0.04")
        assert final_balances["realized_pnl"] == Decimal("-0.002")

        close_transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.transaction_type == "futures_close",
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        assert len(close_transactions) == 2

        completed_requests = list(
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
        assert len(completed_requests) == 3

async def _create_owner_account_for_mode(factory, position_mode: str):
    unique = uuid.uuid4().hex
    email = f"futures-concurrency-{unique}@example.test"

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
        provisioned = await create_account_with_initial_portfolios(
            session,
            owner_user_id=user_id,
            account_name=f"Futures concurrency {unique}",
            futures_position_mode=position_mode,
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

    assert provisioned.futures_position_mode == position_mode
    return user_id, provisioned


def _directional_intent(*, side: str, position_side: str, quantity="0.1"):
    intent = _intent(quantity=quantity)
    intent["side"] = side
    intent["position_side"] = position_side
    return intent


async def _run_concurrent_futures_trades(
    factory,
    *,
    user_id,
    provisioned,
    intents,
    monkeypatch,
):
    original_authorize = futures_execution.authorize_paper_write
    arrivals = 0
    release = asyncio.Event()

    async def synchronized_authorize(session, **kwargs):
        nonlocal arrivals
        arrivals += 1

        if arrivals == len(intents):
            release.set()

        await asyncio.wait_for(release.wait(), timeout=10)
        return await original_authorize(session, **kwargs)

    monkeypatch.setattr(
        futures_execution,
        "authorize_paper_write",
        synchronized_authorize,
    )

    market_data = _make_market_data()

    async def execute_one(intent):
        async with factory() as session:
            return await futures_execution.execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=str(uuid.uuid4()),
                trade_intent=dict(intent),
            )

    return await asyncio.wait_for(
        asyncio.gather(
            *(execute_one(intent) for intent in intents),
            return_exceptions=True,
        ),
        timeout=30,
    )


async def _assert_concurrent_futures_state(
    factory,
    results,
    *,
    expected_positions: int,
    expected_transactions: int,
    expected_requests: int,
):
    successful = [
        result for result in results
        if not isinstance(result, BaseException)
    ]

    assert len(successful) == expected_requests

    position_ids = {
        uuid.UUID(result.response_data["position_id"])
        for result in successful
    }

    async with factory() as session:
        positions = list(
            (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.id.in_(position_ids)
                    )
                )
            ).all()
        )

        assert len(positions) == expected_positions
        assert all(position.status == "open" for position in positions)

        book_id = positions[0].book_id

        all_open_positions = list(
            (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.book_id == book_id,
                        PaperPosition.market_type == "futures",
                        PaperPosition.symbol == "BTCUSDT",
                        PaperPosition.status == "open",
                    )
                )
            ).all()
        )
        assert len(all_open_positions) == expected_positions

        balances = await _snapshot_balances(session, book_id)

        transactions = list(
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
        assert len(transactions) == expected_transactions

        requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == book_id,
                        PaperExecutionRequest.operation_type == "trade",
                    )
                )
            ).all()
        )
        assert len(requests) == expected_requests
        assert all(request.status == "completed" for request in requests)

        assert balances["contributed_capital"] == Decimal("1000.00")

    return all_open_positions, balances


@pytest.mark.asyncio
async def test_postgres_futures_concurrent_same_key_open_replays_once(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account_for_mode(
        futures_pg_sessions,
        "one_way",
    )

    original_authorize = futures_execution.authorize_paper_write
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
        futures_execution,
        "authorize_paper_write",
        synchronized_authorize,
    )

    market_data = _make_market_data()
    shared_key = str(uuid.uuid4())
    intent = _directional_intent(side="buy", position_side="long")

    async def execute_one():
        async with futures_pg_sessions() as session:
            return await futures_execution.execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=shared_key,
                trade_intent=dict(intent),
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            execute_one(),
            execute_one(),
            return_exceptions=True,
        ),
        timeout=30,
    )

    assert all(
        not isinstance(result, BaseException)
        for result in results
    )

    assert sorted(result.replayed for result in results) == [False, True]

    original_result = next(
        result for result in results if result.replayed is False
    )
    replay_result = next(
        result for result in results if result.replayed is True
    )

    assert original_result.response_data == replay_result.response_data
    _assert_futures_response_contract(
        original_result.response_data,
        expected_action="open",
    )

    positions, balances = await _assert_concurrent_futures_state(
        futures_pg_sessions,
        [original_result],
        expected_positions=1,
        expected_transactions=1,
        expected_requests=1,
    )

    assert positions[0].side == "long"
    assert positions[0].initial_quantity == Decimal("0.1")
    assert positions[0].remaining_quantity == Decimal("0.1")
    assert balances["futures_margin_locked"] == Decimal("1.0001")
    assert balances["fee_expense"] == Decimal("0.01")

@pytest.mark.asyncio
async def test_postgres_one_way_opposite_direction_concurrency_is_serialized(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account_for_mode(
        futures_pg_sessions,
        "one_way",
    )

    results = await _run_concurrent_futures_trades(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        intents=[
            _directional_intent(side="buy", position_side="long"),
            _directional_intent(side="sell", position_side="short"),
        ],
        monkeypatch=monkeypatch,
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
    assert isinstance(rejected[0], futures_execution.FuturesTradeExecutionError)
    assert successful[0].response_data["position_action"] == "open"

    positions, balances = await _assert_concurrent_futures_state(
        futures_pg_sessions,
        results,
        expected_positions=1,
        expected_transactions=1,
        expected_requests=1,
    )

    assert positions[0].side in {"long", "short"}
    assert positions[0].remaining_quantity == Decimal("0.1")
    assert balances["futures_margin_locked"] > ZERO


@pytest.mark.asyncio
async def test_postgres_hedge_opposite_direction_concurrency_creates_two_legs(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account_for_mode(
        futures_pg_sessions,
        "hedge",
    )

    results = await _run_concurrent_futures_trades(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        intents=[
            _directional_intent(side="buy", position_side="long"),
            _directional_intent(side="sell", position_side="short"),
        ],
        monkeypatch=monkeypatch,
    )

    assert all(
        not isinstance(result, BaseException)
        for result in results
    )
    assert all(
        result.response_data["position_action"] == "open"
        for result in results
    )

    positions, balances = await _assert_concurrent_futures_state(
        futures_pg_sessions,
        results,
        expected_positions=2,
        expected_transactions=2,
        expected_requests=2,
    )

    assert {position.side for position in positions} == {"long", "short"}
    assert all(
        position.remaining_quantity == Decimal("0.1")
        for position in positions
    )
    assert balances["futures_margin_locked"] > ZERO


@pytest.mark.asyncio
async def test_postgres_futures_concurrent_same_key_increase_replays_once(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account_for_mode(
        futures_pg_sessions,
        "one_way",
    )
    market_data = _make_market_data()

    # Establish the existing LONG position before testing concurrent increases.
    async with futures_pg_sessions() as session:
        opened = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_intent(quantity="0.1"),
        )

    assert opened.replayed is False
    _assert_futures_response_contract(
        opened.response_data,
        expected_action="open",
    )
    position_id = opened.response_data["position_id"]

    original_authorize = futures_execution.authorize_paper_write
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
        futures_execution,
        "authorize_paper_write",
        synchronized_authorize,
    )

    shared_key = str(uuid.uuid4())
    increase_intent = _intent(
        quantity="0.2",
        position_id=position_id,
    )

    async def execute_increase():
        async with futures_pg_sessions() as session:
            return await futures_execution.execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=shared_key,
                trade_intent=dict(increase_intent),
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            execute_increase(),
            execute_increase(),
            return_exceptions=True,
        ),
        timeout=30,
    )

    assert all(
        not isinstance(result, BaseException)
        for result in results
    )
    assert sorted(result.replayed for result in results) == [False, True]

    original_result = next(
        result for result in results if result.replayed is False
    )
    replay_result = next(
        result for result in results if result.replayed is True
    )

    assert original_result.response_data == replay_result.response_data
    _assert_futures_response_contract(
        original_result.response_data,
        expected_action="increase",
    )
    assert original_result.response_data["position_id"] == position_id

    positions, balances = await _assert_concurrent_futures_state(
        futures_pg_sessions,
        [opened, original_result],
        expected_positions=1,
        expected_transactions=2,
        expected_requests=2,
    )

    position = positions[0]
    assert position.id == uuid.UUID(position_id)
    assert position.side == "long"
    assert position.initial_quantity == Decimal("0.3")
    assert position.remaining_quantity == Decimal("0.3")
    assert position.margin_locked == Decimal("3.0003")
    assert position.entry_fee_original == Decimal("0.030003")
    assert position.entry_fee_posted == Decimal("0.03")

    assert balances["futures_margin_locked"] == Decimal("3.0003")
    assert balances["fee_expense"] == Decimal("0.03")
    assert balances["cash_available"] == Decimal("996.9697")

@pytest.mark.asyncio
async def test_postgres_futures_concurrent_same_key_reduce_replays_once(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account_for_mode(
        futures_pg_sessions,
        "one_way",
    )
    market_data = _make_market_data()

    # Open the initial LONG position before concurrent reduce requests.
    async with futures_pg_sessions() as session:
        opened = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_intent(quantity="0.3"),
        )

    assert opened.replayed is False
    _assert_futures_response_contract(
        opened.response_data,
        expected_action="open",
    )
    position_id = opened.response_data["position_id"]

    original_authorize = futures_execution.authorize_paper_write
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
        futures_execution,
        "authorize_paper_write",
        synchronized_authorize,
    )

    shared_key = str(uuid.uuid4())
    reduce_intent = _close_intent(
        quantity="0.1",
        position_id=position_id,
    )

    async def execute_reduce():
        async with futures_pg_sessions() as session:
            return await futures_execution.execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=shared_key,
                trade_intent=dict(reduce_intent),
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            execute_reduce(),
            execute_reduce(),
            return_exceptions=True,
        ),
        timeout=30,
    )

    assert all(
        not isinstance(result, BaseException)
        for result in results
    )
    assert sorted(result.replayed for result in results) == [False, True]

    original_result = next(
        result for result in results if result.replayed is False
    )
    replay_result = next(
        result for result in results if result.replayed is True
    )

    assert original_result.response_data == replay_result.response_data

    response = original_result.response_data
    _assert_futures_response_contract(
        response,
        expected_action="reduce",
    )

    assert response["position_id"] == position_id
    assert response["position_status"] == "open"
    assert Decimal(response["quantity"]) == Decimal("0.1")
    assert Decimal(response["execution_price"]) == Decimal("100.00")
    assert Decimal(response["fee_original"]) == Decimal("0.01")
    assert Decimal(response["fee_posted"]) == Decimal("0.01")
    assert Decimal(response["realized_pnl"]) == Decimal("-0.001")
    assert Decimal(response["remaining_quantity"]) == Decimal("0.2")
    assert Decimal(response["remaining_margin"]) == Decimal("2.0002")

    async with futures_pg_sessions() as session:
        position = await session.get(
            PaperPosition,
            uuid.UUID(position_id),
        )
        assert position is not None
        assert position.status == "open"
        assert position.closed_at is None
        assert position.initial_quantity == Decimal("0.3")
        assert position.remaining_quantity == Decimal("0.2")
        assert position.margin_locked == Decimal("2.0002")
        assert position.realized_pnl == Decimal("-0.001")

        balances = await _snapshot_balances(
            session,
            position.book_id,
        )
        assert balances["cash_available"] == Decimal("997.9588")
        assert balances["futures_margin_locked"] == Decimal("2.0002")
        assert balances["fee_expense"] == Decimal("0.04")
        assert balances["realized_pnl"] == Decimal("-0.001")

        close_transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == position.book_id,
                        PaperLedgerTransaction.transaction_type == "futures_close",
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        assert len(close_transactions) == 1
        assert close_transactions[0].idempotency_key == futures_execution.hash_idempotency_key(shared_key)

        completed_requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == position.book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )
        assert len(completed_requests) == 2

        matching_requests = [
            request
            for request in completed_requests
            if request.idempotency_key
            == futures_execution.hash_idempotency_key(shared_key)
        ]
        assert len(matching_requests) == 1

@pytest.mark.parametrize("position_mode", ["one_way", "hedge"])
@pytest.mark.asyncio
async def test_postgres_same_side_concurrency_increases_one_position(
    futures_pg_sessions,
    monkeypatch,
    position_mode,
):
    user_id, provisioned = await _create_owner_account_for_mode(
        futures_pg_sessions,
        position_mode,
    )

    results = await _run_concurrent_futures_trades(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        intents=[
            _directional_intent(side="buy", position_side="long"),
            _directional_intent(side="buy", position_side="long"),
        ],
        monkeypatch=monkeypatch,
    )

    assert all(
        not isinstance(result, BaseException)
        for result in results
    )

    actions = sorted(
        result.response_data["position_action"]
        for result in results
    )
    assert actions == ["increase", "open"]

    returned_position_ids = {
        result.response_data["position_id"]
        for result in results
    }
    assert len(returned_position_ids) == 1

    positions, balances = await _assert_concurrent_futures_state(
        futures_pg_sessions,
        results,
        expected_positions=1,
        expected_transactions=2,
        expected_requests=2,
    )

    position = positions[0]
    assert position.side == "long"
    assert position.initial_quantity == Decimal("0.2")
    assert position.remaining_quantity == Decimal("0.2")
    assert balances["futures_margin_locked"] > ZERO


# D4 SUFFICIENT CASH ACCEPTANCE TESTS

async def _d4_set_high_fee_policy(factory, user_id, provisioned):
    async with factory() as session:
        async with session.begin():
            await set_paper_fee_policy(
                session,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                entry_fee_rate=Decimal("0.99"),
                exit_fee_rate=Decimal("0.99"),
            )


async def _d4_execute(
    factory,
    *,
    user_id,
    provisioned,
    market_data,
    idempotency_key,
    trade_intent,
):
    async with factory() as session:
        return await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=idempotency_key,
            trade_intent=trade_intent,
        )


async def _d4_assert_cash_rejection_is_atomic(
    factory,
    *,
    user_id,
    provisioned,
    market_data,
    book_id,
    idempotency_key,
    trade_intent,
    expected_cash,
):
    async with factory() as session:
        balances_before = await _snapshot_balances(session, book_id)
        positions_before = list(
            (
                await session.scalars(
                    select(PaperPosition)
                    .where(PaperPosition.book_id == book_id)
                    .order_by(PaperPosition.id)
                )
            ).all()
        )
        position_state_before = [
            (
                item.id,
                item.status,
                item.initial_quantity,
                item.remaining_quantity,
                item.entry_price,
                item.margin_locked,
                item.realized_pnl,
                item.closed_at,
            )
            for item in positions_before
        ]

    assert balances_before["cash_available"] == expected_cash

    with pytest.raises(
        futures_execution.FuturesTradeExecutionError,
        match="Insufficient available cash",
    ):
        await _d4_execute(
            factory,
            user_id=user_id,
            provisioned=provisioned,
            market_data=market_data,
            idempotency_key=idempotency_key,
            trade_intent=trade_intent,
        )

    async with factory() as session:
        balances_after = await _snapshot_balances(session, book_id)
        assert balances_after == balances_before

        positions_after = list(
            (
                await session.scalars(
                    select(PaperPosition)
                    .where(PaperPosition.book_id == book_id)
                    .order_by(PaperPosition.id)
                )
            ).all()
        )
        position_state_after = [
            (
                item.id,
                item.status,
                item.initial_quantity,
                item.remaining_quantity,
                item.entry_price,
                item.margin_locked,
                item.realized_pnl,
                item.closed_at,
            )
            for item in positions_after
        ]
        assert position_state_after == position_state_before

        rejected_transaction = await session.scalar(
            select(PaperLedgerTransaction.id).where(
                PaperLedgerTransaction.book_id == book_id,
                PaperLedgerTransaction.idempotency_key == idempotency_key,
            )
        )
        assert rejected_transaction is None

        rejected_request = await session.scalar(
            select(PaperExecutionRequest.id).where(
                PaperExecutionRequest.book_id == book_id,
                PaperExecutionRequest.idempotency_key
                == futures_execution.hash_idempotency_key(idempotency_key),
            )
        )
        assert rejected_request is None


async def _d4_open_nine_one_btc_fills(
    factory,
    *,
    user_id,
    provisioned,
    market_data,
):
    opened = await _d4_execute(
        factory,
        user_id=user_id,
        provisioned=provisioned,
        market_data=market_data,
        idempotency_key=str(uuid.uuid4()),
        trade_intent=_intent(quantity="1"),
    )
    position_id = uuid.UUID(opened.response_data["position_id"])

    async with factory() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None
        book_id = position.book_id

    for _ in range(8):
        await _d4_execute(
            factory,
            user_id=user_id,
            provisioned=provisioned,
            market_data=market_data,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_intent(
                quantity="1",
                position_id=str(position_id),
            ),
        )

    async with factory() as session:
        position = await session.get(PaperPosition, position_id)
        assert position is not None
        assert position.status == "open"
        assert position.remaining_quantity == Decimal("9")
        assert position.margin_locked == Decimal("90.009")
        balances = await _snapshot_balances(session, book_id)
        assert balances["cash_available"] == Decimal("18.901")

    return position_id, book_id


@pytest.mark.asyncio
async def test_postgres_futures_insufficient_cash_rejects_new_open_atomically(
    futures_pg_sessions,
):
    user_id, provisioned = await _create_owner_account(futures_pg_sessions)
    await _d4_set_high_fee_policy(futures_pg_sessions, user_id, provisioned)
    market_data = _make_market_data()
    book_id = None

    for _ in range(5):
        opened = await _d4_execute(
            futures_pg_sessions,
            user_id=user_id,
            provisioned=provisioned,
            market_data=market_data,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_intent(quantity="1"),
        )
        position_id = uuid.UUID(opened.response_data["position_id"])

        async with futures_pg_sessions() as session:
            position = await session.get(PaperPosition, position_id)
            assert position is not None
            book_id = position.book_id

        await _d4_execute(
            futures_pg_sessions,
            user_id=user_id,
            provisioned=provisioned,
            market_data=market_data,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_close_intent(
                quantity="1",
                position_id=str(position_id),
            ),
        )

    assert book_id is not None
    rejected_key = str(uuid.uuid4())

    await _d4_assert_cash_rejection_is_atomic(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        market_data=market_data,
        book_id=book_id,
        idempotency_key=rejected_key,
        trade_intent=_intent(quantity="1"),
        expected_cash=Decimal("9.900"),
    )


@pytest.mark.asyncio
async def test_postgres_futures_insufficient_cash_rejects_increase_atomically(
    futures_pg_sessions,
):
    user_id, provisioned = await _create_owner_account(futures_pg_sessions)
    await _d4_set_high_fee_policy(futures_pg_sessions, user_id, provisioned)
    market_data = _make_market_data()

    position_id, book_id = await _d4_open_nine_one_btc_fills(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        market_data=market_data,
    )

    rejected_key = str(uuid.uuid4())
    await _d4_assert_cash_rejection_is_atomic(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        market_data=market_data,
        book_id=book_id,
        idempotency_key=rejected_key,
        trade_intent=_intent(
            quantity="1",
            position_id=str(position_id),
        ),
        expected_cash=Decimal("18.901"),
    )


@pytest.mark.asyncio
async def test_postgres_futures_insufficient_cash_rejects_losing_close_atomically(
    futures_pg_sessions,
):
    user_id, provisioned = await _create_owner_account(futures_pg_sessions)
    await _d4_set_high_fee_policy(futures_pg_sessions, user_id, provisioned)
    market_data = _make_market_data()

    position_id, book_id = await _d4_open_nine_one_btc_fills(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        market_data=market_data,
    )

    rejected_key = str(uuid.uuid4())
    await _d4_assert_cash_rejection_is_atomic(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        market_data=market_data,
        book_id=book_id,
        idempotency_key=rejected_key,
        trade_intent=_close_intent(
            quantity="1",
            position_id=str(position_id),
        ),
        expected_cash=Decimal("18.901"),
    )



@pytest.mark.asyncio
async def test_postgres_manual_top_up_ledger_snapshot_and_replay(
    futures_pg_sessions,
):
    user_id, provisioned = await _create_owner_account(futures_pg_sessions)
    await _d4_set_high_fee_policy(futures_pg_sessions, user_id, provisioned)

    position_id, book_id = await _d4_open_nine_one_btc_fills(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        market_data=_make_market_data(),
    )

    idempotency_key = str(uuid.uuid4())

    # Read the precondition in a separate session so the service
    # can own its transaction through session.begin().
    async with futures_pg_sessions() as session:
        before = await _snapshot_balances(session, book_id)
        assert before["cash_available"] == Decimal("18.901")

    # First top-up: this session has no prior transaction.
    async with futures_pg_sessions() as session:
        result = await portfolio_operations.manual_top_up_paper_portfolio(
            session,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=idempotency_key,
            confirmed=True,
        )

    assert result.replayed is False
    assert result.response_data["transaction_type"] == "manual_top_up"
    assert result.response_data["status"] == "posted"
    assert Decimal(result.response_data["previous_balance"]) == Decimal("18.901")
    assert Decimal(result.response_data["amount"]) == Decimal("981.099")
    assert Decimal(result.response_data["new_balance"]) == Decimal("1000")

    # Verify the committed ledger and trigger-maintained snapshots.
    async with futures_pg_sessions() as session:
        after = await _snapshot_balances(session, book_id)
        assert after["cash_available"] == Decimal("1000")
        assert after["contributed_capital"] == Decimal("1981.099")
        assert after["futures_margin_locked"] > 0

    # Replay in a fresh session; no second ledger transaction is allowed.
    async with futures_pg_sessions() as session:
        replay = await portfolio_operations.manual_top_up_paper_portfolio(
            session,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=idempotency_key,
            confirmed=True,
        )

    assert replay.replayed is True
    assert replay.response_data == result.response_data

    async with futures_pg_sessions() as session:
        transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.transaction_type == "manual_top_up",
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
                        PaperLedgerEntry.transaction_id == transactions[0].id,
                    )
                )
            ).all()
        )
        assert len(entries) == 2

        requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.scope_type == "portfolio",
                        PaperExecutionRequest.portfolio_id == provisioned.futures_portfolio_id,
                        PaperExecutionRequest.operation_type == "manual_top_up",
                    )
                )
            ).all()
        )
        assert len(requests) == 1
        assert requests[0].status == "completed"
        assert requests[0].response_data == result.response_data

        final_balances = await _snapshot_balances(session, book_id)
        assert final_balances == after

    assert position_id is not None


@pytest.mark.asyncio
async def test_postgres_manual_top_up_rolls_back_after_snapshot_posting(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account(futures_pg_sessions)
    await _d4_set_high_fee_policy(futures_pg_sessions, user_id, provisioned)

    position_id, book_id = await _d4_open_nine_one_btc_fills(
        futures_pg_sessions,
        user_id=user_id,
        provisioned=provisioned,
        market_data=_make_market_data(),
    )

    async with futures_pg_sessions() as session:
        before = await _snapshot_balances(session, book_id)
        assert before["cash_available"] == Decimal("18.901")

    original_dumps = portfolio_operations.json.dumps

    def fail_after_ledger_posting(value, *args, **kwargs):
        if (
            isinstance(value, dict)
            and "transaction_id" in value
            and "amount" in value
            and value.get("transaction_type") == "manual_top_up"
        ):
            raise ValueError("Injected response serialization failure")
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(
        portfolio_operations.json,
        "dumps",
        fail_after_ledger_posting,
    )

    with pytest.raises(portfolio_operations.PaperPortfolioOperationError):
        async with futures_pg_sessions() as session:
            await portfolio_operations.manual_top_up_paper_portfolio(
                session,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=str(uuid.uuid4()),
                confirmed=True,
            )

    # A fresh session verifies the database state after rollback.
    async with futures_pg_sessions() as session:
        after = await _snapshot_balances(session, book_id)
        assert after == before

        top_up_transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == book_id,
                        PaperLedgerTransaction.transaction_type == "manual_top_up",
                    )
                )
            ).all()
        )
        assert top_up_transactions == []

        top_up_requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.scope_type == "portfolio",
                        PaperExecutionRequest.portfolio_id == provisioned.futures_portfolio_id,
                        PaperExecutionRequest.operation_type == "manual_top_up",
                    )
                )
            ).all()
        )
        assert top_up_requests == []

    assert position_id is not None


@pytest.mark.asyncio
async def test_postgres_futures_concurrent_same_key_full_close_replays_once(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account_for_mode(
        futures_pg_sessions,
        "one_way",
    )
    market_data = _make_market_data()

    # Open a fresh 0.2 BTC LONG position.
    async with futures_pg_sessions() as session:
        opened = await futures_execution.execute_futures_trade(
            session,
            market_data=market_data,
            authenticated_user_id=user_id,
            account_id=provisioned.account_id,
            portfolio_id=provisioned.futures_portfolio_id,
            idempotency_key=str(uuid.uuid4()),
            trade_intent=_intent(quantity="0.2"),
        )

    assert opened.replayed is False
    _assert_futures_response_contract(
        opened.response_data,
        expected_action="open",
    )
    position_id = opened.response_data["position_id"]

    original_authorize = futures_execution.authorize_paper_write
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
        futures_execution,
        "authorize_paper_write",
        synchronized_authorize,
    )

    shared_key = str(uuid.uuid4())
    close_intent = _close_intent(
        quantity="0.2",
        position_id=position_id,
    )

    async def execute_close():
        async with futures_pg_sessions() as session:
            return await futures_execution.execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=shared_key,
                trade_intent=dict(close_intent),
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            execute_close(),
            execute_close(),
            return_exceptions=True,
        ),
        timeout=30,
    )

    assert all(
        not isinstance(result, BaseException)
        for result in results
    )
    assert sorted(result.replayed for result in results) == [False, True]

    original_result = next(
        result for result in results if result.replayed is False
    )
    replay_result = next(
        result for result in results if result.replayed is True
    )

    assert original_result.response_data == replay_result.response_data

    response = original_result.response_data
    _assert_futures_response_contract(
        response,
        expected_action="close",
    )

    assert response["position_id"] == position_id
    assert response["position_status"] == "closed"
    assert Decimal(response["quantity"]) == Decimal("0.2")
    assert Decimal(response["execution_price"]) == Decimal("100.00")
    assert Decimal(response["fee_original"]) == Decimal("0.02")
    assert Decimal(response["fee_posted"]) == Decimal("0.02")
    assert Decimal(response["realized_pnl"]) == Decimal("-0.002")
    assert Decimal(response["position_realized_pnl"]) == Decimal("-0.002")
    assert Decimal(response["remaining_quantity"]) == ZERO
    assert Decimal(response["remaining_margin"]) == ZERO

    async with futures_pg_sessions() as session:
        position = await session.get(
            PaperPosition,
            uuid.UUID(position_id),
        )
        assert position is not None
        assert position.status == "closed"
        assert position.closed_at is not None
        assert position.initial_quantity == Decimal("0.2")
        assert position.remaining_quantity == ZERO
        assert position.margin_locked == ZERO
        assert position.realized_pnl == Decimal("-0.002")

        balances = await _snapshot_balances(
            session,
            position.book_id,
        )
        assert balances["cash_available"] == Decimal("999.958")
        assert balances["futures_margin_locked"] == ZERO
        assert balances["fee_expense"] == Decimal("0.04")
        assert balances["realized_pnl"] == Decimal("-0.002")

        close_transactions = list(
            (
                await session.scalars(
                    select(PaperLedgerTransaction).where(
                        PaperLedgerTransaction.book_id == position.book_id,
                        PaperLedgerTransaction.transaction_type == "futures_close",
                        PaperLedgerTransaction.status == "posted",
                    )
                )
            ).all()
        )
        assert len(close_transactions) == 1
        assert (
            close_transactions[0].idempotency_key
            == futures_execution.hash_idempotency_key(shared_key)
        )

        completed_requests = list(
            (
                await session.scalars(
                    select(PaperExecutionRequest).where(
                        PaperExecutionRequest.book_id == position.book_id,
                        PaperExecutionRequest.operation_type == "trade",
                        PaperExecutionRequest.status == "completed",
                    )
                )
            ).all()
        )
        assert len(completed_requests) == 2

        matching_requests = [
            request
            for request in completed_requests
            if request.idempotency_key
            == futures_execution.hash_idempotency_key(shared_key)
        ]
        assert len(matching_requests) == 1
        assert matching_requests[0].response_data == response


@pytest.mark.asyncio
async def test_postgres_futures_concurrent_same_key_different_payload_conflicts(
    futures_pg_sessions,
    monkeypatch,
):
    user_id, provisioned = await _create_owner_account_for_mode(
        futures_pg_sessions,
        "one_way",
    )
    market_data = _make_market_data()

    original_authorize = futures_execution.authorize_paper_write
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
        futures_execution,
        "authorize_paper_write",
        synchronized_authorize,
    )

    shared_key = str(uuid.uuid4())

    async def execute_open(quantity: str):
        async with futures_pg_sessions() as session:
            return await futures_execution.execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=user_id,
                account_id=provisioned.account_id,
                portfolio_id=provisioned.futures_portfolio_id,
                idempotency_key=shared_key,
                trade_intent=_intent(quantity=quantity),
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            execute_open("0.1"),
            execute_open("0.2"),
            return_exceptions=True,
        ),
        timeout=30,
    )

    conflicts = [
        result
        for result in results
        if isinstance(result, futures_execution.PaperIdempotencyConflict)
    ]
    successes = [
        result
        for result in results
        if not isinstance(result, BaseException)
    ]
    unexpected_errors = [
        result
        for result in results
        if isinstance(result, BaseException)
        and not isinstance(
            result,
            futures_execution.PaperIdempotencyConflict,
        )
    ]

    assert unexpected_errors == []
    assert len(successes) == 1
    assert len(conflicts) == 1

    successful_result = successes[0]
    assert successful_result.replayed is False

    response = successful_result.response_data
    _assert_futures_response_contract(
        response,
        expected_action="open",
    )

    assert Decimal(response["quantity"]) in {
        Decimal("0.1"),
        Decimal("0.2"),
    }

    async with futures_pg_sessions() as session:
        position_id = uuid.UUID(response["position_id"])
        position = await session.get(PaperPosition, position_id)

        assert position is not None
        assert position.status == "open"
        assert position.initial_quantity == Decimal(response["quantity"])
        assert position.remaining_quantity == Decimal(response["quantity"])
        assert position.margin_locked == Decimal(response["remaining_margin"])

        book_id = position.book_id

        positions = list(
            (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.book_id == book_id,
                    )
                )
            ).all()
        )
        assert len(positions) == 1

        balances = await _snapshot_balances(session, book_id)
        assert balances["futures_margin_locked"] == Decimal(
            response["remaining_margin"]
        )
        assert balances["fee_expense"] == Decimal(response["fee_posted"])
        assert balances["cash_available"] == (
            Decimal("1000.0000")
            - balances["futures_margin_locked"]
            - balances["fee_expense"]
        )

        open_transactions = list(
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
        assert len(open_transactions) == 1
        assert (
            open_transactions[0].idempotency_key
            == futures_execution.hash_idempotency_key(shared_key)
        )

        completed_requests = list(
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
        assert len(completed_requests) == 1
        assert (
            completed_requests[0].idempotency_key
            == futures_execution.hash_idempotency_key(shared_key)
        )
        assert completed_requests[0].response_data == response
