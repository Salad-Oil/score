"""Tests for :mod:`roostoo.metrics`.

The competition score is ``0.40 * Sortino + 0.30 * Sharpe + 0.30 * Calmar``, so
the weights themselves are treated as a contract here rather than as an
implementation detail.  Curves are hand-built so the returns -- and therefore
the signs of the ratios -- are obvious without running the code.
"""

from __future__ import annotations

import math
import unittest

from roostoo.indicators import stdev
from roostoo.metrics import (
    COMPOSITE_WEIGHTS,
    MS_PER_DAY,
    MetricTracker,
    PerformanceMetrics,
    annualize_return,
    composite_score,
    compute_metrics,
    daily_equity_marks,
    equity_returns,
)

DAY = MS_PER_DAY


def make_curve(returns: list[float], start: float = 10_000.0) -> list[float]:
    """Build an equity curve from an explicit list of period returns."""
    curve = [start]
    for r in returns:
        curve.append(curve[-1] * (1.0 + r))
    return curve


def make_uptrend(n: int = 21, rate: float = 0.01, start: float = 10_000.0) -> list[float]:
    """``n`` daily marks compounding at ``rate`` per day.

    Note: every return is identical, so the return variance is exactly zero and
    Sharpe/Sortino/Calmar are all ``None`` for this curve.  Use
    :func:`make_wobbly_uptrend` when a real ratio is wanted.
    """
    curve = [start]
    for _ in range(n - 1):
        curve.append(curve[-1] * (1.0 + rate))
    return curve


def _wobble_returns(n: int, rate: float, wobble: float, dip_every: int, dip: float) -> list[float]:
    """Deterministic daily returns: a wobble, plus a real loss every ``dip_every``.

    The loss matters: downside deviation would be exactly zero for a lossless
    curve, which makes Sortino undefined rather than large.
    """
    out = []
    for k in range(n):
        r = rate + (wobble if k % 2 == 0 else -wobble)
        if dip_every and k % dip_every == dip_every - 1:
            r = dip
        out.append(r)
    return out


def make_wobbly_uptrend(
    n: int = 30, rate: float = 0.01, wobble: float = 0.002, dip_every: int = 5, dip: float = -0.005
) -> list[float]:
    """A rising curve whose returns genuinely vary, including occasional losses.

    With ``dip=0.0`` (and no wobble) it degrades to :func:`make_uptrend`.
    """
    return make_curve(_wobble_returns(n, rate, wobble, dip_every, dip))


def make_wobbly_downtrend(
    n: int = 30, rate: float = -0.01, wobble: float = 0.002, dip_every: int = 5, dip: float = -0.015
) -> list[float]:
    """The mirror image of :func:`make_wobbly_uptrend`: a falling curve."""
    return make_curve(_wobble_returns(n, rate, wobble, dip_every, dip))


UPTREND = make_wobbly_uptrend()
DOWNTREND = make_wobbly_downtrend()

# Nine +0.05% days and one -0.1% day: the mean is positive, most of the spread
# is upside, and there is one genuine loss.  A loss is required for Sortino to
# have a denominator at all; keeping the whole scale small keeps both ratios
# under DEFAULT_RATIO_CAP, so the Sortino > Sharpe comparison is not flattened
# by the clamp.
ASYMMETRIC = make_curve([0.0002] * 9 + [-0.0008])


# ---------------------------------------------------------------------------
# Equity-curve helpers
# ---------------------------------------------------------------------------


class TestEquityReturns(unittest.TestCase):
    def test_successive_simple_returns(self) -> None:
        returns = equity_returns([100.0, 110.0, 99.0])
        self.assertAlmostEqual(returns[0], 0.1, places=12)
        self.assertAlmostEqual(returns[1], -0.1, places=12)

    def test_length_is_one_less_than_the_curve(self) -> None:
        self.assertEqual(len(equity_returns(UPTREND)), len(UPTREND) - 1)

    def test_non_positive_previous_equity_is_skipped(self) -> None:
        """A wiped-out account has no meaningful return, and must not divide by zero."""
        self.assertEqual(equity_returns([0.0, 50.0]), [])

    def test_single_mark_has_no_returns(self) -> None:
        self.assertEqual(equity_returns([100.0]), [])

    def test_empty_curve_has_no_returns(self) -> None:
        self.assertEqual(equity_returns([]), [])


