#!/usr/bin/env python3
"""Backtest the mean-reversion rules on historical 30-minute candles.

Examples
--------
Fetch a year of real 30m history, then backtest it::

    python scripts/fetch_history.py --days 365 --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT
    python run_backtest.py --data-dir data --oos-frac 0.25

Try a parameter, without touching .env::

    python run_backtest.py --params '{"z_entry": 2.5, "adx_max": 20}'

Turn on one supplementary filter at a time (the plan in the playbook)::

    python run_backtest.py --params '{"enable_relative_volume_filter": true}'

Inspect what the strategy was thinking::

    python run_backtest.py --journal
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roostoo import config as config_mod  # noqa: E402
from roostoo.backtest import Backtester, load_universe_candles, slice_candles, split_timeline  # noqa: E402
from roostoo.candles import validate_candles  # noqa: E402
from roostoo.journal import Journal  # noqa: E402
from roostoo.metrics import DEFAULT_RATIO_CAP  # noqa: E402
from roostoo.strategies.base import load_strategy  # noqa: E402


def discover_pairs(data_dir: Path, interval: str) -> list[str]:
    """Find ``BTC-USD_30m.csv`` style files and return ``BTC/USD`` labels.

    Real data wins over the committed ``sample_`` files when both exist.
    """
    pattern = re.compile(rf"^(?:sample_)?(.+?)_{re.escape(interval)}$")
    real: list[str] = []
    sample: list[str] = []
    for path in sorted(data_dir.glob(f"*_{interval}.csv")):
        match = pattern.match(path.stem)
        if not match:
            continue
        label = match.group(1).replace("-", "/")
        (sample if path.stem.startswith("sample_") else real).append(label)
    return real or sample


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--pairs", default="", help="comma separated; default = every CSV found in --data-dir")
    parser.add_argument("--interval", default="30m", help="file suffix to read (default 30m)")
    parser.add_argument("--env", default=".env", help="dotenv file to load (optional)")
    parser.add_argument("--strategy", default=None, help="module:Class; default from config")
    parser.add_argument("--params", default="{}", help="JSON object merged over the strategy defaults")
    parser.add_argument("--delay", type=int, default=1, help="bars between decision and fill (default 1)")
    parser.add_argument("--spread-bps", type=float, default=5.0, help="assumed quoted spread (default 5bps)")
    parser.add_argument(
        "--oos-frac",
        type=float,
        default=0.0,
        help="hold out the last fraction of the timeline (e.g. 0.25) and report it separately",
    )
    parser.add_argument("--out-dir", default="reports")
    parser.add_argument("--journal", action="store_true", help="write the full decision journal")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging_level = "WARNING" if args.quiet else "INFO"
    import logging

    logging.basicConfig(level=logging_level, format="%(levelname)-7s %(name)s: %(message)s")

    config_mod.load_dotenv(args.env)
    cfg = config_mod.Config.from_env(require_keys=False)
    if args.strategy:
        cfg.strategy = args.strategy
    overrides = json.loads(args.params)
    if not isinstance(overrides, dict):
        parser.error("--params must be a JSON object")

    data_dir = Path(args.data_dir)
    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()] or discover_pairs(data_dir, args.interval)
    if not pairs:
        parser.error(
            f"no candle files found in {data_dir}. Run:\n"
            f"  python scripts/fetch_history.py --days 365 --symbols BTCUSDT,ETHUSDT,SOLUSDT"
        )

    candles = load_universe_candles(str(data_dir), pairs, interval=args.interval, bar_seconds=cfg.bar_seconds)
    if not candles:
        parser.error(f"could not load any candles for {pairs} from {data_dir}")

    # Data quality gate: a malformed bar can fabricate a signal.
    for pair, series in candles.items():
        problems = validate_candles(series)
        if problems:
            print(f"!! {pair}: {len(problems)} data problem(s), first: {problems[0]}", file=sys.stderr)

    span = {p: (s[0].ts_ms, s[-1].ts_ms, len(s)) for p, s in candles.items()}
    print(f"loaded {len(candles)} pair(s) from {data_dir}:")
    for pair, (first, last, count) in sorted(span.items()):
        print(f"  {pair:<10} {count:>6} bars  {iso(first)} -> {iso(last)}")

    counts = [c for _, _, c in span.values()]
    if counts and max(counts) > 2 * min(counts):
        print(
            "!! pairs differ a lot in coverage "
            f"({min(counts)}..{max(counts)} bars). An in/out-of-sample split is taken on the "
            "union timeline, so the short pairs can land entirely in one half and make the "
            "comparison meaningless. Re-fetch a common window with --start/--end first.",
            file=sys.stderr,
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    # --- in-sample (or full sample) -------------------------------------
    strategy = load_strategy(cfg.strategy, {**cfg.strategy_params, **overrides})
    print(f"\nstrategy: {strategy.describe()}")
    print(f"warm-up : {strategy.required_bars} bars per pair\n")

    if args.oos_frac > 0:
        split_ts, _ = split_timeline(candles, 1.0 - args.oos_frac)
        train = slice_candles(candles, end_ts=split_ts - 1)
        warmup = strategy.required_bars
        # The OOS slice carries a warm-up prefix so indicators are already primed
        # at the split, but trading is disabled until the split itself.
        first_ts = min(c[0].ts_ms for c in candles.values())
        oos_start = _warmup_start(candles, split_ts, warmup) or first_ts
        test = slice_candles(candles, start_ts=oos_start)
        plan = [
            ("in-sample", train, None),
            ("out-of-sample", test, split_ts),
        ]
    else:
        plan = [("full-sample", candles, None)]

    for label, subset, trade_from in plan:
        if not subset or not any(subset.values()):
            print(f"skipping {label}: empty slice")
            continue
        run_strategy = load_strategy(cfg.strategy, {**cfg.strategy_params, **overrides})
        journal = Journal(cfg.journal_dir, run_id=f"bt-{label}", enabled=args.journal)
        engine = Backtester(
            cfg,
            run_strategy,
            subset,
            execution_delay_bars=args.delay,
            assumed_spread_bps=args.spread_bps,
            label=label,
            trade_from_ts=trade_from,
            journal=journal,
        )
        result = engine.run()
        print(result.report())
        print()
        results.append(result)
        _write_outputs(out_dir, label, result)

    if len(results) == 2:
        _print_comparison(results[0], results[1])
    return 0


def _warmup_start(candles, split_ts: int, warmup: int):
    """Earliest bar such that the split has ``warmup`` bars of history before it."""
    timeline = sorted({c.ts_ms for s in candles.values() for c in s})
    before = [ts for ts in timeline if ts < split_ts]
    if len(before) <= warmup:
        return timeline[0] if timeline else None
    return before[-warmup]


def iso(ts_ms: int) -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts_ms / 1000))


def _write_outputs(out_dir: Path, label: str, result) -> None:
    slug = label.replace(" ", "_")
    (out_dir / f"backtest_{slug}.json").write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    equity_path = out_dir / f"equity_{slug}.csv"
    with equity_path.open("w", encoding="utf-8") as handle:
        handle.write("ts_ms,utc,nav\n")
        for ts, nav in result.equity:
            handle.write(f"{ts},{iso(ts)},{nav:.4f}\n")
    if result.trades:
        trades_path = out_dir / f"trades_{slug}.csv"
        with trades_path.open("w", encoding="utf-8", newline="") as handle:
            import csv

            writer = csv.DictWriter(handle, fieldnames=list(result.trades[0].to_row().keys()))
            writer.writeheader()
            for fill in result.trades:
                writer.writerow(fill.to_row())
    print(f"wrote {out_dir}/backtest_{slug}.json, equity_{slug}.csv")


def _print_comparison(train, test) -> None:
    print("=== in-sample vs out-of-sample ===")
    print(f"{'metric':<20}{'in-sample':>14}{'out-of-sample':>16}")
    rows = [
        ("total return", train.metrics.total_return, test.metrics.total_return),
        ("sharpe", train.metrics.sharpe, test.metrics.sharpe),
        ("sortino", train.metrics.sortino, test.metrics.sortino),
        ("calmar", train.metrics.calmar, test.metrics.calmar),
        ("max drawdown", train.metrics.max_drawdown, test.metrics.max_drawdown),
        ("composite", train.metrics.composite, test.metrics.composite),
        ("fills", float(len(train.trades)), float(len(test.trades))),
    ]
    for name, a, b in rows:
        print(f"{name:<20}{_fmt(a):>14}{_fmt(b):>16}")

    # A ratio that reached the cap is a floor, not a measurement. Saying which
    # columns are saturated is the difference between "Sortino was -10" and
    # "Sortino was at least -10".
    capped = [
        f"{label} {name}"
        for label, metrics in (("IS", train.metrics), ("OOS", test.metrics))
        for name, value in (("sharpe", metrics.sharpe), ("sortino", metrics.sortino), ("calmar", metrics.calmar))
        if value is not None and abs(value) >= DEFAULT_RATIO_CAP - 1e-9
    ]
    if capped:
        print(
            f"\nnote: {', '.join(capped)} reached the +/-{DEFAULT_RATIO_CAP:g} reporting cap, so those "
            "figures are lower bounds, not the measured ratio."
        )
    for label, metrics in (("in-sample", train.metrics), ("out-of-sample", test.metrics)):
        if metrics.composite_is_partial:
            print(f"note: the {label} composite is missing at least one ratio; its weights were renormalised.")
    if test.metrics.periods_per_year == 0:
        print(
            "note: the out-of-sample window is under one day, so annualised figures and the composite "
            "are not reported."
        )

    # Only meaningful when the in-sample score is a *positive* number. Two
    # negative composites divide to a positive ratio -- the README's own
    # headline run gives (-8.94 / -7.13) == 1.25, which the old check printed as
    # "(holds up)" for a strategy that lost 12% out of sample.
    is_c, oos_c = train.metrics.composite, test.metrics.composite
    if is_c is None or oos_c is None:
        return
    if is_c <= 0:
        print(
            "\nOOS/IS ratio: not interpretable -- both comparisons are losses, so the ratio carries no "
            "signal about generalisation. Compare total return and drawdown instead."
        )
        return
    ratio = oos_c / is_c
    print(
        f"\nOOS/IS composite ratio: {ratio:.2f} "
        f"({'holds up' if ratio > 0.5 else 'likely overfit -- treat with suspicion'})"
    )


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and abs(value) < 10:
        return f"{value:+.3f}"
    return f"{value:,.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
