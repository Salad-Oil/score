"""Tests for :mod:`roostoo.indicators`.

Every fixture here is hand-checkable: short arithmetic or geometric series with
answers that can be verified on paper.  The recurring theme is totality -- these
functions feed a decision loop, so "not enough history yet" must be ``None``,
never an exception.
"""

from __future__ import annotations

import math
import unittest

from roostoo.indicators import (
    atr,
    current_drawdown,
    donchian,
    downside_deviation,
    drawdown_series,
    ema,
    ema_series,
    log_returns,
    max_drawdown,
    mean,
    momentum,
    normalized_slope,
    percentile_rank,
    realized_volatility,
    rsi,
    simple_returns,
    sma,
    stdev,
    true_range_proxy,
    zscore,
)

RISING = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
FLAT = [5.0] * 10
FALLING = [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------


class TestSimpleReturns(unittest.TestCase):
    def test_doubling_series(self) -> None:
        self.assertEqual(simple_returns([100.0, 200.0, 100.0]), [1.0, -0.5])

    def test_known_fractional_return(self) -> None:
        self.assertAlmostEqual(simple_returns([100.0, 110.0])[0], 0.1, places=12)

    def test_flat_series_has_zero_returns(self) -> None:
        self.assertEqual(simple_returns([5.0, 5.0, 5.0]), [0.0, 0.0])

    def test_length_is_one_less_than_input(self) -> None:
        self.assertEqual(len(simple_returns(RISING)), len(RISING) - 1)

    def test_single_price_has_no_returns(self) -> None:
        self.assertEqual(simple_returns([42.0]), [])

    def test_zero_divisor_is_skipped(self) -> None:
        """A zero previous price cannot produce a return: skip, do not raise."""
        self.assertEqual(simple_returns([0.0, 5.0, 10.0]), [1.0])


class TestLogReturns(unittest.TestCase):
    def test_natural_log_of_the_ratio(self) -> None:
        self.assertAlmostEqual(log_returns([1.0, math.e])[0], 1.0, places=12)

    def test_symmetric_around_zero(self) -> None:
        up, down = log_returns([100.0, 110.0, 100.0])
        self.assertAlmostEqual(up, -down, places=12)

    def test_non_positive_prices_are_skipped(self) -> None:
        """log(0) is undefined: the pair is dropped rather than raising."""
        self.assertEqual(log_returns([0.0, 5.0]), [])
        self.assertEqual(log_returns([5.0, 0.0]), [])

    def test_flat_series_has_zero_log_returns(self) -> None:
        for value in log_returns([5.0, 5.0, 5.0]):
            self.assertAlmostEqual(value, 0.0, places=12)


# ---------------------------------------------------------------------------
# Dispersion
# ---------------------------------------------------------------------------


class TestStdev(unittest.TestCase):
    def test_population_stdev_of_a_known_sample(self) -> None:
        """[2,4,4,4,5,5,7,9]: population sigma == sqrt(32/8) == 2.0."""
        self.assertAlmostEqual(stdev([2, 4, 4, 4, 5, 5, 7, 9], sample=False), 2.0, places=12)

    def test_sample_stdev_of_a_known_sample(self) -> None:
        """Same data with the n-1 denominator == sqrt(32/7)."""
        self.assertAlmostEqual(stdev([2, 4, 4, 4, 5, 5, 7, 9], sample=True), math.sqrt(32 / 7), places=12)

    def test_sample_stdev_exceeds_population_stdev(self) -> None:
        data = [1.0, 5.0, 9.0]
        self.assertGreater(stdev(data, sample=True), stdev(data, sample=False))

    def test_sample_stdev_of_1234_is_the_textbook_value(self) -> None:
        self.assertAlmostEqual(stdev([1, 2, 3, 4], sample=True), math.sqrt(5 / 3), places=12)

    def test_population_stdev_of_1234_is_the_textbook_value(self) -> None:
        self.assertAlmostEqual(stdev([1, 2, 3, 4], sample=False), math.sqrt(5 / 4), places=12)

    def test_identical_values_have_zero_stdev(self) -> None:
        self.assertAlmostEqual(stdev([3.0] * 5), 0.0, places=12)

    def test_sample_needs_two_points(self) -> None:
        self.assertIsNone(stdev([1.0], sample=True))
        self.assertIsNone(stdev([], sample=True))

    def test_population_needs_one_point(self) -> None:
        self.assertAlmostEqual(stdev([1.0], sample=False), 0.0)

    def test_nan_values_are_dropped(self) -> None:
        """A NaN sample must not poison the whole window."""
        self.assertAlmostEqual(stdev([1.0, float("nan"), 3.0], sample=False), 1.0, places=12)

    def test_mean_of_a_window(self) -> None:
        self.assertAlmostEqual(mean([1.0, 2.0, 3.0]), 2.0)
        self.assertIsNone(mean([]))


class TestDownsideDeviation(unittest.TestCase):
    def test_only_negative_returns_count(self) -> None:
        """Upside is free: a series with no losses has zero downside deviation."""
        self.assertAlmostEqual(downside_deviation([0.01, 0.02, 0.03]), 0.0, places=12)

    def test_hand_computed_value(self) -> None:
        """[0.01, -0.02]: sqrt((0 + 0.0004) / 2) == sqrt(0.0002)."""
        self.assertAlmostEqual(downside_deviation([0.01, -0.02]), math.sqrt(0.0002), places=12)

    def test_downside_deviation_is_below_total_stdev(self) -> None:
        returns = [0.02, 0.02, 0.02, -0.01]
        self.assertLess(downside_deviation(returns), stdev(returns))

    def test_empty_input_returns_none(self) -> None:
        self.assertIsNone(downside_deviation([]))

    def test_realized_volatility_annualises(self) -> None:
        sd = stdev([0.01, -0.01, 0.02, -0.02])
        self.assertAlmostEqual(realized_volatility([0.01, -0.01, 0.02, -0.02], 365), sd * math.sqrt(365))

    def test_realized_volatility_is_none_without_enough_data(self) -> None:
        self.assertIsNone(realized_volatility([0.01], 365))


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------


class TestSma(unittest.TestCase):
    def test_mean_of_the_last_window(self) -> None:
        self.assertAlmostEqual(sma([1, 2, 3, 4, 5], 3), 4.0)

    def test_whole_series_when_window_equals_length(self) -> None:
        self.assertAlmostEqual(sma([1, 2, 3, 4], 4), 2.5)

    def test_uses_the_most_recent_values(self) -> None:
        """Only the tail matters: an old outlier must not leak into the average."""
        self.assertAlmostEqual(sma([1000.0, 1.0, 2.0, 3.0], 3), 2.0)

    def test_window_longer_than_data_returns_none(self) -> None:
        self.assertIsNone(sma([1.0, 2.0], 5))

    def test_non_positive_window_returns_none(self) -> None:
        self.assertIsNone(sma([1.0, 2.0], 0))
        self.assertIsNone(sma([1.0, 2.0], -3))


class TestEma(unittest.TestCase):
    def test_seed_is_the_sma_of_the_first_window(self) -> None:
        """The documented convention: the first output is the SMA seed."""
        series = ema_series([1.0, 2.0, 3.0, 4.0], 3)
        self.assertAlmostEqual(series[0], 2.0)  # SMA of (1, 2, 3)

    def test_recursion_matches_the_textbook_formula(self) -> None:
        """k == 2/(n+1) == 0.5 for n=3; next is 4*0.5 + 2*0.5 == 3.0."""
        series = ema_series([1.0, 2.0, 3.0, 4.0], 3)
        self.assertAlmostEqual(series[1], 3.0)

    def test_series_length_is_data_minus_window_plus_one(self) -> None:
        self.assertEqual(len(ema_series(RISING, 4)), len(RISING) - 4 + 1)

    def test_ema_returns_the_latest_series_value(self) -> None:
        self.assertAlmostEqual(ema(RISING, 4), ema_series(RISING, 4)[-1])

    def test_flat_series_has_a_flat_ema(self) -> None:
        self.assertAlmostEqual(ema(FLAT, 5), 5.0)

    def test_window_longer_than_data_returns_none(self) -> None:
        self.assertIsNone(ema([1.0, 2.0], 5))
        self.assertEqual(ema_series([1.0, 2.0], 5), [])

    def test_non_positive_window_returns_none(self) -> None:
        self.assertIsNone(ema(RISING, 0))


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


class TestRsi(unittest.TestCase):
    def test_monotonically_rising_series_is_100(self) -> None:
        """No losses at all means RSI saturates at 100."""
        self.assertAlmostEqual(rsi(RISING, window=5), 100.0, places=9)

    def test_flat_series_is_50(self) -> None:
        """No gains and no losses is neutral, not a division by zero."""
        self.assertAlmostEqual(rsi(FLAT, window=5), 50.0, places=9)

    def test_monotonically_falling_series_is_about_zero(self) -> None:
        """All losses, no gains: RS == 0, so RSI == 0."""
        self.assertAlmostEqual(rsi(FALLING, window=5), 0.0, places=9)

    def test_hand_computed_mixed_series(self) -> None:
        """[1,2,1,2,1,2,1] with window 6: 3 gains of 1, 3 losses of 1 -> RSI 50."""
        self.assertAlmostEqual(rsi([1.0, 2.0, 1.0, 2.0, 1.0, 2.0, 1.0], window=6), 50.0, places=9)

    def test_result_is_within_bounds(self) -> None:
        value = rsi([5.0, 7.0, 6.0, 9.0, 8.0, 11.0, 10.0], window=6)
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 100.0)

    def test_too_little_history_returns_none(self) -> None:
        self.assertIsNone(rsi([1.0, 2.0, 3.0], window=14))

    def test_window_below_two_returns_none(self) -> None:
        self.assertIsNone(rsi(RISING, window=1))
        self.assertIsNone(rsi(RISING, window=0))