class TestDailyEquityMarks(unittest.TestCase):
    # Three days, with two marks on day 1 and one on day 2.
    SNAPSHOTS = [
        (5 * DAY + 3_600_000, 100.0),
        (5 * DAY + 7_200_000, 105.0),  # last mark of day 5
        (6 * DAY + 3_600_000, 110.0),  # last (and only) mark of day 6
        (7 * DAY + 1_000, 120.0),
    ]

    def test_keeps_only_the_last_mark_of_each_day(self) -> None:
        self.assertEqual(daily_equity_marks(self.SNAPSHOTS), [105.0, 110.0, 120.0])

    def test_one_mark_per_utc_day(self) -> None:
        marks = daily_equity_marks(self.SNAPSHOTS)
        self.assertEqual(len(marks), 3)

    def test_unsorted_input_is_sorted_first(self) -> None:
        """Marks arrive from a live loop, so ordering cannot be assumed."""
        shuffled = [self.SNAPSHOTS[2], self.SNAPSHOTS[0], self.SNAPSHOTS[3], self.SNAPSHOTS[1]]
        self.assertEqual(daily_equity_marks(shuffled), [105.0, 110.0, 120.0])

    def test_same_day_only_collapses_to_one_mark(self) -> None:
        same_day = [(5 * DAY + 1_000, 1.0), (5 * DAY + 2_000, 2.0)]
        self.assertEqual(daily_equity_marks(same_day), [2.0])

    def test_empty_input(self) -> None:
        self.assertEqual(daily_equity_marks([]), [])


# ---------------------------------------------------------------------------
# compute_metrics on a hand-built curve
# ---------------------------------------------------------------------------


class TestComputeMetricsExactValues(unittest.TestCase):
    def test_initial_and_final_equity_are_the_curve_endpoints(self) -> None:
        metrics = compute_metrics(UPTREND)
        self.assertAlmostEqual(metrics.initial_equity, UPTREND[0], places=9)
        self.assertAlmostEqual(metrics.final_equity, UPTREND[-1], places=9)

    def test_total_return_is_exact(self) -> None:
        metrics = compute_metrics([100.0, 150.0])
        self.assertAlmostEqual(metrics.total_return, 0.5, places=12)

    def test_total_return_of_a_loss_is_negative(self) -> None:
        metrics = compute_metrics([100.0, 75.0])
        self.assertAlmostEqual(metrics.total_return, -0.25, places=12)

    def test_observations_counts_the_marks(self) -> None:
        self.assertEqual(compute_metrics(UPTREND).observations, len(UPTREND))

    def test_max_drawdown_is_exact(self) -> None:
        """130 -> 91 is a 30% peak-to-trough fall."""
        metrics = compute_metrics([100.0, 130.0, 91.0])
        self.assertAlmostEqual(metrics.max_drawdown, 0.30, places=12)

    def test_current_drawdown_is_exact(self) -> None:
        metrics = compute_metrics([100.0, 130.0, 91.0])
        self.assertAlmostEqual(metrics.current_drawdown, 0.30, places=12)

    def test_current_drawdown_is_less_than_max_after_a_partial_recovery(self) -> None:
        metrics = compute_metrics([100.0, 130.0, 91.0, 117.0])
        self.assertAlmostEqual(metrics.max_drawdown, 0.30, places=12)
        self.assertAlmostEqual(metrics.current_drawdown, 0.10, places=12)

    def test_monotonic_uptrend_has_no_drawdown(self) -> None:
        metrics = compute_metrics(make_uptrend())
        self.assertAlmostEqual(metrics.max_drawdown, 0.0, places=12)
        self.assertAlmostEqual(metrics.current_drawdown, 0.0, places=12)

    def test_a_smooth_riser_with_zero_variance_has_no_drawdown(self) -> None:
        """Every mark is a new high, so the peak-to-trough fall is exactly zero."""
        metrics = compute_metrics(make_uptrend(n=10, rate=0.01))
        self.assertAlmostEqual(metrics.max_drawdown, 0.0, places=12)

    def test_win_rate_counts_periods_with_the_curve_mostly_up(self) -> None:
        """The wobble curve has one losing day in five, so 80% of days win."""
        self.assertAlmostEqual(compute_metrics(UPTREND).win_rate, 0.8, places=12)

    def test_win_rate_of_a_lossless_curve_is_one(self) -> None:
        self.assertAlmostEqual(compute_metrics(make_uptrend()).win_rate, 1.0, places=12)

    def test_win_rate_of_a_smooth_downtrend_is_zero(self) -> None:
        self.assertAlmostEqual(compute_metrics(DOWNTREND).win_rate, 0.0, places=12)

    def test_best_and_worst_period_are_reported(self) -> None:
        metrics = compute_metrics([100.0, 110.0, 99.0])
        self.assertAlmostEqual(metrics.best_period, 0.1, places=12)
        self.assertAlmostEqual(metrics.worst_period, -0.1, places=12)


