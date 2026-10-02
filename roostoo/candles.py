"""Candles, and the means of producing them.

Rule 2 is defined on **30-minute bars** (48 bars = 24 hours), and Rules 3-6 plus
every supplementary filter need per-bar high/low/close/volume. The Roostoo public
API provides none of that: ``/v3/ticker`` returns a single snapshot.

So there are exactly two ways to get bars, and this module implements both:

``CandleBuilder``
    **Live.** Every loop we sample the ticker and fold the observation into the
    bar for the current interval. High/low come from the sampled extremes, and
    volume is the *change* in the ticker's rolling 24h ``CoinTradeValue``, which
    is the only volume information the API leaks. Bars only close at interval
    boundaries, so the strategy is evaluated once per closed bar -- which is
    precisely what "30-minute candle" means.

``load_candles``
    **Backtest.** Read a CSV of real OHLCV history. ``scripts/fetch_history.py``
    writes exactly this schema from Binance's public klines endpoint (the
    organiser's own Data Sources Pack recommends Binance Vision for bulk history).

Sampled bars are an approximation of true OHLC: an intrabar spike that happens
between two samples is invisible. That is a real limitation of the live venue and
is worth stating in the submitted README.
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

MS_PER_DAY = 86_400_000


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def bar_open_ms(ts_ms: int, bar_seconds: int) -> int:
    """Floor a timestamp to the open of its bar (UTC grid, aligned to epoch)."""
    step = bar_seconds * 1000
    return int(ts_ms) // step * step


def trading_day_id(ts_ms: int, offset_hours: int = 8) -> int:
    """Day index for Rule 11, on a UTC+offset calendar.

    Rule 11 halts new entries once the day's portfolio loss reaches 2%. "The day"
    has to be pinned somewhere: the team chose UTC+8 (Hong Kong / Singapore,
    which is also the organisers' timezone). A fixed offset is used rather than a
    tz database so behaviour never depends on the host's tzdata.
    """
    return (int(ts_ms) + offset_hours * 3_600_000) // MS_PER_DAY


def bar_index(ts_ms: int, bar_seconds: int) -> int:
    """Monotonic bar counter, used to measure holding time and cooldowns."""
    return bar_open_ms(ts_ms, bar_seconds) // (bar_seconds * 1000)


# ---------------------------------------------------------------------------
# Candle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar. ``ts_ms`` is the bar's OPEN time in UTC milliseconds."""

    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    quote_volume: float = 0.0
    trades: int = 0

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    def to_row(self) -> dict[str, Any]:
        return {
            "ts_ms": self.ts_ms,
            "open": f"{self.open:.8f}",
            "high": f"{self.high:.8f}",
            "low": f"{self.low:.8f}",
            "close": f"{self.close:.8f}",
            "volume": f"{self.volume:.8f}",
            "quote_volume": f"{self.quote_volume:.8f}",
            "trades": self.trades,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Candle":
        """Build from a CSV/API row. Tolerates missing optional columns."""
        return cls(
            ts_ms=int(float(row["ts_ms"])),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume") or 0.0),
            quote_volume=float(row.get("quote_volume") or 0.0),
            trades=int(float(row.get("trades") or 0)),
        )

    @classmethod
    def from_samples(cls, ts_ms: int, samples: list[tuple[float, float, float]], volume: float = 0.0) -> "Candle":
        """Build a bar from sampled ``(price, bid, ask)`` observations.

        ``open`` is the first sample, ``close`` the last, and high/low the
        extremes across both the traded price and the quotes -- using the quotes
        as well means a wide spread shows up as intrabar range, which is the
        honest reading for stop placement.
        """
        if not samples:
            raise ValueError("cannot build a candle from zero samples")
        prices = [p for p, _, _ in samples]
        highs = [max(p, a if a > 0 else p) for p, _, a in samples]
        lows = [min(p, b if b > 0 else p) for p, b, _ in samples]
        close = samples[-1][0]
        return cls(
            ts_ms=ts_ms,
            open=samples[0][0],
            high=max(highs),
            low=min(lows),
            close=close,
            volume=volume,
            quote_volume=volume * close,
        )


def load_candles(path: str | Path, bar_seconds: Optional[int] = None) -> list[Candle]:
    """Read a candles CSV, sorted ascending and de-duplicated by timestamp."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"candle file not found: {p}")
    by_ts: dict[int, Candle] = {}
    with p.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        missing = {"ts_ms", "open", "high", "low", "close"} - set(reader.fieldnames)
        if missing:
            raise ValueError(f"{p} is missing required columns: {sorted(missing)}")
        for row in reader:
            candle = Candle.from_row(row)
            if bar_seconds:
                candle = Candle(
                    ts_ms=bar_open_ms(candle.ts_ms, bar_seconds),
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    quote_volume=candle.quote_volume,
                    trades=candle.trades,
                )
            by_ts[candle.ts_ms] = candle  # later duplicate wins
    return [by_ts[ts] for ts in sorted(by_ts)]


def validate_candles(candles: Iterable[Candle]) -> list[str]:
    """Return a list of data-quality complaints (empty means clean).

    Run before a backtest: one malformed bar can silently fabricate a signal.
    """
    problems: list[str] = []
    prev_ts = -1
    for candle in candles:
        if candle.ts_ms <= prev_ts:
            problems.append(f"non-ascending/duplicate timestamp at {candle.ts_ms}")
        prev_ts = candle.ts_ms
        if candle.high < candle.low:
            problems.append(f"high < low at {candle.ts_ms}")
        if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
            problems.append(f"open/close outside high-low range at {candle.ts_ms}")
        if candle.close <= 0:
            problems.append(f"non-positive close at {candle.ts_ms}")
    return problems


# ---------------------------------------------------------------------------
# Live bar construction
# ---------------------------------------------------------------------------


class CandleBuilder:
    """Fold ticker samples into fixed-interval bars, one series per pair.

    Usage in the live loop::

        closed = builder.add(pair, ts_ms, price, bid, ask, cumulative_volume)
        if closed is not None:
            ...  # a bar just finished; the strategy may now act on it

    ``add`` returns the completed :class:`Candle` when the observation belongs to
    a new interval, else ``None``. The just-closed bar is appended to history
    before being returned, so the strategy always sees it.
    """

    def __init__(self, bar_seconds: int = 1800, max_bars: int = 500) -> None:
        if bar_seconds < 1:
            raise ValueError("bar_seconds must be >= 1")
        self.bar_seconds = int(bar_seconds)
        self.max_bars = int(max_bars)
        self._history: dict[str, list[Candle]] = {}
        self._open: dict[str, dict[str, Any]] = {}
        self._last_cum_volume: dict[str, float] = {}
        # Bars we had to synthesise because sampling missed a whole interval.
        self.gap_count = 0

    # -- accessors ------------------------------------------------------
    def history(self, pair: str) -> list[Candle]:
        return self._history.get(pair, [])

    def histories(self) -> dict[str, list[Candle]]:
        return self._history

    def has_history(self, pair: str, bars: int) -> bool:
        return len(self._history.get(pair, [])) > 0 and len(self._history[pair]) >= bars

    def current_bar(self, pair: str) -> Optional[Candle]:
        """The still-forming bar, for intrabar stop checks and for seeding a run."""
        state = self._open.get(pair)
        if not state or not state["samples"]:
            return None
        return Candle.from_samples(
            state["ts_ms"], state["samples"], volume=state["volume"]
        )

    def seed(self, pair: str, candles: Iterable[Candle]) -> None:
        """Prime history from a CSV so a cold start does not need 48 live bars.

        The competition's prep window (Oct 1-3) is for exactly this: warming up
        the indicator state so the bot can trade on day one.
        """
        existing = self._history.setdefault(pair, [])
        for candle in sorted(candles, key=lambda c: c.ts_ms):
            if existing and candle.ts_ms <= existing[-1].ts_ms:
                continue
            existing.append(candle)
        if len(existing) > self.max_bars:
            del existing[: len(existing) - self.max_bars]

    # -- ingestion ------------------------------------------------------
    def add(
        self,
        pair: str,
        ts_ms: int,
        price: float,
        bid: float = 0.0,
        ask: float = 0.0,
        cumulative_volume: Optional[float] = None,
    ) -> Optional[Candle]:
        """Record one observation; return the bar that just closed, if any."""
        # `price <= 0` is False for NaN, so an explicit finiteness test is what
        # actually keeps a poisoned sample out. A NaN here propagates into the
        # bar's high/low/close and then into ATR/ADX for the whole Wilder window,
        # and a NaN ATR is what silently approved stop-free entries downstream.
        if not math.isfinite(price) or price <= 0:
            return None
        bucket = bar_open_ms(ts_ms, self.bar_seconds)
        state = self._open.get(pair)

        if state is None:
            self._open[pair] = {
                "ts_ms": bucket,
                "samples": [(price, bid, ask)],
                "volume": 0.0,
                "last_ts": ts_ms,
            }
            self._remember_cumulative(pair, cumulative_volume)
            return None

        if bucket == state["ts_ms"]:
            state["samples"].append((price, bid, ask))
            state["volume"] += self._volume_delta(pair, cumulative_volume)
            state["last_ts"] = ts_ms
            return None

        # Interval rolled: close the old bar, open a new one.
        closed = Candle.from_samples(state["ts_ms"], state["samples"], volume=state["volume"])
        self._append(pair, closed)

        # If sampling skipped whole intervals (a restart, or an API outage),
        # record the gap rather than pretending the market was continuous.
        expected = state["ts_ms"] + self.bar_seconds * 1000
        missed = max(0, (bucket - expected) // (self.bar_seconds * 1000))
        if missed:
            self.gap_count += int(missed)
            log.warning("%s: %d missing bar(s) between %d and %d", pair, missed, expected, bucket)

        self._open[pair] = {
            "ts_ms": bucket,
            "samples": [(price, bid, ask)],
            "volume": 0.0,
            "last_ts": ts_ms,
        }
        self._remember_cumulative(pair, cumulative_volume)
        return closed

    def flush(self, pair: str) -> Optional[Candle]:
        """Close the in-progress bar early (used at shutdown and in tests)."""
        state = self._open.pop(pair, None)
        if not state or not state["samples"]:
            return None
        closed = Candle.from_samples(state["ts_ms"], state["samples"], volume=state["volume"])
        self._append(pair, closed)
        return closed

    # -- internals ------------------------------------------------------
    def _append(self, pair: str, candle: Candle) -> None:
        series = self._history.setdefault(pair, [])
        if series and candle.ts_ms <= series[-1].ts_ms:
            return
        series.append(candle)
        if len(series) > self.max_bars:
            del series[: len(series) - self.max_bars]

    def _remember_cumulative(self, pair: str, cumulative_volume: Optional[float]) -> None:
        if cumulative_volume is None:
            return
        self._last_cum_volume[pair] = float(cumulative_volume)

    def _volume_delta(self, pair: str, cumulative_volume: Optional[float]) -> float:
        """Turn the ticker's rolling 24h traded value into a per-interval volume.

        ``CoinTradeValue`` is a *rolling* cumulative figure, so the traded value
        since the previous sample is the difference. A decrease means the window
        rolled over (or the field reset); we clamp at zero rather than booking a
        negative volume.
        """
        if cumulative_volume is None:
            return 0.0
        current = float(cumulative_volume)
        previous = self._last_cum_volume.get(pair)
        self._last_cum_volume[pair] = current
        if previous is None:
            return 0.0
        return max(0.0, current - previous)


def candles_from_rows(rows: Iterable[dict[str, Any]]) -> list[Candle]:
    """Convenience for tests and for adapting an API payload to candles."""
    return [Candle.from_row(row) for row in rows]
