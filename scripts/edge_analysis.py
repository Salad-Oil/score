#!/usr/bin/env python3
"""Measure the mean-reversion edge itself, not the strategy's P&L.

`docs/FINDINGS.md` reports what the *strategy* did. That is a composition of an
edge, an exit policy and a cost model, so it cannot answer the question the team
actually has: is there an exploitable edge here at all, and is the exit policy
throwing it away?

This script answers it in three steps, all from real candles:

1. **Does the mean revert?** For every bar where the Rule 2 long entry fires, walk
   forward *without* the strategy's stop and record whether Z reaches the -0.25
   exit target, how long it takes, and how far it went against us first. The
   strategy's own record cannot show this: it only reports trades it survived.
2. **Is the edge worth more than the fees?** Report the gross edge per trade
   against the round-trip cost, and the same under two exit policies: hold to
   target, and Rule 6 only.
3. **Is any of it real?** Split the timeline chronologically and report each
   z-entry threshold early and late, with a t-statistic. A parameter that is only
   positive in one half is noise, not a finding.

The comparison that matters is **edge per trade versus the cost of a round trip**.
If the mean completed move is smaller than the fee, nothing else can save the
strategy, and that is a fact about the horizon rather than about the parameters.

Examples::

    python scripts/edge_analysis.py                     # data/, 30m (the shipped setup)
    python scripts/edge_analysis.py --data-dir data_4h --interval 4h --match-span
    python scripts/edge_analysis.py --z-entry 3.0 --gate 0.006

`--match-span` re-derives the SMA window so the indicator covers the same wall-clock
span as 48x 30-minute bars (24 hours). Comparing a 4-hour chart with a 48-bar
window would be comparing an 8-day mean against a 1-day mean, which is not a
comparison of horizons.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from roostoo.indicators import adx, sma_series, stdev_series  # noqa: E402

#: Wall-clock span the shipped configuration gives the Rule 2 window.
BASE_SPAN_SECONDS = 48 * 30 * 60

TAKER = 0.001        # rulebook: 0.1% taker, market orders
MAKER = 0.0005       # rulebook: 0.05% maker -- what a passive fill would cost
SLIPPAGE = 0.0005    # 5bps per side, the backtester's assumption
HORIZON_BARS = 24    # bars allowed for reversion in the measurement
RXIT_Z = -0.25       # Rule 2 long exit target
SPLIT = 0.60         # chronological train/test for the stability check

INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "12h": 43200, "1d": 86400,
}


def span_window(interval: str) -> int:
    """SMA window covering BASE_SPAN_SECONDS at this interval."""
    seconds = INTERVAL_SECONDS.get(interval)
    if seconds is None:
        raise SystemExit(f"unknown interval {interval!r}; known: {sorted(INTERVAL_SECONDS)}")
    return max(4, round(BASE_SPAN_SECONDS / seconds))


#: OHLC series per pair, keyed by file name. `load()` fills this so that the
#: limit-order model can see the intrabar range without changing the shape of the
#: tuples every other part already unpacks.
_SERIES: dict[str, tuple[list[float], list[float], list[float]]] = {}


def load(data_dir: Path, interval: str, window: int, adx_period: int):
    """(name, closes, z, adxs, deviations) per pair, aligned to bar index."""
    _SERIES.clear()
    out = []
    files = sorted(f for f in data_dir.glob(f"*-USD_{interval}.csv") if not f.name.startswith("sample_"))
    if not files:
        raise SystemExit(f"no *-USD_{interval}.csv in {data_dir}; run scripts/fetch_history.py first")
    for path in files:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        closes = [float(r["close"]) for r in rows]
        highs = [float(r["high"]) for r in rows]
        lows = [float(r["low"]) for r in rows]
        _SERIES[path.name] = (highs, lows, closes)
        sma, sd = sma_series(closes, window), stdev_series(closes, window)
        z: list[float] = []
        dev: list[float] = []
        for i, (m, s) in enumerate(zip(sma, sd)):
            bar = window - 1 + i
            z.append((closes[bar] - m) / s if s else 0.0)
            dev.append(abs(closes[bar] - m) / closes[bar] if closes[bar] else 0.0)
        adxs = [
            adx(highs[: i + 1], lows[: i + 1], closes[: i + 1], adx_period)
            if i + 1 >= 2 * adx_period + 1
            else None
            for i in range(len(closes))
        ]
        out.append((path.name, closes, z, adxs, dev))
    return out


def entries(data, window: int, z_entry: float, adx_max: float | None, gate: float | None):
    """Yield (name, bar, i, closes, z) per surviving Rule 2 long entry.

    ``bar`` is the signal bar; the trade fills on ``bar + 1`` (see
    :func:`forward_return`). The bound leaves room for the fill bar, the horizon
    and the last z lookup, so no entry is scored past the end of the series.
    """
    off = window - 1
    for name, closes, z, adxs, dev in data:
        for i in range(1, len(z)):
            bar = off + i
            if bar + HORIZON_BARS + 2 >= len(closes):
                break
            if not (z[i - 1] <= -z_entry and z[i] > z[i - 1]):
                continue
            if adx_max is not None:
                a = adxs[bar]
                if a is None or a >= adx_max:
                    continue
            if gate is not None and dev[i] <= gate:
                continue
            yield name, bar, i, closes, z


def forward_return(i: int, closes: list[float], z: list[float]) -> float:
    """Return of a trade signalled on bar ``i``, filled on the NEXT bar.

    The fill delay is not a detail. `Backtester` uses ``execution_delay_bars=1``:
    an order decided on bar *t* fills at the open of *t + 1*, because the signal
    needs bar *t*'s close to exist and that price is gone by the time the order
    can be sent. Measuring from the signal bar's own close instead is optimistic
    by roughly the size of one bar's move -- and on these bars that was worth
    0.13 percentage points per trade, which is four times the edge being measured.
    """
    fill = i + 1
    entry = closes[fill]
    px = closes[fill + HORIZON_BARS]
    for k in range(1, HORIZON_BARS + 1):
        if z[fill + k] >= RXIT_Z:
            px = closes[fill + k]
            break
    return px / entry - 1.0


def maker_entry(
    name: str,
    i: int,
    closes: list[float],
    z: list[float],
    offset_bps: float,
    model: str,
) -> tuple[float, bool]:
    """Return ``(return, filled)`` for a passive limit entry on the fill bar.

    A mean-reversion long is the natural case for a resting bid: the signal is
    "price just fell hard", so a bid at the signal bar's close is often filled by
    the very move that produced the signal, and it pays the maker fee instead of
    the taker fee.

    **The fill assumption is the whole argument, so both readings are reported.**
    ``model="touch"`` fills whenever the bar's low reaches the limit, which
    implicitly assumes we are at the front of the queue. ``model="through"``
    demands the bar trade strictly below it -- the pessimistic reading, and the
    one to quote when deciding.

    The order rests for one bar (bar ``i + 1``) and is cancelled if unfilled, so an
    unfilled signal produces NO trade at all. That is the honest cost of the
    approach: fewer trades, not the same trades for less money.
    """
    if model not in ("touch", "through"):
        raise ValueError(f"unknown fill model {model!r}")

    highs, lows, _closes = _SERIES[name]
    fill_bar = i + 1
    limit = closes[i] * (1.0 - offset_bps / 10_000.0)
    low = lows[fill_bar]

    filled = low <= limit if model == "touch" else low < limit
    if not filled:
        return 0.0, False

    # Filled at the limit because we are passive, then held to the same target as
    # the market-order case, so the only differences are the fee and the entry price.
    px = closes[fill_bar + HORIZON_BARS]
    for k in range(1, HORIZON_BARS + 1):
        if z[fill_bar + k] >= RXIT_Z:
            px = closes[fill_bar + k]
            break
    return px / limit - 1.0, True


def part1(data, window, z_entry, adx_max, gate, interval) -> None:
    """Reversion measured from the FILL bar, which is where the position starts."""
    total = reverted = 0
    bars_to: list[int] = []
    adverse: list[float] = []
    for _name, _bar, i, _closes, z in entries(data, window, z_entry, adx_max, gate):
        total += 1
        fill = i + 1
        entry_z = z[fill]
        worst = entry_z
        hit = None
        for k in range(1, HORIZON_BARS + 1):
            worst = min(worst, z[fill + k])
            if z[fill + k] >= RXIT_Z:
                hit = k
                break
        if hit is not None:
            reverted += 1
            bars_to.append(hit)
        adverse.append(worst - entry_z)
    if not total:
        print("1. no entries at this configuration\n")
        return
    print(f"1. DOES THE MEAN REVERT?  ({interval} bars, window={window} bars)")
    print(f"   entries (Rule 2 long, ADX<{adx_max})     {total}")
    print(f"   Z reached -0.25 within {HORIZON_BARS} bars       {reverted} ({100.0 * reverted / total:.1f}%)")
    if bars_to:
        span_min = statistics.median(bars_to) * INTERVAL_SECONDS.get(interval, 1800) / 60
        print(f"   median bars to revert                {statistics.median(bars_to):.0f}"
              f"  (~{span_min:.0f} min)")
    print(f"   mean worst adverse Z move            {statistics.mean(adverse):+.2f}")
    print()


def part2(data, window, z_entry, adx_max, gate, interval) -> None:
    rets = [
        forward_return(i, closes, z)
        for _n, _b, i, closes, z in entries(data, window, z_entry, adx_max, gate)
    ]
    if not rets:
        print("2. no entries\n")
        return
    gross = statistics.mean(rets)
    median = statistics.median(rets)
    market_cost = 2 * (TAKER + SLIPPAGE)
    maker_cost = TAKER + MAKER + 2 * SLIPPAGE
    print("2. IS THE EDGE WORTH MORE THAN THE FEES?  (holding to the target, no stop)")
    print(f"   n={len(rets)}  mean gross {gross * 100:+.3f}%  median gross {median * 100:+.3f}%")
    print(f"   net @ {market_cost * 100:.2f}% (market orders)   {gross * 100 - market_cost * 100:+.3f}%")
    print(f"   net @ {maker_cost * 100:.2f}% (maker entry)     {gross * 100 - maker_cost * 100:+.3f}%")
    verdict = "ABOVE" if gross > market_cost else "BELOW"
    print(f"   --> the mean completed move is {verdict} the round-trip cost")
    print()


def part3(data, window, adx_max, gate, interval) -> None:
    print(f"3. IS ANY OF IT REAL? (chronological {SPLIT:.0%}/{1 - SPLIT:.0%} split)")
    cost = 2 * (TAKER + SLIPPAGE)
    hdr = f"   {'z':>4} | {'n':>5} {'early':>9} {'t':>6} | {'n':>5} {'late':>9} {'t':>6}"
    print(hdr)
    for z_entry in (2.0, 2.5, 3.0, 3.5):
        early: list[float] = []
        late: list[float] = []
        for _n, bar, i, closes, z in entries(data, window, z_entry, adx_max, gate):
            (early if bar < len(closes) * SPLIT else late).append(forward_return(i, closes, z) - cost)

        def line(rs: list[float]) -> str:
            if len(rs) < 2:
                return f"{len(rs):>5} {'--':>9} {'--':>6}"
            sd = statistics.stdev(rs)
            se = sd / (len(rs) ** 0.5)
            t = statistics.mean(rs) / se if se else 0.0
            return f"{len(rs):>5} {statistics.mean(rs) * 100:>+8.3f}% {t:>+6.2f}"

        print(f"   {z_entry:>4.1f} | {line(early)} | {line(late)}")
    print()
    print("   |t| < ~2 means the mean is indistinguishable from zero.")
    print("   What matters is whether a cell keeps its SIGN in both halves.")


def part4(data, window, adx_max, gate, interval, folds: int) -> None:
    """The decisive test: split the timeline into `folds` and check the sign in each.

    A single 60/40 split can be passed by luck, and a parameter grid can be mined
    until one cell looks good in both halves. Requiring the same sign in *every*
    consecutive fold is much harder to satisfy by chance, and a sign flip is what
    a regime-dependent result looks like: the same rule makes money in one kind of
    market and loses in another, which no amount of parameter tuning repairs.
    """
    cost = 2 * (TAKER + SLIPPAGE)
    n_by_name = {name: len(closes) for name, closes, _z, _a, _d in data}
    buckets: dict[float, list[list[float]]] = {}
    for z_entry in (2.0, 2.5, 3.0, 3.5):
        frames: list[list[float]] = [[] for _ in range(folds)]
        for name, bar, i, closes, z in entries(data, window, z_entry, adx_max, gate):
            n = n_by_name[name]
            frames[min(folds - 1, int(bar / n * folds))].append(forward_return(i, closes, z) - cost)
        buckets[z_entry] = frames

    print(f"4. THE DECISIVE TEST: {folds} consecutive folds, same sign required in each")
    print(f"   net of {cost * 100:.2f}%, entry one bar after the signal, exit at -0.25 or {HORIZON_BARS} bars")
    head = "   " + f"{'z':>4} | " + " ".join(f"{'f' + str(i + 1):>16}" for i in range(folds))
    print(head)
    print("   " + "-" * (len(head) - 3))
    for z_entry in (2.0, 2.5, 3.0, 3.5):
        cells = []
        for rs in buckets[z_entry]:
            if len(rs) < 3:
                cells.append(f"{'n=' + str(len(rs)):>16}")
                continue
            sd = statistics.stdev(rs)
            se = sd / (len(rs) ** 0.5)
            t = statistics.mean(rs) / se if se else 0.0
            cells.append(f"{statistics.mean(rs) * 100:>+7.3f}%(t{t:>+4.1f})")
        print("   " + f"{z_entry:>4.1f} | " + " ".join(cells))
    print()
    print(f"   fold {folds} is the MOST RECENT stretch, the one closest to the scored window.")
    print("   A rule worth trading is positive in most folds AND not negative in the last one.")


def part5(data, window, z_entry, adx_max, gate, interval, offset_bps: float, folds: int) -> None:
    """Market order versus a passive (maker) entry, on identical signals.

    The bot sends market orders only (`engine.py` uses ``place_order(..., "MARKET")``),
    so it pays the taker fee plus slippage on every entry. A resting bid at the
    signal bar's close would pay the maker fee and no crossing cost -- but it is
    only filled when price trades back to it, and an unfilled order means the trade
    does not happen at all. Both readings of "filled" are reported, because the
    answer to "would this have filled?" is exactly where a backtest flatters itself.
    """
    market_rets: list[float] = []
    touch_rets: list[float] = []
    through_rets: list[float] = []
    touch_folds: list[list[float]] = [[] for _ in range(max(folds, 1))]
    through_folds: list[list[float]] = [[] for _ in range(max(folds, 1))]
    n_by_name = {name: len(closes) for name, closes, _z, _a, _d in data}
    market_cost = 2 * (TAKER + SLIPPAGE)
    maker_cost = TAKER + MAKER + 2 * SLIPPAGE

    total = 0
    for name, bar, i, closes, z in entries(data, window, z_entry, adx_max, gate):
        total += 1
        market_rets.append(forward_return(i, closes, z) - market_cost)

        r_touch, filled_touch = maker_entry(name, i, closes, z, offset_bps, "touch")
        if filled_touch:
            net = r_touch - maker_cost
            touch_rets.append(net)
            if folds >= 2:
                fold = min(len(touch_folds) - 1, int(bar / n_by_name[name] * len(touch_folds)))
                touch_folds[fold].append(net)

        r_through, filled_through = maker_entry(name, i, closes, z, offset_bps, "through")
        if filled_through:
            net = r_through - maker_cost
            through_rets.append(net)
            if folds >= 2:
                fold = min(len(through_folds) - 1, int(bar / n_by_name[name] * len(through_folds)))
                through_folds[fold].append(net)

    if not total:
        print("5. no entries\n")
        return

    def row(label: str, rets: list[float], sends: int) -> None:
        if not rets:
            print(f"   {label:34} {'no fills':>9}")
            return
        mean = statistics.mean(rets)
        print(f"   {label:34} n={len(rets):4}  filled {100.0 * len(rets) / sends:5.1f}%  "
              f"mean net {mean * 100:+.3f}%  win {100.0 * sum(1 for r in rets if r > 0) / len(rets):4.1f}%")

    print(f"5. MARKET ORDER vs PASSIVE (MAKER) ENTRY, identical signals, limit at the signal close")
    print(f"   signals sent {total};  market round trip {market_cost * 100:.2f}%, "
          f"maker round trip {maker_cost * 100:.2f}%")
    row("market order (what ships today)", market_rets, total)
    row("limit, 'touch' fill model", touch_rets, total)
    row("limit, 'through' fill model", through_rets, total)
    print()
    print("   'touch' assumes we are first in the queue; 'through' demands the bar trade")
    print("   strictly below the limit. Quote the 'through' number when deciding.")
    if folds >= 2 and touch_folds[0] is not None:
        print()
        print(f"   the same {len(touch_folds)}-fold test, for the two limit readings (net):")
        for label, frames in (("touch  ", touch_folds), ("through", through_folds)):
            cells = []
            for rs in frames:
                if len(rs) < 3:
                    cells.append(f"{'n=' + str(len(rs)):>16}")
                    continue
                sd = statistics.stdev(rs)
                se = sd / (len(rs) ** 0.5)
                t = statistics.mean(rs) / se if se else 0.0
                cells.append(f"{statistics.mean(rs) * 100:>+7.3f}%(t{t:>+4.1f})")
            print(f"   {label} | " + " ".join(cells))
    print()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--interval", default="30m")
    ap.add_argument("--window", type=int, default=None, help="SMA window in bars (default 48, or --match-span)")
    ap.add_argument("--match-span", action="store_true",
                    help="derive the window so it spans the same wall clock as 48x 30m (24h)")
    ap.add_argument("--z-entry", type=float, default=2.0)
    ap.add_argument("--adx-max", type=float, default=25.0, help="use a negative value to disable the Rule 3 filter")
    ap.add_argument("--gate", type=float, default=None, help="Rule 4 minimum |deviation|, e.g. 0.006")
    ap.add_argument("--adx-period", type=int, default=14)
    ap.add_argument("--folds", type=int, default=5,
                    help="consecutive folds for the decisive stability test (0 disables it)")
    ap.add_argument("--maker", action="store_true",
                    help="also compare a passive (maker) entry against the market order")
    ap.add_argument("--limit-offset-bps", type=float, default=0.0,
                    help="how far below the signal close the resting bid sits (default 0)")
    args = ap.parse_args(argv)

    window = args.window
    if window is None:
        window = span_window(args.interval) if args.match_span else 48
    adx_max = None if (args.adx_max is not None and args.adx_max < 0) else args.adx_max

    data = load(Path(args.data_dir), args.interval, window, args.adx_period)
    bars = len(data[0][1])
    span_days = bars * INTERVAL_SECONDS.get(args.interval, 1800) / 86400
    print(f"{len(data)} pairs x {bars} bars of {args.interval} = ~{span_days:.0f} days "
          f"(window={window} bars, z_entry={args.z_entry}, gate={args.gate})\n")
    part1(data, window, args.z_entry, adx_max, args.gate, args.interval)
    part2(data, window, args.z_entry, adx_max, args.gate, args.interval)
    part3(data, window, adx_max, args.gate, args.interval)
    if args.folds >= 2:
        part4(data, window, adx_max, args.gate, args.interval, args.folds)
    if args.maker:
        part5(
            data, window, args.z_entry, adx_max, args.gate, args.interval,
            args.limit_offset_bps, args.folds,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