class TestRatioSigns(unittest.TestCase):
    def test_uptrend_has_positive_sharpe_and_sortino(self) -> None:
        metrics = compute_metrics(UPTREND)
        self.assertIsNotNone(metrics.sharpe)
        self.assertIsNotNone(metrics.sortino)
        self.assertGreater(metrics.sharpe, 0.0)
        self.assertGreater(metrics.sortino, 0.0)

    def test_downtrend_has_negative_sharpe_and_sortino(self) -> None:
        metrics = compute_metrics(DOWNTREND)
        self.assertIsNotNone(metrics.sharpe)
        self.assertIsNotNone(metrics.sortino)
        self.assertLess(metrics.sharpe, 0.0)
        self.assertLess(metrics.sortino, 0.0)

    def test_uptrend_has_positive_calmar(self) -> None:
        metrics = compute_metrics(UPTREND)
        self.assertIsNotNone(metrics.calmar)
        self.assertGreater(metrics.calmar, 0.0)

    def test_downtrend_has_negative_calmar(self) -> None:
        metrics = compute_metrics(DOWNTREND)
        self.assertIsNotNone(metrics.calmar)
        self.assertLess(metrics.calmar, 0.0)

    def test_uptrend_has_positive_annualized_return(self) -> None:
        metrics = compute_metrics(UPTREND)
        self.assertIsNotNone(metrics.annualized_return)
        self.assertGreater(metrics.annualized_return, 0.0)

    def test_annualized_volatility_is_positive_when_prices_move(self) -> None:
        metrics = compute_metrics(UPTREND)
        self.assertIsNotNone(metrics.annualized_volatility)
        self.assertGreater(metrics.annualized_volatility, 0.0)

    def test_a_bigger_move_gives_a_bigger_sharpe(self) -> None:
        """Sanity check that the ratio responds to return, not only to noise.

        Both rates are chosen to stay under ``DEFAULT_RATIO_CAP``, otherwise the
        clamp would flatten them to the same value.
        """
        slow = compute_metrics(make_wobbly_uptrend(rate=0.0005))
        fast = compute_metrics(make_wobbly_uptrend(rate=0.001))
        self.assertGreater(fast.sharpe, slow.sharpe)
        self.assertLess(fast.sharpe, 10.0)


class TestSortinoVersusSharpe(unittest.TestCase):
    def test_sortino_exceeds_sharpe_when_downside_is_smaller(self) -> None:
        """One bad day among many good ones: downside deviation < total stdev."""
        metrics = compute_metrics(ASYMMETRIC)
        self.assertIsNotNone(metrics.sharpe)
        self.assertIsNotNone(metrics.sortino)
        total_stdev = stdev(equity_returns(ASYMMETRIC))
        self.assertLess(metrics.downside_deviation, total_stdev)
        self.assertGreater(metrics.sortino, metrics.sharpe)

    def test_lossless_curve_has_zero_downside_deviation(self) -> None:
        """Upside volatility is free: a lossless curve has no downside at all."""
        metrics = compute_metrics(make_uptrend(n=20, rate=0.01))
        self.assertAlmostEqual(metrics.downside_deviation, 0.0, places=12)
        self.assertIsNone(metrics.sortino)  # denominator is zero -> undefined

    def test_zero_variance_curve_has_no_ratios_at_all(self) -> None:
        """A constant-rate riser has identical returns, so stdev is exactly zero."""
        metrics = compute_metrics(make_uptrend(n=20, rate=0.01))
        self.assertAlmostEqual(metrics.annualized_volatility, 0.0, places=12)
        self.assertIsNone(metrics.sharpe)
        self.assertIsNone(metrics.sortino)

    def test_a_single_loss_creates_a_sortino_denominator(self) -> None:
        metrics = compute_metrics(make_curve([0.01] * 4 + [-0.005] + [0.01]))
        self.assertGreater(metrics.downside_deviation, 0.0)
        self.assertIsNotNone(metrics.sortino)


