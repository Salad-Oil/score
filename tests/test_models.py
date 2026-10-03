"""Tests for :mod:`roostoo.models`.

Two themes run through this module:

* ``fmt`` must round **down** and never emit float repr noise, because a signed
  payload that does not match the server's idea of the value is rejected.
* An order response is only a fill if it *says* it is.  The documented MAKER
  response shape is ``PENDING`` with a zero filled quantity, and treating that
  as a fill would double-count inventory.
"""

from __future__ import annotations

import math
import unittest

from roostoo.models import (
    ExchangeInfo,
    OrderResult,
    Position,
    ShortPosition,
    Ticker,
    TradePair,
    WalletBalance,
    fmt,
)


# ---------------------------------------------------------------------------
# fmt
# ---------------------------------------------------------------------------


class TestFmt(unittest.TestCase):
    def test_float_noise_is_removed(self) -> None:
        """``0.1 + 0.2`` must serialise as ``0.30``, not ``0.30000000000000004``."""
        self.assertEqual(fmt(0.1 + 0.2, 2), "0.30")

    def test_rounds_down_never_up(self) -> None:
        """ROUND_DOWN can only ever shrink an order -- never overspend."""
        self.assertEqual(fmt(1.009, 2), "1.00")
        self.assertEqual(fmt(1.999, 2), "1.99")
        self.assertEqual(fmt(2.999, 0), "2")
        self.assertEqual(fmt(0.9999999, 6), "0.999999")

    def test_exact_values_are_unchanged(self) -> None:
        self.assertEqual(fmt(1.005, 2), "1.00")
        self.assertEqual(fmt(610.5, 2), "610.50")
        self.assertEqual(fmt(2000.0, 0), "2000")

    def test_precision_zero_has_no_decimal_point(self) -> None:
        self.assertEqual(fmt(7.8, 0), "7")
        self.assertNotIn(".", fmt(7.8, 0))

    def test_precision_six_pads_with_zeros(self) -> None:
        self.assertEqual(fmt(0.5, 6), "0.500000")
        self.assertEqual(fmt(0.123456, 6), "0.123456")

    def test_precision_six_truncates_beyond_six_places(self) -> None:
        self.assertEqual(fmt(0.1234567, 6), "0.123456")

    def test_padding_is_applied_for_small_precision(self) -> None:
        """The exchange expects a fixed width, so trailing zeros matter."""
        self.assertEqual(fmt(2, 3), "2.000")
        self.assertEqual(fmt(0.0, 2), "0.00")

    def test_negative_values_round_towards_zero(self) -> None:
        """ROUND_DOWN truncates towards zero: -1.239 -> -1.23."""
        self.assertEqual(fmt(-1.239, 2), "-1.23")

    def test_negative_precision_raises(self) -> None:
        with self.assertRaises(ValueError):
            fmt(1.0, -1)

    def test_integer_input_is_supported(self) -> None:
        self.assertEqual(fmt(3, 2), "3.00")

    def test_string_numeric_input_is_supported(self) -> None:
        """Callers often pass an already-formatted value straight through."""
        self.assertEqual(fmt("1.239", 2), "1.23")


# ---------------------------------------------------------------------------
# TradePair
# ---------------------------------------------------------------------------


def make_pair(**overrides: object) -> TradePair:
    base: dict[str, object] = dict(
        pair="BNB/USD",
        coin="BNB",
        unit="USD",
        can_trade=True,
        price_precision=3,
        amount_precision=3,
        min_order=1.0,
    )
    base.update(overrides)
    return TradePair(**base)  # type: ignore[arg-type]


