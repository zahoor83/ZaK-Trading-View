from decimal import Decimal

import pytest

from app.market_data.symbol_metadata import (
    MetadataStaleError,
    SymbolNotFoundError,
    MetadataValidationError,
    parse_spot_exchange_info,
    parse_usdm_futures_exchange_info,
)
from app.market_data.symbol_filters import (
    FilterValidationError,
    ReferencePrice,
    get_price_tick_size,
    parse_decimal,
    validate_order,
)


def price_filter(minimum="0.1", maximum="100", tick="0.1"):
    return {
        "filterType": "PRICE_FILTER",
        "minPrice": minimum,
        "maxPrice": maximum,
        "tickSize": tick,
    }


def lot_filter(minimum="0.01", maximum="100", step="0.01"):
    return {
        "filterType": "LOT_SIZE",
        "minQty": minimum,
        "maxQty": maximum,
        "stepSize": step,
    }


def market_lot_filter():
    return {
        "filterType": "MARKET_LOT_SIZE",
        "minQty": "0.01",
        "maxQty": "100",
        "stepSize": "0.01",
    }


def spot_symbol(filters=None):
    return {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": (
            [price_filter(), lot_filter()]
            if filters is None else filters
        ),
    }


def futures_symbol(filters=None):
    return {
        "symbol": "BTCUSDT",
        "pair": "BTCUSDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "deliveryDate": 0,
        "onboardDate": 1600000000000,
        "pricePrecision": 2,
        "quantityPrecision": 3,
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": (
            [price_filter(), lot_filter()]
            if filters is None else filters
        ),
    }


def snapshot(market="spot", filters=None, fetched=100):
    if market == "spot":
        return parse_spot_exchange_info(
            {
                "serverTime": 1700000000000,
                "symbols": [spot_symbol(filters)],
            },
            fetched_at_monotonic=fetched,
        )

    return parse_usdm_futures_exchange_info(
        {
            "serverTime": 1700000000000,
            "assets": [{
                "asset": "USDT",
                "marginAvailable": True,
                "autoAssetExchange": "",
            }],
            "symbols": [futures_symbol(filters)],
        },
        fetched_at_monotonic=fetched,
    )


def validate(snap, **kwargs):
    defaults = {
        "order_type": "LIMIT",
        "side": "BUY",
        "quantity": "0.03",
        "price": "10.2",
        "now_monotonic": 101,
    }
    defaults.update(kwargs)
    return validate_order(snap, "BTCUSDT", **defaults)


@pytest.mark.parametrize("bad", [
    True,
    False,
    1.25,
    float("nan"),
    float("inf"),
    " NaN",
    " 1.2",
    "1.2 ",
    "1e-3",
    "Infinity",
    "NaN",
    "",
    ".5",
    "1.",
])
def test_decimal_rejects_unsafe_input(bad):
    with pytest.raises(FilterValidationError):
        parse_decimal(bad)


def test_decimal_rejects_excessive_text_length():
    with pytest.raises(FilterValidationError):
        parse_decimal("1" * 129)


def test_decimal_rejects_excessive_fractional_scale():
    with pytest.raises(FilterValidationError):
        parse_decimal("0." + "1" * 37)


def test_decimal_rejects_values_outside_safe_bound():
    with pytest.raises(FilterValidationError):
        parse_decimal("1000000000000000000000000000000000001")


def test_decimal_accepts_exact_decimal():
    assert parse_decimal("0.00100") == Decimal("0.00100")
    assert parse_decimal(Decimal("1.25")) == Decimal("1.25")


def test_spot_price_grid_is_zero_based():
    snap = snapshot("spot")
    validate(snap, price="10.3")
    with pytest.raises(FilterValidationError, match="off-grid"):
        validate(snap, price="10.31")


def test_spot_price_minimum_boundary():
    snap = snapshot("spot")
    validate(snap, price="0.1")
    with pytest.raises(FilterValidationError, match="below minimum"):
        validate(snap, price="0.09")


def test_spot_price_maximum_boundary():
    snap = snapshot("spot")
    validate(snap, price="100")
    with pytest.raises(FilterValidationError, match="above maximum"):
        validate(snap, price="100.1")


def test_futures_price_grid_is_minimum_offset():
    snap = snapshot(
        "usdm_futures",
        [price_filter("0.1", "100", "0.2"), lot_filter()],
    )
    validate(snap, price="0.3")
    with pytest.raises(FilterValidationError, match="off-grid"):
        validate(snap, price="0.2")


def test_futures_quantity_grid_is_minimum_offset():
    snap = snapshot(
        "usdm_futures",
        [price_filter(), lot_filter("0.01", "100", "0.02")],
    )
    validate(snap, quantity="0.03")
    with pytest.raises(FilterValidationError, match="off-grid"):
        validate(snap, quantity="0.02")


def test_spot_quantity_grid_is_zero_based():
    snap = snapshot("spot")
    validate(snap, quantity="0.03")
    with pytest.raises(FilterValidationError, match="off-grid"):
        validate(snap, quantity="0.035")


