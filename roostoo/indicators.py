"""Pure-python technical analysis over rolling windows.

The public API exposes no candles, so every indicator here is built from the
sequence of sampled mid prices the bot collects itself (see
:mod:`roostoo.engine`). All functions are total: they return ``None`` when there
is not enough history rather than raising, which keeps the decision loop simple.

No numpy, on purpose -- the AWS box gets a bare Python, and a 500-sample window
is trivial work at a 60-second cadence.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

SECONDS_PER_YEAR = 365 * 24 * 3600


def _clean(values: Sequence[float]) -> list[float]:
    return [float(v) for v in values if v is not None and not math.isnan(float(v))]


# ---------------------------------------------------------------------------
# Returns and volatility
# ---------------------------------------------------------------------------


def simple_returns(prices: Sequence[float]) -> list[float]:
    """Period-over-period simple returns ``p[t]/p[t-1] - 1``."""
    out: list[float] = []
    for prev, cur in zip(prices, prices[1:]):
        if prev:
            out.append(cur / prev - 1.0)
    return out


def log_returns(prices: Sequence[float]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(prices, prices[1:]):
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def stdev(values: Sequence[float], sample: bool = True) -> Optional[float]:
    """Standard deviation. ``sample=True`` uses the n-1 denominator."""
    data = _clean(values)
    n = len(data)
    if n < (2 if sample else 1):
        return None
    mean = sum(data) / n
    var = sum((x - mean) ** 2 for x in data) / (n - 1 if sample else n)
    return math.sqrt(max(var, 0.0))


def mean(values: Sequence[float]) -> Optional[float]:
    data = _clean(values)
    if not data:
        return None
    return sum(data) / len(data)


def realized_volatility(returns: Sequence[float], periods_per_year: float) -> Optional[float]:
    """Annualised volatility of a return series."""
    sd = stdev(returns)
    if sd is None:
        return None
    return sd * math.sqrt(periods_per_year)


def downside_deviation(returns: Sequence[float], target: float = 0.0) -> Optional[float]:
    """Deviation of returns that fall below ``target`` (Sortino denominator).

    Only downside is penalised, which is exactly what the hackathon's
    Sortino-weighted score rewards.
    """
    data = _clean(returns)
    if not data:
        return None
    downside = [min(0.0, r - target) ** 2 for r in data]
    return math.sqrt(sum(downside) / len(data))


# ---------------------------------------------------------------------------
# Moving averages and oscillators
# ---------------------------------------------------------------------------


def sma(values: Sequence[float], window: int) -> Optional[float]:
    data = _clean(values)
    if window <= 0 or len(data) < window:
        return None
    return sum(data[-window:]) / window


def ema_series(values: Sequence[float], window: int) -> list[float]:
    """Exponential moving average for the whole series (seeded with an SMA)."""
    data = _clean(values)
    if window <= 0 or len(data) < window:
        return []
    k = 2.0 / (window + 1.0)
    out = [sum(data[:window]) / window]
    for value in data[window:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def ema(values: Sequence[float], window: int) -> Optional[float]:
    series = ema_series(values, window)
    return series[-1] if series else None


def rsi(values: Sequence[float], window: int = 14) -> Optional[float]:
    """Wilder's RSI over the last ``window`` changes.

    Uses simple averages of gains/losses instead of Wilder smoothing so that the
    result depends only on the last ``window + 1`` samples -- nicer for tests and
    for a fixed rolling buffer.
    """
    data = _clean(values)
    if len(data) < window + 1 or window < 2:
        return None
    changes = [b - a for a, b in zip(data[-window - 1 : -1], data[-window:])]
    gains = sum(c for c in changes if c > 0) / window
    losses = -sum(c for c in changes if c < 0) / window
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def true_range_proxy(prices: Sequence[float]) -> list[float]:
    """Absolute successive price changes.

    A stand-in for ATR: without OHLC bars the best available range proxy is the
    size of the move between samples.
    """
    data = _clean(prices)
    return [abs(b - a) for a, b in zip(data, data[1:])]


def atr(prices: Sequence[float], window: int = 14) -> Optional[float]:
    """Average absolute change -- a volatility unit for stop placement."""
    tr = true_range_proxy(prices)
    if len(tr) < window:
        return None
    return sum(tr[-window:]) / window


def zscore(values: Sequence[float], window: int) -> Optional[float]:
    """How many standard deviations the latest value sits from the window mean."""
    data = _clean(values)
    if len(data) < window or window < 2:
        return None
    window_data = data[-window:]
    mu = sum(window_data) / window
    sd = stdev(window_data)
    if not sd:
        return 0.0
    return (window_data[-1] - mu) / sd


def percentile_rank(values: Sequence[float], window: int) -> Optional[float]:
    """Position of the latest value inside its own recent range, in [0, 1]."""
    data = _clean(values)
    if len(data) < window:
        return None
    window_data = data[-window:]
    lo, hi = min(window_data), max(window_data)
    if hi <= lo:
        return 0.5
    return (window_data[-1] - lo) / (hi - lo)


def donchian(prices: Sequence[float], window: int) -> Optional[tuple[float, float]]:
    """Trailing ``(lowest, highest)`` over the window, excluding the latest bar."""
    data = _clean(prices)
    if len(data) < window + 1:
        return None
    window_data = data[-window - 1 : -1]
    return min(window_data), max(window_data)


def momentum(prices: Sequence[float], lookback: int) -> Optional[float]:
    """Total return over the last ``lookback`` samples."""
    data = _clean(prices)
    if len(data) < lookback + 1 or lookback <= 0:
        return None
    base = data[-lookback - 1]
    if base <= 0:
        return None
    return data[-1] / base - 1.0


def normalized_slope(values: Sequence[float], window: int) -> Optional[float]:
    """Least-squares slope divided by mean level: a scale-free trend strength.

    Positive means the sampled price has been trending up over the window.
    """
    data = _clean(values)
    if len(data) < window or window < 3:
        return None
    y = data[-window:]
    n = len(y)
    x_mean = (n - 1) / 2.0
    y_mean = sum(y) / n
    denom = sum((i - x_mean) ** 2 for i in range(n))
    if denom == 0 or y_mean == 0:
        return None
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(y))
    return (num / denom) / y_mean


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


def drawdown_series(equity: Sequence[float]) -> list[float]:
    """Drawdown at each point, as a negative fraction of the running peak."""
    out: list[float] = []
    peak = float("-inf")
    for value in equity:
        peak = max(peak, value)
        out.append((value - peak) / peak if peak > 0 else 0.0)
    return out


def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0.2 == -20%)."""
    if not equity:
        return 0.0
    return abs(min(drawdown_series(equity)))