class TestTradePair(unittest.TestCase):
    def test_base_and_quote_aliases(self) -> None:
        pair = make_pair()
        self.assertEqual(pair.base, "BNB")
        self.assertEqual(pair.quote, "USD")

    def test_from_api_reads_documented_fields(self) -> None:
        pair = TradePair.from_api(
            "BNB/USD",
            {
                "Coin": "BNB",
                "Unit": "USD",
                "CanTrade": True,
                "PricePrecision": 3,
                "AmountPrecision": 4,
                "MiniOrder": 5.0,
            },
        )
        self.assertEqual(pair.pair, "BNB/USD")
        self.assertEqual(pair.coin, "BNB")
        self.assertEqual(pair.unit, "USD")
        self.assertTrue(pair.can_trade)
        self.assertEqual(pair.price_precision, 3)
        self.assertEqual(pair.amount_precision, 4)
        self.assertAlmostEqual(pair.min_order, 5.0)

    def test_from_api_defaults_when_fields_are_missing(self) -> None:
        """A sparse row must not blow up: sensible precision defaults apply."""
        pair = TradePair.from_api("ETH/USD", {})
        self.assertEqual(pair.coin, "ETH")  # derived from the pair name
        self.assertEqual(pair.unit, "USD")
        self.assertFalse(pair.can_trade)
        self.assertEqual(pair.price_precision, 2)
        self.assertEqual(pair.amount_precision, 6)
        self.assertAlmostEqual(pair.min_order, 0.0)

    def test_round_qty_uses_amount_precision(self) -> None:
        self.assertEqual(make_pair(amount_precision=3).round_qty(1.23456), "1.234")

    def test_round_price_uses_price_precision(self) -> None:
        self.assertEqual(make_pair(price_precision=3).round_price(610.5678), "610.567")

    def test_rounding_is_down_for_both(self) -> None:
        pair = make_pair(price_precision=2, amount_precision=2)
        self.assertEqual(pair.round_price(1.999), "1.99")
        self.assertEqual(pair.round_qty(1.999), "1.99")

    def test_min_qty_for_divides_notional_by_price(self) -> None:
        """Smallest tradable quantity for a $10 minimum at $100 is 0.1."""
        self.assertAlmostEqual(make_pair(min_order=10.0).min_qty_for(100.0), 0.1)

    def test_min_qty_for_non_positive_price_is_infinite(self) -> None:
        """No price means no tradable quantity -- infinity blocks the order."""
        pair = make_pair(min_order=10.0)
        self.assertTrue(math.isinf(pair.min_qty_for(0.0)))
        self.assertTrue(math.isinf(pair.min_qty_for(-5.0)))


# ---------------------------------------------------------------------------
# Ticker
# ---------------------------------------------------------------------------


class TestTicker(unittest.TestCase):
    def test_mid_is_the_bid_ask_midpoint(self) -> None:
        ticker = Ticker("BNB/USD", 100.0, 99.0, 101.0, 0.0, 0.0, 0.0, 1)
        self.assertAlmostEqual(ticker.mid, 100.0)

    def test_spread_bps_is_quoted_spread_over_mid(self) -> None:
        """2.0 wide on a 100.0 mid is 200 bps."""
        ticker = Ticker("BNB/USD", 100.0, 99.0, 101.0, 0.0, 0.0, 0.0, 1)
        self.assertAlmostEqual(ticker.spread_bps, 200.0)

    def test_spread_bps_is_ten_for_a_ten_basis_point_book(self) -> None:
        ticker = Ticker("BNB/USD", 100.0, 99.95, 100.05, 0.0, 0.0, 0.0, 1)
        self.assertAlmostEqual(ticker.spread_bps, 10.0, places=9)

    def test_mid_falls_back_to_last_when_the_book_is_empty(self) -> None:
        """No bid/ask (pre-open) means the last trade is the best estimate."""
        ticker = Ticker("BNB/USD", 42.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1)
        self.assertAlmostEqual(ticker.mid, 42.0)

    def test_spread_bps_is_zero_without_a_book(self) -> None:
        ticker = Ticker("BNB/USD", 42.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1)
        self.assertAlmostEqual(ticker.spread_bps, 0.0)

    def test_from_api_maps_every_documented_field(self) -> None:
        ticker = Ticker.from_api(
            "BNB/USD",
            {
                "LastPrice": 610.5,
                "MaxBid": 610.0,
                "MinAsk": 611.0,
                "Change": 0.02,
                "CoinTradeValue": 12.5,
                "UnitTradeValue": 7600.0,
            },
            server_time_ms=1_700_000_000_000,
        )
        self.assertEqual(ticker.pair, "BNB/USD")
        self.assertAlmostEqual(ticker.last, 610.5)
        self.assertAlmostEqual(ticker.max_bid, 610.0)
        self.assertAlmostEqual(ticker.min_ask, 611.0)
        self.assertAlmostEqual(ticker.change_24h, 0.02)
        self.assertAlmostEqual(ticker.coin_volume, 12.5)
        self.assertAlmostEqual(ticker.unit_volume, 7600.0)
        self.assertEqual(ticker.server_time_ms, 1_700_000_000_000)

    def test_from_api_tolerates_a_sparse_row(self) -> None:
        ticker = Ticker.from_api("XRP/USD", {}, server_time_ms=0)
        self.assertAlmostEqual(ticker.last, 0.0)
        self.assertAlmostEqual(ticker.mid, 0.0)
        self.assertAlmostEqual(ticker.spread_bps, 0.0)


