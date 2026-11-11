"""Stable JSON response contracts for the read-only market-data API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PipelineHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group: str
    state: str
    symbols: list[str]
    active_streams: list[str]
    clock_synchronized: bool
    metadata_fresh: bool
    book_synchronized: bool
    last_event_type: str | None
    last_event_timestamp_ms: int | None
    last_freshness_state: str | None
    error_code: str | None


class MarketDataHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: str
    ready: bool
    pipelines: list[PipelineHealthResponse]


class MarketDataLatestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group: str
    symbol: str
    event_type: str
    event_timestamp_ms: int
    event_timestamp_utc: str
    freshness_state: str | None
    book_synchronized: bool
    payload: dict[str, object]