# ---------------------------------------------------------------------------
# Z-score and percentile rank
# ---------------------------------------------------------------------------


class TestZscore(unittest.TestCase):
    def test_latest_value_at_the_window_mean_is_zero(self) -> None:
        self.assertAlmostEqual(zscore([1.0, 2.0, 3.0, 2.0], window=4), 0.0, places=12)

    def test_known_distance_in_standard_deviations(self) -> None:
        """[1,2,3,4]: mean 2.5, sample sd sqrt(5/3); (4-2.5)/sd."""
        expected = 1.5 / math.sqrt(5 / 3)
        self.assertAlmostEqual(zscore([1.0, 2.0, 3.0, 4.0], window=4), expected, places=12)

    def test_rising_series_has_a_positive_zscore(self) -> None:
        self.assertGreater(zscore([1.0, 2.0, 3.0, 4.0, 5.0], window=5), 0.0)

    def test_falling_series_has_a_negative_zscore(self) -> None:
        self.assertLess(zscore([5.0, 4.0, 3.0, 2.0, 1.0], window=5), 0.0)

    def test_zero_variance_window_returns_zero_not_nan(self) -> None:
        value = zscore([5.0, 5.0, 5.0], window=3)
        self.assertEqual(value, 0.0)
        self.assertFalse(math.isnan(value))

    def test_window_longer_than_data_returns_none(self) -> None:
        self.assertIsNone(zscore([1.0, 2.0], window=5))

    def test_window_below_two_returns_none(self) -> None:
        self.assertIsNone(zscore(RISING, window=1))