# ---------------------------------------------------------------------------
# OrderResult
# ---------------------------------------------------------------------------


TAKER_RESPONSE = {
    "Success": True,
    "ErrMsg": "",
    "OrderDetail": {
        "Pair": "BNB/USD",
        "OrderID": 1001,
        "Status": "FILLED",
        "Role": "TAKER",
        "ServerTimeUsage": 0.001,
        "CreateTimestamp": 1_700_000_000_000,
        "FinishTimestamp": 1_700_000_000_010,
        "Side": "BUY",
        "Type": "MARKET",
        "StopType": "GTC",
        "Price": 610.5,
        "Quantity": 1.5,
        "FilledQuantity": 1.5,
        "FilledAverPrice": 610.5,
        "CoinChange": 1.5,
        "UnitChange": -915.75,
        "CommissionCoin": "USD",
        "CommissionChargeValue": 0.91575,
        "CommissionPercent": 0.001,
    },
}

MAKER_RESPONSE = {
    "Success": True,
    "ErrMsg": "",
    "OrderDetail": {
        "Pair": "BNB/USD",
        "OrderID": 1002,
        "Status": "PENDING",
        "Role": "MAKER",
        "CreateTimestamp": 1_700_000_000_000,
        "FinishTimestamp": 0,
        "Side": "BUY",
        "Type": "LIMIT",
        "Price": 600.0,
        "Quantity": 1.0,
        "FilledQuantity": 0.0,
        "FilledAverPrice": 0.0,
        "CommissionCoin": "USD",
        "CommissionChargeValue": 0.0,
        "CommissionPercent": 0.0005,
    },
}


class TestOrderResultTaker(unittest.TestCase):
    """The documented fully-filled (TAKER) response shape."""

    def test_taker_order_is_filled(self) -> None:
        result = OrderResult.from_api("BNB/USD", "BUY", "MARKET", 1.5, TAKER_RESPONSE)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.role, "TAKER")
        self.assertEqual(result.order_id, 1001)

    def test_taker_order_reports_its_fill(self) -> None:
        result = OrderResult.from_api("BNB/USD", "BUY", "MARKET", 1.5, TAKER_RESPONSE)
        self.assertAlmostEqual(result.filled_quantity, 1.5)
        self.assertAlmostEqual(result.avg_fill_price, 610.5)
        self.assertAlmostEqual(result.commission, 0.91575)
        self.assertEqual(result.commission_coin, "USD")

    def test_taker_order_is_live(self) -> None:
        result = OrderResult.from_api("BNB/USD", "BUY", "MARKET", 1.5, TAKER_RESPONSE)
        self.assertTrue(result.is_live)

    def test_raw_payload_is_retained_for_the_journal(self) -> None:
        result = OrderResult.from_api("BNB/USD", "BUY", "MARKET", 1.5, TAKER_RESPONSE)
        self.assertIs(result.raw, TAKER_RESPONSE)


class TestOrderResultMaker(unittest.TestCase):
    """The documented resting-limit (MAKER/PENDING) response shape."""

    def test_maker_order_is_pending_with_zero_fill(self) -> None:
        """A resting limit order has not traded: filled_quantity must be 0."""
        result = OrderResult.from_api("BNB/USD", "BUY", "LIMIT", 1.0, MAKER_RESPONSE)
        self.assertEqual(result.status, "PENDING")
        self.assertAlmostEqual(result.filled_quantity, 0.0)
        self.assertAlmostEqual(result.avg_fill_price, 0.0)

    def test_maker_order_is_not_a_fill(self) -> None:
        """It is live (the engine must track it) but it is not filled."""
        result = OrderResult.from_api("BNB/USD", "BUY", "LIMIT", 1.0, MAKER_RESPONSE)
        self.assertTrue(result.is_live)
        self.assertNotEqual(result.status, "FILLED")
        self.assertLess(result.filled_quantity, result.quantity)

    def test_maker_order_keeps_its_price_and_role(self) -> None:
        result = OrderResult.from_api("BNB/USD", "BUY", "LIMIT", 1.0, MAKER_RESPONSE)
        self.assertAlmostEqual(result.price, 600.0)
        self.assertEqual(result.role, "MAKER")
        self.assertEqual(result.order_type, "LIMIT")

    def test_maker_order_charges_no_commission_yet(self) -> None:
        result = OrderResult.from_api("BNB/USD", "BUY", "LIMIT", 1.0, MAKER_RESPONSE)
        self.assertAlmostEqual(result.commission, 0.0)


