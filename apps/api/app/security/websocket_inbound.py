"""Fail-closed Binance public market-data WebSocket inbound validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping


class InboundWebSocketViolation(ValueError):
    """Raised when an inbound public WebSocket message is not trusted."""


@dataclass(frozen=True, slots=True)
class ValidatedInboundMessage:
    kind: str
    market: str
    stream: str | None
    event_type: str | None
    symbol: str | None
    payload: Mapping[str, Any]


MAX_MESSAGE_BYTES = 1_048_576
MAX_INTEGER = 9_223_372_036_854_775_807

_SYMBOL = re.compile(r"^[A-Z0-9]{2,30}$")
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InboundWebSocketViolation("Duplicate JSON keys are prohibited.")
        result[key] = value
    return result


def _reject_constant(value):
    raise InboundWebSocketViolation("Non-standard JSON numeric constants are prohibited.")


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InboundWebSocketViolation(f"{field} must be an integer.")
    if not 0 <= value <= MAX_INTEGER:
        raise InboundWebSocketViolation(f"{field} is out of range.")
    return value


def _decimal(
    value: Any,
    field: str,
    *,
    allow_zero: bool = False,
    allow_negative: bool = False,
) -> Decimal:
    pattern = r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$" if allow_negative else _DECIMAL

    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise InboundWebSocketViolation(f"{field} must be a valid decimal string.")

    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise InboundWebSocketViolation(f"{field} is invalid.") from exc

    if not parsed.is_finite():
        raise InboundWebSocketViolation(f"{field} must be finite.")
    if parsed < 0 and not allow_negative:
        raise InboundWebSocketViolation(f"{field} cannot be negative.")
    if parsed == 0 and not allow_zero:
        raise InboundWebSocketViolation(f"{field} must be non-zero.")

    return parsed


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze JSON containers before exposing validated data."""
    if isinstance(value, dict):
        return MappingProxyType({
            key: _deep_freeze(item)
            for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _symbol(value: Any) -> str:
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
        raise InboundWebSocketViolation("Invalid event symbol.")
    return value


def _timestamp(value: Any, field: str) -> int:
    result = _integer(value, field)
    if result == 0:
        raise InboundWebSocketViolation(f"{field} must be positive.")
    return result


def _object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise InboundWebSocketViolation(f"{field} must be an object.")
    return value


def _exact_fields(obj: dict, required: set[str], optional: set[str] = frozenset()):
    keys = set(obj)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise InboundWebSocketViolation("Unexpected or missing event fields.")


def _validate_trade(event: dict, symbol: str) -> None:
    required = {"e", "E", "s", "t", "p", "q", "T", "m", "M"}
    optional = {"b", "a"}
    _exact_fields(event, required, optional)

    if event["e"] != "trade":
        raise InboundWebSocketViolation("Unexpected Spot event type.")
    if _symbol(event["s"]) != symbol:
        raise InboundWebSocketViolation("Spot trade symbol mismatch.")

    _timestamp(event["E"], "E")
    _timestamp(event["T"], "T")
    _integer(event["t"], "t")
    if "b" in event:
        _integer(event["b"], "b")
    if "a" in event:
        _integer(event["a"], "a")

    _decimal(event["p"], "p")
    _decimal(event["q"], "q")

    if not isinstance(event["m"], bool) or not isinstance(event["M"], bool):
        raise InboundWebSocketViolation("Trade maker flags must be booleans.")


def _validate_depth(event: dict, symbol: str) -> None:
    required = {"e", "E", "T", "s", "U", "u", "pu", "b", "a"}
    _exact_fields(event, required)

    if event["e"] != "depthUpdate":
        raise InboundWebSocketViolation("Unexpected Futures depth event.")
    if _symbol(event["s"]) != symbol:
        raise InboundWebSocketViolation("Depth symbol mismatch.")

    _timestamp(event["E"], "E")
    _timestamp(event["T"], "T")
    first = _integer(event["U"], "U")
    last = _integer(event["u"], "u")
    previous = _integer(event["pu"], "pu")

    if first > last:
        raise InboundWebSocketViolation("Depth update ID range is invalid.")
    if previous > last:
        raise InboundWebSocketViolation("Depth previous update ID is invalid.")

    for field in ("b", "a"):
        levels = event[field]
        if not isinstance(levels, list):
            raise InboundWebSocketViolation(f"{field} must be a list.")
        for level in levels:
            if not isinstance(level, list) or len(level) != 2:
                raise InboundWebSocketViolation("Invalid depth level.")
            _decimal(level[0], "depth price")
            _decimal(level[1], "depth quantity", allow_zero=True)

    if not event["b"] and not event["a"]:
        raise InboundWebSocketViolation("Empty depth update is prohibited.")


def _validate_mark_price(event: dict, symbol: str) -> None:
    required = {"e", "E", "s", "p", "i", "P", "r", "T"}
    optional = {"ap", "st"}
    _exact_fields(event, required, optional)

    if event["e"] != "markPriceUpdate":
        raise InboundWebSocketViolation("Unexpected Futures mark-price event.")
    if _symbol(event["s"]) != symbol:
        raise InboundWebSocketViolation("Mark-price symbol mismatch.")

    _timestamp(event["E"], "E")
    _timestamp(event["T"], "T")
    _decimal(event["p"], "mark price")
    _decimal(event["i"], "index price")
    _decimal(event["P"], "estimated settlement price")
    _decimal(event["r"], "funding rate", allow_zero=True, allow_negative=True)

    if "ap" in event:
        _decimal(event["ap"], "ap")

    if "st" in event:
        _integer(event["st"], "st")


def _request_id(value: Any) -> None:
    if isinstance(value, bool):
        raise InboundWebSocketViolation("Invalid response ID.")
    if isinstance(value, int):
        if not 0 <= value <= 2_147_483_647:
            raise InboundWebSocketViolation("Response ID is out of range.")
        return
    if isinstance(value, str) and 0 < len(value) <= 36:
        return
    raise InboundWebSocketViolation("Invalid response ID.")


def _approved_streams(approved_request) -> tuple[str, ...]:
    market = getattr(approved_request, "market", None)
    streams = getattr(approved_request, "streams", None)

    if market not in {"spot", "usdm_futures"}:
        raise InboundWebSocketViolation("Unapproved market context.")
    if not isinstance(streams, tuple):
        raise InboundWebSocketViolation("Approved stream context is missing.")

    if len(set(streams)) != len(streams):
        raise InboundWebSocketViolation("Duplicate approved streams.")

    for stream in streams:
        if not isinstance(stream, str):
            raise InboundWebSocketViolation("Invalid approved stream.")
        if market == "spot":
            valid = re.fullmatch(r"[a-z0-9]{2,30}@trade", stream)
        else:
            valid = re.fullmatch(
                r"[a-z0-9]{2,30}@(depth|markPrice)", stream
            )
        if valid is None:
            raise InboundWebSocketViolation("Unsupported approved stream.")

    return streams


def validate_public_websocket_inbound_message(
    payload: str | bytes,
    approved_request,
) -> ValidatedInboundMessage:
    """Validate one inbound Binance message against its approved connection."""

    if isinstance(payload, bytes):
        if len(payload) > MAX_MESSAGE_BYTES:
            raise InboundWebSocketViolation("Message is too large.")
        try:
            payload = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise InboundWebSocketViolation("Message must be UTF-8.") from exc
    elif isinstance(payload, str):
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise InboundWebSocketViolation("Message must be UTF-8.") from exc
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise InboundWebSocketViolation("Message is too large.")
    else:
        raise InboundWebSocketViolation("Message must be str or bytes.")

    try:
        message = json.loads(
            payload,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except InboundWebSocketViolation:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise InboundWebSocketViolation("Malformed JSON message.") from exc

    message = _object(message, "message")
    market = getattr(approved_request, "market", None)
    streams = _approved_streams(approved_request)

    # Binance subscription acknowledgement or LIST_SUBSCRIPTIONS response.
    if "id" in message and ("result" in message or "code" in message):
        _request_id(message["id"])

        if "code" in message:
            _exact_fields(message, {"code", "msg", "id"})
            code = message["code"]
            if isinstance(code, bool) or not isinstance(code, int) or code == 0:
                raise InboundWebSocketViolation("Invalid provider error code.")
            if not isinstance(message["msg"], str) or not message["msg"]:
                raise InboundWebSocketViolation("Invalid provider error message.")
            return ValidatedInboundMessage(
                "provider_error", market, None, None, None,
                _deep_freeze(message),
            )

        _exact_fields(message, {"result", "id"})
        result = message["result"]

        if result is not None:
            if not isinstance(result, list):
                raise InboundWebSocketViolation("Subscription result must be null or a list.")
            if any(not isinstance(item, str) for item in result):
                raise InboundWebSocketViolation("Invalid subscription list.")
            if len(set(result)) != len(result):
                raise InboundWebSocketViolation("Duplicate subscription results.")
            if not set(result).issubset(set(streams)):
                raise InboundWebSocketViolation(
                    "Provider returned a stream outside the active approved set."
                )
            for item in result:
                supported = (
                    re.fullmatch(r"[a-z0-9]{2,30}@trade", item)
                    if market == "spot"
                    else re.fullmatch(
                        r"[a-z0-9]{2,30}@(depth|markPrice)", item
                    )
                )
                if supported is None:
                    raise InboundWebSocketViolation(
                        "Provider returned an unsupported stream."
                    )

        return ValidatedInboundMessage(
            "acknowledgement", market, None, None, None,
            _deep_freeze(message),
        )

    # Combined stream wrapper.
    if "stream" in message or "data" in message:
        _exact_fields(message, {"stream", "data"})
        stream = message["stream"]
        event = _object(message["data"], "data")
        if not isinstance(stream, str) or stream not in streams:
            raise InboundWebSocketViolation("Unapproved combined stream.")
    else:
        event = message
        stream = None

    event_type = event.get("e")
    symbol = _symbol(event.get("s"))

    event_stream_type = {
        "trade": "trade",
        "depthUpdate": "depth",
        "markPriceUpdate": "markPrice",
    }.get(event_type)

    matching = [
        candidate for candidate in streams
        if candidate.startswith(symbol.lower() + "@")
        and candidate.split("@", 1)[1] == event_stream_type
    ]
    if stream is not None:
        matching = [candidate for candidate in matching if candidate == stream]

    if len(matching) != 1:
        raise InboundWebSocketViolation("Event does not match exactly one approved stream.")

    selected = matching[0]
    stream_type = selected.split("@", 1)[1]
    if stream_type.endswith("@1s"):
        stream_type = stream_type[:-3]

    if market == "spot" and stream_type == "trade":
        _validate_trade(event, symbol)
    elif market == "usdm_futures" and stream_type == "depth":
        _validate_depth(event, symbol)
    elif market == "usdm_futures" and stream_type == "markPrice":
        _validate_mark_price(event, symbol)
    else:
        raise InboundWebSocketViolation("Unsupported event for approved stream.")

    return ValidatedInboundMessage(
        "event",
        market,
        selected,
        event_type,
        symbol,
        _deep_freeze(event),
    )