def current_drawdown(equity: Sequence[float]) -> float:
    """Present drawdown from the high-water mark, as a positive fraction."""
    if not equity:
        return 0.0
    return abs(drawdown_series(equity)[-1])


# ---------------------------------------------------------------------------
# Rolling series (Rule 2 needs Z_t AND Z_{t-1})
# ---------------------------------------------------------------------------


def sma_series(values: Sequence[float], window: int) -> list[float]:
    """Rolling simple moving average, aligned to the tail of ``values``.

    Output index ``i`` corresponds to input index ``window - 1 + i``.
    """
    data = _clean(values)
    if window <= 0 or len(data) < window:
        return []
    running = sum(data[:window])
    out = [running / window]
    for i in range(window, len(data)):
        running += data[i] - data[i - window]
        out.append(running / window)
    return out


def stdev_series(values: Sequence[float], window: int, sample: bool = True) -> list[float]:
    """Rolling standard deviation, aligned to the tail of ``values``."""
    data = _clean(values)
    if window < 2 or len(data) < window:
        return []
    out: list[float] = []
    for end in range(window, len(data) + 1):
        sd = stdev(data[end - window : end], sample=sample)
        out.append(0.0 if sd is None else sd)
    return out


def zscore_series(values: Sequence[float], window: int) -> list[float]:
    """Rolling z-score of the price against its own trailing window.

    ``Z = (P_t - SMA_window) / Std_window`` (Rule 2).

    The team's entry trigger is a *turn*: an extreme reading on the previous bar
    combined with a shrinking magnitude on the current bar. That needs
    ``Z_t`` and ``Z_{t-1}``, so the whole tail is returned instead of one value.
    """
    data = _clean(values)
    if window < 2 or len(data) < window:
        return []
    out: list[float] = []
    for end in range(window, len(data) + 1):
        w = data[end - window : end]
        sd = stdev(w)
        mu = sum(w) / window
        out.append(0.0 if not sd else (w[-1] - mu) / sd)
    return out