class TestOrderResultFallbacks(unittest.TestCase):
    def test_request_values_are_used_when_the_detail_is_missing(self) -> None:
        """A sparse response must not lose the caller's intent."""
        result = OrderResult.from_api("ETH/USD", "SELL", "MARKET", 2.0, {"Success": True})
        self.assertEqual(result.pair, "ETH/USD")
        self.assertEqual(result.side, "SELL")
        self.assertEqual(result.order_type, "MARKET")
        self.assertAlmostEqual(result.quantity, 2.0)
        self.assertIsNone(result.order_id)

    def test_a_missing_status_is_unknown_never_filled(self) -> None:
        """Unstated status must not be read as a completed fill.

        Defaulting to FILLED meant a history row with no Status -- and
        FilledQuantity 0 -- reached the execution path as a full-size fill, which
        booked a phantom position at price zero and wrote it to positions.json.
        """
        result = OrderResult.from_api("ETH/USD", "SELL", "MARKET", 2.0, {"Success": True})
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.is_live)

        blank = OrderResult.from_api(
            "ETH/USD", "SELL", "MARKET", 2.0, {"Success": True, "OrderDetail": {"Status": ""}}
        )
        self.assertEqual(blank.status, "UNKNOWN")

    def test_lowercase_status_is_normalised(self) -> None:
        result = OrderResult.from_api(
            "ETH/USD", "SELL", "MARKET", 2.0, {"Success": True, "OrderDetail": {"Status": "filled"}}
        )
        self.assertEqual(result.status, "FILLED")

    def test_rejected_status_is_not_live(self) -> None:
        result = OrderResult(
            pair="BNB/USD",
            side="BUY",
            order_type="MARKET",
            quantity=1.0,
            price=0.0,
            status="REJECTED",
            err_msg="insufficient balance",
        )
        self.assertFalse(result.is_live)

    def test_unknown_status_is_not_live(self) -> None:
        """UNKNOWN must never look live: the engine reconciles it explicitly."""
        result = OrderResult(
            pair="BNB/USD",
            side="BUY",
            order_type="MARKET",
            quantity=1.0,
            price=0.0,
            status="UNKNOWN",
        )
        self.assertFalse(result.is_live)


# ---------------------------------------------------------------------------
# ShortPosition
# ---------------------------------------------------------------------------


class TestShortPosition(unittest.TestCase):
    def test_from_api_maps_documented_fields(self) -> None:
        position = ShortPosition.from_api(
            {
                "ID": 55,
                "Pair": "BNB/USD",
                "EntryPrice": 600.0,
                "ShortQty": 0.5,
                "Collateral": 300.0,
                "CurrentPrice": 580.0,
                "UnrealizedPNL": 10.0,
                "UnrealizedPNLPct": 0.0333,
                "PositionValue": 310.0,
                "CreateTimestamp": 1_700_000_000_000,
            }
        )
        self.assertEqual(position.position_id, 55)
        self.assertEqual(position.pair, "BNB/USD")
        self.assertAlmostEqual(position.entry_price, 600.0)
        self.assertAlmostEqual(position.quantity, 0.5)
        self.assertAlmostEqual(position.collateral, 300.0)
        self.assertAlmostEqual(position.current_price, 580.0)
        self.assertAlmostEqual(position.unrealized_pnl, 10.0)
        self.assertAlmostEqual(position.unrealized_pct, 0.0333)
        self.assertAlmostEqual(position.position_value, 310.0)
        self.assertEqual(position.created_ts_ms, 1_700_000_000_000)

    def test_from_api_tolerates_a_sparse_row(self) -> None:
        position = ShortPosition.from_api({})
        self.assertEqual(position.position_id, 0)
        self.assertEqual(position.pair, "")
        self.assertAlmostEqual(position.collateral, 0.0)


# ---------------------------------------------------------------------------
# ExchangeInfo
# ---------------------------------------------------------------------------


