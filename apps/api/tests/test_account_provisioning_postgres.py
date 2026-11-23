from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.identity import AccountMembership, TradingAccount, User
from app.models.portfolio import PaperPortfolio, PaperSession
from app.services.account_service import (
    ensure_default_account_with_initial_portfolios,
)


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
async def account_pg_sessions():
    engine = create_async_engine(
        _validated_test_url(),
        poolclass=NullPool,
        pool_pre_ping=True,
    )

    factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )

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


async def _provision_default(factory, user_id):
    async with factory() as session:
        return await ensure_default_account_with_initial_portfolios(
            session,
            owner_user_id=user_id,
        )


@pytest.mark.asyncio
async def test_postgres_concurrent_default_account_provisioning_is_serialized(
    account_pg_sessions,
):
    unique = uuid.uuid4().hex
    email = f"account-provisioning-{unique}@example.test"

    async with account_pg_sessions() as session:
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

    ready_count = 0
    start_gate = asyncio.Event()

    async def synchronized_provision():
        nonlocal ready_count

        ready_count += 1

        if ready_count == 2:
            start_gate.set()

        await asyncio.wait_for(start_gate.wait(), timeout=10)

        return await _provision_default(
            account_pg_sessions,
            user_id,
        )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                synchronized_provision(),
                synchronized_provision(),
            ),
            timeout=30,
        )

        first, second = results

        assert first.account_id == second.account_id
        assert first.spot_portfolio_id == second.spot_portfolio_id
        assert first.futures_portfolio_id == second.futures_portfolio_id
        assert first.spot_session_id == second.spot_session_id
        assert first.futures_session_id == second.futures_session_id
        assert first.futures_position_mode == "hedge"
        assert second.futures_position_mode == "hedge"

        async with account_pg_sessions() as session:
            account_count = await session.scalar(
                select(func.count())
                .select_from(TradingAccount)
                .where(TradingAccount.owner_user_id == user_id)
            )

            assert account_count == 1

            account = await session.get(
                TradingAccount,
                first.account_id,
            )

            assert account is not None
            assert account.name == "Primary Account"
            assert account.status == "active"

            membership_count = await session.scalar(
                select(func.count())
                .select_from(AccountMembership)
                .where(
                    AccountMembership.account_id == first.account_id,
                    AccountMembership.user_id == user_id,
                    AccountMembership.role == "owner",
                    AccountMembership.status == "active",
                )
            )

            assert membership_count == 1

            portfolios = list(
                (
                    await session.scalars(
                        select(PaperPortfolio).where(
                            PaperPortfolio.account_id == first.account_id
                        )
                    )
                ).all()
            )

            assert len(portfolios) == 2

            by_market = {
                portfolio.market_type: portfolio
                for portfolio in portfolios
            }

            assert set(by_market) == {"spot", "futures"}
            assert by_market["spot"].position_mode is None
            assert by_market["spot"].status == "active"
            assert by_market["futures"].position_mode == "hedge"
            assert by_market["futures"].status == "active"

            sessions = list(
                (
                    await session.scalars(
                        select(PaperSession).where(
                            PaperSession.account_id == first.account_id
                        )
                    )
                ).all()
            )

            assert len(sessions) == 2
            assert all(item.status == "active" for item in sessions)
            assert all(item.session_number == 1 for item in sessions)
            assert {
                item.portfolio_id for item in sessions
            } == {
                first.spot_portfolio_id,
                first.futures_portfolio_id,
            }

    finally:
        # Immutable paper-ledger records reference this account with ON DELETE RESTRICT.
        # Keep uniquely identified integration-test records for auditability.
        # Cleanup belongs to isolated test-database teardown, not account deletion.
        pass