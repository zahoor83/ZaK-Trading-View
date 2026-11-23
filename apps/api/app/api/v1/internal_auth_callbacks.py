"""Internal BFF callback endpoint for completing OIDC authentication."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.v1.auth_models import (
    InternalSessionData,
    InternalUserIdentity,
    OIDCCallbackRequest,
    OIDCCallbackResponse,
)
from app.security.internal_service_auth import (
    InternalServicePrincipal,
    get_internal_service_principal,
)
from app.services.oidc_login_orchestration import (
    OIDCLoginError,
    complete_oidc_login,
)

router = APIRouter(prefix="/internal/v1/auth", tags=["internal-auth"])


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Authentication service unavailable",
        headers={"Cache-Control": "no-store"},
    )


def _rejected() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Authentication could not be completed",
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/oidc/callback",
    response_model=OIDCCallbackResponse,
    status_code=200,
)
async def complete_oidc_callback(
    payload: OIDCCallbackRequest,
    request: Request,
    response: Response,
    principal: InternalServicePrincipal = Depends(
        get_internal_service_principal
    ),
) -> OIDCCallbackResponse:
    """Complete an OIDC callback for the authenticated web BFF only."""

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"

    runtime = getattr(request.app.state, "oidc_runtime", None)
    if runtime is None:
        raise _unavailable()

    try:
        if not runtime.available:
            raise _unavailable()

        metadata = await runtime.get_metadata()

        result = await complete_oidc_login(
            settings=runtime.settings,
            metadata=metadata,
            encryption=runtime.encryption,
            session_factory=runtime.session_factory,
            jwks_cache=runtime.jwks_cache,
            state=payload.state,
            browser_binding=payload.browser_binding,
            authorization_code=payload.authorization_code,
        )

        return OIDCCallbackResponse(
            session=InternalSessionData(
                token=result.raw_session_token,
                expires_at=result.expires_at,
            ),
            user=InternalUserIdentity(
                id=str(result.user_id),
                email=result.email,
            ),
        )

    except OIDCLoginError:
        raise _rejected() from None
    except HTTPException:
        raise
    except Exception:
        raise _unavailable() from None