class TestDegenerateCurves(unittest.TestCase):
    def test_constant_curve_yields_none_ratios(self) -> None:
        """Zero variance must give ``None``, never a division by zero or NaN."""
        metrics = compute_metrics([100.0] * 10)
        self.assertIsNone(metrics.sharpe)
        self.assertIsNone(metrics.sortino)
        self.assertIsNone(metrics.calmar)
        self.assertIsNone(metrics.composite)

    def test_constant_curve_does_not_raise_and_reports_zero_return(self) -> None:
        metrics = compute_metrics([100.0] * 10)
        self.assertAlmostEqual(metrics.total_return, 0.0, places=12)
        self.assertAlmostEqual(metrics.max_drawdown, 0.0, places=12)

    def test_no_ratio_is_ever_nan(self) -> None:
        """NaN would silently poison the leaderboard score."""
        for curve in ([100.0] * 5, UPTREND, DOWNTREND, ASYMMETRIC, [100.0, 0.0, 0.0]):
            metrics = compute_metrics(curve)
            for name in ("sharpe", "sortino", "calmar", "composite", "total_return"):
                value = getattr(metrics, name)
                if value is not None:
                    self.assertFalse(math.isnan(value), f"{name} is NaN for {curve[:3]}...")
                    self.assertFalse(math.isinf(value), f"{name} is inf for {curve[:3]}...")

    def test_single_mark_has_no_ratios(self) -> None:
        metrics = compute_metrics([100.0])
        self.assertIsNone(metrics.total_return)
        self.assertIsNone(metrics.sharpe)
        self.assertEqual(metrics.initial_equity, 100.0)

    def test_empty_curve_is_all_defaults(self) -> None:
        metrics = compute_metrics([])
        self.assertIsNone(metrics.total_return)
        self.assertEqual(metrics.observations, 0)

    def test_zero_start_equity_does_not_divide_by_zero(self) -> None:
        metrics = compute_metrics([0.0, 10.0])
        self.assertIsNone(metrics.total_return)

    def test_summary_lines_render_without_ratios(self) -> None:
        """A degenerate report must still print (the journal calls this)."""
        text = compute_metrics([100.0] * 5).report()
        self.assertIn("n/a", text)
        self.assertIn("composite score", text)

    def test_cap_limits_an_absurd_ratio(self) -> None:
        """A near-zero-variance streak is capped so it cannot dominate the score.

        A wobble-shaped curve has a real but tiny standard deviation, which would
        otherwise annualise into a ratio far above the cap.
        """
        uncapped = compute_metrics(make_wobbly_uptrend(n=10, rate=0.01))
        self.assertGreater(uncapped.sharpe, 3.0)

        capped = compute_metrics(make_wobbly_uptrend(n=10, rate=0.01), cap=3.0)
        self.assertAlmostEqual(capped.sharpe, 3.0, places=12)
        self.assertAlmostEqual(capped.sortino, 3.0, places=12)


class TestAnnualizeReturn(unittest.TestCase):
    def test_full_year_of_daily_periods(self) -> None:
        """365 periods at the daily convention is one year: 10% -> 10%."""
        self.assertAlmostEqual(annualize_return(0.10, 365, 365), 0.10, places=12)

    def test_half_year_grows_faster(self) -> None:
        self.assertGreater(annualize_return(0.10, 183, 365), 0.10)

    def test_total_loss_is_not_annualised(self) -> None:
        self.assertIsNone(annualize_return(-1.0, 10, 365))

    def test_no_periods_returns_none(self) -> None:
        self.assertIsNone(annualize_return(0.1, 0, 365))


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------