def price_deviation_pct(prices: Sequence[float], window: int) -> Optional[float]:
    """``|Price - SMA_window| / Price`` -- Rule 4's "is it worth trading" test.

    The z-score says the move is unusual *relative to recent noise*; this says it
    is also large in absolute terms. In a very quiet market a 2-sigma move can be
    economically meaningless and be eaten by fees, so both must pass.
    """
    data = _clean(prices)
    if len(data) < window or window < 2:
        return None
    avg = sum(data[-window:]) / window
    last = data[-1]
    if last <= 0:
        return None
    return abs(last - avg) / last


# ---------------------------------------------------------------------------
# OHLCV indicators
# ---------------------------------------------------------------------------
# Rules 3, 5, 6 and the supplementary filters need genuine candles: Wilder's ADX
# to refuse mean reversion inside a real trend, Wilder's ATR to place stops, VWAP
# to see where volume actually traded, and volume/range expansion to avoid
# buying into a repricing. The Roostoo public API exposes no candle endpoint, so
# these consume bars the bot builds itself (live) or an external OHLCV feed
# (backtest).
# ---------------------------------------------------------------------------


def _wilder(values: Sequence[float], period: int) -> list[float]:
    """Wilder's smoothing: a running sum seeded by the first ``period`` values.

    Note the seeding convention: the first output is the *sum* of the first
    ``period`` inputs, so dividing the last element by ``period`` yields
    Wilder's average. TR/+DM/-DM use it as a sum; ADX uses it as an average.

    Non-finite inputs are treated as "no observation" and skipped rather than
    folded into the running sum. A running sum is exactly the wrong place for a
    NaN: one poisoned sample would otherwise invalidate every later ATR/ADX
    reading for the rest of the window, and a NaN ATR is what used to approve a
    stop-free entry.
    """
    data = [float(v) for v in values]
    usable = [v for v in data if math.isfinite(v)]
    if period < 1 or len(usable) < period:
        return []
    out = [sum(usable[:period])]
    for value in usable[period:]:
        out.append(out[-1] - out[-1] / period + value)
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    """Wilder's true range, one value per bar after the first.

    Length stays ``n - 1`` so it lines up with the directional-movement series
    that :func:`directional_movement` zips it against; a bar with a non-finite
    input is marked ``nan`` (and skipped by :func:`_wilder`) instead of being
    dropped, which would silently shift every later value by one bar.
    """
    n = min(len(highs), len(lows), len(closes))
    out: list[float] = []
    for i in range(1, n):
        high, low = float(highs[i]), float(lows[i])
        prev_close = float(closes[i - 1])
        if not (math.isfinite(high) and math.isfinite(low) and math.isfinite(prev_close)):
            out.append(math.nan)
            continue
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr_wilder(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> Optional[float]:
    """Wilder's Average True Range -- the unit for Rule 5's stop distance.

    Returns ``None`` rather than a non-finite value. The risk layer treats a
    missing ATR as "no stop distance available", and the caller must then refuse
    the entry instead of approving a position with no stop at all.
    """
    smoothed = _wilder(true_range(highs, lows, closes), period)
    if not smoothed:
        return None
    value = smoothed[-1] / period
    return value if math.isfinite(value) else None


def directional_movement(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> Optional[tuple[float, float, float, float]]:
    """Return ``(+DI, -DI, ADX, DX)`` for the most recent bar.

    Classic Wilder construction: directional movement is smoothed with the same
    running sum as true range, the DI lines are the ratios scaled to 100, DX is
    their normalised spread, and ADX is Wilder's average of DX.

    Returns ``None`` until there are enough bars (``2 * period + 1``), because
    ADX needs a full period of DX values and each DX needs a full period of
    smoothed TR/DM.
    """
    n = min(len(highs), len(lows), len(closes))
    if period < 2 or n < 2 * period + 1:
        return None

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    tr_smooth = _wilder(true_range(highs, lows, closes), period)
    plus_smooth = _wilder(plus_dm, period)
    minus_smooth = _wilder(minus_dm, period)
    if not tr_smooth or not plus_smooth or not minus_smooth:
        return None

    dx_values: list[float] = []
    plus_di_last = minus_di_last = 0.0
    for tr_v, p_v, m_v in zip(tr_smooth, plus_smooth, minus_smooth):
        if tr_v <= 0:
            plus_di_last = minus_di_last = 0.0
        else:
            plus_di_last = 100.0 * p_v / tr_v
            minus_di_last = 100.0 * m_v / tr_v
        total = plus_di_last + minus_di_last
        dx_values.append(0.0 if total <= 0 else 100.0 * abs(plus_di_last - minus_di_last) / total)

    adx_smooth = _wilder(dx_values, period)
    if not adx_smooth:
        return None
    adx_value = adx_smooth[-1] / period
    if not (math.isfinite(adx_value) and math.isfinite(plus_di_last) and math.isfinite(minus_di_last)):
        # A non-finite ADX must read as "unknown", not as a number. The Rule 3
        # gate compares it with `>=`, and `nan >= 25.0` is False -- so a NaN ADX
        # would wave through exactly the trending market the filter exists to
        # refuse.
        return None
    return plus_di_last, minus_di_last, adx_value, dx_values[-1]


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Optional[float]:
    """ADX alone -- Rule 3's trend filter (``ADX < 25`` means "range-bound").

    ADX measures trend *strength* without direction, which is exactly what a
    mean-reversion book needs to know: fading a stretched move is profitable in a
    range and ruinous in a trend.
    """
    result = directional_movement(highs, lows, closes, period)
    return None if result is None else result[2]


def rolling_ranges(highs: Sequence[float], lows: Sequence[float]) -> list[float]:
    """Per-bar ``high - low`` -- the input to VolExpansion."""
    return [float(h) - float(l) for h, l in zip(highs, lows)]


def vol_expansion(highs: Sequence[float], lows: Sequence[float], short: int = 6, long: int = 48) -> Optional[float]:
    """``EMA_short(High - Low) / EMA_long(High - Low)`` (supplementary Rule 4).

    A ratio near 1 means the market's bar-to-bar range is normal. A reading of
    2-3 means volatility has regime-shifted, and mean reversion is the wrong
    trade because the market is repricing, not oscillating.
    """
    ranges = rolling_ranges(highs, lows)
    short_ema = ema(ranges, short)
    long_ema = ema(ranges, long)
    if short_ema is None or long_ema is None or long_ema <= 0:
        return None
    return short_ema / long_ema


def vwap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    window: int = 48,
) -> Optional[float]:
    """Rolling volume-weighted average price using the typical price.

    ``typical = (H + L + C) / 3``; ``VWAP = sum(typical * volume) / sum(volume)``.
    """
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < window or window < 1:
        return None
    num = den = 0.0
    for i in range(n - window, n):
        typical = (highs[i] + lows[i] + closes[i]) / 3.0
        vol = float(volumes[i])
        num += typical * vol
        den += vol
    if den <= 0:
        return None
    return num / den


def vwap_gap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    window: int = 48,
) -> Optional[float]:
    """``(Close - VWAP) / VWAP`` (supplementary Rule 2).

    Negative means price sits below where volume actually traded -- a long
    leaning; positive means the opposite for a short.
    """
    anchor = vwap(highs, lows, closes, volumes, window)
    if anchor is None or anchor <= 0 or not closes:
        return None
    return (closes[-1] - anchor) / anchor


