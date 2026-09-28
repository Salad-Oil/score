#!/usr/bin/env python3
"""Regenerate the tiny committed sample bar files under ``data/``.

Why this exists
---------------
Tests, demos and the backtest smoke-run must not depend on network access (CI
boxes are often sandboxed, and Binance is geo-blocked in some regions).  This
wrapper just calls the *synthetic* generator that already lives in
:mod:`scripts.fetch_history` -- nothing is duplicated -- and writes two small
files with the same CSV schema as a real download:

    data/sample_BTC-USD_30m.csv
    data/sample_ETH-USD_30m.csv

They are ~1200 bars (~25 days at 30m) with the fixed seed 7, so the output is
byte-stable across runs and safe to commit.

The import is done by putting this script's own directory on ``sys.path``.  The
project deliberately has no ``scripts/__init__.py`` (scripts here are run as
files, not imported as a package), so this is the smallest correct way to share
the generator.

Usage::

    python scripts/make_sample_data.py
    python scripts/make_sample_data.py --bars 2000 --out-dir data
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from fetch_history import (  # noqa: E402  (import after sys.path fix-up)
    generate_synthetic_bars,
    parse_interval_ms,
    write_csv,
)

DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
DEFAULT_BARS = 1200
DEFAULT_SEED = 7
DEFAULT_INTERVAL = "30m"
REPO_ROOT = _HERE.parent


def bars_to_window(interval: str, bars: int, *, end_ms: int | None = None) -> tuple[int, int]:
    """Build a ``[start_ms, end_ms)`` window holding exactly ``bars`` bars."""
    step = parse_interval_ms(interval)
    now_ms = int(time.time() * 1000) if end_ms is None else int(end_ms)
    # Snap to the interval grid so the newest bar is complete, not half-formed.
    end_ms = (now_ms // step) * step
    return end_ms - bars * step, end_ms


def make(out_dir: Path, symbols: tuple[str, ...], bars: int, interval: str, seed: int, quiet: bool = False) -> int:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start_ms, end_ms = bars_to_window(interval, bars)

    for symbol in symbols:
        rows = generate_synthetic_bars(symbol, interval, start_ms, end_ms, seed=seed)
        if not rows:
            print(f"error: no bars generated for {symbol}", file=sys.stderr)
            return 1
        base = symbol.upper().replace("USDT", "").replace("USD", "")
        path = out_dir / f"sample_{base}-USD_{interval}.csv"
        count = write_csv(path, rows)
        if not quiet:
            print(f"wrote {count} bars -> {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_sample_data.py",
        description="Write small committed sample OHLCV CSV files (synthetic, no network).",
    )
    parser.add_argument("--out-dir", default="data", help="output directory (default: data)")
    parser.add_argument("--bars", type=int, default=DEFAULT_BARS, help=f"bars per symbol (default: {DEFAULT_BARS})")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL, help=f"bar interval (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="comma-separated symbols")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"RNG seed (default: {DEFAULT_SEED})")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.bars <= 0:
        print("error: --bars must be positive", file=sys.stderr)
        return 2
    symbols = tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip())
    if not symbols:
        print("error: no symbols given", file=sys.stderr)
        return 2
    # Relative --out-dir resolves against the repo root so the committed sample
    # lands in <repo>/data no matter which cwd the script was invoked from.
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    return make(out_dir, symbols, args.bars, args.interval, args.seed, quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
