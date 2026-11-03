"""Fail-closed policy for outbound public WebSocket subscription messages."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.security.websocket_egress import (
    WebSocketEgressViolation,
    _validate_streams,
)


class WebSocketMessageViolation(ValueError):
    """Raised when an outbound WebSocket message is not permitted."""


@dataclass(frozen=True, slots=True)
class ApprovedWebSocketMessage:
    method: str
    params: tuple[str, ...]
    request_id: int | str | None


_ALLOWED_METHODS = frozenset({
    "SUBSCRIBE",
    "UNSUBSCRIBE",
    "LIST_SUBSCRIPTIONS",
})
_MAX_MESSAGE_BYTES = 64 * 1024
_MAX_PARAMS = 1024


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WebSocketMessageViolation("Duplicate JSON keys are prohibited.")
        result[key] = value
    return result


def validate_public_websocket_message(
    payload: str | bytes,
) -> ApprovedWebSocketMessage:
    """Validate one outbound public subscription-control JSON message."""
    if isinstance(payload, bytes):
        if len(payload) > _MAX_MESSAGE_BYTES:
            raise WebSocketMessageViolation("WebSocket message is too large.")
        try:
            payload = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WebSocketMessageViolation("Message must be valid UTF-8.") from exc

    elif isinstance(payload, str):
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise WebSocketMessageViolation("Message must be valid UTF-8.") from exc

        if len(encoded) > _MAX_MESSAGE_BYTES:
            raise WebSocketMessageViolation("WebSocket message is too large.")

    else:
        raise WebSocketMessageViolation("Message must be str or bytes.")

    try:
        message = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except WebSocketMessageViolation:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise WebSocketMessageViolation("Malformed JSON WebSocket message.") from exc

    if not isinstance(message, dict):
        raise WebSocketMessageViolation("Message must be a JSON object.")

    if not set(message).issubset({"method", "params", "id"}):
        raise WebSocketMessageViolation("Unknown message fields are prohibited.")

    if "method" not in message or "id" not in message:
        raise WebSocketMessageViolation("Message method and request id are required.")

    method = message["method"]
    request_id = message["id"]

    if not isinstance(method, str) or method not in _ALLOWED_METHODS:
        raise WebSocketMessageViolation("WebSocket method is not allowlisted.")

    if request_id is not None:
        if isinstance(request_id, bool):
            raise WebSocketMessageViolation("Boolean request IDs are prohibited.")
        if isinstance(request_id, int):
            if not 0 <= request_id <= 2_147_483_647:
                raise WebSocketMessageViolation("Integer request ID is out of range.")
        elif isinstance(request_id, str):
            if not request_id or len(request_id) > 36:
                raise WebSocketMessageViolation("String request ID is invalid.")
        else:
            raise WebSocketMessageViolation("Request ID must be integer, string, or null.")

    params = message.get("params", [])

    if not isinstance(params, list):
        raise WebSocketMessageViolation("Message params must be a list.")

    if method == "LIST_SUBSCRIPTIONS":
        if params:
            raise WebSocketMessageViolation(
                "LIST_SUBSCRIPTIONS does not accept stream parameters."
            )
        return ApprovedWebSocketMessage(method, (), request_id)

    if not params or len(params) > _MAX_PARAMS:
        raise WebSocketMessageViolation("Invalid subscription parameter count.")

    if any(not isinstance(item, str) or "/" in item for item in params):
        raise WebSocketMessageViolation("Each parameter must be one stream name.")

    validated: list[str] = []

    for stream in params:
        try:
            parsed = _validate_streams(stream)
        except WebSocketEgressViolation as exc:
            raise WebSocketMessageViolation("Invalid public stream parameter.") from exc

        if len(parsed) != 1:
            raise WebSocketMessageViolation("Each parameter must contain exactly one stream.")

        validated.append(parsed[0])

    if len(set(validated)) != len(validated):
        raise WebSocketMessageViolation("Duplicate stream parameters are prohibited.")

    return ApprovedWebSocketMessage(
        method=method,
        params=tuple(validated),
        request_id=request_id,
    )
