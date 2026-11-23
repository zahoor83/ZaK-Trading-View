from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import UUID

from app.security.paper_idempotency import fingerprint_request


_HEX_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SYMBOL = re.compile(r"^[A-Z0-9_-]{1,64}$")

_INTENT_KEYS = {
    "position_id",
    "symbol",
    "side",
    "position_side",
    "quantity",
    "order_type",
    "leverage",
    "limit_price",
    "reduce_only",
}

_ENTRY_KEYS = {"account_code", "direction", "amount"}


def _decimal_text(value: Any, field: str) -> str:
    if not isinstance(value, Decimal):
        raise ValueError(f"{field} must be Decimal")

    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be finite and positive")

    if abs(value) >= Decimal("1e36"):
        raise ValueError(f"{field} exceeds supported magnitude")

    if max(0, -value.as_tuple().exponent) > 36:
        raise ValueError(f"{field} exceeds supported scale")

    normalized = value.normalize()
    return format(normalized, "f")


def _validate_position_id(value: Any) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str) or not _HEX_UUID_V4.fullmatch(value):
        raise ValueError("position_id must be a canonical lowercase UUIDv4 or None")

    parsed = UUID(value)
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("position_id must be a canonical lowercase UUIDv4")

    return value


def _validate_intent(
    market_type: str,
    transaction_type: str,
    intent: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(intent, Mapping):
        raise ValueError("trade_intent must be a mapping")

    allowed_transaction_types = {
        "spot": {"spot_buy", "spot_sell"},
        "futures": {"futures_open", "futures_close"},
    }
    if transaction_type not in allowed_transaction_types.get(market_type, set()):
        raise ValueError("transaction_type is not permitted for market_type")

    if set(intent.keys()) != _INTENT_KEYS:
        raise ValueError("trade_intent must contain exactly the documented keys")

    symbol = intent["symbol"]
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError("symbol must be uppercase ASCII exchange notation")

    side = intent["side"]
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")

    position_side = intent["position_side"]
    if position_side not in (None, "long", "short"):
        raise ValueError("position_side must be None, long, or short")

    order_type = intent["order_type"]
    if order_type not in ("market", "limit"):
        raise ValueError("order_type must be market or limit")

    reduce_only = intent["reduce_only"]
    if not isinstance(reduce_only, bool):
        raise ValueError("reduce_only must be a boolean")

    position_id = _validate_position_id(intent["position_id"])
    quantity = _decimal_text(intent["quantity"], "quantity")

    leverage_value = intent["leverage"]
    limit_value = intent["limit_price"]

    if market_type == "spot":
        if leverage_value is not None:
            raise ValueError("spot intent must not specify leverage")
        if position_side is not None:
            raise ValueError("spot intent must not specify position_side")
        if reduce_only:
            raise ValueError("spot intent must not specify reduce_only")
        leverage = None
    else:
        if position_side not in ("long", "short"):
            raise ValueError("futures intent requires long or short position_side")
        leverage = _decimal_text(leverage_value, "leverage")
        if Decimal(leverage) < 1:
            raise ValueError("futures leverage must be at least 1")

    if order_type == "market":
        if limit_value is not None:
            raise ValueError("market intent must not specify limit_price")
        limit_price = None
    else:
        limit_price = _decimal_text(limit_value, "limit_price")

    if market_type == "futures":
        if order_type != "market":
            raise ValueError("futures currently supports market orders only")

        if transaction_type == "futures_open":
            if reduce_only:
                raise ValueError("futures_open must not be reduce_only")
            expected_side = {"long": "buy", "short": "sell"}[position_side]
            if side != expected_side:
                raise ValueError("futures_open side must open or increase position_side")

        elif transaction_type == "futures_close":
            if not reduce_only:
                raise ValueError("futures_close must be reduce_only")
            if position_id is None:
                raise ValueError("futures_close requires position_id")
            expected_side = {"long": "sell", "short": "buy"}[position_side]
            if side != expected_side:
                raise ValueError("futures_close side must close position_side")

    return {
        "position_id": position_id,
        "symbol": symbol,
        "side": side,
        "position_side": position_side,
        "quantity": quantity,
        "order_type": order_type,
        "leverage": leverage,
        "limit_price": limit_price,
        "reduce_only": reduce_only,
    }


def _validate_journal_entries(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
        raise ValueError("journal_entries must be a sequence")

    if len(entries) < 2:
        raise ValueError("journal_entries must contain at least two entries")

    normalized: list[dict[str, str]] = []

    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry.keys()) != _ENTRY_KEYS:
            raise ValueError("each journal entry must contain exactly the documented keys")

        account_code = entry["account_code"]
        direction = entry["direction"]

        if not isinstance(account_code, str) or not account_code.strip():
            raise ValueError("journal account_code must be non-empty text")

        if direction not in ("debit", "credit"):
            raise ValueError("journal direction must be debit or credit")

        amount = _decimal_text(entry["amount"], "journal amount")

        normalized.append({
            "account_code": account_code,
            "direction": direction,
            "amount": amount,
        })

    return normalized


def fingerprint_trade_execution_request(
    *,
    transaction_type: str,
    market_type: str,
    trade_intent: Mapping[str, Any],
    journal_entries: Sequence[Mapping[str, Any]],
) -> str:
    """
    Fingerprint stable client intent plus the proposed journal semantics.

    Live market quotes and server-generated execution timestamps are
    deliberately excluded. The caller must supply already validated,
    immutable trade intent and journal values.
    """
    if not isinstance(transaction_type, str) or not transaction_type.strip():
        raise ValueError("transaction_type must be non-empty text")

    if market_type not in ("spot", "futures"):
        raise ValueError("market_type must be spot or futures")

    semantic_intent = _validate_intent(market_type, transaction_type, trade_intent)
    semantic_entries = _validate_journal_entries(journal_entries)

    return fingerprint_request(
        "paper_trade_execution",
        {
            "transaction_type": transaction_type,
            "market_type": market_type,
            "trade_intent": semantic_intent,
            "journal_entries": semantic_entries,
        },
    )

def fingerprint_trade_intent_request(
    *,
    transaction_type: str,
    market_type: str,
    trade_intent: Mapping[str, Any],
) -> str:
    """
    Fingerprint immutable client trade intent independently of
    calculated journal values and live execution data.
    """
    if not isinstance(transaction_type, str) or not transaction_type.strip():
        raise ValueError("transaction_type must be non-empty text")

    if market_type not in ("spot", "futures"):
        raise ValueError("market_type must be spot or futures")

    allowed_types = {
        "spot": {"spot_buy", "spot_sell"},
        "futures": {"futures_open", "futures_close"},
    }

    if transaction_type not in allowed_types[market_type]:
        raise ValueError("transaction_type is not permitted for market_type")

    semantic_intent = _validate_intent(market_type, transaction_type, trade_intent)

    return fingerprint_request(
        "paper_trade_intent",
        {
            "transaction_type": transaction_type,
            "market_type": market_type,
            "trade_intent": semantic_intent,
        },
    )
