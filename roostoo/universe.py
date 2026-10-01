"""Rule 1 -- universe selection.

    "Filter 8 top trading volume cryptos in the past 24h to ensure small spread
     and slippage. Exclude assets whose bid-ask spread exceeds 0.1%. Only enter a
     position when order-book depth within +/-0.5% of the mid-price exceeds $X."

The first two clauses are directly computable from ``/v3/ticker``:

* 24h trading volume is ``UnitTradeValue`` (quote-currency turnover).
* the bid-ask spread is ``(MinAsk - MaxBid) / mid``, which is a *real* cost on
  this venue: every entry pays the half-spread plus 0.1% taker commission.

The third clause is the problem. **The Roostoo public API documents no
order-book endpoint.** The README's ``available_sub`` block (socket.io ``DEPTH``)
is commented out, and ``/v3/ticker`` gives only the best bid and best ask --
top-of-book, not depth. So "depth within +/-0.5%" cannot be measured from the
documented API today.

Rather than pretend, the depth test is a pluggable :class:`DepthProvider`:

``NullDepthProvider``
    Default. Depth is **not** checked. This is the correct setting for a
    historical backtest, where no L2 snapshot history exists (the team's own
    decision: select the majors, ignore depth offline).
``BinanceDepthProvider``
    Real L2 depth from Binance's public ``/api/v3/depth``. Free, no key. Use it
    live as a *proxy* for venue liquidity, remembering that the execution venue
    is Roostoo, not Binance.
``RoostooDepthProvider``
    Points at a Roostoo path from ``ROOSTOO_DEPTH_PATH``. Nothing is assumed
    about the response schema beyond a tolerant parser, so the moment the
    organisers confirm an endpoint (ask in the WhatsApp group / Oct 1 workshop)
    this becomes a one-line config change.
``TickerLiquidityProxy``
    Last resort: estimate tradable flow from 24h turnover. An assumption, not a
    measurement, and off by default for that reason.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

from .client import UrllibTransport
from .config import Config
from .models import Ticker

log = logging.getLogger(__name__)

BINANCE_DEPTH_URL = "https://api.binance.com/api/v3/depth"


def binance_symbol(pair: str) -> str:
    """``BTC/USD`` -> ``BTCUSDT`` (Binance quotes in USDT; we treat it as USD)."""
    base, _, quote = pair.upper().partition("/")
    quote = quote or "USD"
    return f"{base}{'USDT' if quote in ('USD', 'USDT') else quote}"


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DepthSnapshot:
    """Resting notional within a band around mid, per side, in quote currency."""

    pair: str
    mid: float
    bid_notional: float  # buy-side support: resting bids within the band
    ask_notional: float  # sell-side liquidity: resting asks within the band
    source: str = "unknown"

    def for_side(self, side: str) -> float:
        """Depth available to *us*: a BUY consumes asks, a SELL consumes bids."""
        return self.ask_notional if side.upper() == "BUY" else self.bid_notional


class DepthProvider(Protocol):  # pragma: no cover - structural typing only
    name: str

    def snapshot(self, pair: str, band_pct: float) -> Optional[DepthSnapshot]: ...


class NullDepthProvider:
    """No depth check. Default, and the only honest option for a backtest."""

    name = "none"

    def snapshot(self, pair: str, band_pct: float) -> Optional[DepthSnapshot]:
        return None


class TickerLiquidityProxy:
    """Estimate tradable flow from 24h turnover.

    ``per_bar = UnitTradeValue / bars_per_day``, then assume we may consume
    ``share`` of one bar's turnover without moving the market. Both inputs are
    guesses; ``share`` is configurable via ``DEPTH_LIQUIDITY_SHARE`` because the
    right value has to come out of the backtest, not out of thin air.
    """

    name = "ticker"

    def __init__(self, tickers: dict[str, Ticker], bars_per_day: float, share: float = 0.02) -> None:
        self._tickers = tickers
        self._bars_per_day = max(bars_per_day, 1.0)
        self._share = max(share, 0.0)

    def snapshot(self, pair: str, band_pct: float) -> Optional[DepthSnapshot]:
        ticker = self._tickers.get(pair)
        if ticker is None or ticker.unit_volume <= 0:
            return None
        estimate = ticker.unit_volume / self._bars_per_day * self._share
        return DepthSnapshot(
            pair=pair,
            mid=ticker.mid,
            bid_notional=estimate,
            ask_notional=estimate,
            source="ticker-proxy",
        )


class BinanceDepthProvider:
    """Real L2 depth from Binance's public order book (no API key)."""

    name = "binance"

    def __init__(
        self,
        url: str = BINANCE_DEPTH_URL,
        limit: int = 1000,
        timeout: float = 10.0,
        transport: Optional[UrllibTransport] = None,
        cache_seconds: float = 20.0,
    ) -> None:
        self.url = url
        self.limit = limit
        self.timeout = timeout
        self._transport = transport or UrllibTransport()
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, DepthSnapshot]] = {}

    def snapshot(self, pair: str, band_pct: float) -> Optional[DepthSnapshot]:
        import time as _time

        now = _time.time()
        cached = self._cache.get(pair)
        if cached and now - cached[0] < self._cache_seconds:
            return cached[1]

        symbol = binance_symbol(pair)
        url = f"{self.url}?symbol={symbol}&limit={self.limit}"
        try:
            status, text = self._transport.send(
                "GET", url, None, {"Accept": "application/json"}, self.timeout
            )
        except Exception as exc:  # network failure must never block trading
            log.warning("binance depth for %s failed: %s", pair, exc)
            return None
        if status != 200:
            log.warning("binance depth for %s -> HTTP %s", pair, status)
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None

        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        if not bids or not asks:
            return None
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        floor = mid * (1.0 - band_pct)
        ceiling = mid * (1.0 + band_pct)
        bid_notional = sum(float(p) * float(q) for p, q in bids if float(p) >= floor)
        ask_notional = sum(float(p) * float(q) for p, q in asks if float(p) <= ceiling)
        snapshot = DepthSnapshot(pair, mid, bid_notional, ask_notional, source="binance")
        self._cache[pair] = (now, snapshot)
        return snapshot


