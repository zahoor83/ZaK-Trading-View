from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4
from app.services.paper_futures_execution import FuturesTradeExecutionError
from sqlalchemy.exc import SQLAlchemyError
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from fastapi import FastAPI

from app.api.v1 import paper_trading
from app.market_data.service import MarketDataService
from app.security.paper_write_authorization import PaperWriteAuthorizationDenied
from app.security.session_authentication import AuthenticatedUser, get_authenticated_user
from app.services.paper_ledger_posting import PaperIdempotencyConflict


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSessionFactory:
    def __call__(self):
        return FakeSession()


class FakeDatabaseManager:
    is_initialized = True

    def session_factory(self):
        return FakeSessionFactory()


@asynccontextmanager
async def client_context(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def build_app():
    app = FastAPI()
    app.include_router(paper_trading.router)
    app.state.database_manager = FakeDatabaseManager()
    # The endpoint's orchestration function is replaced in these API-contract tests.
    app.state.market_data_service = object.__new__(MarketDataService)
    user = AuthenticatedUser(
        user_id=uuid4(),
        session_id=uuid4(),
        email="trader@example.invalid",
        email_verified=True,
        expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    app.dependency_overrides[get_authenticated_user] = lambda: user
    return app, user


@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True])
async def test_spot_trade_route_builds_exact_intent_and_returns_execution_result(
    monkeypatch,
    replayed,
):
    app, user = build_app()
    account_id = uuid4()
    portfolio_id = uuid4()
    key = str(uuid4())
    calls = {}

    async def fake_execute(session, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            replayed=replayed,
            response_data=_spot_api_response_data(),
        )

    monkeypatch.setattr(paper_trading, "execute_spot_trade", fake_execute)

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{account_id}/portfolios/{portfolio_id}/paper-trades/spot",
            headers={"Idempotency-Key": key},
            json={"symbol": "BTCUSDT", "side": "buy", "quantity": "0.001"},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["replayed"] is replayed
    assert set(response.json()) == set(_spot_api_response_data()) | {"replayed"}
    assert calls["authenticated_user_id"] == user.user_id
    assert calls["account_id"] == account_id
    assert calls["portfolio_id"] == portfolio_id
    assert calls["idempotency_key"] == key
    assert calls["trade_intent"] == {
        "position_id": None,
        "symbol": "BTCUSDT",
        "side": "buy",
        "position_side": None,
        "quantity": __import__("decimal").Decimal("0.001"),
        "order_type": "market",
        "leverage": None,
        "limit_price": None,
        "reduce_only": False,
    }



@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True])
@pytest.mark.parametrize(
    ("malformed_field", "invalid_value"),
    [
        ("quote_source", "private-invalid-quote-source"),
        ("missing_book_id", None),
    ],
)
async def test_spot_malformed_service_response_returns_generic_500(
    monkeypatch,
    replayed,
    malformed_field,
    invalid_value,
):
    app, _ = build_app()
    response_data = _spot_api_response_data()

    if malformed_field == "missing_book_id":
        response_data.pop("book_id")
    else:
        response_data[malformed_field] = invalid_value

    async def fake_execute(session, **kwargs):
        return SimpleNamespace(
            replayed=replayed,
            response_data=response_data,
        )

    monkeypatch.setattr(paper_trading, "execute_spot_trade", fake_execute)

    transport = httpx.ASGITransport(
        app=app,
        raise_app_exceptions=False,
    )

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/paper-trades/spot",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "symbol": "BTCUSDT",
                "side": "buy",
                "quantity": "0.001",
            },
        )

    assert response.status_code == 500
    assert "private-invalid-quote-source" not in response.text
    assert "ResponseValidationError" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_spot_sell_requires_position_and_quantity_is_exact_decimal_text():
    app, _ = build_app()
    async with client_context(app) as client:
        missing_position = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/paper-trades/spot",
            headers={"Idempotency-Key": str(uuid4())},
            json={"symbol": "BTCUSDT", "side": "sell", "quantity": "0.001"},
        )
        float_quantity = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/paper-trades/spot",
            headers={"Idempotency-Key": str(uuid4())},
            json={"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001},
        )
        invalid_decimal = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/paper-trades/spot",
            headers={"Idempotency-Key": str(uuid4())},
            json={"symbol": "BTCUSDT", "side": "buy", "quantity": "1e-3"},
        )
    assert missing_position.status_code == 422
    assert float_quantity.status_code == 422
    assert invalid_decimal.status_code == 422