class TestPercentileRank(unittest.TestCase):
    def test_latest_value_at_the_high_is_one(self) -> None:
        self.assertAlmostEqual(percentile_rank([1.0, 2.0, 3.0], window=3), 1.0)

    def test_latest_value_at_the_low_is_zero(self) -> None:
        self.assertAlmostEqual(percentile_rank([3.0, 2.0, 1.0], window=3), 0.0)

    def test_midpoint_of_the_range_is_half(self) -> None:
        """[0, 10, 5]: (5 - 0) / (10 - 0) == 0.5."""
        self.assertAlmostEqual(percentile_rank([0.0, 10.0, 5.0], window=3), 0.5)

    def test_degenerate_range_returns_half(self) -> None:
        self.assertAlmostEqual(percentile_rank(FLAT, window=5), 0.5)

    def test_window_longer_than_data_returns_none(self) -> None:
        self.assertIsNone(percentile_rank([1.0], window=5))


# ---------------------------------------------------------------------------
# Donchian, momentum, slope
# ---------------------------------------------------------------------------


class TestDonchian(unittest.TestCase):
    def test_excludes_the_latest_bar(self) -> None:
        """The breakout must compare today against the *previous* range."""
        prices = [1.0, 2.0, 3.0, 4.0, 100.0]
        low, high = donchian(prices, window=4)
        self.assertAlmostEqual(low, 1.0)
        self.assertAlmostEqual(high, 4.0)

    def test_latest_high_is_not_included(self) -> None:
        """window=3 over [5,6,7,8,9] covers bars -4..-2, i.e. [6,7,8]."""
        prices = [5.0, 6.0, 7.0, 8.0, 9.0]
        low, high = donchian(prices, window=3)
        self.assertAlmostEqual(low, 6.0)
        self.assertAlmostEqual(high, 8.0)
        self.assertNotIn(9.0, (low, high))

    def test_window_of_one_looks_only_at_the_previous_bar(self) -> None:
        low, high = donchian([1.0, 2.0, 3.0], window=1)
        self.assertAlmostEqual(low, 2.0)
        self.assertAlmostEqual(high, 2.0)

    def test_needs_window_plus_one_bars(self) -> None:
        self.assertIsNone(donchian([1.0, 2.0, 3.0], window=3))
        self.assertIsNotNone(donchian([1.0, 2.0, 3.0], window=2))