def test_quantity_minimum_and_maximum():
    snap = snapshot(
        "spot",
        [price_filter(), lot_filter("0.02", "0.05", "0.01")],
    )
    validate(snap, quantity="0.02")
    validate(snap, quantity="0.05")

    with pytest.raises(FilterValidationError, match="below minimum"):
        validate(snap, quantity="0.01")

    with pytest.raises(FilterValidationError, match="above maximum"):
        validate(snap, quantity="0.06")


def test_market_lot_size_only_applies_to_market_orders():
    snap = snapshot(
        "spot",
        [price_filter(), lot_filter(), market_lot_filter()],
    )
    validate(snap, quantity="0.03")
    validate(snap, order_type="MARKET", price=None, quantity="0.03")


def test_market_lot_size_rejects_invalid_market_quantity():
    market_filter = market_lot_filter()
    market_filter["stepSize"] = "0.02"
    snap = snapshot(
        "spot",
        [price_filter(), lot_filter(), market_filter],
    )

    with pytest.raises(FilterValidationError, match="off-grid"):
        validate(snap, order_type="MARKET", price=None, quantity="0.03")


def test_disabled_zero_price_components():
    snap = snapshot(
        "spot",
        [
            price_filter("0", "0", "0"),
            lot_filter(),
        ],
    )
    validate(snap, price="123.456")


def test_zero_quantity_step_is_rejected():
    snap = snapshot(
        "spot",
        [price_filter(), lot_filter(step="0")],
    )
    with pytest.raises(FilterValidationError, match="zero stepSize"):
        validate(snap)


def test_missing_mandatory_lot_size_fails_closed():
    snap = snapshot("spot", [price_filter()])
    with pytest.raises(FilterValidationError, match="LOT_SIZE"):
        validate(snap)


def test_missing_mandatory_price_filter_fails_closed():
    snap = snapshot("spot", [lot_filter()])
    with pytest.raises(FilterValidationError, match="PRICE_FILTER"):
        validate(snap)


def test_unknown_filter_is_preserved_and_rejected():
    unknown = {
        "filterType": "FUTURE_PROVIDER_FILTER",
        "providerValue": "preserved",
    }
    snap = snapshot("spot", [
        price_filter(),
        lot_filter(),
        unknown,
    ])

    metadata = snap.require_symbol("BTCUSDT", now_monotonic=101)
    assert metadata.filters[-1]["providerValue"] == "preserved"

    with pytest.raises(FilterValidationError, match="Unsupported filters"):
        validate(snap)


def test_filter_metadata_is_immutable():
    snap = snapshot("spot")
    metadata = snap.require_symbol("BTCUSDT", now_monotonic=101)

    with pytest.raises(TypeError):
        metadata.filters[0]["tickSize"] = "1"


def test_duplicate_filter_types_are_rejected():
    with pytest.raises(MetadataValidationError, match="duplicate filterType"):
        snapshot("spot", [
            price_filter(),
            price_filter(),
            lot_filter(),
        ])


def test_spot_min_notional():
    minimum = {
        "filterType": "MIN_NOTIONAL",
        "minNotional": "10",
        "applyToMarket": True,
        "avgPriceMins": 5,
    }
    snap = snapshot("spot", [price_filter(), lot_filter(), minimum])

    validate(snap, quantity="1", price="10")

    with pytest.raises(FilterValidationError, match="below minimum notional"):
        validate(snap, quantity="0.5", price="10")


def test_spot_maximum_notional():
    notional = {
        "filterType": "NOTIONAL",
        "minNotional": "1",
        "maxNotional": "20",
        "applyMinToMarket": True,
        "applyMaxToMarket": True,
        "avgPriceMins": 5,
    }
    snap = snapshot("spot", [price_filter(), lot_filter(), notional])

    with pytest.raises(FilterValidationError, match="above maximum notional"):
        validate(snap, quantity="3", price="10")


def test_spot_market_min_notional_requires_reference_price():
    minimum = {
        "filterType": "MIN_NOTIONAL",
        "minNotional": "10",
        "applyToMarket": True,
        "avgPriceMins": 5,
    }
    snap = snapshot("spot", [price_filter(), lot_filter(), minimum])

    with pytest.raises(FilterValidationError, match="reference price"):
        validate(snap, order_type="MARKET", price=None, quantity="1")

    ref = ReferencePrice("10", 100, 5)
    validate(
        snap,
        order_type="MARKET",
        price=None,
        quantity="1",
        reference_price=ref,
    )


def test_spot_market_notional_is_skipped_when_disabled():
    minimum = {
        "filterType": "MIN_NOTIONAL",
        "minNotional": "10",
        "applyToMarket": False,
        "avgPriceMins": 5,
    }
    snap = snapshot("spot", [price_filter(), lot_filter(), minimum])

    validate(snap, order_type="MARKET", price=None, quantity="0.03")


def test_futures_min_notional_uses_notional_field():
    minimum = {
        "filterType": "MIN_NOTIONAL",
        "notional": "10",
    }
    snap = snapshot(
        "usdm_futures",
        [price_filter(), lot_filter(), minimum],
    )

    validate(snap, price="10", quantity="1")

    with pytest.raises(FilterValidationError, match="below minimum notional"):
        validate(snap, price="5", quantity="1")


