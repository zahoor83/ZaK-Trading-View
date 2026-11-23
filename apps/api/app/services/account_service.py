"""Atomic account and initial paper-portfolio creation service."""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import (
    AccountMembership,
    TradingAccount,
    User,
)
from app.models.portfolio import PaperPortfolio, PaperSession
from app.services.paper_ledger_provisioning import (
    provision_initial_paper_ledger,
)
from app.services.paper_ledger_validation import (
    validate_existing_paper_ledger,
)


class AccountCreationError(Exception):
    """Base exception for account creation failures."""


class InvalidAccountNameError(AccountCreationError, ValueError):
    """The supplied account name is invalid."""


class InvalidFuturesPositionModeError(AccountCreationError, ValueError):
    """The Futures position mode is missing or unsupported."""


class UserNotFoundError(AccountCreationError):
    """The authenticated user does not exist."""


class UserNotEligibleError(AccountCreationError):
    """The user is not active and email verified."""


class TransactionOwnershipError(AccountCreationError):
    """The supplied session already has an active transaction."""


@dataclass(frozen=True, slots=True)
class AccountCreationResult:
    account_id: uuid.UUID
    spot_portfolio_id: uuid.UUID
    futures_portfolio_id: uuid.UUID
    spot_session_id: uuid.UUID
    futures_session_id: uuid.UUID
    futures_position_mode: str


def _validate_inputs(
    account_name: str,
    futures_position_mode: str,
) -> tuple[str, str]:
    if not isinstance(account_name, str):
        raise InvalidAccountNameError("Account name must be text.")

    normalized_name = account_name.strip()

    if (
        not normalized_name
        or len(normalized_name) > 120
        or any(unicodedata.category(char) == "Cc" for char in normalized_name)
    ):
        raise InvalidAccountNameError(
            "Account name must contain 1-120 valid characters."
        )

    if futures_position_mode not in {"one_way", "hedge"}:
        raise InvalidFuturesPositionModeError(
            "Futures position mode must be 'one_way' or 'hedge'."
        )

    return normalized_name, futures_position_mode


async def create_account_with_initial_portfolios(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    account_name: str,
    futures_position_mode: str,
) -> AccountCreationResult:
    """Create an account, its Owner membership, two portfolios and sessions.

    The caller must supply a fresh AsyncSession with no active transaction.
    This service owns the transaction and commits all records together.
    Any failure rolls back the entire operation.

    The owner must already exist, be active and have a verified email.
    Independent Spot and Futures paper ledgers are provisioned atomically.
    No real funds or exchange orders are created.
    """

    if not isinstance(owner_user_id, uuid.UUID):
        raise ValueError("owner_user_id must be a UUID.")

    normalized_name, position_mode = _validate_inputs(
        account_name,
        futures_position_mode,
    )

    if session.in_transaction():
        raise TransactionOwnershipError(
            "Account creation requires a session with no active transaction."
        )

    async with session.begin():
        user_result = await session.execute(
            select(User)
            .where(User.id == owner_user_id)
            .with_for_update()
        )

        owner = user_result.scalar_one_or_none()

        if owner is None:
            raise UserNotFoundError("The owner user does not exist.")

        if owner.status != "active" or owner.email_verified is not True:
            raise UserNotEligibleError(
                "Account creation requires an active, email-verified user."
            )

        account = TradingAccount(
            owner_user_id=owner_user_id,
            name=normalized_name,
            status="active",
        )
        session.add(account)

        # Obtain the database-generated account UUID.
        await session.flush()

        membership = AccountMembership(
            account_id=account.id,
            user_id=owner_user_id,
            role="owner",
            status="active",
        )

        spot_portfolio = PaperPortfolio(
            account_id=account.id,
            market_type="spot",
            position_mode=None,
            status="active",
        )

        futures_portfolio = PaperPortfolio(
            account_id=account.id,
            market_type="futures",
            position_mode=position_mode,
            status="active",
        )

        session.add(membership)
        session.add(spot_portfolio)
        session.add(futures_portfolio)

        # Obtain portfolio UUIDs before creating their sessions.
        await session.flush()

        spot_session = PaperSession(
            account_id=account.id,
            portfolio_id=spot_portfolio.id,
            session_number=1,
            status="active",
        )

        futures_session = PaperSession(
            account_id=account.id,
            portfolio_id=futures_portfolio.id,
            session_number=1,
            status="active",
        )

        session.add(spot_session)
        session.add(futures_session)

        await session.flush()

        await provision_initial_paper_ledger(
            session,
            account_id=account.id,
            portfolio_id=spot_portfolio.id,
            session_id=spot_session.id,
            market_type="spot",
        )
        await provision_initial_paper_ledger(
            session,
            account_id=account.id,
            portfolio_id=futures_portfolio.id,
            session_id=futures_session.id,
            market_type="futures",
        )

        result = AccountCreationResult(
            account_id=account.id,
            spot_portfolio_id=spot_portfolio.id,
            futures_portfolio_id=futures_portfolio.id,
            spot_session_id=spot_session.id,
            futures_session_id=futures_session.id,
            futures_position_mode=position_mode,
        )

    # Exiting session.begin() successfully commits the complete transaction.
    return result