class TestMomentum(unittest.TestCase):
    def test_total_return_over_the_lookback(self) -> None:
        """Latest (5) versus 3 bars back (2): 1.5."""
        self.assertAlmostEqual(momentum([1.0, 2.0, 3.0, 4.0, 5.0], lookback=3), 1.5)

    def test_flat_series_has_zero_momentum(self) -> None:
        self.assertAlmostEqual(momentum(FLAT, lookback=3), 0.0)

    def test_falling_series_has_negative_momentum(self) -> None:
        self.assertLess(momentum([5.0, 4.0, 3.0, 2.0], lookback=3), 0.0)

    def test_lookback_equal_to_the_span_needs_one_extra_bar(self) -> None:
        self.assertIsNone(momentum([1.0, 2.0, 3.0], lookback=3))
        self.assertAlmostEqual(momentum([1.0, 2.0, 3.0], lookback=2), 2.0)

    def test_non_positive_lookback_returns_none(self) -> None:
        self.assertIsNone(momentum(RISING, lookback=0))

    def test_zero_base_price_returns_none(self) -> None:
        self.assertIsNone(momentum([0.0, 1.0, 2.0], lookback=2))


class TestNormalizedSlope(unittest.TestCase):
    def test_rising_series_has_a_positive_slope(self) -> None:
        self.assertGreater(normalized_slope(RISING, window=5), 0.0)

    def test_falling_series_has_a_negative_slope(self) -> None:
        self.assertLess(normalized_slope(FALLING, window=5), 0.0)

    def test_flat_series_has_a_zero_slope(self) -> None:
        self.assertAlmostEqual(normalized_slope(FLAT, window=5), 0.0, places=12)

    def test_linear_series_matches_the_analytic_slope(self) -> None:
        """y = 2x over x=0..4: slope 2, mean 4, so the ratio is 0.5."""
        self.assertAlmostEqual(normalized_slope([0.0, 2.0, 4.0, 6.0, 8.0], window=5), 0.5, places=12)

    def test_scale_free(self) -> None:
        """Multiplying the series by a constant leaves the slope unchanged."""
        base = [1.0, 2.0, 4.0, 8.0, 16.0]
        scaled = [v * 1000.0 for v in base]
        self.assertAlmostEqual(normalized_slope(base, 5), normalized_slope(scaled, 5), places=12)

    def test_window_longer_than_data_returns_none(self) -> None:
        self.assertIsNone(normalized_slope([1.0, 2.0], window=5))

    def test_window_below_three_returns_none(self) -> None:
        self.assertIsNone(normalized_slope(RISING, window=2))


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


class TestAtr(unittest.TestCase):
    def test_true_range_proxy_is_absolute_change(self) -> None:
        self.assertEqual(true_range_proxy([1.0, 3.0, 2.0]), [2.0, 1.0])

    def test_atr_is_the_mean_absolute_change(self) -> None:
        """Changes (+1, -1, +1, -1) average 1.0 over any window <= 4."""
        self.assertAlmostEqual(atr([1.0, 2.0, 1.0, 2.0, 1.0], window=4), 1.0)

    def test_atr_of_a_flat_series_is_zero(self) -> None:
        self.assertAlmostEqual(atr(FLAT, window=4), 0.0)

    def test_atr_uses_the_most_recent_changes(self) -> None:
        """A big early jump must not dominate a later calm window."""
        prices = [1.0, 100.0, 101.0, 102.0, 103.0]
        self.assertAlmostEqual(atr(prices, window=3), 1.0)

    def test_window_longer_than_changes_returns_none(self) -> None:
        self.assertIsNone(atr([1.0, 2.0, 3.0], window=14))


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