def relative_volume(volumes: Sequence[float], window: int = 48, include_current: bool = False) -> Optional[float]:
    """``Volume_t / SMA_window(Volume)`` (supplementary Rule 3).

    ``include_current=False`` (the default) divides by the average of the
    *preceding* ``window`` bars, so a single large bar does not inflate its own
    benchmark. Set ``include_current=True`` for the literal reading of the rule.
    """
    data = _clean(volumes)
    if include_current:
        baseline = sma(data, window)
        if baseline is None or baseline <= 0:
            return None
        return data[-1] / baseline
    if len(data) < window + 1:
        return None
    baseline = sum(data[-window - 1 : -1]) / window
    if baseline <= 0:
        return None
    return data[-1] / baseline


def return_shock_z(closes: Sequence[float], window: int = 12) -> Optional[float]:
    """Standardised latest return (supplementary Rule 1).

    ``rt = P_t / P_{t-1} - 1`` compared against the mean and standard deviation
    of the ``window`` returns *before* it. Standardising matters because a 3%
    move is routine for DOGE and a shock for BTC; dividing by each pair's own
    recent return dispersion makes the filter comparable across the universe.

    The reference window deliberately excludes ``rt``: including it would
    dampen the very outlier this filter exists to catch.
    """
    rets = simple_returns(closes)
    if len(rets) < window + 1 or window < 2:
        return None
    current = rets[-1]
    history = rets[-window - 1 : -1]
    mu = sum(history) / len(history)
    sd = stdev(history)
    if not sd:
        return None
    return (current - mu) / sd
