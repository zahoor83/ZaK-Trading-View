import pytest

from app.market_data.symbol_metadata import (
    METADATA_MAX_AGE_SECONDS,
    MetadataStaleError,
    MetadataValidationError,
    SymbolNotFoundError,
    SymbolNotTradingError,
    parse_spot_exchange_info,
    parse_usdm_futures_exchange_info,
)


def spot_symbol(**overrides):
    record = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": [{"filterType": "PRICE_FILTER"}],
    }
    record.update(overrides)
    return record


def spot_payload(symbols=None):
    return {
        "serverTime": 1_700_000_000_000,
        "symbols": [spot_symbol()] if symbols is None else symbols,
    }


def futures_symbol(**overrides):
    record = {
        "symbol": "BTCUSDT",
        "pair": "BTCUSDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "deliveryDate": 0,
        "onboardDate": 1_600_000_000_000,
        "pricePrecision": 2,
        "quantityPrecision": 3,
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": [{"filterType": "PRICE_FILTER"}],
    }
    record.update(overrides)
    return record


def futures_payload(symbols=None, assets=None):
    return {
        "serverTime": 1_700_000_000_000,
        "assets": [
            {
                "asset": "USDT",
                "marginAvailable": True,
                "autoAssetExchange": "",
            }
        ] if assets is None else assets,
        "symbols": [futures_symbol()] if symbols is None else symbols,
    }


def test_spot_snapshot_accepts_trading_symbol():
    snapshot = parse_spot_exchange_info(
        spot_payload(),
        fetched_at_monotonic=100.0,
    )

    record = snapshot.require_symbol("BTCUSDT", now_monotonic=101.0)

    assert record.market == "spot"
    assert record.symbol == "BTCUSDT"
    assert record.is_active is True
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"


def test_futures_snapshot_accepts_perpetual_symbol():
    snapshot = parse_usdm_futures_exchange_info(
        futures_payload(),
        fetched_at_monotonic=100.0,
    )

    record = snapshot.require_symbol("BTCUSDT", now_monotonic=101.0)

    assert record.market == "usdm_futures"
    assert record.contract_type == "PERPETUAL"
    assert record.margin_asset == "USDT"


def test_unknown_active_futures_contract_type_is_not_rejected():
    snapshot = parse_usdm_futures_exchange_info(
        futures_payload([
            futures_symbol(contractType="NEW_PROVIDER_CONTRACT_TYPE")
        ]),
        fetched_at_monotonic=100.0,
    )

    record = snapshot.require_symbol("BTCUSDT", now_monotonic=101.0)

    assert record.contract_type == "NEW_PROVIDER_CONTRACT_TYPE"
    assert record.is_active is True


@pytest.mark.parametrize(
    "status",
    ["END_OF_DAY", "HALT", "BREAK", "CANCEL_ONLY", "UNKNOWN_STATUS"],
)
def test_non_trading_spot_status_is_not_eligible(status):
    snapshot = parse_spot_exchange_info(
        spot_payload([spot_symbol(status=status)]),
        fetched_at_monotonic=100.0,
    )

    with pytest.raises(SymbolNotTradingError):
        snapshot.require_symbol("BTCUSDT", now_monotonic=101.0)


def test_non_trading_futures_status_is_not_eligible():
    snapshot = parse_usdm_futures_exchange_info(
        futures_payload([futures_symbol(status="PENDING_TRADING")]),
        fetched_at_monotonic=100.0,
    )

    with pytest.raises(SymbolNotTradingError):
        snapshot.require_symbol("BTCUSDT", now_monotonic=101.0)


def test_missing_symbol_fails_closed():
    snapshot = parse_spot_exchange_info(
        spot_payload(),
        fetched_at_monotonic=100.0,
    )

    with pytest.raises(SymbolNotFoundError):
        snapshot.require_symbol("ETHUSDT", now_monotonic=101.0)


def test_metadata_is_fresh_at_exact_ttl_boundary():
    snapshot = parse_spot_exchange_info(
        spot_payload(),
        fetched_at_monotonic=100.0,
    )

    snapshot.require_symbol(
        "BTCUSDT",
        now_monotonic=100.0 + METADATA_MAX_AGE_SECONDS,
    )


def test_metadata_expires_after_ttl():
    snapshot = parse_spot_exchange_info(
        spot_payload(),
        fetched_at_monotonic=100.0,
    )

    with pytest.raises(MetadataStaleError):
        snapshot.require_symbol(
            "BTCUSDT",
            now_monotonic=100.0 + METADATA_MAX_AGE_SECONDS + 0.001,
        )


