"""Read-only Binance USD-M Futures REST market-data client."""

from __future__ import annotations

import json
import math
import re
from urllib.parse import urlencode

from app.security.public_http import fetch_public_market_data


BASE_URL = "https://fapi.binance.com"

SERVER_TIME_PATH = "/fapi/v1/time"
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
KLINES_PATH = "/fapi/v1/klines"

DEFAULT_KLINE_LIMIT = 500
MAX_KLINE_LIMIT = 1500

SUPPORTED_INTERVALS = frozenset({
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
})

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+$")
_PROVIDER_DELIVERY_SUFFIX = re.compile(r"^[0-9]{6}$")

def _valid_provider_identifier(value: str, *, allow_delivery_suffix: bool = False) -> bool:
    if not isinstance(value, str) or not value or len(value) > 30:
        return False
    if allow_delivery_suffix and "_" in value:
        if value.count("_") != 1:
            return False
        prefix, suffix = value.rsplit("_", 1)
        return bool(prefix) and prefix.isalnum() and bool(_PROVIDER_DELIVERY_SUFFIX.fullmatch(suffix))
    return value.isalnum()


class FuturesMarketDataResponseError(ValueError):
    """Raised when a Binance Futures response is malformed."""


def _validate_symbol(symbol: str) -> str:
    if (
        not isinstance(symbol, str)
        or not _SYMBOL_PATTERN.fullmatch(symbol)
    ):
        raise ValueError("Invalid USD-M Futures symbol.")
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


def _nonnegative_int(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= 0
    )