class TestCompositeScoreWeights(unittest.TestCase):
    def test_weights_are_the_published_040_030_030(self) -> None:
        self.assertAlmostEqual(COMPOSITE_WEIGHTS["sortino"], 0.40)
        self.assertAlmostEqual(COMPOSITE_WEIGHTS["sharpe"], 0.30)
        self.assertAlmostEqual(COMPOSITE_WEIGHTS["calmar"], 0.30)
        self.assertAlmostEqual(sum(COMPOSITE_WEIGHTS.values()), 1.0, places=12)

    def test_exact_weighted_average(self) -> None:
        """0.4*2 + 0.3*4 + 0.3*3 == 2.9 with all three components present."""
        metrics = PerformanceMetrics(sortino=2.0, sharpe=4.0, calmar=3.0)
        score, components = composite_score(metrics)
        self.assertAlmostEqual(score, 0.4 * 2.0 + 0.3 * 4.0 + 0.3 * 3.0, places=12)
        self.assertEqual(components, {"sortino": 0.40, "sharpe": 0.30, "calmar": 0.30})

    def test_sortino_dominates_the_score(self) -> None:
        """Sortino carries the largest weight, so it must move the score most."""
        base = PerformanceMetrics(sortino=1.0, sharpe=1.0, calmar=1.0)
        more_sortino = PerformanceMetrics(sortino=2.0, sharpe=1.0, calmar=1.0)
        more_sharpe = PerformanceMetrics(sortino=1.0, sharpe=2.0, calmar=1.0)
        self.assertGreater(composite_score(more_sortino)[0], composite_score(more_sharpe)[0])

    def test_renormalises_when_sortino_is_missing(self) -> None:
        """0.3*2 + 0.3*1 == 1.2, renormalised by the surviving 0.6 of weight."""
        metrics = PerformanceMetrics(sortino=None, sharpe=2.0, calmar=1.0)
        score, components = composite_score(metrics)
        self.assertAlmostEqual(score, (0.3 * 2.0 + 0.3 * 1.0) / 0.6, places=12)
        self.assertEqual(components, {"sharpe": 0.5, "calmar": 0.5})

    def test_renormalises_when_sharpe_is_missing(self) -> None:
        metrics = PerformanceMetrics(sortino=4.0, sharpe=None, calmar=1.0)
        score, components = composite_score(metrics)
        self.assertAlmostEqual(score, (0.4 * 4.0 + 0.3 * 1.0) / 0.7, places=12)
        self.assertAlmostEqual(components["sortino"], 0.4 / 0.7)
        self.assertAlmostEqual(components["calmar"], 0.3 / 0.7)

    def test_single_component_is_returned_unchanged(self) -> None:
        """With one ratio available it keeps its own value, weight-scaled to 1."""
        score, components = composite_score(PerformanceMetrics(sortino=None, sharpe=None, calmar=2.5))
        self.assertAlmostEqual(score, 2.5, places=12)
        self.assertEqual(components, {"calmar": 1.0})

    def test_all_missing_yields_none(self) -> None:
        score, components = composite_score(PerformanceMetrics())
        self.assertIsNone(score)
        self.assertEqual(components, {})

    def test_compute_metrics_populates_the_composite(self) -> None:
        metrics = compute_metrics(UPTREND)
        self.assertIsNotNone(metrics.composite)
        self.assertGreater(metrics.composite, 0.0)
        self.assertAlmostEqual(sum(metrics.composite_components.values()), 1.0, places=12)

    def test_negative_ratios_produce_a_negative_composite(self) -> None:
        metrics = compute_metrics(DOWNTREND)
        self.assertIsNotNone(metrics.composite)
        self.assertLess(metrics.composite, 0.0)


# ---------------------------------------------------------------------------
# MetricTracker
# ---------------------------------------------------------------------------