def test_monotonic_clock_regression_fails_closed():
    snapshot = parse_spot_exchange_info(
        spot_payload(),
        fetched_at_monotonic=100.0,
    )

    with pytest.raises(MetadataStaleError):
        snapshot.require_symbol("BTCUSDT", now_monotonic=99.0)


def test_snapshot_mapping_is_immutable():
    snapshot = parse_spot_exchange_info(
        spot_payload(),
        fetched_at_monotonic=100.0,
    )

    with pytest.raises(TypeError):
        snapshot.symbols["ETHUSDT"] = snapshot.symbols["BTCUSDT"]


def test_duplicate_spot_symbols_are_rejected():
    with pytest.raises(MetadataValidationError, match="Duplicate spot symbol"):
        parse_spot_exchange_info(
            spot_payload([spot_symbol(), spot_symbol()]),
            fetched_at_monotonic=100.0,
        )


def test_duplicate_futures_symbols_are_rejected():
    with pytest.raises(MetadataValidationError, match="Duplicate usdm_futures symbol"):
        parse_usdm_futures_exchange_info(
            futures_payload([futures_symbol(), futures_symbol()]),
            fetched_at_monotonic=100.0,
        )


def test_duplicate_futures_assets_are_rejected():
    asset = {
        "asset": "USDT",
        "marginAvailable": True,
        "autoAssetExchange": "",
    }

    with pytest.raises(MetadataValidationError, match="Duplicate Futures asset"):
        parse_usdm_futures_exchange_info(
            futures_payload(assets=[asset, asset]),
            fetched_at_monotonic=100.0,
        )


def test_invalid_futures_asset_boolean_is_rejected():
    asset = {
        "asset": "USDT",
        "marginAvailable": "true",
        "autoAssetExchange": "",
    }

    with pytest.raises(MetadataValidationError, match="marginAvailable"):
        parse_usdm_futures_exchange_info(
            futures_payload(assets=[asset]),
            fetched_at_monotonic=100.0,
        )


@pytest.mark.parametrize("bad_server_time", [-1, True, "1700000000000", 1.5])
def test_invalid_server_time_is_rejected(bad_server_time):
    payload = spot_payload()
    payload["serverTime"] = bad_server_time

    with pytest.raises(MetadataValidationError):
        parse_spot_exchange_info(payload, fetched_at_monotonic=100.0)


@pytest.mark.parametrize("bad_status", ["", " TRADING", "TRADING "])
def test_invalid_status_string_is_rejected(bad_status):
    with pytest.raises(MetadataValidationError):
        parse_spot_exchange_info(
            spot_payload([spot_symbol(status=bad_status)]),
            fetched_at_monotonic=100.0,
        )


def test_same_symbol_in_separate_markets_has_independent_metadata():
    spot = parse_spot_exchange_info(
        spot_payload(),
        fetched_at_monotonic=100.0,
    )
    futures = parse_usdm_futures_exchange_info(
        futures_payload(),
        fetched_at_monotonic=200.0,
    )

    assert spot.market == "spot"
    assert futures.market == "usdm_futures"
    assert spot.fetched_at_monotonic != futures.fetched_at_monotonic


def test_spot_rejects_duplicate_filter_types():
    record = spot_symbol(
        filters=[
            {"filterType": "PRICE_FILTER"},
            {"filterType": "PRICE_FILTER"},
        ]
    )

    with pytest.raises(MetadataValidationError, match="duplicate filterType"):
        parse_spot_exchange_info(
            spot_payload([record]),
            fetched_at_monotonic=100.0,
        )


def test_spot_rejects_filter_without_filter_type():
    record = spot_symbol(filters=[{"minPrice": "0.01"}])

    with pytest.raises(MetadataValidationError, match="filterType"):
        parse_spot_exchange_info(
            spot_payload([record]),
            fetched_at_monotonic=100.0,
        )


def test_unknown_filter_type_is_preserved():
    record = spot_symbol(
        filters=[{"filterType": "FUTURE_PROVIDER_FILTER"}]
    )

    snapshot = parse_spot_exchange_info(
        spot_payload([record]),
        fetched_at_monotonic=100.0,
    )

    assert snapshot.require_symbol(
        "BTCUSDT",
        now_monotonic=101.0,
    ).filter_types == ("FUTURE_PROVIDER_FILTER",)


