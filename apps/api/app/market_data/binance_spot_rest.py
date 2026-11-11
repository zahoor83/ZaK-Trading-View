"""Read-only Binance Spot REST market-data client."""

from __future__ import annotations

import json
import math
import re
from urllib.parse import urlencode

from app.security.public_http import fetch_public_market_data


BASE_URL = "https://data-api.binance.vision"

SERVER_TIME_PATH = "/api/v3/time"
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
KLINES_PATH = "/api/v3/klines"

DEFAULT_KLINE_LIMIT = 500
MAX_KLINE_LIMIT = 1000

SUPPORTED_INTERVALS = frozenset({
    "1s",
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
})

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+$")


class MarketDataResponseError(ValueError):
    """Raised when a provider response is malformed or unexpected."""


def _validate_symbol(symbol: str) -> str:
    if (
        not isinstance(symbol, str)
        or not _SYMBOL_PATTERN.fullmatch(symbol)
    ):
        raise ValueError("Invalid Spot symbol.")
    return symbol


def _validate_timestamp(value: int | None, name: str) -> None:
    if value is None:
        return

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ValueError(
            f"{name} must be a non-negative integer in milliseconds."
        )


def _validate_json_float(value: str) -> float:
    parsed = float(value)

    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number.")

    return parsed


def _reject_json_constant(value: str):
    raise ValueError(f"Prohibited JSON constant: {value}")


