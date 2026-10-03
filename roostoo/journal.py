"""Append-only audit trail.

Two of the competition's screening rules are about evidence, not returns:

* **Trade Log Integrity** -- "bots must demonstrate consistent, autonomous trade
  execution aligned with their declared strategy".
* **Commit History Transparency** -- "no traces of manually called APIs".

So every cycle writes what the bot saw, what it decided, what it sent and what
came back. The JSONL files are the machine-readable evidence; ``trades.csv`` is
the human-readable one a judge can open directly.

Everything is append-only and per-UTC-day, which means a crash loses at most the
final line and a 14-day run never produces one unmanageable file.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

MS_PER_DAY = 86_400_000

TRADE_CSV_COLUMNS = [
    "ts_ms",
    "pair",
    "action",
    "side",
    "quantity",
    "price",
    "notional",
    "fee",
    "order_id",
    "role",
    "reason",
]


def _jsonable(value: Any) -> Any:
    """Best-effort conversion so one odd object cannot break a journal write."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # A bare NaN/Infinity token is not valid JSON, and every downstream
        # reader (judges' tooling included) is entitled to assume the audit trail
        # parses. Record it as a string instead -- the event still shows what
        # happened, and the file stays machine-readable.
        return value if math.isfinite(value) else repr(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return repr(value)


class Journal:
    """Writes decisions, orders, fills and equity marks for one live run."""

    def __init__(self, directory: str | Path = "journal", run_id: Optional[str] = None, enabled: bool = True) -> None:
        self.dir = Path(directory)
        self.enabled = enabled
        self.run_id = run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
        self._lock = threading.Lock()
        self._seq = 0
        self._day: Optional[int] = None
        self._events_handle = None
        self._trades_handle = None
        self._trades_writer: Optional[csv.writer] = None
        self.counts: dict[str, int] = {}
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    # -- files ----------------------------------------------------------
    def _ensure_day(self, ts_ms: int) -> None:
        day = int(ts_ms) // MS_PER_DAY
        if day == self._day and self._events_handle is not None:
            return
        self._rotate(day)

    def _rotate(self, day: int) -> None:
        self._close_handles()
        stamp = time.strftime("%Y%m%d", time.gmtime(day * MS_PER_DAY / 1000))
        events_path = self.dir / f"decisions-{stamp}.jsonl"
        trades_path = self.dir / f"trades-{stamp}.csv"
        self._events_handle = events_path.open("a", encoding="utf-8")
        is_new = not trades_path.exists() or trades_path.stat().st_size == 0
        self._trades_handle = trades_path.open("a", encoding="utf-8", newline="")
        self._trades_writer = csv.DictWriter(self._trades_handle, fieldnames=TRADE_CSV_COLUMNS)
        if is_new:
            self._trades_writer.writeheader()
            self._trades_handle.flush()
        self._day = day
        log.info("journal day %s -> %s", stamp, events_path)

    def _close_handles(self) -> None:
        for handle in (self._events_handle, self._trades_handle):
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except Exception:  # pragma: no cover - best effort at shutdown
                    pass
        self._events_handle = self._trades_handle = None
        self._trades_writer = None

    # -- core -----------------------------------------------------------
    def event(self, kind: str, ts_ms: Optional[int] = None, **fields: Any) -> None:
        """Append one structured event."""
        if not self.enabled:
            return
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        try:
            with self._lock:
                self._ensure_day(ts)
                self._seq += 1
                record = {
                    "seq": self._seq,
                    "ts_ms": ts,
                    "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts / 1000)),
                    "run_id": self.run_id,
                    "kind": kind,
                }
                record.update({k: _jsonable(v) for k, v in fields.items()})
                assert self._events_handle is not None
                self._events_handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                self._events_handle.flush()
        except Exception as exc:  # journaling must never kill the trading loop
            log.error("journal write failed (%s): %s", kind, exc)

    # -- typed helpers ---------------------------------------------------
    def startup(self, config_dump: dict[str, Any], strategy: str, extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"config": config_dump, "strategy": strategy}
        if extra:
            payload.update(extra)
        self.event("startup", **payload)

    def cycle(self, bar_index_value: int, now_ms: int, nav: float, cash_usd: float, depth: dict[str, Any] | None = None) -> None:
        self.event("cycle", ts_ms=now_ms, bar_index=bar_index_value, nav=round(nav, 4), cash_usd=round(cash_usd, 4), depth=depth)

    def universe(self, ts_ms: int, selection: dict[str, Any]) -> None:
        self.event("universe", ts_ms=ts_ms, **selection)

    def signals(self, ts_ms: int, signals: Iterable[Any], diagnostics: dict[str, Any] | None = None) -> None:
        rows = [s.to_dict() if hasattr(s, "to_dict") else _jsonable(s) for s in signals]
        if not rows and not diagnostics:
            return
        self.event("signals", ts_ms=ts_ms, count=len(rows), signals=rows, diagnostics=diagnostics or {})

    def decision(self, ts_ms: int, decision: Any) -> None:
        self.event("risk_decision", ts_ms=ts_ms, **_jsonable(decision))

    def order(self, ts_ms: int, action: Any, result: Any = None, error: str = "") -> None:
        self.event("order", ts_ms=ts_ms, action=_jsonable(action), result=_jsonable(result), error=error)

    def reconciliation(self, ts_ms: int, pair: str, outcome: str, detail: dict[str, Any] | None = None) -> None:
        self.event("reconcile", ts_ms=ts_ms, pair=pair, outcome=outcome, detail=detail or {})

    def risk(self, ts_ms: int, **fields: Any) -> None:
        self.event("risk", ts_ms=ts_ms, **fields)

    def halt(self, ts_ms: int, reason: str, **fields: Any) -> None:
        self.event("halt", ts_ms=ts_ms, reason=reason, **fields)

    def error(self, where: str, message: str, ts_ms: Optional[int] = None, **fields: Any) -> None:
        self.event("error", ts_ms=ts_ms, where=where, message=message, **fields)

    def equity(self, ts_ms: int, nav: float, metrics: Optional[dict[str, Any]] = None) -> None:
        self.event("equity", ts_ms=ts_ms, nav=round(nav, 4), metrics=metrics or {})

    # -- the judge-facing table -----------------------------------------
    def trade(
        self,
        ts_ms: int,
        pair: str,
        action: str,
        side: str,
        quantity: float,
        price: float,
        fee: float,
        order_id: Any = "",
        role: str = "",
        reason: str = "",
    ) -> None:
        if not self.enabled:
            return
        row = {
            "ts_ms": ts_ms,
            "pair": pair,
            "action": action,
            "side": side,
            "quantity": f"{quantity:.10f}",
            "price": f"{price:.10f}",
            "notional": f"{quantity * price:.6f}",
            "fee": f"{fee:.6f}",
            "order_id": order_id if order_id is not None else "",
            "role": role,
            "reason": reason,
        }
        try:
            with self._lock:
                self._ensure_day(ts_ms)
                assert self._trades_writer is not None and self._trades_handle is not None
                self._trades_writer.writerow(row)
                self._trades_handle.flush()
        except Exception as exc:
            log.error("trade log write failed: %s", exc)
        self.counts["trade"] = self.counts.get("trade", 0) + 1

    # -- lifecycle -------------------------------------------------------
    @property
    def events_path(self) -> Optional[Path]:
        return self.dir / f"decisions-{time.strftime('%Y%m%d', time.gmtime((self._day or 0) * MS_PER_DAY / 1000))}.jsonl" if self._day is not None else None

    def summary(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))

    def close(self) -> None:
        with self._lock:
            self._close_handles()


def read_events(path: str | Path, kinds: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    """Read a journal file back, optionally filtered by event kind."""
    wanted = {k for k in kinds} if kinds else None
    out: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if wanted is None or record.get("kind") in wanted:
                out.append(record)
    return out


def load_trades(path: str | Path) -> list[dict[str, Any]]:
    """Read back a trades CSV (used by tests and post-run analysis)."""
    p = Path(path)
    if not p.is_file():
        return []
    with p.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