class RoostooDepthProvider:
    """Depth from a Roostoo endpoint, once the organisers confirm one exists.

    The documented API has no such endpoint, so the response schema is unknown.
    The parser below accepts the shapes a book snapshot is normally returned in
    and refuses anything else loudly rather than silently reporting zero depth
    (a silent zero would look like "no liquidity" and quietly disable the bot).
    """

    name = "roostoo"

    def __init__(
        self,
        client: Any,
        path: str,
        transport: Optional[UrllibTransport] = None,
        timeout: float = 10.0,
    ) -> None:
        if not path:
            raise ValueError("RoostooDepthProvider requires a path (ROOSTOO_DEPTH_PATH)")
        self.client = client
        self.path = path
        self.timeout = timeout
        self._transport = transport or UrllibTransport()
        self._warned = False

    def snapshot(self, pair: str, band_pct: float) -> Optional[DepthSnapshot]:
        signer = getattr(self.client, "sign_headers", None)
        params: dict[str, Any] = {"pair": pair}
        if callable(signer) and hasattr(self.client, "timestamp_ms"):
            params["timestamp"] = self.client.timestamp_ms()
        query = "&".join(f"{k}={params[k]}" for k in sorted(params))
        headers = {"Accept": "application/json"}
        if callable(signer):
            # Sign the exact query string that goes on the wire.
            headers.update(signer(params, canonical=query))
        url = f"{self.client.base_url}{self.path}?{query}"
        try:
            status, text = self._transport.send("GET", url, None, headers, self.timeout)
            payload = json.loads(text) if status == 200 else {}
        except Exception as exc:
            log.warning("roostoo depth for %s failed: %s", pair, exc)
            return None
        parsed = _parse_book(payload, pair, band_pct)
        if parsed is None and not self._warned:
            self._warned = True
            log.error(
                "could not parse depth from %s; payload keys=%s. Depth checking is "
                "effectively disabled -- confirm the schema with the organisers.",
                self.path,
                sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
            )
        return parsed


