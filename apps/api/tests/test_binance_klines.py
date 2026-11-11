import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from app.market_data.binance_klines import (
    BinanceCandle,
    KlineParseError,
    parse_binance_klines,
)


class BinanceKlinesParserTests(unittest.TestCase):

    def valid_row(self):
        return [
            1000,
            "100.25",
            "110.50",
            "95.00",
            "105.75",
            "12.500",
            1999,
            "1300.25",
            42,
            "5.000",
            "520.10",
            "0",
        ]

    def parse(self, rows, market="spot", symbol="BTCUSDT", interval="1m"):
        return parse_binance_klines(
            rows,
            market=market,
            symbol=symbol,
            interval=interval,
        )

    def test_parses_spot_candle(self):
        candles = self.parse([self.valid_row()])

        self.assertEqual(len(candles), 1)
        candle = candles[0]

        self.assertIsInstance(candle, BinanceCandle)
        self.assertEqual(candle.exchange, "BINANCE")
        self.assertEqual(candle.market, "spot")
        self.assertEqual(candle.symbol, "BTCUSDT")
        self.assertEqual(candle.interval, "1m")

        self.assertEqual(candle.open_time, 1000)
        self.assertEqual(candle.close_time, 1999)
        self.assertEqual(candle.open, Decimal("100.25"))
        self.assertEqual(candle.high, Decimal("110.50"))
        self.assertEqual(candle.low, Decimal("95.00"))
        self.assertEqual(candle.close, Decimal("105.75"))
        self.assertEqual(candle.volume, Decimal("12.500"))
        self.assertEqual(candle.quote_volume, Decimal("1300.25"))
        self.assertEqual(candle.trade_count, 42)
        self.assertEqual(candle.taker_buy_base_volume, Decimal("5.000"))
        self.assertEqual(candle.taker_buy_quote_volume, Decimal("520.10"))

    def test_parses_futures_candle(self):
        candles = self.parse(
            [self.valid_row()],
            market="usdm_futures",
        )

        self.assertEqual(candles[0].market, "usdm_futures")

    def test_returns_immutable_tuple(self):
        candles = self.parse([self.valid_row()])

        self.assertIsInstance(candles, tuple)

        with self.assertRaises(FrozenInstanceError):
            candles[0].close = Decimal("200")

    def test_accepts_empty_response(self):
        self.assertEqual(self.parse([]), ())

    def test_accepts_tuple_input(self):
        candles = self.parse((tuple(self.valid_row()),))
        self.assertEqual(len(candles), 1)

    def test_spot_supports_one_second_interval(self):
        candles = self.parse(
            [self.valid_row()],
            interval="1s",
        )
        self.assertEqual(candles[0].interval, "1s")

    def test_futures_rejects_one_second_interval(self):
        with self.assertRaises(KlineParseError):
            self.parse(
                [self.valid_row()],
                market="usdm_futures",
                interval="1s",
            )

    def test_rejects_invalid_market(self):
        for market in ("future", "FUTURES", "", None, 1):
            with self.subTest(market=market):
                with self.assertRaises(KlineParseError):
                    self.parse([self.valid_row()], market=market)

    def test_rejects_invalid_symbol(self):
        for symbol in ("btcusdt", "", "BTC/USDT", "BTC USDT", None, 123):
            with self.subTest(symbol=symbol):
                with self.assertRaises(KlineParseError):
                    self.parse([self.valid_row()], symbol=symbol)

    def test_rejects_invalid_interval(self):
        for interval in ("2m", "", None, 1, "1M "):
            with self.subTest(interval=interval):
                with self.assertRaises(KlineParseError):
                    self.parse([self.valid_row()], interval=interval)

    def test_rejects_invalid_response_container(self):
        for rows in (None, "invalid", b"invalid", {}, 123):
            with self.subTest(rows=rows):
                with self.assertRaises(KlineParseError):
                    self.parse(rows)

    def test_rejects_invalid_row_shapes(self):
        invalid_rows = (
            [1],
            [],
            ["x"] * 11,
            ["x"] * 13,
            "invalid",
            b"invalid",
            None,
        )

        for row in invalid_rows:
            with self.subTest(row=row):
                with self.assertRaises(KlineParseError):
                    self.parse([row])

    def test_rejects_invalid_timestamps(self):
        for value in (-1, True, False, 1.5, "1000", None):
            with self.subTest(value=value):
                row = self.valid_row()
                row[0] = value

                with self.assertRaises(KlineParseError):
                    self.parse([row])

    def test_rejects_invalid_close_timestamp(self):
        for value in (-1, True, 1.5, "1999", None):
            with self.subTest(value=value):
                row = self.valid_row()
                row[6] = value

                with self.assertRaises(KlineParseError):
                    self.parse([row])

    def test_rejects_non_increasing_time_bounds(self):
        for close_time in (1000, 999):
            with self.subTest(close_time=close_time):
                row = self.valid_row()
                row[6] = close_time

                with self.assertRaises(KlineParseError):
                    self.parse([row])

    def test_rejects_invalid_trade_count(self):
        for value in (-1, True, False, 1.5, "42", None):
            with self.subTest(value=value):
                row = self.valid_row()
                row[8] = value

                with self.assertRaises(KlineParseError):
                    self.parse([row])

    def test_rejects_invalid_decimal_fields(self):
        decimal_indexes = (1, 2, 3, 4, 5, 7, 9, 10)

        invalid_values = (
            "",
            " ",
            "NaN",
            "Infinity",
            "-Infinity",
            "-1",
            "1e5",
            ".5",
            "1.",
            "abc",
            "1,000",
            "0x10",
            "9" * 129,
            None,
            1.5,
            100,
            True,
        )

        for index in decimal_indexes:
            for value in invalid_values:
                with self.subTest(index=index, value=value):
                    row = self.valid_row()
                    row[index] = value

                    with self.assertRaises(KlineParseError):
                        self.parse([row])

    def test_rejects_zero_or_negative_prices(self):
        for index in (1, 2, 3, 4):
            for value in ("0", "0.000", "0.1"):
                row = self.valid_row()
                row[index] = value

                if value == "0.1":
                    continue

                with self.subTest(index=index, value=value):
                    with self.assertRaises(KlineParseError):
                        self.parse([row])

    def test_rejects_invalid_ohlc_relationships(self):
        invalid_changes = (
            (2, "90"),   # High below low/open/close.
            (3, "120"),  # Low above high/open/close.
            (1, "120"),  # Open above high.
            (4, "120"),  # Close above high.
            (1, "90"),   # Open below low.
            (4, "90"),   # Close below low.
        )

        for index, value in invalid_changes:
            with self.subTest(index=index, value=value):
                row = self.valid_row()
                row[index] = value

                with self.assertRaises(KlineParseError):
                    self.parse([row])

    def test_rejects_negative_volumes(self):
        for index in (5, 7, 9, 10):
            row = self.valid_row()
            row[index] = "-0.001"

            with self.subTest(index=index):
                with self.assertRaises(KlineParseError):
                    self.parse([row])

    def test_rejects_taker_base_volume_above_total(self):
        row = self.valid_row()
        row[9] = "12.501"

        with self.assertRaises(KlineParseError):
            self.parse([row])

    def test_rejects_taker_quote_volume_above_total(self):
        row = self.valid_row()
        row[10] = "1300.26"

        with self.assertRaises(KlineParseError):
            self.parse([row])

    def test_rejects_invalid_final_field(self):
        for value in ("", None, 0, True):
            row = self.valid_row()
            row[11] = value

            with self.subTest(value=value):
                with self.assertRaises(KlineParseError):
                    self.parse([row])

    def test_preserves_input_order_and_does_not_deduplicate(self):
        first = self.valid_row()
        second = self.valid_row()
        second[0] = 3000
        second[6] = 3999

        third = self.valid_row()
        third[0] = 1000
        third[6] = 1999

        candles = self.parse([first, second, third])

        self.assertEqual(
            [candle.open_time for candle in candles],
            [1000, 3000, 1000],
        )

    def test_does_not_mutate_input_rows(self):
        row = self.valid_row()
        original = list(row)

        self.parse([row])

        self.assertEqual(row, original)

    def test_rejects_bad_row_after_valid_row(self):
        rows = [self.valid_row(), ["invalid"]]

        with self.assertRaises(KlineParseError):
            self.parse(rows)


if __name__ == "__main__":
    unittest.main()