@pytest.mark.asyncio
async def test_spot_trade_route_maps_idempotency_conflict_and_authorization_denial(monkeypatch):
    app, _ = build_app()

    async def conflict(*args, **kwargs):
        raise PaperIdempotencyConflict("private detail")

    monkeypatch.setattr(paper_trading, "execute_spot_trade", conflict)
    async with client_context(app) as client:
        conflict_response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/paper-trades/spot",
            headers={"Idempotency-Key": str(uuid4())},
            json={"symbol": "BTCUSDT", "side": "buy", "quantity": "0.001"},
        )
    assert conflict_response.status_code == 409
    assert "private detail" not in conflict_response.text

    async def denied(*args, **kwargs):
        raise PaperWriteAuthorizationDenied("private detail")

    monkeypatch.setattr(paper_trading, "execute_spot_trade", denied)
    async with client_context(app) as client:
        denied_response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/paper-trades/spot",
            headers={"Idempotency-Key": str(uuid4())},
            json={"symbol": "BTCUSDT", "side": "buy", "quantity": "0.001"},
        )
    assert denied_response.status_code == 403
    assert "private detail" not in denied_response.text


@pytest.mark.asyncio
async def test_spot_trade_route_fails_closed_when_market_data_or_database_is_missing():
    app, _ = build_app()
    app.state.market_data_service = None
    async with client_context(app) as client:
        no_market_data = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/paper-trades/spot",
            headers={"Idempotency-Key": str(uuid4())},
            json={"symbol": "BTCUSDT", "side": "buy", "quantity": "0.001"},
        )
    assert no_market_data.status_code == 503

    app.state.market_data_service = object.__new__(MarketDataService)
    app.state.database_manager = None
    async with client_context(app) as client:
        no_database = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/paper-trades/spot",
            headers={"Idempotency-Key": str(uuid4())},
            json={"symbol": "BTCUSDT", "side": "buy", "quantity": "0.001"},
        )
    assert no_database.status_code == 503


def _spot_api_response_data(**overrides):
    response_data = {
        "transaction_id": str(uuid4()),
        "book_id": str(uuid4()),
        "market_type": "spot",
        "transaction_type": "spot_buy",
        "status": "posted",
        "entry_count": 2,
        "debit_total": "10.01",
        "credit_total": "10.01",
        "position_id": str(uuid4()),
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": "0.001",
        "execution_price": "10000",
        "gross_value": "10",
        "fee_original": "0.01",
        "fee_posted": "0.01",
        "realized_pnl": "0",
        "remaining_quantity": "0.001",
        "remaining_cost_basis": "10",
        "position_status": "open",
        "quote_source": "spot_book_ticker",
        "quote_update_id": 12345,
        "fee_config_snapshot": {
            "schema_version": 1,
            "policy_id": "spot-policy-v1",
            "version": 1,
            "market_type": "spot",
            "entry_fee_rate": "0.001",
            "exit_fee_rate": "0.001",
        },
        "symbol_metadata_snapshot": {
            "schema_version": 1,
            "market": "spot",
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "order_types": ["LIMIT", "MARKET"],
            "filter_types": ["PRICE_FILTER", "LOT_SIZE"],
            "price_precision": 2,
            "quantity_precision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            ],
            "metadata_server_time_ms": 1760000000000,
            "tick_size": "0.10",
        },
    }
    response_data.update(overrides)
    return response_data