class TestExchangeInfo(unittest.TestCase):
    def test_from_api_builds_the_pair_map(self) -> None:
        info = ExchangeInfo.from_api(
            {
                "IsRunning": True,
                "InitialWallet": {"USD": 100000.0, "BNB": 0.0},
                "TradePairs": {
                    "BNB/USD": {
                        "Coin": "BNB",
                        "Unit": "USD",
                        "CanTrade": True,
                        "PricePrecision": 3,
                        "AmountPrecision": 3,
                        "MiniOrder": 1.0,
                    },
                    "BTC/USD": {"Coin": "BTC", "Unit": "USD", "CanTrade": True},
                },
            }
        )
        self.assertTrue(info.is_running)
        self.assertAlmostEqual(info.initial_wallet["USD"], 100000.0)
        self.assertEqual(set(info.pairs), {"BNB/USD", "BTC/USD"})
        self.assertEqual(info.pairs["BNB/USD"].price_precision, 3)
        self.assertEqual(info.pairs["BTC/USD"].coin, "BTC")

    def test_from_api_defaults_are_safe(self) -> None:
        info = ExchangeInfo.from_api({})
        self.assertFalse(info.is_running)
        self.assertEqual(info.initial_wallet, {})
        self.assertEqual(info.pairs, {})


# ---------------------------------------------------------------------------
# Position P&L
# ---------------------------------------------------------------------------


class TestPositionPnl(unittest.TestCase):
    def test_long_profit(self) -> None:
        position = Position(pair="BNB/USD", quantity=2.0, avg_price=100.0, mark_price=110.0)
        self.assertAlmostEqual(position.unrealized_pnl, 20.0)
        self.assertAlmostEqual(position.unrealized_pct, 0.10)
        self.assertAlmostEqual(position.notional, 220.0)

    def test_long_loss(self) -> None:
        position = Position(pair="BNB/USD", quantity=2.0, avg_price=100.0, mark_price=90.0)
        self.assertAlmostEqual(position.unrealized_pnl, -20.0)
        self.assertAlmostEqual(position.unrealized_pct, -0.10)

    def test_short_profit(self) -> None:
        """A short gains when the mark falls; the basis is the collateral."""
        position = Position(
            pair="BNB/USD", quantity=2.0, avg_price=100.0, mark_price=90.0, is_short=True, collateral=100.0
        )
        self.assertAlmostEqual(position.unrealized_pnl, 20.0)
        self.assertAlmostEqual(position.unrealized_pct, 0.20)

    def test_short_loss(self) -> None:
        position = Position(
            pair="BNB/USD", quantity=2.0, avg_price=100.0, mark_price=110.0, is_short=True, collateral=100.0
        )
        self.assertAlmostEqual(position.unrealized_pnl, -20.0)
        self.assertAlmostEqual(position.unrealized_pct, -0.20)

    def test_zero_quantity_yields_zero_pct_not_a_division_error(self) -> None:
        position = Position(pair="BNB/USD", quantity=0.0, avg_price=100.0, mark_price=110.0)
        self.assertAlmostEqual(position.unrealized_pnl, 0.0)
        self.assertAlmostEqual(position.unrealized_pct, 0.0)

    def test_short_without_collateral_yields_zero_pct(self) -> None:
        """A degenerate short must not raise ZeroDivisionError."""
        position = Position(
            pair="BNB/USD", quantity=1.0, avg_price=100.0, mark_price=90.0, is_short=True, collateral=0.0
        )
        self.assertAlmostEqual(position.unrealized_pct, 0.0)

    def test_notional_falls_back_to_avg_price_without_a_mark(self) -> None:
        position = Position(pair="BNB/USD", quantity=3.0, avg_price=50.0)
        self.assertAlmostEqual(position.notional, 150.0)

    def test_update_mark_tracks_the_high_water_price(self) -> None:
        position = Position(pair="BNB/USD", quantity=1.0, avg_price=100.0)
        position.update_mark(120.0)
        position.update_mark(110.0)
        self.assertAlmostEqual(position.mark_price, 110.0)
        self.assertAlmostEqual(position.peak_price, 120.0)


# ---------------------------------------------------------------------------
# WalletBalance
# ---------------------------------------------------------------------------


class TestWalletBalance(unittest.TestCase):
    def test_total_sums_free_and_locked(self) -> None:
        self.assertAlmostEqual(WalletBalance(asset="USD", free=10.0, locked=5.0).total, 15.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
