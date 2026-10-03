"""Risk-adjusted performance metrics -- aligned with the competition scoring.

The finalist score is::

    composite = 0.40 * Sortino + 0.30 * Sharpe + 0.30 * Calmar

Two consequences shape the design of this bot, and this module is where they are
made measurable:

* **Downside only matters to Sortino.** Upside volatility is free; the bot should
  never pay for it with de-risking.
* **Calmar punishes the worst peak-to-trough fall**, so a single large drawdown
  can cost 30% of the score even in a profitable run. Drawdown control is
  therefore an alpha decision, not just hygiene.

Ratios are annualised from the sampling frequency. The live tracker feeds daily
equity marks (``periods_per_year=365``), which is the convention the leaderboard
data supports; per-period values are also exposed for inspection.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from .indicators import downside_deviation, max_drawdown, stdev

SECONDS_PER_YEAR = 365 * 24 * 3600
MS_PER_DAY = 86_400_000

# The published weighting. Kept as data so a rule change is a one-line edit.
COMPOSITE_WEIGHTS = {"sortino": 0.40, "sharpe": 0.30, "calmar": 0.30}

# Ratios explode when variance collapses (a flat equity curve has zero downside
# deviation). Capping keeps a lucky 3-day streak from dominating the report.
DEFAULT_RATIO_CAP = 10.0


def equity_returns(equity: Sequence[float]) -> list[float]:
    """Simple returns between successive equity marks."""
    out: list[float] = []
    for prev, cur in zip(equity, equity[1:]):
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def daily_equity_marks(snapshots: Sequence[tuple[int, float]]) -> list[float]:
    """Collapse ``(timestamp_ms, equity)`` marks to one closing value per UTC day.

    Using the last mark of each day matches how a daily-return leaderboard would
    reconstruct the curve, and it removes the intraday sampling rate from the
    statistics.
    """
    if not snapshots:
        return []
    ordered = sorted(snapshots, key=lambda s: s[0])
    out: list[float] = []
    current_day: Optional[int] = None
    last_value = 0.0
    for ts_ms, equity in ordered:
        day = int(ts_ms) // MS_PER_DAY
        if current_day is None:
            current_day = day
        if day != current_day:
            out.append(last_value)
            current_day = day
        last_value = float(equity)
    out.append(last_value)
    return out


@dataclass
class PerformanceMetrics:
    """Everything the judges look at, plus the context needed to trust it."""

    initial_equity: float = 0.0
    final_equity: float = 0.0
    total_return: Optional[float] = None
    annualized_return: Optional[float] = None
    annualized_volatility: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    calmar: Optional[float] = None
    max_drawdown: Optional[float] = None
    current_drawdown: Optional[float] = None
    downside_deviation: Optional[float] = None
    win_rate: Optional[float] = None
    best_period: Optional[float] = None
    worst_period: Optional[float] = None
    observations: int = 0
    periods_per_year: int = 365
    composite: Optional[float] = None
    composite_components: dict[str, float] = field(default_factory=dict)
    #: True when at least one ratio was missing, so the weights in
    #: ``composite_components`` were renormalised over the survivors. Such a score
    #: is comparable across early partial reports but is NOT the leaderboard's
    #: fixed-weight 0.4/0.3/0.3 formula, and a caller that quotes it as though it
    #: were should say so.
    composite_is_partial: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- presentation ---------------------------------------------------
    def summary_lines(self) -> list[str]:
        def pct(value: Optional[float]) -> str:
            return "n/a" if value is None else f"{value * 100:+.2f}%"

        def num(value: Optional[float]) -> str:
            return "n/a" if value is None else f"{value:.3f}"

        return [
            f"equity            {self.initial_equity:,.2f} -> {self.final_equity:,.2f}",
            f"total return      {pct(self.total_return)}",
            f"annualised return {pct(self.annualized_return)}",
            f"annualised vol    {pct(self.annualized_volatility)}",
            f"Sharpe            {num(self.sharpe)}",
            f"Sortino           {num(self.sortino)}",
            f"Calmar            {num(self.calmar)}",
            f"max drawdown      {pct(None if self.max_drawdown is None else -self.max_drawdown)}",
            f"win rate          {pct(self.win_rate)}",
            f"composite score   {num(self.composite)}",
        ]

    def report(self) -> str:
        return "\n".join(self.summary_lines())


def annualize_return(total_return: float, periods: int, periods_per_year: float) -> Optional[float]:
    if periods <= 0 or total_return <= -1.0:
        return None
    years = periods / periods_per_year
    if years <= 0:
        return None
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def _safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def compute_metrics(
    equity: Sequence[float],
    periods_per_year: int = 365,
    risk_free_rate: float = 0.0,
    cap: float = DEFAULT_RATIO_CAP,
) -> PerformanceMetrics:
    """Compute the full metric set from an equity curve sampled at a fixed rate."""
    curve = [float(v) for v in equity if v is not None and math.isfinite(float(v))]
    metrics = PerformanceMetrics(periods_per_year=periods_per_year)
    if len(curve) < 2:
        if curve:
            metrics.initial_equity = metrics.final_equity = curve[0]
        return metrics

    metrics.initial_equity = curve[0]
    metrics.final_equity = curve[-1]
    metrics.observations = len(curve)

    rets = equity_returns(curve)
    if not rets:
        return metrics

    metrics.total_return = (curve[-1] / curve[0] - 1.0) if curve[0] > 0 else None
    metrics.best_period = max(rets)
    metrics.worst_period = min(rets)
    metrics.win_rate = sum(1 for r in rets if r > 0) / len(rets)

    rf_period = risk_free_rate / periods_per_year
    excess = [r - rf_period for r in rets]

    sd = stdev(rets)
    dd = downside_deviation(rets, target=rf_period)
    metrics.downside_deviation = dd
    if sd is not None:
        metrics.annualized_volatility = sd * math.sqrt(periods_per_year)

    if metrics.total_return is not None:
        metrics.annualized_return = annualize_return(metrics.total_return, len(rets), periods_per_year)

    scale = math.sqrt(periods_per_year)
    mean_excess = sum(excess) / len(excess)
    if sd:
        metrics.sharpe = _clamp(mean_excess / sd * scale, cap)
    if dd:
        metrics.sortino = _clamp(mean_excess / dd * scale, cap)

    metrics.max_drawdown = max_drawdown(curve)
    peak = max(curve)
    metrics.current_drawdown = (peak - curve[-1]) / peak if peak > 0 else 0.0
    if metrics.annualized_return is not None and metrics.max_drawdown:
        metrics.calmar = _clamp(metrics.annualized_return / metrics.max_drawdown, cap)

    metrics.composite, metrics.composite_components = composite_score(metrics)
    metrics.composite_is_partial = len(metrics.composite_components) != len(COMPOSITE_WEIGHTS)
    return metrics


def _as_intraday_report(metrics: PerformanceMetrics) -> PerformanceMetrics:
    """Strip annualised figures from a report that spans less than one day.

    Ratios and returns are annualised from an assumed sampling rate, and with
    fewer than two daily marks there is no daily rate to annualise from. The
    level figures (equity, total return, drawdown, win rate) are still meaningful
    and stay; the per-year extrapolations become ``None`` and the composite is
    left unset rather than quoting a score built on a made-up factor.
    """
    for name in (
        "annualized_return",
        "annualized_volatility",
        "sharpe",
        "sortino",
        "calmar",
        "composite",
    ):
        setattr(metrics, name, None)
    metrics.periods_per_year = 0
    metrics.composite_components = {}
    metrics.composite_is_partial = True
    return metrics


def _clamp(value: Optional[float], cap: float) -> Optional[float]:
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return max(-cap, min(cap, value))


def composite_score(metrics: PerformanceMetrics) -> tuple[Optional[float], dict[str, float]]:
    """Apply ``0.4*Sortino + 0.3*Sharpe + 0.3*Calmar`` to the available ratios.

    Missing components have their weight redistributed over the present ones, so
    an early-competition report is still comparable instead of being ``None``.
    """
    available = {
        "sortino": metrics.sortino,
        "sharpe": metrics.sharpe,
        "calmar": metrics.calmar,
    }
    present = {k: v for k, v in available.items() if v is not None}
    if not present:
        return None, {}
    total_weight = sum(COMPOSITE_WEIGHTS[k] for k in present)
    if total_weight <= 0:
        return None, {}
    score = sum(COMPOSITE_WEIGHTS[k] * v for k, v in present.items()) / total_weight
    return score, {k: COMPOSITE_WEIGHTS[k] / total_weight for k in present}


class MetricTracker:
    """Incremental equity tracker for the live loop.

    Snapshots are appended every cycle and metrics are recomputed on demand.
    Reported ratios use the daily-resampled curve, because a 60-second sampling
    rate would otherwise inflate the annualisation factor by ~24x.
    """

    def __init__(self, initial_equity: float, periods_per_year: int = 365, risk_free_rate: float = 0.0):
        self.initial_equity = float(initial_equity)
        self.periods_per_year = periods_per_year
        self.risk_free_rate = risk_free_rate
        self.snapshots: list[tuple[int, float]] = []
        self.peak_equity = float(initial_equity)

    def record(self, ts_ms: int, equity: float) -> None:
        """Append one equity mark, ignoring a non-finite one.

        A NaN mark would poison ``peak_equity`` (``max(nan, x)`` is NaN for every
        x), the daily resampling, every ratio and the persisted state -- and it
        compares False against every bound, so it would silently disable the
        drawdown guard rather than trip it.
        """
        value = float(equity)
        if not math.isfinite(value):
            return
        self.snapshots.append((int(ts_ms), value))
        self.peak_equity = max(self.peak_equity, value)

    @property
    def latest_equity(self) -> float:
        return self.snapshots[-1][1] if self.snapshots else self.initial_equity

    @property
    def drawdown(self) -> float:
        """Current drawdown from the high-water mark as a positive fraction."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.latest_equity) / self.peak_equity)

    @property
    def total_return(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return self.latest_equity / self.initial_equity - 1.0

    def metrics(self, daily: bool = True) -> PerformanceMetrics:
        """Metrics for the live tracker.

        Reported ratios use the daily-resampled curve, because a 60-second
        sampling rate annualised with a *daily* factor would inflate every ratio
        by roughly 24x (a 5-second loop far more).

        The first UTC day is the case that used to slip through: with a single
        daily mark there is nothing to resample, the raw per-loop marks are used
        instead, and they were still annualised with ``periods_per_year``. So for
        the first hours of the competition the report showed ratios built from a
        grid the factor did not describe. Here the factor is derived from the
        marks themselves whenever they are not daily.
        """
        if daily:
            curve = daily_equity_marks(self.snapshots)
            if len(curve) < 2:
                # Same-day only: fall back to the raw marks so the report is not
                # empty, but report them as *not annualised* rather than pretending
                # a per-loop grid is a daily one. Annualising 60-second marks with
                # a daily factor inflated every ratio by ~24x (a 5-second loop far
                # more) for the first hours of the run.
                curve = [self.initial_equity] + [v for _, v in self.snapshots]
                intraday = compute_metrics(
                    curve,
                    periods_per_year=self.periods_per_year,
                    risk_free_rate=self.risk_free_rate,
                )
                return _as_intraday_report(intraday)
        else:
            curve = [self.initial_equity] + [v for _, v in self.snapshots]
        return compute_metrics(
            curve,
            periods_per_year=self.periods_per_year,
            risk_free_rate=self.risk_free_rate,
        )

    def implied_seconds_per_period(self) -> Optional[float]:
        """Median gap between marks, in seconds (``None`` before there are two)."""
        if len(self.snapshots) < 2:
            return None
        gaps = sorted(b[0] - a[0] for a, b in zip(self.snapshots, self.snapshots[1:]) if b[0] > a[0])
        if not gaps:
            return None
        return gaps[len(gaps) // 2] / 1000.0

    def day_start_equity(self, ts_ms: int) -> float:
        """Equity at the start of the UTC day containing ``ts_ms`` (for the daily loss cap)."""
        day = int(ts_ms) // MS_PER_DAY
        candidates = [(ts, eq) for ts, eq in self.snapshots if ts // MS_PER_DAY <= day]
        if candidates:
            first_of_day = [eq for ts, eq in candidates if ts // MS_PER_DAY == day]
            if first_of_day:
                return first_of_day[0]
            return candidates[-1][1]
        return self.initial_equity