def _futures_api_response_data():
    return {
        "transaction_id": str(uuid4()),
        "book_id": str(uuid4()),
        "market_type": "futures",
        "transaction_type": "futures_open",
        "status": "posted",
        "entry_count": 2,
        "debit_total": "10.01",
        "credit_total": "10.01",
        "position_id": str(uuid4()),
        "symbol": "BTCUSDT",
        "side": "buy",
        "position_side": "long",
        "position_action": "open",
        "quantity": "0.01",
        "execution_price": "1000",
        "gross_value": "10",
        "fee_original": "0.01",
        "fee_posted": "0.01",
        "realized_pnl": "0",
        "position_realized_pnl": "0",
        "remaining_quantity": "0.01",
        "remaining_margin": "1.01",
        "entry_price": "1000",
        "leverage": "10",
        "position_status": "open",
        "quote_source": "futures_depth_book",
        "quote_update_id": 12345,
        "fee_config_snapshot": {
            "schema_version": 1,
            "policy_id": "futures-policy-v1",
            "version": 1,
            "market_type": "futures",
            "entry_fee_rate": "0.001",
            "exit_fee_rate": "0.001",
        },
        "symbol_metadata_snapshot": {
            "schema_version": 1,
            "market": "usdm_futures",
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "pair": "BTCUSDT",
            "contract_type": "PERPETUAL",
            "margin_asset": "USDT",
            "delivery_date": 4133404800000,
            "onboard_date": 1569398400000,
            "order_types": ["LIMIT", "MARKET"],
            "filter_types": ["PRICE_FILTER", "LOT_SIZE"],
            "price_precision": 2,
            "quantity_precision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            ],
            "metadata_server_time_ms": 1760000000000,
            "tick_size": "0.10",
        },
    }


def _futures_request_payload(**overrides):
    payload = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "position_side": "long",
        "quantity": "0.01",
        "leverage": "10",
        "order_type": "market",
        "reduce_only": False,
    }
    payload.update(overrides)
    return payload


def _futures_endpoint_url():
    return (
        f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}"
        "/paper-trades/futures"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True])
async def test_futures_trade_route_builds_exact_intent_and_returns_typed_response(
    monkeypatch,
    replayed,
):
    app, user = build_app()
    account_id = uuid4()
    portfolio_id = uuid4()
    key = str(uuid4())
    calls = {}
    response_data = _futures_api_response_data()

    async def fake_execute(session, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            replayed=replayed,
            response_data=response_data,
        )

    monkeypatch.setattr(paper_trading, "execute_futures_trade", fake_execute)

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{account_id}/portfolios/{portfolio_id}/paper-trades/futures",
            headers={"Idempotency-Key": key},
            json=_futures_request_payload(),
        )

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()

    assert body["replayed"] is replayed
    assert set(body) == set(response_data) | {"replayed"}
    assert body["fee_config_snapshot"] == response_data["fee_config_snapshot"]
    assert body["symbol_metadata_snapshot"] == response_data["symbol_metadata_snapshot"]

    assert calls["authenticated_user_id"] == user.user_id
    assert calls["account_id"] == account_id
    assert calls["portfolio_id"] == portfolio_id
    assert calls["idempotency_key"] == key
    assert calls["market_data"] is app.state.market_data_service
    assert calls["trade_intent"] == {
        "position_id": None,
        "symbol": "BTCUSDT",
        "side": "buy",
        "position_side": "long",
        "quantity": Decimal("0.01"),
        "order_type": "market",
        "leverage": Decimal("10"),
        "limit_price": None,
        "reduce_only": False,
    }