def _parse_book(payload: Any, pair: str, band_pct: float) -> Optional[DepthSnapshot]:
    """Tolerant book parser: handles a few plausible snapshot shapes."""
    if not isinstance(payload, dict):
        return None
    node: Any = payload
    for key in ("Data", "data", "Depth", "depth", "Book", "book"):
        if isinstance(node.get(key), dict):
            node = node[key]
            break
    bids = node.get("Bids") or node.get("bids") or node.get("B") or []
    asks = node.get("Asks") or node.get("asks") or node.get("A") or []
    if not bids or not asks:
        return None

    def norm(level: Any) -> Optional[tuple[float, float]]:
        if isinstance(level, dict):
            price = level.get("Price", level.get("price"))
            qty = level.get("Quantity", level.get("quantity", level.get("Qty", level.get("qty"))))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, qty = level[0], level[1]
        else:
            return None
        try:
            return float(price), float(qty)
        except (TypeError, ValueError):
            return None

    norm_bids = [x for x in (norm(b) for b in bids) if x is not None]
    # `x`, not `a`: the inner generator's loop variable is not in scope in the
    # outer condition, so `if a is not None` raised NameError for every payload
    # that had an ask side -- i.e. always. This parser had therefore never once
    # returned a snapshot.
    norm_asks = [x for x in (norm(a) for a in asks) if x is not None]
    if not norm_bids or not norm_asks:
        return None
    mid = (norm_bids[0][0] + norm_asks[0][0]) / 2.0
    floor = mid * (1.0 - band_pct)
    ceiling = mid * (1.0 + band_pct)
    return DepthSnapshot(
        pair=pair,
        mid=mid,
        bid_notional=sum(p * q for p, q in norm_bids if p >= floor),
        ask_notional=sum(p * q for p, q in norm_asks if p <= ceiling),
        source="roostoo",
    )


def build_depth_provider(cfg: Config, tickers: dict[str, Ticker], client: Any = None) -> DepthProvider:
    """Factory driven by ``DEPTH_PROVIDER``."""
    kind = (cfg.depth_provider or "none").lower()
    if kind == "ticker":
        return TickerLiquidityProxy(tickers, cfg.bars_per_day(), cfg.depth_liquidity_share)
    if kind == "binance":
        return BinanceDepthProvider(timeout=cfg.request_timeout_sec)
    if kind == "roostoo":
        if client is None:
            raise ValueError("DEPTH_PROVIDER=roostoo requires a live client instance")
        return RoostooDepthProvider(client, cfg.depth_path, timeout=cfg.request_timeout_sec)
    return NullDepthProvider()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


@dataclass
class UniverseSelection:
    """Outcome of one selection pass, with the audit trail for the journal."""

    selected: list[str] = field(default_factory=list)
    ranked: list[tuple[str, float]] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    depth: dict[str, DepthSnapshot] = field(default_factory=dict)
    depth_checked: bool = False
    depth_available: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": list(self.selected),
            "ranked": [[p, round(v, 2)] for p, v in self.ranked],
            "rejected": dict(self.rejected),
            "depth_checked": self.depth_checked,
            "depth_available": self.depth_available,
            "depth": {
                p: {"mid": s.mid, "bid": round(s.bid_notional, 2), "ask": round(s.ask_notional, 2), "src": s.source}
                for p, s in self.depth.items()
            },
            "notes": list(self.notes),
        }


