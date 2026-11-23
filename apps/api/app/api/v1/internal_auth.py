from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.v1.auth_models import (
    BrowserBindingCookie,
    InternalSessionMetadata,
    InternalUserIdentity,
    OIDCTransactionCreateRequest,
    OIDCTransactionCreateResponse,
    SessionRevokeRequest,
    SessionRevokeResponse,
    SessionValidateRequest,
    SessionValidateResponse,
)
from app.database.session import get_db_session
from app.security.session_authentication import (
    load_authenticated_user,
    load_session_owner_id,
)
from app.services.session_service import revoke_user_session
from app.security.internal_service_auth import (
    InternalServicePrincipal,
    get_internal_service_principal,
)
from app.services.oidc_auth_service import build_authorization_url
from app.services.oidc_idempotency_service import (
    OIDCIdempotencyConflictError,
    OIDCIdempotencyPersistenceError,
    create_or_replay_oidc_transaction,
)


router = APIRouter(prefix="/internal/v1/auth", tags=["internal-auth"])


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Authentication service unavailable",
        headers={"Cache-Control": "no-store"},
    )


def _conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="Authentication transaction request conflicts",
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/oidc/transactions",
    response_model=OIDCTransactionCreateResponse,
    status_code=201,
)
async def create_oidc_transaction(
    payload: OIDCTransactionCreateRequest,
    request: Request,
    response: Response,
    principal: InternalServicePrincipal = Depends(
        get_internal_service_principal
    ),
) -> OIDCTransactionCreateResponse:
    response.headers["Cache-Control"] = "no-store"

    runtime = getattr(request.app.state, "oidc_runtime", None)
    if runtime is None:
        raise _unavailable()

    try:
        metadata = await runtime.get_metadata()

        async with runtime.session_factory() as session:
            result = await create_or_replay_oidc_transaction(
                session,
                service_id=principal.service_id,
                request_id=payload.request_id,
                encryption=runtime.encryption,
                authorization_url_builder=lambda transaction: (
                    build_authorization_url(
                        runtime.settings,
                        metadata,
                        transaction,
                    )
                ),
            )

        stored = result.response
        return OIDCTransactionCreateResponse(
            authorization_url=stored["authorization_url"],
            state=stored["state"],
            browser_binding_cookie=BrowserBindingCookie(
                name=stored["browser_cookie_name"],
                value=stored["browser_cookie_value"],
                max_age_seconds=stored["max_age"],
            ),
            expires_at=datetime.fromisoformat(stored["expires_at"]),
        )
    except OIDCIdempotencyConflictError:
        raise _conflict() from None
    except HTTPException:
        raise
    except (OIDCIdempotencyPersistenceError, Exception):
        raise _unavailable() from None



@router.post(
    "/session/validate",
    response_model=SessionValidateResponse,
)
async def validate_user_session(
    payload: SessionValidateRequest,
    response: Response,
    principal: InternalServicePrincipal = Depends(
        get_internal_service_principal
    ),
    session=Depends(get_db_session),
) -> SessionValidateResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"

    try:
        authenticated = await load_authenticated_user(
            session,
            payload.session_token,
        )

        if authenticated is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required.",
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                },
            )

        return SessionValidateResponse(
            authenticated=True,
            user=InternalUserIdentity(
                id=str(authenticated.user_id),
                email=authenticated.email,
            ),
            session=InternalSessionMetadata(
                expires_at=authenticated.expires_at,
            ),
        )
    except HTTPException:
        raise
    except Exception:
        raise _unavailable() from None


@router.post(
    "/session/revoke",
    response_model=SessionRevokeResponse,
)
async def revoke_user_session_endpoint(
    payload: SessionRevokeRequest,
    request: Request,
    response: Response,
    principal: InternalServicePrincipal = Depends(
        get_internal_service_principal
    ),
) -> SessionRevokeResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"

    manager = getattr(request.app.state, "database_manager", None)

    if manager is None or not manager.is_initialized:
        raise _unavailable()

    try:
        factory = manager.session_factory()

        # Close the lookup session before the lifecycle service starts
        # its own transaction in a fresh database session.
        async with factory() as lookup_session:
            owner_id = await load_session_owner_id(
                lookup_session,
                payload.session_token,
            )

        if owner_id is None:
            return SessionRevokeResponse(revoked=False)

        async with factory() as revoke_session:
            revoked = await revoke_user_session(
                revoke_session,
                authenticated_user_id=owner_id,
                raw_token=payload.session_token,
            )

        return SessionRevokeResponse(revoked=revoked)

    except HTTPException:
        raise
    except Exception:
        raise _unavailable() from None