class AccountProvisioningStateError(AccountCreationError):
    """Existing account state is ambiguous or incomplete; no repair was attempted."""


async def ensure_default_account_with_initial_portfolios(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
) -> AccountCreationResult:
    """Return the user's sole valid account or atomically create the default one.

    The user row is locked before inspecting account state. All account-creation
    paths must use the same user-row lock protocol for this to serialize safely.

    Existing account structures are validated, never repaired or rewritten.
    New default accounts use Futures hedge mode.
    """
    if not isinstance(owner_user_id, uuid.UUID):
        raise ValueError("owner_user_id must be a UUID.")

    if session.in_transaction():
        raise TransactionOwnershipError(
            "Default account provisioning requires a session with no active transaction."
        )

    async with session.begin():
        user_result = await session.execute(
            select(User)
            .where(User.id == owner_user_id)
            .with_for_update()
        )
        owner = user_result.scalar_one_or_none()

        if owner is None:
            raise UserNotFoundError("The owner user does not exist.")
        if owner.status != "active" or owner.email_verified is not True:
            raise UserNotEligibleError(
                "Default account provisioning requires an active, email-verified user."
            )

        owner_membership_result = await session.execute(
            select(AccountMembership).where(
                AccountMembership.user_id == owner_user_id,
                AccountMembership.role == "owner",
                AccountMembership.status == "active",
            )
        )
        owner_memberships = list(
            owner_membership_result.scalars().all()
        )

        if len(owner_memberships) > 1:
            raise AccountProvisioningStateError(
                "Multiple active owner memberships exist; manual review is required."
            )

        membership_account = None
        if owner_memberships:
            membership_account_result = await session.execute(
                select(TradingAccount).where(
                    TradingAccount.id == owner_memberships[0].account_id
                )
            )
            membership_account = (
                membership_account_result.scalar_one_or_none()
            )
            if membership_account is None:
                raise AccountProvisioningStateError(
                    "An active owner membership references a missing account."
                )

        account_result = await session.execute(
            select(TradingAccount).where(
                TradingAccount.owner_user_id == owner_user_id
            )
        )
        accounts_by_id = {
            item.id: item
            for item in account_result.scalars().all()
        }

        if membership_account is not None:
            accounts_by_id[membership_account.id] = membership_account

        accounts = list(accounts_by_id.values())

        if len(accounts) > 1:
            raise AccountProvisioningStateError(
                "Multiple owned accounts exist; manual review is required."
            )

        if not accounts:
            account = TradingAccount(
                owner_user_id=owner_user_id,
                name="Primary Account",
                status="active",
            )
            session.add(account)
            await session.flush()

            membership = AccountMembership(
                account_id=account.id,
                user_id=owner_user_id,
                role="owner",
                status="active",
            )
            spot = PaperPortfolio(
                account_id=account.id,
                market_type="spot",
                position_mode=None,
                status="active",
            )
            futures = PaperPortfolio(
                account_id=account.id,
                market_type="futures",
                position_mode="hedge",
                status="active",
            )
            session.add(membership)
            session.add(spot)
            session.add(futures)
            await session.flush()

            spot_session = PaperSession(
                account_id=account.id,
                portfolio_id=spot.id,
                session_number=1,
                status="active",
            )
            futures_session = PaperSession(
                account_id=account.id,
                portfolio_id=futures.id,
                session_number=1,
                status="active",
            )
            session.add(spot_session)
            session.add(futures_session)
            await session.flush()

            await provision_initial_paper_ledger(
                session,
                account_id=account.id,
                portfolio_id=spot.id,
                session_id=spot_session.id,
                market_type="spot",
            )
            await provision_initial_paper_ledger(
                session,
                account_id=account.id,
                portfolio_id=futures.id,
                session_id=futures_session.id,
                market_type="futures",
            )

            result = AccountCreationResult(
                account_id=account.id,
                spot_portfolio_id=spot.id,
                futures_portfolio_id=futures.id,
                spot_session_id=spot_session.id,
                futures_session_id=futures_session.id,
                futures_position_mode="hedge",
            )
        else:
            account = accounts[0]
            if account.status != "active" or account.owner_user_id != owner_user_id:
                raise AccountProvisioningStateError(
                    "The existing account is not in a valid active-owner state."
                )

            membership_result = await session.execute(
                select(AccountMembership).where(
                    AccountMembership.account_id == account.id
                )
            )
            memberships = list(membership_result.scalars().all())
            active_owners = [
                item for item in memberships
                if item.role == "owner" and item.status == "active"
            ]
            if (
                len(active_owners) != 1
                or active_owners[0].user_id != owner_user_id
            ):
                raise AccountProvisioningStateError(
                    "The existing account has an invalid owner membership."
                )

            portfolio_result = await session.execute(
                select(PaperPortfolio).where(
                    PaperPortfolio.account_id == account.id
                )
            )
            portfolios = list(portfolio_result.scalars().all())

            if len(portfolios) != 2:
                raise AccountProvisioningStateError(
                    "The existing account must have exactly two portfolios."
                )

            by_market = {item.market_type: item for item in portfolios}
            if set(by_market) != {"spot", "futures"}:
                raise AccountProvisioningStateError(
                    "The existing account must have one Spot and one Futures portfolio."
                )

            spot = by_market["spot"]
            futures = by_market["futures"]
            if (
                spot.position_mode is not None
                or spot.status not in {"active", "disabled"}
                or futures.position_mode not in {"one_way", "hedge"}
                or futures.status not in {"active", "disabled"}
            ):
                raise AccountProvisioningStateError(
                    "The existing portfolio configuration is invalid."
                )

            session_result = await session.execute(
                select(PaperSession).where(
                    PaperSession.account_id == account.id
                )
            )
            paper_sessions = list(session_result.scalars().all())

            sessions_by_portfolio = {
                spot.id: [],
                futures.id: [],
            }
            for item in paper_sessions:
                if item.portfolio_id not in sessions_by_portfolio:
                    raise AccountProvisioningStateError(
                        "A paper session references an unexpected portfolio."
                    )
                if item.session_number < 1:
                    raise AccountProvisioningStateError(
                        "A paper session has an invalid session number."
                    )
                sessions_by_portfolio[item.portfolio_id].append(item)

            active_sessions = {}
            for portfolio_id, items in sessions_by_portfolio.items():
                numbers = [item.session_number for item in items]
                if len(numbers) != len(set(numbers)):
                    raise AccountProvisioningStateError(
                        "Duplicate paper session numbers exist."
                    )
                active = [item for item in items if item.status == "active"]
                if (
                    len(active) != 1
                    or any(item.status not in {"active", "archived"} for item in items)
                ):
                    raise AccountProvisioningStateError(
                        "Each portfolio must have exactly one active session."
                    )
                active_sessions[portfolio_id] = active[0]

            # Existing financial books are validated only.
            # Missing or inconsistent ledgers fail closed; never auto-fund.
            await validate_existing_paper_ledger(
                session,
                account_id=account.id,
                portfolio_id=spot.id,
                session_id=active_sessions[spot.id].id,
                market_type="spot",
            )
            await validate_existing_paper_ledger(
                session,
                account_id=account.id,
                portfolio_id=futures.id,
                session_id=active_sessions[futures.id].id,
                market_type="futures",
            )

            result = AccountCreationResult(
                account_id=account.id,
                spot_portfolio_id=spot.id,
                futures_portfolio_id=futures.id,
                spot_session_id=active_sessions[spot.id].id,
                futures_session_id=active_sessions[futures.id].id,
                futures_position_mode=futures.position_mode,
            )

    return result