class TestMetricTracker(unittest.TestCase):
    def test_latest_equity_defaults_to_initial_capital(self) -> None:
        tracker = MetricTracker(10_000.0)
        self.assertAlmostEqual(tracker.latest_equity, 10_000.0)

    def test_latest_equity_follows_the_last_snapshot(self) -> None:
        tracker = MetricTracker(10_000.0)
        tracker.record(DAY, 10_500.0)
        self.assertAlmostEqual(tracker.latest_equity, 10_500.0)

    def test_total_return_uses_initial_capital_as_the_base(self) -> None:
        tracker = MetricTracker(10_000.0)
        tracker.record(DAY, 11_000.0)
        self.assertAlmostEqual(tracker.total_return, 0.10, places=12)

    def test_drawdown_tracks_the_high_water_mark(self) -> None:
        tracker = MetricTracker(10_000.0)
        tracker.record(DAY, 12_000.0)
        tracker.record(2 * DAY, 9_000.0)
        self.assertAlmostEqual(tracker.drawdown, 0.25, places=12)
        self.assertAlmostEqual(tracker.peak_equity, 12_000.0)

    def test_metrics_use_the_daily_curve(self) -> None:
        """One mark per day means each mark is one period of the reported curve."""
        tracker = MetricTracker(10_000.0)
        tracker.record(DAY, 10_100.0)
        tracker.record(2 * DAY, 10_302.0)
        tracker.record(3 * DAY, 10_250.49)
        metrics = tracker.metrics()
        self.assertEqual(metrics.observations, 3)  # three daily marks
        self.assertIsNotNone(metrics.sharpe)
        self.assertGreater(metrics.sharpe, 0.0)

    def test_daily_curve_drops_intraday_marks(self) -> None:
        """Intraday noise is resampled away: only the closing mark survives."""
        tracker = MetricTracker(10_000.0)
        tracker.record(DAY + 1_000, 9_000.0)
        tracker.record(DAY + 2_000, 10_100.0)  # last mark of day 1 wins
        tracker.record(2 * DAY + 1_000, 10_302.0)
        tracker.record(3 * DAY + 1_000, 10_250.49)
        self.assertEqual(tracker.metrics().observations, 3)

    def test_metrics_fall_back_to_raw_marks_when_all_on_one_day(self) -> None:
        """A same-day-only tracker must not produce an empty report."""
        tracker = MetricTracker(10_000.0)
        tracker.record(DAY + 1_000, 10_100.0)
        tracker.record(DAY + 2_000, 10_201.0)
        metrics = tracker.metrics()
        self.assertEqual(metrics.observations, 3)
        self.assertIsNotNone(metrics.total_return)

    def test_metrics_without_snapshots_are_empty(self) -> None:
        metrics = MetricTracker(10_000.0).metrics()
        self.assertIsNone(metrics.total_return)


class TestDayStartEquity(unittest.TestCase):
    def make_tracker(self) -> MetricTracker:
        tracker = MetricTracker(10_000.0)
        tracker.record(5 * DAY + 1_000, 100.0)
        tracker.record(5 * DAY + 2_000, 105.0)
        tracker.record(6 * DAY + 1_000, 110.0)
        tracker.record(6 * DAY + 2_000, 115.0)
        return tracker

    def test_returns_the_first_mark_of_the_requested_day(self) -> None:
        tracker = self.make_tracker()
        self.assertAlmostEqual(tracker.day_start_equity(6 * DAY + 1_000), 110.0)

    def test_returns_the_first_mark_even_mid_day(self) -> None:
        tracker = self.make_tracker()
        self.assertAlmostEqual(tracker.day_start_equity(6 * DAY + 5_000), 110.0)

    def test_first_day_mark_is_used_for_the_first_day(self) -> None:
        tracker = self.make_tracker()
        self.assertAlmostEqual(tracker.day_start_equity(5 * DAY + 2_000), 100.0)

    def test_falls_back_to_the_last_known_mark_for_a_later_day(self) -> None:
        tracker = self.make_tracker()
        self.assertAlmostEqual(tracker.day_start_equity(7 * DAY), 115.0)

    def test_returns_initial_equity_before_any_snapshot(self) -> None:
        tracker = MetricTracker(10_000.0)
        self.assertAlmostEqual(tracker.day_start_equity(5 * DAY), 10_000.0)

    def test_unsorted_snapshots_still_resolve_per_day(self) -> None:
        """``record`` appends in arrival order, which is not necessarily sorted."""
        tracker = MetricTracker(10_000.0)
        tracker.record(6 * DAY + 2_000, 115.0)
        tracker.record(5 * DAY + 1_000, 100.0)
        self.assertAlmostEqual(tracker.day_start_equity(5 * DAY), 100.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