@pytest.mark.asyncio
async def test_futures_request_validation_rejects_invalid_semantics_and_numeric_types(
    monkeypatch,
):
    app, _ = build_app()
    called = False

    async def fake_execute(*args, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            replayed=False,
            response_data=_futures_api_response_data(),
        )

    monkeypatch.setattr(paper_trading, "execute_futures_trade", fake_execute)

    invalid_payloads = [
        _futures_request_payload(
            side="sell",
            position_side="long",
            reduce_only=False,
        ),
        _futures_request_payload(
            side="sell",
            position_side="long",
            reduce_only=True,
        ),
        _futures_request_payload(quantity=0.01),
        _futures_request_payload(quantity="1e-2"),
        _futures_request_payload(leverage="0"),
        _futures_request_payload(unexpected_field="forbidden"),
    ]

    async with client_context(app) as client:
        responses = []
        for payload in invalid_payloads:
            responses.append(
                await client.post(
                    _futures_endpoint_url(),
                    headers={"Idempotency-Key": str(uuid4())},
                    json=payload,
                )
            )

    assert all(response.status_code == 422 for response in responses)
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_factory", "expected_status"),
    [
        (lambda: PaperIdempotencyConflict("private detail"), 409),
        (lambda: PaperWriteAuthorizationDenied("private detail"), 403),
        (lambda: FuturesTradeExecutionError("private detail"), 409),
        (lambda: SQLAlchemyError("private detail"), 503),
        (lambda: RuntimeError("private detail"), 503),
    ],
)
async def test_futures_trade_route_maps_service_errors_without_leaking_details(
    monkeypatch,
    exception_factory,
    expected_status,
):
    app, _ = build_app()

    async def failing_execute(*args, **kwargs):
        raise exception_factory()

    monkeypatch.setattr(
        paper_trading,
        "execute_futures_trade",
        failing_execute,
    )

    async with client_context(app) as client:
        response = await client.post(
            _futures_endpoint_url(),
            headers={"Idempotency-Key": str(uuid4())},
            json=_futures_request_payload(),
        )

    assert response.status_code == expected_status
    assert "private detail" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_futures_trade_route_fails_closed_when_market_data_or_database_is_missing(
    monkeypatch,
):
    app, _ = build_app()
    called = False

    async def fake_execute(*args, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            replayed=False,
            response_data=_futures_api_response_data(),
        )

    monkeypatch.setattr(paper_trading, "execute_futures_trade", fake_execute)

    app.state.market_data_service = None
    async with client_context(app) as client:
        no_market_data = await client.post(
            _futures_endpoint_url(),
            headers={"Idempotency-Key": str(uuid4())},
            json=_futures_request_payload(),
        )

    assert no_market_data.status_code == 503

    app.state.market_data_service = object.__new__(MarketDataService)
    app.state.database_manager = None

    async with client_context(app) as client:
        no_database = await client.post(
            _futures_endpoint_url(),
            headers={"Idempotency-Key": str(uuid4())},
            json=_futures_request_payload(),
        )

    assert no_database.status_code == 503
    assert called is False



@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("transaction_type", "futures_increase"),
        ("transaction_type", "futures_reduce"),
        ("status", "draft"),
        ("status", "completed"),
        ("quote_source", "mark_price"),
        ("quote_update_id", 0),
        ("quote_update_id", -1),
    ],
)
def test_futures_response_model_rejects_invalid_service_contract_values(
    field,
    invalid_value,
):
    response_data = {
        **_futures_api_response_data(),
        "replayed": False,
    }
    response_data[field] = invalid_value

    with pytest.raises(ValidationError):
        paper_trading.FuturesPaperTradeResponse.model_validate(response_data)


def test_futures_response_model_accepts_verified_open_and_close_envelopes():
    for transaction_type in ("futures_open", "futures_close"):
        response_data = {
            **_futures_api_response_data(),
            "replayed": False,
            "transaction_type": transaction_type,
            "status": "posted",
            "quote_source": "futures_depth_book",
            "quote_update_id": 123456,
        }
        validated = paper_trading.FuturesPaperTradeResponse.model_validate(
            response_data
        )
        assert validated.transaction_type == transaction_type
        assert validated.status == "posted"
        assert validated.quote_source == "futures_depth_book"
        assert validated.quote_update_id == 123456

@pytest.mark.asyncio
async def test_futures_route_rejects_missing_idempotency_key(monkeypatch):
    app, _ = build_app()
    called = False

    async def fake_execute(*args, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            replayed=False,
            response_data=_futures_api_response_data(),
        )

    monkeypatch.setattr(paper_trading, "execute_futures_trade", fake_execute)

    async with client_context(app) as client:
        response = await client.post(
            _futures_endpoint_url(),
            json=_futures_request_payload(),
        )

    assert response.status_code == 422
    assert called is False


