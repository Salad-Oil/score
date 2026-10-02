#!/usr/bin/env python3
"""Parameter sweep / ablation harness.

The playbook's own plan is to add the supplementary filters one at a time and
keep whichever earns its place. This script does exactly that, and refuses to let
a result be judged on in-sample data alone: every row reports the out-of-sample
composite alongside the in-sample one, plus an overfit ratio.

Two independent axes can be swept:

* ``--grid``        strategy parameters (Rules 2-4 and the supplementary filters)
* ``--config-grid`` portfolio / exit settings (Rules 5-12), i.e. ``Config``
  fields. Use ``null`` to disable one, e.g. ``{"stop_atr_mult": [1.5, null]}``.

Examples
--------
Which z-entry threshold actually works?::

    python run_sweep.py --grid '{"z_entry": [1.5, 2.0, 2.5, 3.0]}'

Does the ADX filter (Rule 3) earn its place?::

    python run_sweep.py --grid '{"require_adx": [true, false]}'

Is the Rule 5 ATR stop helping or hurting? (the decisive experiment)::

    python run_sweep.py --config-grid '{"stop_atr_mult": [null, 1.5, 3.0, 5.0]}'

Combine both::

    python run_sweep.py --config-grid '{"stop_atr_mult": [null, 3.0]}' \
                        --grid '{"z_entry": [2.0, 2.5]}'

A single run is not evidence. Look for a *plateau* across neighbouring
parameters, and distrust any cell whose out-of-sample score collapses.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import itertools
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roostoo import config as config_mod  # noqa: E402
from roostoo.backtest import Backtester, load_universe_candles, slice_candles, split_timeline  # noqa: E402
from roostoo.strategies.base import load_strategy  # noqa: E402

METRIC_KEYS = ("composite", "sharpe", "sortino", "calmar", "total_return", "max_drawdown")


def metric_of(result: Any, key: str) -> Optional[float]:
    return getattr(result.metrics, key, None) if result.metrics else None


def build_grid(specs: list[str]) -> list[dict[str, Any]]:
    """Expand ``{"param": [values]}`` JSON objects into a cartesian product.

    A spec may also be ``@path/to/file.json``, which loads the JSON from a file.
    That exists for a practical reason: PowerShell mangles double quotes when
    passing inline JSON to a native command (``'{"z_entry": [2.0]}'`` arrives as
    ``{z_entry: [2.0]}``), so on Windows the file form is the reliable one.
    An object with several keys is a cartesian product across those keys, so a
    single file can describe a whole multi-axis sweep.
    """
    axes: dict[str, list[Any]] = {}
    for spec in specs:
        text = Path(spec[1:]).read_text(encoding="utf-8") if spec.startswith("@") else spec
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"could not parse grid spec {spec!r}: {exc}. "
                "Use @file.json on Windows: the shell strips quotes from inline JSON."
            ) from exc
        if not isinstance(parsed, dict):
            raise SystemExit(f"--grid/--config-grid must be a JSON object, got {spec!r}")
        for key, values in parsed.items():
            axes[key] = values if isinstance(values, list) else [values]
    if not axes:
        return [{}]
    keys = list(axes)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(axes[k] for k in keys))]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--pairs", default="")
    parser.add_argument("--interval", default="30m")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--params", default="{}", help="base strategy overrides applied to every run")
    parser.add_argument("--grid", action="append", default=[], help="strategy JSON {param: [values]}; repeatable")
    parser.add_argument(
        "--config-grid",
        action="append",
        default=[],
        help='Config/risk JSON {field: [values]}; repeatable. Use null to disable, e.g. {"stop_atr_mult": [1.5, null]}',
    )
    parser.add_argument("--oos-frac", type=float, default=0.25, help="0 disables the holdout")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--spread-bps", type=float, default=5.0)
    parser.add_argument(
        "--rank-by",
        default="composite_oos",
        choices=[f"{k}_is" for k in METRIC_KEYS] + [f"{k}_oos" for k in METRIC_KEYS],
    )
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--max-runs", type=int, default=60, help="safety cap on the grid size")
    parser.add_argument("--out", default="reports/sweep.csv")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else "ERROR", format="%(levelname)s %(name)s: %(message)s")
    config_mod.load_dotenv(args.env)
    cfg = config_mod.Config.from_env(require_keys=False)
    if args.strategy:
        cfg.strategy = args.strategy

    base = json.loads(args.params)
    combos = build_grid(args.grid)
    config_combos = build_grid(args.config_grid) if args.config_grid else [{}]
    total_runs = len(combos) * len(config_combos)
    if total_runs > args.max_runs:
        raise SystemExit(
            f"grid expands to {total_runs} runs ({len(combos)} strategy x {len(config_combos)} config), "
            f"above --max-runs {args.max_runs}. Narrow it, or raise --max-runs deliberately."
        )
    known_fields = {f.name for f in dataclasses.fields(cfg)}
    for field in config_combos[0]:
        if field not in known_fields:
            raise SystemExit(f"--config-grid field {field!r} is not a Config field; known: {sorted(known_fields)}")

    from run_backtest import discover_pairs  # sibling script

    data_dir = Path(args.data_dir)
    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()] or discover_pairs(data_dir, args.interval)
    candles = load_universe_candles(str(data_dir), pairs, interval=args.interval, bar_seconds=cfg.bar_seconds)
    if not candles:
        raise SystemExit(f"no candles for {pairs} in {data_dir}; run scripts/fetch_history.py first")

    train: Optional[dict[str, Any]] = candles
    test: Optional[dict[str, Any]] = None
    trade_from: Optional[int] = None
    if args.oos_frac > 0:
        split_ts, _ = split_timeline(candles, 1.0 - args.oos_frac)
        train = slice_candles(candles, end_ts=split_ts - 1)
        warmup = load_strategy(cfg.strategy, {**cfg.strategy_params, **base, **combos[0]}).required_bars
        timeline = sorted({c.ts_ms for s in candles.values() for c in s})
        before = [ts for ts in timeline if ts < split_ts]
        test = slice_candles(candles, start_ts=before[-warmup] if len(before) > warmup else timeline[0])
        trade_from = split_ts

    print(f"pairs     : {sorted(candles)}")
    print(f"runs      : {total_runs}  (oos-frac={args.oos_frac}, delay={args.delay}, spread={args.spread_bps}bps)\n")

    runs: list[dict[str, Any]] = []
    header = f"{'#':>3}  {'strategy':<40}{'config':<26}{'IS comp':>9}{'OOS comp':>9}{'IS ret':>9}{'OOS ret':>9}{'OOS n':>7}"
    print(header)
    print("-" * len(header))

    index = 0
    for strategy_overrides, config_overrides in itertools.product(combos, config_combos):
        index += 1
        run_cfg = dataclasses.replace(cfg, **config_overrides) if config_overrides else cfg
        overrides = {**cfg.strategy_params, **base, **strategy_overrides}
        row: dict[str, Any] = {
            "run": index,
            "strategy": json.dumps(strategy_overrides, sort_keys=True),
            "config": json.dumps(config_overrides, sort_keys=True),
        }
        started = time.time()
        for label, subset, from_ts in (("is", train, None), ("oos", test, trade_from)):
            if subset is None or not any(subset.values()):
                continue
            strategy = load_strategy(cfg.strategy, overrides)
            engine = Backtester(
                run_cfg,
                strategy,
                subset,
                execution_delay_bars=args.delay,
                assumed_spread_bps=args.spread_bps,
                label=label,
            )
            result = engine.run()
            for key in METRIC_KEYS:
                row[f"{key}_{label}"] = metric_of(result, key)
            row[f"fills_{label}"] = len(result.trades)
            row[f"fees_{label}"] = round(result.fees_paid, 2)
        row["seconds"] = round(time.time() - started, 2)
        is_c, oos_c = row.get("composite_is"), row.get("composite_oos")
        # A ratio is only meaningful when the in-sample score is a positive number
        # comfortably away from zero. Two *negative* composites divide to a
        # positive ratio -- the default configuration's -8.94 / -7.13 reads as
        # "1.25, ok" while losing money in both windows. Suppress anything that
        # cannot carry the "generalises" meaning the flag implies.
        if is_c is not None and oos_c is not None and is_c >= 0.5:
            row["overfit_ratio"] = oos_c / is_c
        else:
            row["overfit_ratio"] = None
        runs.append(row)
        print(
            f"{index:>3}  {row['strategy'][:40]:<40}{row['config'][:26]:<26}"
            f"{_fmt(is_c):>9}{_fmt(oos_c):>9}"
            f"{_fmt(row.get('total_return_is')):>9}{_fmt(row.get('total_return_oos')):>9}"
            f"{row.get('fills_oos', 0):>7}"
        )

    ranked = sorted(runs, key=lambda r: (r.get(args.rank_by) is None, -(r.get(args.rank_by) or -1e9)))
    print(f"\n=== top {min(args.top, len(ranked))} by {args.rank_by} ===")
    print(f"{'strategy':<42}{'config':<26}{'IS comp':>9}{'OOS comp':>9}{'OOS/IS':>8}")
    for row in ranked[: args.top]:
        ratio = row.get("overfit_ratio")
        flag = "" if ratio is None else ("ok" if ratio > 0.5 else "overfit?")
        print(
            f"{row['strategy'][:42]:<42}{row['config'][:26]:<26}"
            f"{_fmt(row.get('composite_is')):>9}{_fmt(row.get('composite_oos')):>9}"
            f"{(f'{ratio:.2f}' if ratio is not None else 'n/a'):>8} {flag}"
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(runs[0].keys()))
        writer.writeheader()
        writer.writerows(runs)
    print(f"\nwrote {out} ({len(runs)} runs)")
    print(
        "\nRead this as a search for a plateau, not a peak: one best cell is noise, and a high "
        "in-sample score with a poor out-of-sample score is overfitting."
    )
    return 0


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}" if abs(value) < 10 else f"{value:+.0f}"


if __name__ == "__main__":
    raise SystemExit(main())
