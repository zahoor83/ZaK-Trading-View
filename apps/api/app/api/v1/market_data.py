"""Read-only market-data HTTP endpoints."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.market_data.api_models import (
    MarketDataHealthResponse,
    MarketDataLatestResponse,
    PipelineHealthResponse,
)
from app.market_data.freshness import FreshnessState
from app.market_data.service_models import (
    MarketDataServiceState,
    PipelineState,
)


router = APIRouter(prefix="/api/v1/market-data", tags=["market-data"])
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _service(request: Request):
    service = getattr(request.app.state, "market_data_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market-data service is unavailable.",
        )
    return service


def _safe_error_code(value: str | None) -> str | None:
    if value is None:
        return None
    if _SAFE_ERROR_CODE.fullmatch(value):
        return value
    return "PipelineError"


def _freshness_value(event: Any) -> str | None:
    freshness = getattr(event, "freshness", None)
    state = getattr(freshness, "state", None)
    value = getattr(state, "value", state)
    return value if isinstance(value, str) else None


def _json_value(value: Any) -> Any:
    """Thaw the service's immutable JSON payload into JSON-compatible values."""
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if hasattr(value, "items") and callable(value.items):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Cached event contains a non-JSON value.")


@router.get("/health", response_model=MarketDataHealthResponse)
async def market_data_health(request: Request) -> MarketDataHealthResponse:
    """Return diagnostic state without exposing process-relative clock values."""
    service = _service(request)
    try:
        snapshot = service.health_snapshot()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market-data health is temporarily unavailable.",
        ) from None

    pipelines = [
        PipelineHealthResponse(
            group=item.group.value,
            state=item.state.value,
            symbols=list(item.symbols),
            active_streams=list(item.active_streams),
            clock_synchronized=item.clock_synchronized,
            metadata_fresh=item.metadata_fresh,
            book_synchronized=item.book_synchronized,
            last_event_type=item.last_event_type,
            last_event_timestamp_ms=item.last_event_timestamp_ms,
            last_freshness_state=(
                item.last_freshness_state.value
                if item.last_freshness_state is not None
                else None
            ),
            error_code=_safe_error_code(item.last_error),
        )
        for item in snapshot.pipelines
    ]
    ready = bool(pipelines) and all(
        item.state is PipelineState.READY for item in snapshot.pipelines
    )
    return MarketDataHealthResponse(
        state=snapshot.state.value,
        ready=ready,
        pipelines=pipelines,
    )


@router.get(
    "/latest/{group}/{symbol}",
    response_model=MarketDataLatestResponse,
)
async def market_data_latest(
    group: str,
    symbol: str,
    request: Request,
) -> MarketDataLatestResponse:
    """Return only the currently validated latest event for a configured symbol."""
    service = _service(request)
    if group not in {"spot", "futures_depth", "futures_mark_price"}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Market-data group or symbol is not configured.",
        )

    try:
        event = service.latest_event(group, symbol)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Market-data group or symbol is not configured.",
        ) from None
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market data is temporarily unavailable.",
        ) from None

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No validated market-data event is currently available.",
        )

    expected_event_types = {
        "spot": "trade",
        "futures_depth": "depthUpdate",
        "futures_mark_price": "markPriceUpdate",
    }
    if (
        getattr(event, "group", None) != group
        or getattr(event, "symbol", None) != symbol
        or getattr(event, "event_type", None) != expected_event_types[group]
        or type(getattr(event, "event_timestamp_ms", None)) is not int
        or event.event_timestamp_ms <= 0
        or type(getattr(event, "book_synchronized", None)) is not bool
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cached market-data event is invalid.",
        )

    freshness_state = _freshness_value(event)
    if group == "futures_depth":
        if event.book_synchronized is not True:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Futures depth book is not synchronized.",
            )
    elif freshness_state != FreshnessState.FRESH.value:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market-data event is not fresh.",
        )

    try:
        timestamp = datetime.fromtimestamp(
            event.event_timestamp_ms / 1000,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        payload = _json_value(event.payload)
    except (OverflowError, OSError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cached market-data event is invalid.",
        ) from None

    return MarketDataLatestResponse(
        group=event.group,
        symbol=event.symbol,
        event_type=event.event_type,
        event_timestamp_ms=event.event_timestamp_ms,
        event_timestamp_utc=timestamp,
        freshness_state=freshness_state,
        book_synchronized=event.book_synchronized,
        payload=payload,
    )