@pytest.mark.asyncio
async def test_futures_route_fails_closed_when_database_is_uninitialized(
    monkeypatch,
):
    app, _ = build_app()
    app.state.database_manager = SimpleNamespace(is_initialized=False)
    called = False

    async def fake_execute(*args, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            replayed=False,
            response_data=_futures_api_response_data(),
        )

    monkeypatch.setattr(paper_trading, "execute_futures_trade", fake_execute)

    async with client_context(app) as client:
        response = await client.post(
            _futures_endpoint_url(),
            headers={"Idempotency-Key": str(uuid4())},
            json=_futures_request_payload(),
        )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert called is False


@pytest.mark.asyncio
async def test_futures_route_rejects_unauthenticated_request(monkeypatch):
    app, _ = build_app()
    app.dependency_overrides.pop(get_authenticated_user)
    called = False

    async def fake_execute(*args, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            replayed=False,
            response_data=_futures_api_response_data(),
        )

    monkeypatch.setattr(paper_trading, "execute_futures_trade", fake_execute)

    async with client_context(app) as client:
        response = await client.post(
            _futures_endpoint_url(),
            headers={"Idempotency-Key": str(uuid4())},
            json=_futures_request_payload(),
        )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert called is False


def test_spot_response_model_accepts_buy_and_sell_envelopes():
    for transaction_type, side in (
        ("spot_buy", "buy"),
        ("spot_sell", "sell"),
    ):
        response_data = {
            **_spot_api_response_data(),
            "transaction_type": transaction_type,
            "side": side,
            "replayed": False,
        }

        validated = paper_trading.SpotPaperTradeResponse.model_validate(
            response_data
        )

        assert validated.transaction_type == transaction_type
        assert validated.side == side
        assert validated.market_type == "spot"
        assert validated.status == "posted"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("market_type", "futures"),
        ("transaction_type", "futures_open"),
        ("status", "pending"),
        ("quote_source", "futures_depth_book"),
        ("quote_update_id", 0),
        ("entry_count", 0),
    ],
)
def test_spot_response_model_rejects_invalid_service_contract_values(
    field,
    invalid_value,
):
    response_data = {
        **_spot_api_response_data(),
        "replayed": False,
    }
    response_data[field] = invalid_value

    with pytest.raises(ValidationError):
        paper_trading.SpotPaperTradeResponse.model_validate(response_data)


@pytest.mark.parametrize(
    ("snapshot_name", "field", "invalid_value"),
    [
        ("fee_config_snapshot", "market_type", "futures"),
        ("symbol_metadata_snapshot", "market", "usdm_futures"),
    ],
)
def test_spot_response_model_rejects_wrong_nested_market_snapshots(
    snapshot_name,
    field,
    invalid_value,
):
    response_data = {
        **_spot_api_response_data(),
        "replayed": False,
    }
    response_data[snapshot_name] = {
        **response_data[snapshot_name],
        field: invalid_value,
    }

    with pytest.raises(ValidationError):
        paper_trading.SpotPaperTradeResponse.model_validate(response_data)


@pytest.mark.parametrize(
    ("snapshot_name", "extra_field"),
    [
        ("fee_config_snapshot", "unexpected"),
        ("symbol_metadata_snapshot", "unexpected"),
    ],
)
def test_spot_response_model_rejects_extra_nested_snapshot_fields(
    snapshot_name,
    extra_field,
):
    response_data = {
        **_spot_api_response_data(),
        "replayed": False,
    }
    response_data[snapshot_name] = {
        **response_data[snapshot_name],
        extra_field: "not-allowed",
    }

    with pytest.raises(ValidationError):
        paper_trading.SpotPaperTradeResponse.model_validate(response_data)


def test_spot_response_model_rejects_missing_and_extra_top_level_fields():
    valid_response = {
        **_spot_api_response_data(),
        "replayed": False,
    }

    missing_field = dict(valid_response)
    missing_field.pop("book_id")

    with pytest.raises(ValidationError):
        paper_trading.SpotPaperTradeResponse.model_validate(missing_field)

    extra_field = {
        **valid_response,
        "unexpected": "not-allowed",
    }

    with pytest.raises(ValidationError):
        paper_trading.SpotPaperTradeResponse.model_validate(extra_field)