class BinanceUsdMFuturesRestClient:
    """Public, read-only Binance USD-M Futures REST client."""

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

    def _request(
        self,
        path: str,
        params: list[tuple[str, object]],
    ) -> object:
        if path not in {
            SERVER_TIME_PATH,
            EXCHANGE_INFO_PATH,
            KLINES_PATH,
        }:
            raise ValueError("Unapproved USD-M Futures endpoint.")

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
            raise FuturesMarketDataResponseError(
                "Unexpected successful HTTP status."
            )

        if not isinstance(response.body, bytes):
            raise FuturesMarketDataResponseError(
                "Response body must be bytes."
            )

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
            raise FuturesMarketDataResponseError(
                "Binance returned invalid Futures JSON."
            ) from exc

        return payload

    def get_server_time(self) -> dict:
        payload = self._request(SERVER_TIME_PATH, [])

        if not isinstance(payload, dict):
            raise FuturesMarketDataResponseError(
                "Unexpected Futures server-time structure."
            )

        if not _nonnegative_int(payload.get("serverTime")):
            raise FuturesMarketDataResponseError(
                "Invalid Futures server timestamp."
            )

        return payload

    def get_exchange_info(self) -> dict:
        payload = self._request(EXCHANGE_INFO_PATH, [])

        if not isinstance(payload, dict):
            raise FuturesMarketDataResponseError(
                "Unexpected Futures exchange-info structure."
            )

        timezone = payload.get("timezone")
        server_time = payload.get("serverTime")
        rate_limits = payload.get("rateLimits")
        exchange_filters = payload.get("exchangeFilters")
        assets = payload.get("assets")
        symbols = payload.get("symbols")

        if not isinstance(timezone, str) or not timezone:
            raise FuturesMarketDataResponseError(
                "Invalid Futures exchange timezone."
            )

        if not _nonnegative_int(server_time):
            raise FuturesMarketDataResponseError(
                "Invalid Futures exchange server time."
            )

        if not isinstance(rate_limits, list):
            raise FuturesMarketDataResponseError(
                "Invalid Futures rate limits."
            )

        for item in rate_limits:
            if not isinstance(item, dict):
                raise FuturesMarketDataResponseError(
                    "Invalid Futures rate-limit record."
                )

            for field in ("rateLimitType", "interval"):
                value = item.get(field)
                if not isinstance(value, str) or not value:
                    raise FuturesMarketDataResponseError(
                        "Invalid Futures rate-limit metadata."
                    )

            for field in ("intervalNum", "limit"):
                value = item.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value <= 0
                ):
                    raise FuturesMarketDataResponseError(
                        "Invalid Futures rate-limit numeric metadata."
                    )

        if (
            not isinstance(exchange_filters, list)
            or not all(isinstance(item, dict) for item in exchange_filters)
        ):
            raise FuturesMarketDataResponseError(
                "Invalid Futures exchange filters."
            )

        if not isinstance(assets, list):
            raise FuturesMarketDataResponseError(
                "Invalid Futures assets collection."
            )

        for asset in assets:
            if not isinstance(asset, dict):
                raise FuturesMarketDataResponseError(
                    "Invalid Futures asset record."
                )

            if not isinstance(asset.get("asset"), str) or not asset["asset"]:
                raise FuturesMarketDataResponseError(
                    "Invalid Futures asset name."
                )

            if not isinstance(asset.get("marginAvailable"), bool):
                raise FuturesMarketDataResponseError(
                    "Invalid Futures asset margin availability."
                )

            if not isinstance(asset.get("autoAssetExchange"), str):
                raise FuturesMarketDataResponseError(
                    "Invalid Futures asset exchange metadata."
                )

        if not isinstance(symbols, list):
            raise FuturesMarketDataResponseError(
                "Futures symbols must be an array."
            )

        required_strings = (
            "symbol",
            "pair",
            "contractType",
            "status",
            "baseAsset",
            "quoteAsset",
            "marginAsset",
        )

        for item in symbols:
            if not isinstance(item, dict):
                raise FuturesMarketDataResponseError(
                    "Invalid Futures symbol record."
                )

            for field in required_strings:
                value = item.get(field)
                if not isinstance(value, str) or not value:
                    raise FuturesMarketDataResponseError(
                        "Missing or invalid Futures symbol metadata."
                    )

            if not _valid_provider_identifier(
                item["symbol"], allow_delivery_suffix=True
            ):
                raise FuturesMarketDataResponseError(
                    "Invalid provider Futures symbol."
                )

            for field in ("deliveryDate", "onboardDate"):
                if not _nonnegative_int(item.get(field)):
                    raise FuturesMarketDataResponseError(
                        "Invalid Futures contract date."
                    )

            for field in ("pricePrecision", "quantityPrecision"):
                if not _nonnegative_int(item.get(field)):
                    raise FuturesMarketDataResponseError(
                        "Invalid Futures precision metadata."
                    )

            order_types = item.get("orderTypes")
            if (
                not isinstance(order_types, list)
                or not all(
                    isinstance(value, str) and value
                    for value in order_types
                )
            ):
                raise FuturesMarketDataResponseError(
                    "Invalid Futures order types."
                )

            filters = item.get("filters")
            if (
                not isinstance(filters, list)
                or not all(isinstance(value, dict) for value in filters)
            ):
                raise FuturesMarketDataResponseError(
                    "Invalid Futures symbol filters."
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
            raise ValueError("Unsupported USD-M Futures Kline interval.")

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
            raise ValueError("Futures Kline limit must be between 1 and 1500.")

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
            raise FuturesMarketDataResponseError(
                "Unexpected Futures Kline response structure."
            )

        for row in payload:
            if not isinstance(row, list) or len(row) != 12:
                raise FuturesMarketDataResponseError(
                    "Each Futures Kline must contain exactly 12 fields."
                )

            for index in (0, 6, 8):
                if not _nonnegative_int(row[index]):
                    raise FuturesMarketDataResponseError(
                        "Invalid Futures Kline timestamp or trade count."
                    )

            for index in (1, 2, 3, 4, 5, 7, 9, 10, 11):
                value = row[index]
                if not isinstance(value, str) or not value:
                    raise FuturesMarketDataResponseError(
                        "Invalid Futures Kline string field."
                    )

        return payload