def test_futures_market_notional_requires_verified_reference():
    minimum = {
        "filterType": "MIN_NOTIONAL",
        "notional": "10",
    }
    snap = snapshot(
        "usdm_futures",
        [price_filter(), lot_filter(), minimum],
    )

    with pytest.raises(FilterValidationError, match="reference price"):
        validate(snap, order_type="MARKET", price=None, quantity="1")

    ref = ReferencePrice("10", 100, 5)
    validate(
        snap,
        order_type="MARKET",
        price=None,
        quantity="1",
        reference_price=ref,
    )


def test_dynamic_percent_filter_requires_reference():
    percent = {
        "filterType": "PERCENT_PRICE",
        "multiplierUp": "1.1",
        "multiplierDown": "0.9",
        "avgPriceMins": 5,
    }
    snap = snapshot("spot", [price_filter(), lot_filter(), percent])

    with pytest.raises(FilterValidationError, match="reference price"):
        validate(snap, price="10")


def test_dynamic_percent_filter_uses_reference():
    percent = {
        "filterType": "PERCENT_PRICE",
        "multiplierUp": "1.1",
        "multiplierDown": "0.9",
        "avgPriceMins": 5,
    }
    snap = snapshot("spot", [price_filter(), lot_filter(), percent])

    ref = ReferencePrice("10", 100, 5)

    validate(snap, price="10.5", reference_price=ref)

    with pytest.raises(FilterValidationError, match="outside reference range"):
        validate(snap, price="12", reference_price=ref)


def test_reference_price_expiration_fails_closed():
    ref = ReferencePrice("10", 100, 2)

    with pytest.raises(FilterValidationError, match="stale"):
        ref.require_fresh(103)


def test_reference_price_future_timestamp_fails_closed():
    ref = ReferencePrice("10", 105, 5)

    with pytest.raises(FilterValidationError, match="stale"):
        ref.require_fresh(100)


def test_stale_metadata_snapshot_is_rejected():
    snap = snapshot("spot", fetched=100)

    with pytest.raises(MetadataStaleError):
        validate(snap, now_monotonic=401)


def test_negative_and_zero_order_values_are_rejected():
    snap = snapshot("spot")

    with pytest.raises(FilterValidationError):
        validate(snap, quantity="0")

    with pytest.raises(FilterValidationError):
        validate(snap, quantity="-1")

    with pytest.raises(FilterValidationError):
        validate(snap, price="0")


def test_market_order_cannot_supply_limit_price():
    snap = snapshot("spot")

    with pytest.raises(FilterValidationError, match="must not provide"):
        validate(snap, order_type="MARKET", price="10")


def test_invalid_order_side_is_rejected():
    snap = snapshot("spot")

    with pytest.raises(FilterValidationError, match="side"):
        validate(snap, side="INVALID")


def test_no_network_or_order_submission_is_performed():
    from app.market_data import symbol_filters

    assert not hasattr(symbol_filters, "requests")
    assert not hasattr(symbol_filters, "httpx")
    assert not hasattr(symbol_filters, "BinanceSpotRestClient")
    assert not hasattr(symbol_filters, "BinanceUsdMFuturesRestClient")

def test_get_price_tick_size_from_fresh_metadata():
    snap = snapshot("spot", fetched=100)

    result = get_price_tick_size(
        snap,
        "BTCUSDT",
        now_monotonic=101,
    )

    assert result == Decimal("0.1")
    assert isinstance(result, Decimal)


def test_get_price_tick_size_rejects_missing_price_filter():
    snap = snapshot("spot", [lot_filter()], fetched=100)

    with pytest.raises(FilterValidationError, match="PRICE_FILTER"):
        get_price_tick_size(snap, "BTCUSDT", now_monotonic=101)


@pytest.mark.parametrize("bad_tick", [
    "0",
    "-0.1",
    "NaN",
    "Infinity",
    "1e-2",
    " 0.1",
])
def test_get_price_tick_size_rejects_invalid_tick_values(bad_tick):
    snap = snapshot(
        "spot",
        [price_filter(tick=bad_tick), lot_filter()],
        fetched=100,
    )

    with pytest.raises(FilterValidationError):
        get_price_tick_size(snap, "BTCUSDT", now_monotonic=101)


def test_get_price_tick_size_rejects_stale_metadata():
    snap = snapshot("spot", fetched=100)

    with pytest.raises(MetadataStaleError):
        get_price_tick_size(snap, "BTCUSDT", now_monotonic=401)


def test_get_price_tick_size_rejects_unverified_snapshot():
    with pytest.raises(FilterValidationError, match="snapshot"):
        get_price_tick_size(object(), "BTCUSDT", now_monotonic=101)


def test_get_price_tick_size_rejects_unknown_symbol():
    snap = snapshot("spot", fetched=100)

    with pytest.raises(SymbolNotFoundError):
        get_price_tick_size(snap, "ETHUSDT", now_monotonic=101)