@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True])
async def test_manual_top_up_route_returns_typed_response_and_forwards_confirmation(
    monkeypatch,
    replayed,
):
    app, user = build_app()
    account_id = uuid4()
    portfolio_id = uuid4()
    key = str(uuid4())
    calls = {}

    response_data = {
        "transaction_id": str(uuid4()),
        "book_id": str(uuid4()),
        "market_type": "futures",
        "transaction_type": "manual_top_up",
        "status": "posted",
        "entry_count": 2,
        "debit_total": "981.099",
        "credit_total": "981.099",
        "amount": "981.099",
        "previous_balance": "18.901",
        "new_balance": "1000.000",
    }

    async def fake_top_up(session, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(replayed=replayed, response_data=response_data)

    monkeypatch.setattr(
        paper_trading,
        "manual_top_up_paper_portfolio",
        fake_top_up,
    )

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{account_id}/portfolios/{portfolio_id}/manual-top-up",
            headers={"Idempotency-Key": key},
            json={"confirmed": True},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["replayed"] is replayed
    assert calls == {
        "authenticated_user_id": user.user_id,
        "account_id": account_id,
        "portfolio_id": portfolio_id,
        "idempotency_key": key,
        "confirmed": True,
    }


@pytest.mark.asyncio
async def test_manual_top_up_route_rejects_non_boolean_confirmation(monkeypatch):
    app, _ = build_app()
    called = False

    async def fake_top_up(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Service must not run for invalid confirmation.")

    monkeypatch.setattr(
        paper_trading,
        "manual_top_up_paper_portfolio",
        fake_top_up,
    )

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/manual-top-up",
            headers={"Idempotency-Key": str(uuid4())},
            json={"confirmed": "true"},
        )

    assert response.status_code == 422
    assert called is False


@pytest.mark.asyncio
async def test_manual_top_up_route_maps_authorization_denial_without_leaking_details(
    monkeypatch,
):
    app, _ = build_app()

    async def denied(*args, **kwargs):
        raise paper_trading.PaperPortfolioAuthorizationDenied("private detail")

    monkeypatch.setattr(
        paper_trading,
        "manual_top_up_paper_portfolio",
        denied,
    )

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/manual-top-up",
            headers={"Idempotency-Key": str(uuid4())},
            json={"confirmed": True},
        )

    assert response.status_code == 403
    assert "private detail" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_manual_top_up_route_maps_idempotency_conflict_without_leaking_details(
    monkeypatch,
):
    app, _ = build_app()

    async def conflict(*args, **kwargs):
        raise paper_trading.PaperPortfolioIdempotencyConflict("private detail")

    monkeypatch.setattr(
        paper_trading,
        "manual_top_up_paper_portfolio",
        conflict,
    )

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/manual-top-up",
            headers={"Idempotency-Key": str(uuid4())},
            json={"confirmed": True},
        )

    assert response.status_code == 409
    assert "private detail" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_manual_top_up_route_fails_closed_when_database_is_uninitialized(
    monkeypatch,
):
    app, _ = build_app()
    app.state.database_manager = SimpleNamespace(is_initialized=False)
    called = False

    async def fake_top_up(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Service must not run without initialized database.")

    monkeypatch.setattr(
        paper_trading,
        "manual_top_up_paper_portfolio",
        fake_top_up,
    )

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/manual-top-up",
            headers={"Idempotency-Key": str(uuid4())},
            json={"confirmed": True},
        )

    assert response.status_code == 503
    assert called is False
    assert response.headers["cache-control"] == "no-store"

def _paper_reset_api_response_data(account_id, portfolio_id):
    return {
        "operation_type": "reset",
        "status": "completed",
        "account_id": str(account_id),
        "portfolio_id": str(portfolio_id),
        "market_type": "futures",
        "previous_session_id": str(uuid4()),
        "previous_session_number": 1,
        "previous_book_id": str(uuid4()),
        "previous_session_archived_at": "2026-11-22T12:00:00+00:00",
        "new_session_id": str(uuid4()),
        "new_session_number": 2,
        "new_book_id": str(uuid4()),
        "initial_balance": "1000.00",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True])
