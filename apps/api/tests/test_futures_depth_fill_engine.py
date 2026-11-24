from decimal import Decimal as D, localcontext
import pytest

from app.services.futures_depth_fill_engine import calculate_futures_depth_fill


@pytest.fixture
def book():
    return {
        "bids": ((D("100.00"), D("2")), (D("99.00"), D("3"))),
        "asks": ((D("101.00"), D("1")), (D("102.00"), D("3"))),
        "tick_size": D("0.01"),
    }


def expected_vwap(numerator, denominator):
    with localcontext() as context:
        context.prec = 120
        return D(numerator) / D(denominator)


def test_buy_consumes_asks_in_price_priority_and_calculates_vwap(book):
    result = calculate_futures_depth_fill(side="buy", quantity=D("2"), **book)
    assert result.gross_value == D("203")
    assert result.vwap == expected_vwap("203", "2")
    assert result.level_count == 2
    assert result.levels[0].price == D("101.00")
    assert result.levels[0].quantity == D("1")
    assert result.levels[1].price == D("102.00")
    assert result.levels[1].quantity == D("1")


def test_sell_consumes_bids_in_price_priority_and_calculates_vwap(book):
    result = calculate_futures_depth_fill(side="sell", quantity=D("3"), **book)
    assert result.gross_value == D("299")
    assert result.vwap == expected_vwap("299", "3")
    assert result.level_count == 2
    assert result.levels[0].price == D("100.00")
    assert result.levels[0].quantity == D("2")
    assert result.levels[1].price == D("99.00")
    assert result.levels[1].quantity == D("1")


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_exact_single_level_fill_is_supported(book, side):
    result = calculate_futures_depth_fill(side=side, quantity=D("1"), **book)
    assert result.executed_quantity == D("1")
    assert result.level_count == 1


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_insufficient_liquidity_rejects_full_order(book, side):
    book["asks"] = ((D("101"), D("1")),)
    book["bids"] = ((D("100"), D("1")),)
    with pytest.raises(ValueError, match="insufficient depth"):
        calculate_futures_depth_fill(side=side, quantity=D("2"), **book)


@pytest.mark.parametrize("side", ["BUY", "Sell", "", "hold"])
def test_invalid_side_rejected(book, side):
    with pytest.raises(ValueError, match="side"):
        calculate_futures_depth_fill(side=side, quantity=D("1"), **book)


@pytest.mark.parametrize("quantity", [D("0"), D("-1"), D("NaN"), D("Infinity"), 1])
def test_invalid_quantity_rejected(book, quantity):
    with pytest.raises(ValueError):
        calculate_futures_depth_fill(side="buy", quantity=quantity, **book)


@pytest.mark.parametrize("tick", [D("0"), D("-0.01"), D("NaN"), 0.01])
def test_invalid_tick_size_rejected(book, tick):
    book["tick_size"] = tick
    with pytest.raises(ValueError):
        calculate_futures_depth_fill(side="buy", quantity=D("1"), **book)


def test_off_tick_price_rejected(book):
    book["asks"] = ((D("101.001"), D("2")),)
    with pytest.raises(ValueError, match="tick aligned"):
        calculate_futures_depth_fill(side="buy", quantity=D("1"), **book)


def test_unsorted_bids_rejected(book):
    book["bids"] = tuple(reversed(book["bids"]))
    with pytest.raises(ValueError, match="sorted"):
        calculate_futures_depth_fill(side="buy", quantity=D("1"), **book)


def test_unsorted_asks_rejected(book):
    book["asks"] = tuple(reversed(book["asks"]))
    with pytest.raises(ValueError, match="sorted"):
        calculate_futures_depth_fill(side="buy", quantity=D("1"), **book)


def test_duplicate_prices_rejected(book):
    book["asks"] = ((D("101"), D("1")), (D("101"), D("2")))
    with pytest.raises(ValueError, match="duplicate"):
        calculate_futures_depth_fill(side="buy", quantity=D("1"), **book)


def test_zero_level_quantity_rejected(book):
    book["asks"] = ((D("101"), D("0")),)
    with pytest.raises(ValueError):
        calculate_futures_depth_fill(side="buy", quantity=D("1"), **book)


def test_malformed_level_rejected(book):
    book["asks"] = ((D("101"), D("1"), D("3")),)
    with pytest.raises(ValueError, match="2-item"):
        calculate_futures_depth_fill(side="buy", quantity=D("1"), **book)


def test_crossed_book_rejected(book):
    book["bids"] = ((D("102"), D("2")),)
    book["asks"] = ((D("101"), D("2")),)
    with pytest.raises(ValueError, match="crossed"):
        calculate_futures_depth_fill(side="buy", quantity=D("1"), **book)


def test_decimal_outputs_and_fill_notional(book):
    result = calculate_futures_depth_fill(side="buy", quantity=D("2"), **book)
    assert type(result.vwap) is D
    assert type(result.gross_value) is D
    assert all(type(level.notional) is D for level in result.levels)
    assert sum((level.notional for level in result.levels), D("0")) == result.gross_value
