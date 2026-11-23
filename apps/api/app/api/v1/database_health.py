"""Database readiness endpoint. Does not expose connection details."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("/database")
async def database_health(request: Request) -> dict[str, bool]:
    manager = getattr(request.app.state, "database_manager", None)

    if manager is None or not manager.is_configured or not manager.is_initialized:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not configured.",
        )

    try:
        ready = await manager.ping()
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is temporarily unavailable.",
        ) from None

    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not ready.",
        )

    return {"configured": True, "ready": True}