def _reject_duplicate_keys(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value

    return result


class BinanceSpotRestClient:
    """Fetch public Binance Spot data without authentication."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Timeout must be finite and positive.")

        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("Response size must be a positive integer.")

        self._timeout = float(timeout)
        self._max_response_bytes = max_response_bytes

    @staticmethod
    def get_supported_intervals() -> frozenset[str]:
        return SUPPORTED_INTERVALS

    def _request(self, path: str, params: list[tuple[str, object]]) -> object:
        if path not in {
            SERVER_TIME_PATH,
            EXCHANGE_INFO_PATH,
            KLINES_PATH,
        }:
            raise ValueError("Unapproved Spot endpoint.")

        url = f"{BASE_URL}{path}"
        query = urlencode(params)

        if query:
            url = f"{url}?{query}"

        response = fetch_public_market_data(
            url,
            headers={"Accept": "application/json"},
            timeout=self._timeout,
            max_response_bytes=self._max_response_bytes,
        )

        if response.status_code != 200:
            raise MarketDataResponseError(
                "Unexpected successful HTTP status."
            )

        if not isinstance(response.body, bytes):
            raise MarketDataResponseError("Response body must be bytes.")

        try:
            decoded = response.body.decode("utf-8", errors="strict")

            payload = json.loads(
                decoded,
                parse_float=_validate_json_float,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )

        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            OverflowError,
        ) as exc:
            raise MarketDataResponseError(
                "Binance returned an invalid JSON response."
            ) from exc

        return payload

    def get_server_time(self) -> dict:
        payload = self._request(SERVER_TIME_PATH, [])

        if not isinstance(payload, dict):
            raise MarketDataResponseError(
                "Unexpected server-time response structure."
            )

        server_time = payload.get("serverTime")

        if (
            isinstance(server_time, bool)
            or not isinstance(server_time, int)
            or server_time < 0
        ):
            raise MarketDataResponseError(
                "Invalid Binance server timestamp."
            )

        return payload

    def get_exchange_info(self, symbol: str | None = None) -> dict:
        params = []

        if symbol is not None:
            params.append(("symbol", _validate_symbol(symbol)))

        payload = self._request(EXCHANGE_INFO_PATH, params)

        if not isinstance(payload, dict):
            raise MarketDataResponseError(
                "Unexpected exchange-info response structure."
            )

        timezone = payload.get("timezone")
        server_time = payload.get("serverTime")
        rate_limits = payload.get("rateLimits")
        exchange_filters = payload.get("exchangeFilters")
        symbols = payload.get("symbols")

        if not isinstance(timezone, str) or not timezone:
            raise MarketDataResponseError("Invalid exchange timezone.")

        if (
            isinstance(server_time, bool)
            or not isinstance(server_time, int)
            or server_time < 0
        ):
            raise MarketDataResponseError("Invalid exchange server time.")

        if not isinstance(rate_limits, list):
            raise MarketDataResponseError("Invalid exchange rate limits.")

        for item in rate_limits:
            if not isinstance(item, dict):
                raise MarketDataResponseError("Invalid rate-limit record.")

            for field in ("rateLimitType", "interval"):
                if not isinstance(item.get(field), str) or not item[field]:
                    raise MarketDataResponseError(
                        "Invalid rate-limit metadata."
                    )

            for field in ("intervalNum", "limit"):
                value = item.get(field)

                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value <= 0
                ):
                    raise MarketDataResponseError(
                        "Invalid rate-limit numeric metadata."
                    )

        if (
            not isinstance(exchange_filters, list)
            or not all(isinstance(item, dict) for item in exchange_filters)
        ):
            raise MarketDataResponseError("Invalid exchange filters.")

        if not isinstance(symbols, list):
            raise MarketDataResponseError(
                "Exchange-info symbols must be an array."
            )

        for item in symbols:
            if not isinstance(item, dict):
                raise MarketDataResponseError("Invalid symbol record.")

            for field in ("symbol", "status", "baseAsset", "quoteAsset"):
                value = item.get(field)

                if not isinstance(value, str) or not value:
                    raise MarketDataResponseError(
                        "Missing or invalid symbol metadata."
                    )

            if not _SYMBOL_PATTERN.fullmatch(item["symbol"]):
                raise MarketDataResponseError("Invalid provider symbol.")

            if not isinstance(item.get("orderTypes"), list):
                raise MarketDataResponseError("Invalid symbol order types.")

            if not all(
                isinstance(value, str) and value
                for value in item["orderTypes"]
            ):
                raise MarketDataResponseError("Invalid symbol order types.")

            filters = item.get("filters")

            if (
                not isinstance(filters, list)
                or not all(isinstance(value, dict) for value in filters)
            ):
                raise MarketDataResponseError("Invalid symbol filters.")

        if symbol is not None and (
            len(symbols) != 1 or symbols[0]["symbol"] != symbol
        ):
            raise MarketDataResponseError(
                "Exchange response does not match requested symbol."
            )

        return payload

    def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = DEFAULT_KLINE_LIMIT,
    ) -> list:
        validated_symbol = _validate_symbol(symbol)

        if (
            not isinstance(interval, str)
            or interval not in SUPPORTED_INTERVALS
        ):
            raise ValueError("Unsupported Binance Spot Kline interval.")

        _validate_timestamp(start_time, "start_time")
        _validate_timestamp(end_time, "end_time")

        if (
            start_time is not None
            and end_time is not None
            and start_time > end_time
        ):
            raise ValueError("start_time cannot exceed end_time.")

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_KLINE_LIMIT
        ):
            raise ValueError("Kline limit must be between 1 and 1000.")

        params = [
            ("symbol", validated_symbol),
            ("interval", interval),
        ]

        if start_time is not None:
            params.append(("startTime", start_time))

        if end_time is not None:
            params.append(("endTime", end_time))

        params.append(("limit", limit))

        payload = self._request(KLINES_PATH, params)

        if not isinstance(payload, list):
            raise MarketDataResponseError(
                "Unexpected Kline response structure."
            )

        for row in payload:
            if not isinstance(row, list) or len(row) != 12:
                raise MarketDataResponseError(
                    "Each Kline must contain exactly 12 fields."
                )

            for index in (0, 6, 8):
                value = row[index]

                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                ):
                    raise MarketDataResponseError(
                        "Invalid Kline timestamp or trade count."
                    )

            for index in (1, 2, 3, 4, 5, 7, 9, 10, 11):
                value = row[index]

                if not isinstance(value, str) or not value:
                    raise MarketDataResponseError(
                        "Invalid Kline string field."
                    )

        return payload