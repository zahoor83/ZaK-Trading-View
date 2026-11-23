from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1 import paper_trading
from app.security.session_authentication import AuthenticatedUser, get_authenticated_user
from app.security.resource_authorization import PortfolioAccessDenied
from app.services.paper_ledger_validation import PaperLedgerValidationError
from app.services.paper_trading_bootstrap import (
    PaperTradingAccountNotFound,
    PaperTradingBootstrapIntegrityError,
)


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
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client


def build_app(*, authenticated: bool = True):
    app = FastAPI()
    app.include_router(paper_trading.router)
    app.state.database_manager = FakeDatabaseManager()
    user = AuthenticatedUser(
        user_id=uuid4(),
        session_id=uuid4(),
        email="owner@example.invalid",
        email_verified=True,
        expires_at=datetime.now(timezone.utc),
    )
    if authenticated:
        app.dependency_overrides[get_authenticated_user] = lambda: user
    return app, user


def valid_payload():
    return {
        "account": {
            "id": str(uuid4()),
            "name": "Primary Account",
            "role": "owner",
        },
        "portfolios": [
            {
                "id": str(uuid4()),
                "market_type": "spot",
                "status": "active",
                "position_mode": None,
                "session": {
                    "id": str(uuid4()),
                    "session_number": 1,
                    "status": "active",
                },
                "ledger": {
                    "book_id": str(uuid4()),
                    "cash_available": "1000.00",
                    "spot_position_cost": "0",
                    "futures_margin_locked": "0",
                    "realized_pnl": "0",
                    "fee_expense": "0",
                    "contributed_capital": "1000.00",
                },
                "positions": [
                    {
                        "id": str(uuid4()),
                        "symbol": "BTCUSDT",
                        "side": "long",
                        "status": "open",
                        "leverage": None,
                        "initial_quantity": "0.0100",
                        "remaining_quantity": "0.0100",
                        "entry_price": "65000.00",
                        "remaining_cost_basis": "650.00",
                        "margin_locked": None,
                        "realized_pnl": "0",
                        "entry_fee_original": "0.650000",
                        "entry_fee_posted": "0.65",
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "closed_at": None,
                    }
                ],
            },
            {
                "id": str(uuid4()),
                "market_type": "futures",
                "status": "active",
                "position_mode": "hedge",
                "session": {
                    "id": str(uuid4()),
                    "session_number": 1,
                    "status": "active",
                },
                "ledger": {
                    "book_id": str(uuid4()),
                    "cash_available": "1000.00",
                    "spot_position_cost": "0",
                    "futures_margin_locked": "0",
                    "realized_pnl": "0",
                    "fee_expense": "0",
                    "contributed_capital": "1000.00",
                },
                "positions": [
                    {
                        "id": str(uuid4()),
                        "symbol": "BTCUSDT",
                        "side": "long",
                        "status": "open",
                        "leverage": "10",
                        "initial_quantity": "0.0100",
                        "remaining_quantity": "0.0100",
                        "entry_price": "65000.00",
                        "remaining_cost_basis": None,
                        "margin_locked": "65.00",
                        "realized_pnl": "0",
                        "entry_fee_original": "0.650000",
                        "entry_fee_posted": "0.65",
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "closed_at": None,
                    }
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_bootstrap_returns_authenticated_read_only_snapshot_and_no_store(monkeypatch):
    app, user = build_app()
    payload = valid_payload()
    calls = {}

    async def fake_loader(session, *, authenticated_user_id):
        calls["session"] = session
        calls["user_id"] = authenticated_user_id
        return payload

    monkeypatch.setattr(
        paper_trading,
        "load_paper_trading_bootstrap",
        fake_loader,
    )

    async with client_context(app) as client:
        response = await client.get("/api/v1/paper-trading/bootstrap")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json() == payload
    assert calls["user_id"] == user.user_id
    assert calls["session"] is not None
    assert response.json()["portfolios"][0]["ledger"]["cash_available"] == "1000.00"
    assert response.json()["portfolios"][0]["positions"][0]["leverage"] is None
    assert response.json()["portfolios"][1]["positions"][0]["leverage"] == "10"
    assert response.json()["portfolios"][0]["positions"][0]["remaining_cost_basis"] == "650.00"
    assert response.json()["portfolios"][1]["positions"][0]["remaining_cost_basis"] is None
    assert response.json()["portfolios"][1]["positions"][0]["margin_locked"] == "65.00"


@pytest.mark.asyncio
async def test_bootstrap_rejects_unauthenticated_request():
    app, _ = build_app(authenticated=False)

    async with client_context(app) as client:
        response = await client.get("/api/v1/paper-trading/bootstrap")

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_bootstrap_hides_missing_or_unauthorized_account(monkeypatch):
    app, _ = build_app()

    async def missing(*args, **kwargs):
        raise PaperTradingAccountNotFound()

    monkeypatch.setattr(paper_trading, "load_paper_trading_bootstrap", missing)
    async with client_context(app) as client:
        missing_response = await client.get("/api/v1/paper-trading/bootstrap")
    assert missing_response.status_code == 404
    assert missing_response.headers["cache-control"] == "no-store"

    async def unauthorized(*args, **kwargs):
        raise PortfolioAccessDenied("sensitive resource detail")

    monkeypatch.setattr(paper_trading, "load_paper_trading_bootstrap", unauthorized)
    async with client_context(app) as client:
        unauthorized_response = await client.get("/api/v1/paper-trading/bootstrap")
    assert unauthorized_response.status_code == 404
    assert "sensitive resource detail" not in unauthorized_response.text


@pytest.mark.asyncio
async def test_bootstrap_fails_closed_for_invalid_ledger_or_inconsistent_records(monkeypatch):
    app, _ = build_app()

    async def invalid_ledger(*args, **kwargs):
        raise PaperLedgerValidationError("private ledger detail")

    monkeypatch.setattr(paper_trading, "load_paper_trading_bootstrap", invalid_ledger)
    async with client_context(app) as client:
        ledger_response = await client.get("/api/v1/paper-trading/bootstrap")
    assert ledger_response.status_code == 409
    assert "private ledger detail" not in ledger_response.text
    assert ledger_response.headers["cache-control"] == "no-store"

    async def inconsistent(*args, **kwargs):
        raise PaperTradingBootstrapIntegrityError("private consistency detail")

    monkeypatch.setattr(paper_trading, "load_paper_trading_bootstrap", inconsistent)
    async with client_context(app) as client:
        inconsistent_response = await client.get("/api/v1/paper-trading/bootstrap")
    assert inconsistent_response.status_code == 409
    assert "private consistency detail" not in inconsistent_response.text


@pytest.mark.asyncio
async def test_bootstrap_maps_database_failure_and_missing_database(monkeypatch):
    app, _ = build_app()

    async def database_failure(*args, **kwargs):
        raise SQLAlchemyError("private database detail")

    monkeypatch.setattr(paper_trading, "load_paper_trading_bootstrap", database_failure)
    async with client_context(app) as client:
        response = await client.get("/api/v1/paper-trading/bootstrap")
    assert response.status_code == 503
    assert "private database detail" not in response.text

    app.state.database_manager = None
    async with client_context(app) as client:
        unavailable = await client.get("/api/v1/paper-trading/bootstrap")
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"