async def test_reset_route_returns_typed_response_and_forwards_exact_confirmation(
    monkeypatch,
    replayed,
):
    app, user = build_app()
    account_id = uuid4()
    portfolio_id = uuid4()
    key = str(uuid4())
    response_data = _paper_reset_api_response_data(account_id, portfolio_id)
    calls = {}

    async def fake_reset(session, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(replayed=replayed, response_data=response_data)

    monkeypatch.setattr(paper_trading, "reset_paper_portfolio", fake_reset)

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{account_id}/portfolios/{portfolio_id}/reset",
            headers={"Idempotency-Key": key},
            json={"confirmed": True},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {**response_data, "replayed": replayed}
    assert calls == {
        "authenticated_user_id": user.user_id,
        "account_id": account_id,
        "portfolio_id": portfolio_id,
        "idempotency_key": key,
        "confirmed": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_confirmation", ["true", 1])
async def test_reset_route_rejects_non_boolean_confirmation(
    monkeypatch,
    invalid_confirmation,
):
    app, _ = build_app()
    called = False

    async def fake_reset(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Service must not run for invalid confirmation.")

    monkeypatch.setattr(paper_trading, "reset_paper_portfolio", fake_reset)

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/reset",
            headers={"Idempotency-Key": str(uuid4())},
            json={"confirmed": invalid_confirmation},
        )

    assert response.status_code == 422
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected_status"),
    [
        (paper_trading.PaperPortfolioResetAuthorizationDenied, 403),
        (paper_trading.PaperPortfolioResetIdempotencyConflict, 409),
    ],
)
async def test_reset_route_maps_service_errors_without_leaking_details(
    monkeypatch,
    error_type,
    expected_status,
):
    app, _ = build_app()

    async def fail_reset(*args, **kwargs):
        raise error_type("private detail must not be exposed")

    monkeypatch.setattr(paper_trading, "reset_paper_portfolio", fail_reset)

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/reset",
            headers={"Idempotency-Key": str(uuid4())},
            json={"confirmed": True},
        )

    assert response.status_code == expected_status
    assert "private detail" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_reset_route_maps_general_reset_error_without_leaking_details(
    monkeypatch,
):
    app, _ = build_app()

    async def fail_reset(*args, **kwargs):
        raise paper_trading.PaperPortfolioResetError("private reset detail")

    monkeypatch.setattr(paper_trading, "reset_paper_portfolio", fail_reset)

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/reset",
            headers={"Idempotency-Key": str(uuid4())},
            json={"confirmed": True},
        )

    assert response.status_code == 409
    assert "private reset detail" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_reset_route_maps_unexpected_runtime_error_to_service_unavailable(
    monkeypatch,
):
    app, _ = build_app()

    async def fail_reset(*args, **kwargs):
        raise RuntimeError("private runtime detail")

    monkeypatch.setattr(paper_trading, "reset_paper_portfolio", fail_reset)

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/reset",
            headers={"Idempotency-Key": str(uuid4())},
            json={"confirmed": True},
        )

    assert response.status_code == 503
    assert "private runtime detail" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_reset_route_fails_closed_when_database_is_uninitialized(
    monkeypatch,
):
    app, _ = build_app()
    app.state.database_manager = SimpleNamespace(is_initialized=False)
    called = False

    async def fake_reset(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Service must not run without initialized database.")

    monkeypatch.setattr(paper_trading, "reset_paper_portfolio", fake_reset)

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/reset",
            headers={"Idempotency-Key": str(uuid4())},
            json={"confirmed": True},
        )

    assert response.status_code == 503
    assert called is False
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_reset_route_rejects_missing_idempotency_key(monkeypatch):
    app, _ = build_app()
    called = False

    async def fake_reset(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Service must not run without idempotency key.")

    monkeypatch.setattr(paper_trading, "reset_paper_portfolio", fake_reset)

    async with client_context(app) as client:
        response = await client.post(
            f"/api/v1/accounts/{uuid4()}/portfolios/{uuid4()}/reset",
            json={"confirmed": True},
        )

    assert response.status_code == 422
    assert called is False