class TestDrawdown(unittest.TestCase):
    # Running peaks: 100, 110, 110, 120, 120, 120; trough 90 vs peak 120 -> -25%.
    CURVE = [100.0, 110.0, 105.0, 120.0, 90.0, 95.0]

    def test_drawdown_series_is_zero_at_every_new_high(self) -> None:
        """A new high is by definition a zero drawdown."""
        series = drawdown_series(self.CURVE)
        self.assertEqual(series[0], 0.0)
        self.assertEqual(series[1], 0.0)
        self.assertEqual(series[3], 0.0)

    def test_drawdown_series_hand_computed_values(self) -> None:
        """Drawdowns are negative: 105/110-1 == -0.0454..., 90/120-1 == -0.25."""
        series = drawdown_series(self.CURVE)
        self.assertAlmostEqual(series[2], 105.0 / 110.0 - 1.0, places=12)
        self.assertAlmostEqual(series[4], -0.25, places=12)
        self.assertAlmostEqual(series[5], 95.0 / 120.0 - 1.0, places=12)

    def test_max_drawdown_is_the_trough_decline(self) -> None:
        self.assertAlmostEqual(max_drawdown(self.CURVE), 0.25, places=12)

    def test_current_drawdown_is_the_latest_decline(self) -> None:
        self.assertAlmostEqual(current_drawdown(self.CURVE), 25.0 / 120.0, places=12)

    def test_monotonic_rise_has_no_drawdown(self) -> None:
        self.assertAlmostEqual(max_drawdown(RISING), 0.0)
        self.assertAlmostEqual(current_drawdown(RISING), 0.0)

    def test_flat_curve_has_no_drawdown(self) -> None:
        self.assertAlmostEqual(max_drawdown(FLAT), 0.0)

    def test_full_loss_is_one(self) -> None:
        self.assertAlmostEqual(max_drawdown([100.0, 50.0, 0.0]), 1.0)

    def test_empty_curve_yields_zero(self) -> None:
        self.assertEqual(max_drawdown([]), 0.0)
        self.assertEqual(current_drawdown([]), 0.0)

    def test_drawdown_series_of_empty_curve_is_empty(self) -> None:
        self.assertEqual(drawdown_series([]), [])

    def test_drawdown_is_always_non_positive_in_the_series(self) -> None:
        for value in drawdown_series(self.CURVE):
            self.assertLessEqual(value, 0.0)


# ---------------------------------------------------------------------------
# Totality: never raise, return None
# ---------------------------------------------------------------------------


class TestWindowsLongerThanData(unittest.TestCase):
    """Every windowed indicator returns None instead of raising."""

    SHORT = [1.0, 2.0, 3.0]
    LONG_WINDOW = 30

    def test_sma(self) -> None:
        self.assertIsNone(sma(self.SHORT, self.LONG_WINDOW))

    def test_ema(self) -> None:
        self.assertIsNone(ema(self.SHORT, self.LONG_WINDOW))

    def test_ema_series(self) -> None:
        self.assertEqual(ema_series(self.SHORT, self.LONG_WINDOW), [])

    def test_rsi(self) -> None:
        self.assertIsNone(rsi(self.SHORT, window=self.LONG_WINDOW))

    def test_zscore(self) -> None:
        self.assertIsNone(zscore(self.SHORT, self.LONG_WINDOW))

    def test_percentile_rank(self) -> None:
        self.assertIsNone(percentile_rank(self.SHORT, self.LONG_WINDOW))

    def test_donchian(self) -> None:
        self.assertIsNone(donchian(self.SHORT, self.LONG_WINDOW))

    def test_momentum(self) -> None:
        self.assertIsNone(momentum(self.SHORT, lookback=self.LONG_WINDOW))

    def test_normalized_slope(self) -> None:
        self.assertIsNone(normalized_slope(self.SHORT, self.LONG_WINDOW))

    def test_atr(self) -> None:
        self.assertIsNone(atr(self.SHORT, window=self.LONG_WINDOW))

    def test_drawdown_helpers_are_total_on_empty_input(self) -> None:
        self.assertEqual(max_drawdown([]), 0.0)
        self.assertEqual(current_drawdown([]), 0.0)
        self.assertEqual(drawdown_series([]), [])

    def test_no_indicator_raises_on_an_empty_series(self) -> None:
        """The whole API is total on empty input."""
        self.assertEqual(sma([], 5), None)
        self.assertEqual(ema([], 5), None)
        self.assertEqual(rsi([], 14), None)
        self.assertEqual(zscore([], 5), None)
        self.assertEqual(percentile_rank([], 5), None)
        self.assertEqual(donchian([], 5), None)
        self.assertEqual(momentum([], 5), None)
        self.assertEqual(normalized_slope([], 5), None)
        self.assertEqual(atr([], 14), None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