@pytest.mark.parametrize(
    "bad_precision",
    [-1, True, "2", 2.5],
)
def test_invalid_futures_precision_is_rejected(bad_precision):
    with pytest.raises(MetadataValidationError):
        parse_usdm_futures_exchange_info(
            futures_payload([
                futures_symbol(pricePrecision=bad_precision)
            ]),
            fetched_at_monotonic=100.0,
        )


def test_futures_precision_is_metadata_not_tick_size():
    snapshot = parse_usdm_futures_exchange_info(
        futures_payload(),
        fetched_at_monotonic=100.0,
    )

    record = snapshot.require_symbol("BTCUSDT", now_monotonic=101.0)

    assert record.price_precision == 2
    assert record.quantity_precision == 3
    assert "tick_size" not in record.__dataclass_fields__


@pytest.mark.parametrize(
    "bad_ttl",
    [0, -1, 301, True, float("nan"), float("inf")],
)
def test_invalid_metadata_ttl_is_rejected(bad_ttl):
    with pytest.raises(MetadataValidationError):
        parse_spot_exchange_info(
            spot_payload(),
            fetched_at_monotonic=100.0,
            max_age_seconds=bad_ttl,
        )


@pytest.mark.parametrize(
    "bad_fetched_at",
    [-1, True, float("nan"), float("inf"), "100"],
)
def test_invalid_snapshot_creation_time_is_rejected(bad_fetched_at):
    with pytest.raises(MetadataValidationError):
        parse_spot_exchange_info(
            spot_payload(),
            fetched_at_monotonic=bad_fetched_at,
        )


def test_empty_spot_symbols_produce_snapshot_but_lookup_fails():
    snapshot = parse_spot_exchange_info(
        spot_payload([]),
        fetched_at_monotonic=100.0,
    )

    assert len(snapshot.symbols) == 0

    with pytest.raises(SymbolNotFoundError):
        snapshot.require_symbol("BTCUSDT", now_monotonic=101.0)


def test_invalid_futures_delivery_date_is_rejected():
    with pytest.raises(MetadataValidationError, match="deliveryDate"):
        parse_usdm_futures_exchange_info(
            futures_payload([
                futures_symbol(deliveryDate=-1)
            ]),
            fetched_at_monotonic=100.0,
        )


def test_futures_assets_are_exposed_as_immutable_set():
    snapshot = parse_usdm_futures_exchange_info(
        futures_payload(),
        fetched_at_monotonic=100.0,
    )

    assert snapshot.assets == frozenset({"USDT"})

    with pytest.raises(AttributeError):
        snapshot.assets.add("BTC")

def test_futures_snapshot_accepts_unicode_provider_identifiers():
    payload = futures_payload([
        futures_symbol(symbol="币安人生USDT", pair="币安人生USDT", baseAsset="币安人生")
    ])
    snapshot = parse_usdm_futures_exchange_info(payload, fetched_at_monotonic=100.0)
    record = snapshot.require_symbol("币安人生USDT", now_monotonic=101.0)
    assert record.pair == "币安人生USDT"
    assert record.base_asset == "币安人生"


def test_futures_snapshot_accepts_dated_delivery_symbol():
    payload = futures_payload([
        futures_symbol(symbol="BTCUSDT_261225", pair="BTCUSDT", contractType="CURRENT_QUARTER")
    ])
    snapshot = parse_usdm_futures_exchange_info(payload, fetched_at_monotonic=100.0)
    record = snapshot.require_symbol("BTCUSDT_261225", now_monotonic=101.0)
    assert record.pair == "BTCUSDT"
    assert record.contract_type == "CURRENT_QUARTER"


@pytest.mark.parametrize(
    "symbol",
    ["BTCUSDT_", "BTCUSDT_26122X", "BTC_USDT_261225", "BTC/USDT", "BTC USDT"],
)
def test_futures_snapshot_rejects_malformed_provider_symbols(symbol):
    with pytest.raises(MetadataValidationError):
        parse_usdm_futures_exchange_info(
            futures_payload([futures_symbol(symbol=symbol)]),
            fetched_at_monotonic=100.0,
        )


def test_spot_snapshot_still_rejects_unicode_symbol():
    with pytest.raises(MetadataValidationError):
        parse_spot_exchange_info(
            spot_payload([spot_symbol(symbol="币安人生USDT")]),
            fetched_at_monotonic=100.0,
        )
