#!/usr/bin/env python3
"""Round-trip forensics for a backtest's trade log.

Answers the questions a loss actually raises -- was the edge absent, or eaten by
costs, or clipped by a stop? -- from ``reports/trades_<label>.csv``.

    python scripts/analyze_backtest.py reports/trades_in-sample.csv

Reported per round trip: entry, exit, bars held, gross P&L, fees, net P&L. The
per-exit-reason table is usually the most informative part: if ``time stop``
dominates, the holding horizon is too short for the deviation to revert; if
``stop hit`` dominates, the stop is inside the noise.
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

BAR_MS_DEFAULT = 1_800_000


@dataclass
class RoundTrip:
    pair: str
    direction: str
    entry_ts: int
    exit_ts: int
    quantity: float
    entry_price: float
    exit_price: float
    fees: float
    exit_reason: str

    @property
    def bars_held(self) -> float:
        return (self.exit_ts - self.entry_ts) / BAR_MS_DEFAULT

    @property
    def gross(self) -> float:
        if self.direction == "long":
            return self.quantity * (self.exit_price - self.entry_price)
        return self.quantity * (self.entry_price - self.exit_price)

    @property
    def net(self) -> float:
        return self.gross - self.fees


ENTRY_SIDES = {"BUY", "SHORT_OPEN"}


def classify_exit(reason: str, side: str) -> str:
    text = (reason or "").lower()
    if "time stop" in text:
        return "time stop (Rule 6)"
    if "stop hit" in text or "trailing stop" in text:
        return "stop (Rule 5)"
    if "take profit" in text:
        return "take profit"
    if "mean reached" in text:
        return "z-exit (Rule 2)"
    if "flatten" in text or "kill" in text:
        return "flatten / kill switch"
    return "other / unexplained"


def pair_up(rows: list[dict[str, Any]]) -> list[RoundTrip]:
    open_legs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trips: list[RoundTrip] = []
    for row in sorted(rows, key=lambda r: int(r["ts_ms"])):
        pair = row["pair"]
        side = row["side"].upper()
        price = float(row["price"])
        qty = float(row["quantity"])
        fee = float(row["fee"])
        ts = int(row["ts_ms"])
        if side in ENTRY_SIDES:
            open_legs[pair].append({"ts": ts, "price": price, "qty": qty, "fee": fee, "side": side})
            continue
        if not open_legs[pair]:
            continue
        leg = open_legs[pair].pop(0)
        trips.append(
            RoundTrip(
                pair=pair,
                direction="long" if leg["side"] == "BUY" else "short",
                entry_ts=leg["ts"],
                exit_ts=ts,
                quantity=min(qty, leg["qty"]),
                entry_price=leg["price"],
                exit_price=price,
                fees=leg["fee"] + fee,
                exit_reason=classify_exit(row.get("reason", ""), side),
            )
        )
    return trips


def summarise(trips: list[RoundTrip], label: str) -> str:
    if not trips:
        return f"{label}: no completed round trips"
    lines = [f"=== {label} ===", f"round trips       {len(trips)}"]
    gross = sum(t.gross for t in trips)
    fees = sum(t.fees for t in trips)
    net = sum(t.net for t in trips)
    notional = sum(t.quantity * t.entry_price for t in trips)
    lines += [
        f"gross P&L         {gross:>12,.2f}",
        f"fees              {fees:>12,.2f}   ({fees / notional * 100:.3f}% of entry notional)" if notional else "",
        f"net P&L           {net:>12,.2f}",
        f"winners           {sum(1 for t in trips if t.net > 0)} / {len(trips)} "
        f"({sum(1 for t in trips if t.net > 0) / len(trips) * 100:.1f}%)",
        f"avg gross / trip  {gross / len(trips):>12,.2f}",
        f"avg fee / trip    {fees / len(trips):>12,.2f}",
        f"avg bars held     {statistics.fmean(t.bars_held for t in trips):>12.1f}",
    ]
    if gross > 0:
        lines.append(f"fee drag          {fees / gross * 100:>11.0f}% of gross P&L")
    else:
        lines.append("fee drag          n/a (gross P&L is already negative)")

    lines.append("")
    lines.append("by exit reason:")
    lines.append(f"  {'reason':<22}{'n':>5}{'gross':>13}{'fees':>12}{'net':>13}{'avg bars':>10}{'win%':>7}")
    by_reason: dict[str, list[RoundTrip]] = defaultdict(list)
    for trip in trips:
        by_reason[trip.exit_reason].append(trip)
    for reason, group in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        wins = sum(1 for t in group if t.net > 0) / len(group) * 100
        lines.append(
            f"  {reason:<22}{len(group):>5}{sum(t.gross for t in group):>13,.0f}"
            f"{sum(t.fees for t in group):>12,.0f}{sum(t.net for t in group):>13,.0f}"
            f"{statistics.fmean(t.bars_held for t in group):>10.1f}{wins:>7.1f}"
        )

    lines.append("")
    lines.append("by direction:")
    for direction in ("long", "short"):
        group = [t for t in trips if t.direction == direction]
        if not group:
            continue
        lines.append(
            f"  {direction:<6} n={len(group):<5} gross={sum(t.gross for t in group):>12,.0f} "
            f"fees={sum(t.fees for t in group):>10,.0f} net={sum(t.net for t in group):>12,.0f} "
            f"win={sum(1 for t in group if t.net > 0) / len(group) * 100:>5.1f}%"
        )

    lines.append("")
    lines.append("by pair (net P&L):")
    by_pair: dict[str, list[RoundTrip]] = defaultdict(list)
    for trip in trips:
        by_pair[trip.pair].append(trip)
    for pair, group in sorted(by_pair.items(), key=lambda kv: sum(t.net for t in kv[1])):
        lines.append(
            f"  {pair:<10} n={len(group):<4} gross={sum(t.gross for t in group):>11,.0f} "
            f"fees={sum(t.fees for t in group):>9,.0f} net={sum(t.net for t in group):>11,.0f}"
        )
    return "\n".join(line for line in lines if line != "")


def load(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="one or more trades CSV files")
    parser.add_argument("--bar-seconds", type=int, default=1800)
    args = parser.parse_args(argv)

    global BAR_MS_DEFAULT
    BAR_MS_DEFAULT = args.bar_seconds * 1000

    for raw in args.paths:
        path = Path(raw)
        if not path.is_file():
            print(f"!! not found: {path}", file=sys.stderr)
            continue
        trips = pair_up(load(path))
        print(summarise(trips, path.name))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
