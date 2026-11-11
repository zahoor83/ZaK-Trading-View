import pytest

from app.market_data.config import MarketDataConfig
from app.security.websocket_egress import validate_public_market_data_websocket_url


def test_empty_configuration_is_disabled():
    config = MarketDataConfig()

    assert config.is_enabled is False
    assert config.configured_groups == ()
    assert config.spot_websocket_url is None
    assert config.futures_depth_websocket_url is None
    assert config.futures_mark_price_websocket_url is None


def test_spot_configuration_builds_approved_url():
    config = MarketDataConfig(spot_symbols=("BTCUSDT", "ETHUSDT"))

    assert config.is_enabled is True

    approved = validate_public_market_data_websocket_url(
        config.spot_websocket_url
    )

    assert approved.market == "spot"
    assert approved.streams == (
        "btcusdt@trade",
        "ethusdt@trade",
    )


def test_futures_depth_uses_public_endpoint_category():
    config = MarketDataConfig(futures_depth_symbols=("BTCUSDT",))

    approved = validate_public_market_data_websocket_url(
        config.futures_depth_websocket_url
    )

    assert approved.market == "usdm_futures"
    assert approved.path == "/public/stream"
    assert approved.streams == ("btcusdt@depth",)


def test_futures_mark_price_uses_market_endpoint_category():
    config = MarketDataConfig(futures_mark_price_symbols=("ETHUSDT",))

    approved = validate_public_market_data_websocket_url(
        config.futures_mark_price_websocket_url
    )

    assert approved.market == "usdm_futures"
    assert approved.path == "/market/stream"
    assert approved.streams == ("ethusdt@markPrice",)


def test_same_symbol_can_be_configured_for_separate_futures_groups():
    config = MarketDataConfig(
        futures_depth_symbols=("BTCUSDT",),
        futures_mark_price_symbols=("BTCUSDT",),
    )

    assert config.futures_depth_websocket_url is not None
    assert config.futures_mark_price_websocket_url is not None


@pytest.mark.parametrize(
    "field",
    [
        "spot_symbols",
        "futures_depth_symbols",
        "futures_mark_price_symbols",
    ],
)
@pytest.mark.parametrize(
    "value",
    [
        ["BTCUSDT"],
        ("btcusdt",),
        ("BTC/USDT",),
        ("BTC USDT",),
        ("",),
        (123,),
    ],
)
def test_invalid_symbol_configuration_is_rejected(field, value):
    with pytest.raises((TypeError, ValueError)):
        MarketDataConfig(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "spot_symbols",
        "futures_depth_symbols",
        "futures_mark_price_symbols",
    ],
)
def test_duplicate_symbols_are_rejected(field):
    with pytest.raises(ValueError):
        MarketDataConfig(**{field: ("BTCUSDT", "BTCUSDT")})


def test_stream_limit_is_enforced():
    symbols = tuple(f"COIN{i:04d}" for i in range(1025))

    with pytest.raises(ValueError):
        MarketDataConfig(spot_symbols=symbols)


def test_configuration_is_immutable():
    config = MarketDataConfig(spot_symbols=("BTCUSDT",))

    with pytest.raises(AttributeError):
        config.spot_symbols = ("ETHUSDT",)


def test_configured_groups_include_only_nonempty_groups():
    config = MarketDataConfig(
        spot_symbols=("BTCUSDT",),
        futures_mark_price_symbols=("ETHUSDT",),
    )

    assert config.configured_groups == (
        ("spot", ("BTCUSDT",)),
        ("futures_mark_price", ("ETHUSDT",)),
    )