class UniverseSelector:
    """Applies Rule 1 on every rebalance.

    Two details that matter more than they look:

    **Hysteresis.** Ranking by turnover changes every bar near the 8th slot. A
    pair flickering in and out would be bought and sold repeatedly, paying the
    half-spread and 0.1% commission each time. A pair already in the universe is
    therefore retained while it stays inside the top ``max_pairs * rank_buffer``.

    **Fail-closed depth.** If a depth provider is configured but returns nothing,
    the pair is excluded rather than assumed liquid. Silent degradation to "no
    liquidity data" is how a bot ends up trading an illiquid book.
    """

    def __init__(
        self,
        cfg: Config,
        depth_provider: Optional[DepthProvider] = None,
        rank_buffer: float = 2.0,
    ) -> None:
        self.cfg = cfg
        self.depth_provider = depth_provider or NullDepthProvider()
        self.rank_buffer = max(rank_buffer, 1.0)

    def select(
        self,
        tickers: dict[str, Ticker],
        required_notional: Optional[float] = None,
        can_trade: Optional[Iterable[str]] = None,
        previous: Optional[Iterable[str]] = None,
    ) -> UniverseSelection:
        out = UniverseSelection()
        required = float(self.cfg.depth_target_notional if required_notional is None else required_notional)
        allowed = {p.upper() for p in can_trade} if can_trade is not None else None
        held = {p for p in (previous or [])}

        # 1. quoted, sane books only.
        quoted: list[Ticker] = []
        for pair, ticker in tickers.items():
            if allowed is not None and pair not in allowed:
                out.rejected[pair] = "not tradable on venue"
                continue
            if ticker.last <= 0 or ticker.max_bid <= 0 or ticker.min_ask <= 0:
                out.rejected[pair] = "no two-sided quote"
                continue
            quoted.append(ticker)

        # 2. rank by 24h turnover (Rule 1, clause 1).
        quoted.sort(key=lambda t: t.unit_volume, reverse=True)
        out.ranked = [(t.pair, t.unit_volume) for t in quoted]

        depth_checked = not isinstance(self.depth_provider, NullDepthProvider)
        out.depth_checked = depth_checked
        if depth_checked:
            out.depth_available = self.depth_provider.name != "none"

        cutoff = self.cfg.max_pairs
        sticky_cutoff = max(cutoff, int(round(cutoff * self.rank_buffer)))

        for rank, ticker in enumerate(quoted):
            pair = ticker.pair
            inside = rank < cutoff
            inside_sticky = rank < sticky_cutoff and pair in held
            if not inside and not inside_sticky:
                out.rejected[pair] = f"rank {rank + 1} outside top {cutoff} by 24h turnover"
                continue

            # 3. spread ceiling (Rule 1, clause 2): 0.1% == 10 bps.
            spread = ticker.spread_bps
            if spread > self.cfg.max_spread_bps:
                out.rejected[pair] = f"spread {spread:.2f}bps > {self.cfg.max_spread_bps:.2f}bps"
                continue

            # 4. depth within the band (Rule 1, clause 3).
            if depth_checked:
                snapshot = self.depth_provider.snapshot(pair, self.cfg.depth_band_pct)
                if snapshot is None:
                    out.rejected[pair] = "depth unavailable (fail-closed)"
                    continue
                out.depth[pair] = snapshot
                # The entry side is a BUY, so the ask side must absorb the order.
                available = snapshot.for_side("BUY")
                if available < required:
                    out.rejected[pair] = (
                        f"depth {available:,.0f} < target {required:,.0f} within "
                        f"+/-{self.cfg.depth_band_pct * 100:.2f}%"
                    )
                    continue

            out.selected.append(pair)

        if depth_checked and not out.selected:
            out.notes.append("depth filter excluded every candidate")
        return out

    def describe(self) -> str:
        return (
            f"Rule 1: top {self.cfg.max_pairs} by 24h turnover, spread <= "
            f"{self.cfg.max_spread_bps:.2f}bps, depth provider "
            f"'{self.depth_provider.name}'"
            + (
                f" (>= ${self.cfg.depth_target_notional:,.0f} within "
                f"+/-{self.cfg.depth_band_pct * 100:.2f}%)"
                if not isinstance(self.depth_provider, NullDepthProvider)
                else " (depth check disabled)"
            )
        )
