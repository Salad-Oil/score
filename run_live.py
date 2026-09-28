#!/usr/bin/env python3
"""Run the trading bot.

    python run_live.py --check              # read-only venue verification (do this first)
    python run_live.py --mock --cycles 20   # full loop against the built-in simulator
    python run_live.py                      # live, forever

``--check`` is the tool for Oct 1-3: it proves the keys sign correctly and that
the clock, universe and balances all look sane, **without sending a single
order**. That matters, because the rulebook forbids manual API calls that trade,
and because a signature bug found on Oct 4 costs a day of the competition.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roostoo import config as config_mod  # noqa: E402
from roostoo.basis import BasisMonitor  # noqa: E402
from roostoo.client import build_client  # noqa: E402
from roostoo.engine import TradingEngine  # noqa: E402
from roostoo.journal import Journal  # noqa: E402
from roostoo.strategies.base import load_strategy  # noqa: E402

log = logging.getLogger("run_live")


# ---------------------------------------------------------------------------
# Read-only venue check
# ---------------------------------------------------------------------------


def run_check(cfg) -> int:
    """Verify credentials, connectivity and the tradable universe. No orders."""
    client = build_client(cfg)
    problems: list[str] = []
    print("=" * 68)
    print("Roostoo connectivity check (read-only: no orders are sent)")
    print("=" * 68)

    try:
        offset = client.sync_time()
        print(f"server time      OK   clock offset {offset:+d} ms")
        if abs(offset) > 30_000:
            problems.append(f"host clock is {offset} ms from the exchange (limit is 60 s)")
    except Exception as exc:
        print(f"server time      FAIL {exc}")
        return 1

    try:
        info = client.exchange_info()
        tradable = [p for p, tp in info.pairs.items() if tp.can_trade]
        print(f"exchange info    OK   {len(info.pairs)} pairs, {len(tradable)} tradable, running={info.is_running}")
        print(f"initial wallet   {info.initial_wallet}")
        if not info.is_running:
            problems.append("exchange reports IsRunning=false")
        for pair in tradable[:12]:
            tp = info.pairs[pair]
            print(
                f"    {pair:<10} price_prec={tp.price_precision} amount_prec={tp.amount_precision} "
                f"min_order={tp.min_order:g}"
            )
    except Exception as exc:
        print(f"exchange info    FAIL {exc}")
        return 1

    try:
        tickers = client.ticker()
        print(f"\ntickers          OK   {len(tickers)} pairs quoted")
        print(f"    {'pair':<10} {'last':>14} {'spread(bps)':>12} {'24h volume':>16}")
        ranked = sorted(tickers.values(), key=lambda t: t.unit_volume, reverse=True)
        for ticker in ranked[:12]:
            print(
                f"    {ticker.pair:<10} {ticker.last:>14,.6f} {ticker.spread_bps:>12.2f} "
                f"{ticker.unit_volume:>16,.0f}"
            )
        wide = [t.pair for t in ranked if t.spread_bps > cfg.max_spread_bps]
        if wide:
            print(f"    pairs above the {cfg.max_spread_bps:.1f}bps ceiling: {wide[:8]}")
    except Exception as exc:
        print(f"tickers          FAIL {exc}")
        problems.append("ticker endpoint failed")

    # The organisers confirmed the mock venue tracks Binance. Verify it once,
    # read-only: if the basis is wide, every Rules 2-3 signal is being computed
    # against a feed that has drifted, and the fix is the symbol mapping or the
    # feed, not the strategy.
    try:
        report = BasisMonitor(timeout=cfg.request_timeout_sec).check(tickers)
        if report.source_ok:
            print(f"\nbasis vs Binance  OK   tolerance +/-{report.threshold_pct * 100:.2f}%")
            print(f"    {'pair':<10} {'venue mid':>15} {'binance':>15} {'basis':>11}")
            for pair in sorted(report.rows):
                row = report.rows[pair]
                ref = "n/a" if row.reference_price is None else f"{row.reference_price:,.6f}"
                pct = "unverified" if row.basis_pct is None else f"{row.basis_pct * 100:+.3f}%"
                print(f"    {pair:<10} {row.venue_mid:>15,.6f} {ref:>15} {pct:>11}")
            if report.blocked():
                print(f"    OVER TOLERANCE: {sorted(report.blocked())}")
                problems.append(
                    f"basis wider than {report.threshold_pct * 100:.2f}% on {sorted(report.blocked())}: "
                    "check the symbol mapping (USDT vs USD) and feed freshness before trading"
                )
            if report.unverified:
                print(f"    no reference price for {sorted(report.unverified)} (not blocked)")
        else:
            print(f"\nbasis vs Binance  WARN {report.error}")
            print("    trading is still possible, but the venue-tracks-Binance premise is unverified")
    except Exception as exc:
        print(f"\nbasis vs Binance  WARN {exc}")

    try:
        balances = client.balance()
        total_usd = balances["USD"].total if "USD" in balances else 0.0
        print(f"\nbalance          OK   USD total {total_usd:,.2f}")
        for asset, book in sorted(balances.items()):
            if book.total:
                print(f"    {asset:<8} free={book.free:,.8f} locked={book.locked:,.8f}")
    except Exception as exc:
        print(f"balance          FAIL {exc}")
        problems.append("balance endpoint failed (check the API key/signature)")

    try:
        total, by_pair = client.pending_count()
        print(f"pending orders   OK   {total} {by_pair if by_pair else ''}")
    except Exception as exc:
        print(f"pending orders   WARN {exc}")

    try:
        shorts = client.short_positions()
        print(f"short positions  OK   {len(shorts)} open")
    except Exception as exc:
        print(f"short positions  WARN {exc} (shorts may be disabled for this competition)")

    print("\n" + "-" * 68)
    if problems:
        print("RESULT: PROBLEMS FOUND")
        for problem in problems:
            print(f"  ! {problem}")
        return 2
    print("RESULT: all read-only checks passed. Signing and clock are good.")
    return 0


# ---------------------------------------------------------------------------
# Live run
# ---------------------------------------------------------------------------


def build_engine(cfg, seed: bool, data_dir: str, interval: str) -> TradingEngine:
    client = build_client(cfg)
    journal = Journal(cfg.journal_dir)
    seed_paths: dict[str, str] = {}
    if seed:
        base = Path(data_dir)
        for pair in cfg.resolved_pairs():
            path = base / f"{pair.replace('/', '-')}_{interval}.csv"
            if path.is_file():
                seed_paths[pair] = str(path)
    return TradingEngine(cfg, client=client, journal=journal, seed_paths=seed_paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--check", action="store_true", help="read-only venue verification, then exit")
    parser.add_argument("--mock", action="store_true", help="use the built-in exchange simulator")
    parser.add_argument("--cycles", type=int, default=0, help="stop after N cycles (0 = forever)")
    parser.add_argument("--flatten-on-exit", action="store_true", help="close every position on shutdown")
    parser.add_argument("--seed", dest="seed", action="store_true", default=True, help="warm up from data/ CSVs")
    parser.add_argument("--no-seed", dest="seed", action="store_false")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--interval", default="30m")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--params", default="", help="JSON object merged over the strategy defaults")
    parser.add_argument("--loop-interval", type=float, default=None, help="override LOOP_INTERVAL_SEC")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    config_mod.load_dotenv(args.env)
    try:
        cfg = config_mod.Config.from_env(require_keys=not args.mock)
        if args.mock:
            cfg.mock = True
            cfg.api_key = cfg.api_key or "MOCK"
            cfg.secret_key = cfg.secret_key or "MOCK"
        if args.strategy:
            cfg.strategy = args.strategy
        if args.params:
            import json

            cfg.strategy_params = {**cfg.strategy_params, **json.loads(args.params)}
        if args.loop_interval:
            cfg.loop_interval_sec = args.loop_interval
        cfg.validate(require_keys=not args.mock)
    except config_mod.ConfigError as exc:
        # A traceback here would be the first thing a teammate sees on a fresh
        # checkout, so say what is wrong and how to fix it instead.
        print(f"configuration error: {exc}", file=sys.stderr)
        print(
            "\nFix it in .env (copy .env.example first), or run fully offline:\n"
            "  python run_live.py --mock --cycles 20",
            file=sys.stderr,
        )
        return 2
    cfg.ensure_dirs()

    log_level = (args.log_level or cfg.log_level).upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(Path(cfg.log_dir) / "bot.log", encoding="utf-8"),
        ],
    )

    if args.check:
        return run_check(cfg)

    engine = build_engine(cfg, args.seed, args.data_dir, args.interval)
    log.info("strategy: %s", engine.strategy.describe())
    log.info("mode: %s, loop=%ss, bar=%ss", "MOCK" if cfg.mock else "LIVE", cfg.loop_interval_sec, cfg.bar_seconds)

    stopping = {"flag": False}

    def handle_signal(signum, _frame):
        log.warning("received signal %s; shutting down%s", signum, " and flattening" if args.flatten_on_exit else "")
        stopping["flag"] = True
        engine._shutting_down = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):  # pragma: no cover - not all platforms
            pass

    stats = None
    try:
        stats = engine.run(max_cycles=args.cycles or None)
    finally:
        engine.shutdown(flatten=args.flatten_on_exit)
        if stats is not None:
            log.info("final stats: %s", stats.to_dict())
            try:
                log.info("journal summary: %s", engine.journal.summary())
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
