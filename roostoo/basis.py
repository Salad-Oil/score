"""Cross-venue basis monitoring.

The organisers confirmed that the mock venue's prices **follow Binance**. That one
fact settles three design questions and creates a fourth:

1. **Signals transfer.** Rules 2-3 are computed from 30-minute bars; using Binance
   klines for research and live sampling for execution is only valid if both
   describe the same market. This is what makes the backtest in
   ``docs/FINDINGS.md`` meaningful at all.
2. **Rule 1's depth clause becomes testable.** "Order-book depth within +/-0.5% of
   mid exceeds $X" has no endpoint on the venue, but if prices track Binance then
   Binance's L2 book is a usable approximation -- see
   :class:`roostoo.universe.BinanceDepthProvider`. The caveat matters: Binance
   depth measures *market* liquidity, not the mock venue's own book, so it
   validates "this asset is liquid", not "Roostoo can absorb my order".
3. **Bars align.** Binance's 30-minute candles close on the UTC :00/:30 grid, and
   ``CandleBuilder`` floors samples to that same epoch-aligned grid, so a live
   sampled bar and the corresponding Binance bar describe the same window.
4. **A mismatch becomes detectable, therefore guardable.** If the venue tracks
   Binance, the basis should sit within a few basis points for majors. Anything
   wider means the reference symbol is wrong (USDT vs USD, or a different pair) or
   one feed is stale -- and fading a z-score computed against a feed that has
   drifted is trading noise, not mean reversion.

This module measures that basis, records it for the audit trail, and can exclude
pairs that fail.

Network policy differs from the depth provider on purpose: depth **fails closed**
(a pair with unknown liquidity is not traded), but the basis check **fails open**
with a loud warning. If Binance is unreachable while the venue is fine, blocking
every pair would turn a monitoring outage into a trading outage. An unverified
pair is reported as unverified rather than silently passed as good.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from .client import UrllibTransport
from .models import Ticker
from .universe import binance_symbol

log = logging.getLogger(__name__)

BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"

#: Default alarm threshold. Crypto majors tracking the same market sit within a
#: few bp of each other; 1% is deliberately loose because the two venues are
#: sampled at different instants and a fast market can genuinely gap that much in
#: seconds. Tighten to ~0.002 once you have seen a day of real basis data.
DEFAULT_BASIS_MAX_PCT = 0.01

#: Environment override, so the threshold is tunable without a redeploy.
ENV_BASIS_MAX_PCT = "BASIS_MAX_PCT"


def compute_basis(venue_mid: float, reference_price: float) -> Optional[float]:
    """``venue / reference - 1``.

    Positive means the venue is quoting *above* the reference.
    """
    if reference_price is None or reference_price <= 0 or venue_mid is None or venue_mid <= 0:
        return None
    return venue_mid / reference_price - 1.0


def resolve_threshold(raw: Optional[str] = None) -> float:
    """Threshold from an explicit value, else ``BASIS_MAX_PCT``, else the default."""
    if raw is None:
        import os

        raw = os.environ.get(ENV_BASIS_MAX_PCT)
    if raw is None or not str(raw).strip():
        return DEFAULT_BASIS_MAX_PCT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r is not numeric; using %s", ENV_BASIS_MAX_PCT, raw, DEFAULT_BASIS_MAX_PCT)
        return DEFAULT_BASIS_MAX_PCT
    return abs(value)


@dataclass
class BasisRow:
    pair: str
    reference_symbol: str
    venue_mid: float
    reference_price: Optional[float]
    basis_pct: Optional[float]

    @property
    def verified(self) -> bool:
        return self.basis_pct is not None

    def exceeds(self, threshold_pct: float) -> bool:
        return self.basis_pct is not None and abs(self.basis_pct) > threshold_pct

    def to_dict(self, threshold_pct: Optional[float] = None) -> dict[str, Any]:
        """Serialise the row. ``exceeds`` needs the threshold to mean anything.

        It used to be ``basis_pct != 0.0``, which ignored the tolerance entirely:
        a pair off by 1e-7 was journalled as ``"exceeds": true`` while
        ``BasisReport.blocked()`` -- which does apply the threshold -- happily
        kept trading it. The audit trail and the decision disagreed.
        """
        threshold = DEFAULT_BASIS_MAX_PCT if threshold_pct is None else threshold_pct
        return {
            "pair": self.pair,
            "reference": self.reference_symbol,
            "venue_mid": round(self.venue_mid, 8),
            "reference_price": None if self.reference_price is None else round(self.reference_price, 8),
            "basis_pct": None if self.basis_pct is None else round(self.basis_pct, 6),
            "exceeds": self.exceeds(threshold),
        }


@dataclass
class BasisReport:
    rows: dict[str, BasisRow] = field(default_factory=dict)
    threshold_pct: float = DEFAULT_BASIS_MAX_PCT
    source_ok: bool = False
    error: str = ""

    def blocked(self) -> set[str]:
        """Pairs to exclude: verified, and outside tolerance.

        Unverified pairs (no reference price) are *not* blocked -- see the module
        docstring on why this check fails open.
        """
        return {pair for pair, row in self.rows.items() if row.exceeds(self.threshold_pct)}

    @property
    def unverified(self) -> set[str]:
        return {pair for pair, row in self.rows.items() if not row.verified}

    def worst(self) -> Optional[BasisRow]:
        verified = [row for row in self.rows.values() if row.verified]
        if not verified:
            return None
        return max(verified, key=lambda r: abs(r.basis_pct or 0.0))

    def to_dict(self) -> dict[str, Any]:
        worst = self.worst()
        return {
            "threshold_pct": self.threshold_pct,
            "source_ok": self.source_ok,
            "error": self.error,
            "blocked": sorted(self.blocked()),
            "unverified": sorted(self.unverified),
            "worst": None if worst is None else {"pair": worst.pair, "basis_pct": round(worst.basis_pct or 0.0, 6)},
            "rows": {pair: row.to_dict(self.threshold_pct) for pair, row in self.rows.items()},
        }

    def summary(self) -> str:
        if not self.source_ok:
            return f"basis check unavailable ({self.error or 'unknown error'})"
        worst = self.worst()
        head = "no verified pair" if worst is None else f"worst {worst.pair} {worst.basis_pct * 100:+.3f}%"
        blocked = self.blocked()
        tail = f"; {len(blocked)} over {self.threshold_pct * 100:.2f}%: {sorted(blocked)}" if blocked else ""
        return head + tail


class BasisMonitor:
    """Fetch reference prices and compare them against the venue's quotes."""

    def __init__(
        self,
        threshold_pct: Optional[float] = None,
        transport: Optional[UrllibTransport] = None,
        timeout: float = 10.0,
        cache_seconds: float = 20.0,
    ) -> None:
        self.threshold_pct = resolve_threshold(None if threshold_pct is None else str(threshold_pct))
        self._transport = transport or UrllibTransport()
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, dict[str, float]] = (0.0, {})
        self.consecutive_failures = 0

    # ------------------------------------------------------------------
    def fetch_reference_prices(self, pairs: list[str], suffix: str = "USDT") -> dict[str, float]:
        """``{pair: last price}`` from Binance's public ticker, one request.

        Uses the multi-symbol form so a whole universe costs a single call per
        cycle rather than one per pair -- the rulebook penalises excessive
        request rates, and this is a monitoring nicety, not a signal.
        """
        now = time.time()
        cached_at, cached = self._cache
        if cached and now - cached_at < self.cache_seconds:
            return {p: cached[p] for p in pairs if p in cached}

        symbols = {pair: binance_symbol(pair) if suffix == "USDT" else binance_symbol(pair).replace("USDT", suffix) for pair in pairs}
        query = urllib.parse.quote(json.dumps(sorted(set(symbols.values())), separators=(",", ":")), safe="")
        url = f"{BINANCE_PRICE_URL}?symbols={query}"
        try:
            status, text = self._transport.send("GET", url, None, {"Accept": "application/json"}, self.timeout)
        except Exception as exc:
            self.consecutive_failures += 1
            log.warning("basis reference prices unavailable: %s", exc)
            return {}
        if status != 200:
            self.consecutive_failures += 1
            log.warning("basis reference prices -> HTTP %s", status)
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self.consecutive_failures += 1
            return {}
        if isinstance(payload, dict) and "code" in payload:
            # e.g. {"code":-1121,"msg":"Invalid symbol."}
            self.consecutive_failures += 1
            log.warning("binance rejected the symbol list: %s", payload.get("msg"))
            return {}

        by_symbol: dict[str, float] = {}
        for row in payload if isinstance(payload, list) else []:
            try:
                by_symbol[str(row["symbol"])] = float(row["price"])
            except (KeyError, TypeError, ValueError):
                continue
        out = {pair: by_symbol[sym] for pair, sym in symbols.items() if sym in by_symbol}
        self.cache_seconds = max(self.cache_seconds, 0.0)
        self._cache = (now, out)
        self.consecutive_failures = 0
        return out

    # ------------------------------------------------------------------
    def check(self, tickers: dict[str, Ticker], suffix: str = "USDT") -> BasisReport:
        """Compare every quoted pair against its reference price."""
        pairs = sorted(tickers)
        report = BasisReport(threshold_pct=self.threshold_pct)
        if not pairs:
            report.error = "no pairs to check"
            return report

        references = self.fetch_reference_prices(pairs, suffix=suffix)
        if not references:
            report.error = f"no reference prices after {self.consecutive_failures} failure(s)"
            return report

        report.source_ok = True
        for pair in pairs:
            ticker = tickers[pair]
            reference = references.get(pair)
            report.rows[pair] = BasisRow(
                pair=pair,
                reference_symbol=binance_symbol(pair) if suffix == "USDT" else binance_symbol(pair).replace("USDT", suffix),
                venue_mid=ticker.mid,
                reference_price=reference,
                basis_pct=compute_basis(ticker.mid, reference) if reference else None,
            )
        return report
