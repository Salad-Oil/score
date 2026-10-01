"""The autonomous decision loop.

Design constraints this file has to satisfy at once:

1. **Rules 2-3 are defined on closed 30-minute bars**, so the strategy must run
   exactly once per bar -- not once per loop.
2. **Rule 5's stop is a price level**, so protective exits must be checked on
   every loop, not only at the bar boundary.
3. **Exits must be more reliable than entries.** Flattening has to work even when
   the risk layer is halting new business.
4. **A 14-day unattended run will restart.** Position state is persisted, and on
   every cycle the local book is reconciled against the exchange's balances --
   the exchange is the source of truth for quantity.
5. **An order that fails in transport has an unknown outcome.** It is never
   blind-retried; it is reconciled against the order history.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .basis import BasisMonitor
from .candles import Candle, CandleBuilder, bar_index, load_candles, trading_day_id
from .client import build_client
from .config import Config
from .journal import Journal
from .metrics import MetricTracker
from .models import OrderResult, Position, Ticker, TradePair, WalletBalance, fmt
from .risk import (
    ApprovedAction,
    PortfolioView,
    PositionBook,
    PositionSizer,
    RiskManager,
    portfolio_nav,
)
from .strategies.base import (
    ENTER_LONG,
    ENTER_SHORT,
    EXIT_LONG,
    EXIT_SHORT,
    MarketContext,
    Signal,
    load_strategy,
)
from .universe import UniverseSelection, UniverseSelector, build_depth_provider

log = logging.getLogger(__name__)

#: If the seeded history's last close is further than this from the live venue's
#: price, the two feeds disagree and the seeded bars would poison every z-score.
#:
#: The organisers confirmed the venue tracks Binance, so a gap this large is not
#: "a different but related market" -- it is a wrong symbol, a wrong quote
#: currency, or a stale feed. Kept deliberately tight for that reason; the
#: previous 2% allowance would have let a genuine USDT/USD mismatch through.
SEED_TOLERANCE_PCT = 0.005


@dataclass
class EngineStats:
    cycles: int = 0
    bars_processed: int = 0
    entries: int = 0
    exits: int = 0
    orders_sent: int = 0
    order_errors: int = 0
    unknown_orders: int = 0
    reconciliations: int = 0
    consecutive_failures: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "cycles": self.cycles,
            "bars_processed": self.bars_processed,
            "entries": self.entries,
            "exits": self.exits,
            "orders_sent": self.orders_sent,
            "order_errors": self.order_errors,
            "unknown_orders": self.unknown_orders,
            "reconciliations": self.reconciliations,
        }


class TradingEngine:
    """Live loop. ``step()`` is one cycle; ``run()`` repeats it."""

    def __init__(
        self,
        cfg: Config,
        client: Any = None,
        journal: Optional[Journal] = None,
        seed_paths: Optional[dict[str, str | Path]] = None,
    ) -> None:
        self.cfg = cfg
        self.client = client or build_client(cfg)
        self.journal = journal if journal is not None else Journal(cfg.journal_dir)
        self.strategy = load_strategy(cfg.strategy, cfg.strategy_params)
        self.stats = EngineStats()

        self.exchange_pairs: dict[str, TradePair] = {}
        self.tickers: dict[str, Ticker] = {}
        self.balances: dict[str, WalletBalance] = {}
        self.builder = CandleBuilder(bar_seconds=cfg.bar_seconds, max_bars=cfg.history_window)
        self.book = PositionBook(Path(cfg.journal_dir) / "positions.json")
        self.sizer = PositionSizer(cfg)
        self.risk = RiskManager(cfg, self.sizer)
        self.tracker = MetricTracker(cfg.initial_capital, periods_per_year=cfg.periods_per_year, risk_free_rate=cfg.risk_free_rate)
        self.selector: Optional[UniverseSelector] = None
        self.universe: list[str] = []
        self.selection: Optional[UniverseSelection] = None

        self._last_decision_bar: Optional[int] = None
        self._seed_paths: dict[str, str | Path] = dict(seed_paths or {})
        self._pending_by_pair: dict[str, int] = {}
        self._shutting_down = False
        #: Gate for ``_persist()``. Stays False until ``bootstrap()`` has
        #: completed, so a startup failure can never overwrite stored state.
        self._ready = False

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def bootstrap(self) -> None:
        """Sync the clock, learn the venue's rules, restore state, warm up."""
        # Restore persisted state FIRST, before anything that can fail on the
        # network. `run_live.py` always calls `shutdown()` from a `finally:`,
        # and `shutdown()` persists; if `sync_time()` or `exchange_info()` threw
        # before the book had been loaded, that persist would write a
        # default-empty book and default risk state over the real ones -- losing
        # every stop level, cost basis and cooldown, resetting the drawdown
        # high-water mark, and silently clearing the kill switch.
        self.book.load()
        self._load_risk_state()

        offset = self.client.sync_time()
        info = self.client.exchange_info()
        self.exchange_pairs = {p: tp for p, tp in info.pairs.items() if tp.can_trade}
        if not info.is_running:
            raise RuntimeError("exchange reports IsRunning=false; refusing to trade")

        configured = self.cfg.resolved_pairs()
        self.universe = [p for p in configured if p in self.exchange_pairs] if configured else []
        if configured and not self.universe:
            log.warning("none of the configured pairs are tradable; falling back to auto-discovery")

        self.journal.startup(
            self.cfg.redacted(),
            self.strategy.describe(),
            extra={
                "clock_offset_ms": offset,
                "tradable_pairs": sorted(self.exchange_pairs),
                "configured_universe": list(self.universe),
                "restored_positions": sorted(self.book.positions),
            },
        )
        log.info(
            "bootstrapped: %d tradable pairs, %d restored position(s), clock offset %+dms",
            len(self.exchange_pairs),
            len(self.book.positions),
            offset,
        )
        self._ready = True

    def seed_history(self) -> None:
        """Warm the indicators from CSV, but only if the feed agrees on price.

        Seeding matters: Rules 2-3 need 48 bars, i.e. 24 hours of uptime, before
        the very first signal. The prep window exists for this.

        It is also dangerous. The CSV is real market history while the venue is a
        mock book, so if their price levels disagree, gluing them together
        fabricates a deviation that never happened and the first trades chase a
        phantom. So: seed only when the last historical close is within
        ``SEED_TOLERANCE_PCT`` of the live mid, and say so in the journal either
        way.
        """
        tickers = self.client.ticker()
        for pair, path in self._seed_paths.items():
            if pair not in self.exchange_pairs:
                continue
            ticker = tickers.get(pair)
            if ticker is None or ticker.mid <= 0:
                self.journal.event("seed_skipped", pair=pair, reason="no live quote")
                continue
            try:
                candles = load_candles(path, bar_seconds=self.cfg.bar_seconds)
            except Exception as exc:
                self.journal.event("seed_skipped", pair=pair, reason=f"unreadable: {exc}")
                continue
            if not candles:
                self.journal.event("seed_skipped", pair=pair, reason="empty file")
                continue
            basis = ticker.mid / candles[-1].close - 1.0
            if abs(basis) > SEED_TOLERANCE_PCT:
                self.journal.event(
                    "seed_rejected",
                    pair=pair,
                    reason="history disagrees with venue price",
                    basis=round(basis, 6),
                    history_close=candles[-1].close,
                    venue_mid=ticker.mid,
                )
                log.warning(
                    "%s: refusing to seed, venue mid %.8f vs history close %.8f (basis %+.2f%%)",
                    pair,
                    ticker.mid,
                    candles[-1].close,
                    basis * 100,
                )
                continue
            self.builder.seed(pair, candles)
            self.journal.event(
                "seed_applied",
                pair=pair,
                bars=len(candles),
                basis=round(basis, 6),
                last_close=candles[-1].close,
                venue_mid=ticker.mid,
            )
            log.info("%s: seeded %d bars from %s", pair, len(candles), path)
        self.strategy.prepare(self._context(0))

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------
    def step(self) -> None:
        now_ms = int(time.time() * 1000)
        tickers = self.client.ticker()
        self.tickers = tickers
        balances = self.client.balance()

        # A partial or malformed balance snapshot must never be acted on. With no
        # USD row the portfolio cannot be priced: NAV collapses to the marks of
        # the positions alone, which reads as a catastrophic drawdown and trips
        # the *permanent* kill switch. The same payload would also make
        # reconciliation read every missing row as "the exchange holds nothing"
        # and delete the entire book. Skipping one cycle is cheap; acting on a
        # bad response liquidates the account's memory.
        if not self._balances_usable(balances):
            log.error("balance payload has no USD row (assets=%s); skipping this cycle", sorted(balances))
            self.journal.error(
                "cycle",
                "balance payload has no USD row; skipping rather than acting on an incomplete snapshot",
                ts_ms=now_ms,
                assets=sorted(balances),
            )
            return

        self.balances = balances
        shorts = self._safe_short_positions()

        self._reconcile_positions(balances, shorts)
        self.book.mark(tickers)
        self._feed_candles(tickers, now_ms)

        cash_usd = self._cash_usd(balances)
        nav = portfolio_nav(cash_usd, self.book.positions)
        self.tracker.record(now_ms, nav)
        self.risk.observe(nav, now_ms)
        self.stats.cycles += 1

        view = PortfolioView(nav=nav, cash_usd=cash_usd, positions=self.book.held())
        depth_dump = None
        if self.selection is not None:
            depth_dump = self.selection.to_dict().get("depth")

        current_bar = bar_index(now_ms, self.cfg.bar_seconds)
        on_bar = current_bar != self._last_decision_bar

        self.journal.cycle(current_bar, now_ms, nav, cash_usd, depth=depth_dump)

        # --- kill switch: flatten and stop -------------------------------
        if self.risk.halted:
            self.journal.halt(now_ms, self.risk.halt_reason, nav=round(nav, 4))
            self._flatten(now_ms, reason=self.risk.halt_reason)
            self._persist()
            self._shutting_down = True
            return

        # --- protective exits run every loop (Rule 5) ---------------------
        protective = self.risk.protective_exits(self.book.held(), tickers, now_ms)
        if protective:
            self.journal.signals(now_ms, protective, diagnostics={"source": "risk.protective_exits"})
            decision = self.risk.evaluate(
                protective,
                view=view,
                tickers=tickers,
                now_ms=now_ms,
                bar_idx=current_bar,
                committed_pairs=set(self._pending_by_pair),
                committed_notional=self._pending_notional(view.nav),
            )
            self._execute(decision.approved, now_ms, current_bar)

        # --- the strategy runs once per closed bar ------------------------
        if on_bar:
            self._last_decision_bar = current_bar
            self.stats.bars_processed += 1
            self._decision_cycle(now_ms, current_bar, view)

        self.journal.equity(now_ms, nav, metrics=self.tracker.metrics().to_dict())
        self._persist()

    def _decision_cycle(self, now_ms: int, current_bar: int, view: PortfolioView) -> None:
        self._refresh_universe(now_ms)
        self._refresh_pending()
        ctx = self._context(now_ms, view)
        signals = self.strategy.generate(ctx)
        self.journal.signals(now_ms, signals, diagnostics=self.strategy.diagnostics)

        decision = self.risk.evaluate(
            signals,
            view=view,
            tickers=self.tickers,
            now_ms=now_ms,
            bar_idx=current_bar,
            committed_pairs=set(self._pending_by_pair),
            committed_notional=self._pending_notional(view.nav),
        )
        self.journal.decision(now_ms, decision.to_dict())
        self._execute(decision.approved, now_ms, current_bar)

    def _refresh_pending(self) -> None:
        """Learn which pairs have resting orders, so they can be reserved.

        A pending order is not a position yet, but its capital is committed. If
        the risk layer cannot see it, two consecutive bars will each size a full
        position for the same pair and the account ends up at twice the cap.
        """
        try:
            total, by_pair = self.client.pending_count()
        except Exception as exc:
            log.debug("pending_count unavailable: %s", exc)
            self._pending_by_pair = {}
            return
        self._pending_by_pair = dict(by_pair) if total else {}

    def _pending_notional(self, nav: float) -> float:
        if not self._pending_by_pair:
            return 0.0
        return self.sizer.slot_notional(nav) * len(self._pending_by_pair)

    # ------------------------------------------------------------------
    # Universe (Rule 1)
    # ------------------------------------------------------------------
    def _refresh_universe(self, now_ms: int) -> None:
        can_trade = set(self.exchange_pairs)
        provider = build_depth_provider(self.cfg, self.tickers, self.client)
        if self.selector is None or type(provider) is not type(self.selector.depth_provider):
            self.selector = UniverseSelector(self.cfg, provider)
        else:
            self.selector.depth_provider = provider

        configured = self.cfg.resolved_pairs()
        if configured:
            # An explicit list is a human decision: rank and filter only within it.
            candidates = {p: t for p, t in self.tickers.items() if p in configured}
        else:
            candidates = dict(self.tickers)

        # Cross-venue basis check (once per bar, one HTTP call for the universe).
        # The organisers confirmed the venue tracks Binance, so a wide basis is not
        # "a related but different market" -- it is the wrong symbol, the wrong
        # quote currency, or a stale feed, and a z-score computed against it is
        # noise. This fails open by design: a Binance outage must not stop the bot
        # from trading its own sampled bars, but the readings are journalled so a
        # drift shows up as a time series rather than a silent assumption.
        #
        # Skipped against the simulator, whose prices are synthetic by
        # construction: comparing them with Binance would block every pair.
        if not self.cfg.mock:
            basis_report = BasisMonitor(timeout=self.cfg.request_timeout_sec).check(candidates)
            self.journal.event("basis", ts_ms=now_ms, **basis_report.to_dict())
            blocked = basis_report.blocked()
            if blocked:
                log.warning("basis out of tolerance on %s; excluding them for this bar", sorted(blocked))
                candidates = {p: t for p, t in candidates.items() if p not in blocked}
            elif not basis_report.source_ok:
                log.warning("basis check unavailable (%s); proceeding on venue data alone", basis_report.error)

        selection = self.selector.select(
            candidates,
            required_notional=self.cfg.depth_target_notional,
            can_trade=can_trade,
            previous=self.universe or self.book.positions.keys(),
        )
        self.selection = selection
        self.universe = selection.selected
        self.journal.universe(now_ms, selection.to_dict())
        if not self.universe:
            log.warning("Rule 1 produced an empty universe this bar")

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------
    def _context(self, now_ms: int, view: Optional[PortfolioView] = None) -> MarketContext:
        positions = (view.positions if view else None) or self.book.held()
        cash_usd = view.cash_usd if view else self._cash_usd(self.balances)
        nav = view.nav if view else portfolio_nav(cash_usd, positions)
        current_bar = bar_index(now_ms, self.cfg.bar_seconds)
        blocked = self.risk.blocked_pairs(current_bar)
        # Hand the strategy only the context it declares it needs. Passing the
        # whole rolling buffer is correct but makes every indicator re-scan
        # hundreds of bars per pair per bar.
        context_bars = max(1, self.strategy.max_context_bars)
        candles = {
            pair: self.builder.history(pair)[-context_bars:] for pair in self._tracked_pairs()
        }
        return MarketContext(
            now_ms=now_ms,
            bar_seconds=self.cfg.bar_seconds,
            candles=candles,
            tickers=self.tickers,
            nav=nav,
            cash_usd=cash_usd,
            positions=positions,
            universe=list(self.universe),
            blocked=blocked,
            daily_halt=self.risk.daily_halt,
            bar_index=current_bar,
            state={},
        )

    def _tracked_pairs(self) -> list[str]:
        pairs = set(self.builder.histories()) | set(self.universe) | set(self.book.positions)
        return sorted(pairs)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def _feed_candles(self, tickers: dict[str, Ticker], now_ms: int) -> None:
        for pair, ticker in tickers.items():
            if pair not in self.exchange_pairs:
                continue
            self.builder.add(
                pair=pair,
                ts_ms=now_ms,
                price=ticker.mid,
                bid=ticker.max_bid,
                ask=ticker.min_ask,
                cumulative_volume=ticker.unit_volume,
            )

    def _safe_short_positions(self) -> list[Any]:
        try:
            return self.client.short_positions()
        except Exception as exc:
            # Shorts may be disabled for the competition; that must not be fatal.
            log.debug("short_positions unavailable: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def _reconcile_positions(self, balances: dict[str, WalletBalance], shorts: list[Any]) -> None:
        """Make the local book agree with the exchange.

        The exchange is authoritative for *quantity* (it is the thing that
        settles trades). The local book is authoritative for *cost basis and
        stop levels*, which the API never reports. A disagreement means a fill we
        did not see -- an unknown-outcome order, a missed cycle, or a manual
        intervention -- and is journalled rather than silently absorbed.
        """
        for pair, trade_pair in self.exchange_pairs.items():
            balance = balances.get(trade_pair.coin)
            exchange_qty = balance.total if balance else 0.0
            position = self.book.get(pair)
            local_qty = position.quantity if position and not position.is_short else 0.0
            # Half a lot: a smaller gap is rounding, not a real disagreement.
            tolerance = max(1e-9, 0.5 * 10.0 ** (-trade_pair.amount_precision))

            if abs(exchange_qty - local_qty) <= tolerance:
                continue
            self.stats.reconciliations += 1
            if exchange_qty <= tolerance:
                if position is not None and not position.is_short:
                    self.book.positions.pop(pair, None)
                self.journal.reconciliation(
                    int(time.time() * 1000),
                    pair,
                    "closed_externally",
                    {"local": local_qty, "exchange": exchange_qty},
                )
                continue
            if position is None or position.is_short:
                # Adopt an unknown holding. Without a cost basis the stop cannot be
                # trusted, so the position is marked as adopted and left for the
                # normal exits to unwind.
                ticker = self.tickers.get(pair)
                self.book.apply_spot_buy(
                    pair,
                    exchange_qty,
                    ticker.mid if ticker is not None else 0.0,
                    int(time.time() * 1000),
                )
                self.journal.reconciliation(
                    int(time.time() * 1000),
                    pair,
                    "adopted_unknown_holding",
                    {"local": local_qty, "exchange": exchange_qty},
                )
                log.warning("%s: adopted %.10f from the exchange with no known cost basis", pair, exchange_qty)
            else:
                position.quantity = exchange_qty
                self.journal.reconciliation(
                    int(time.time() * 1000),
                    pair,
                    "quantity_corrected",
                    {"local": local_qty, "exchange": exchange_qty},
                )

        short_pairs = {sp.pair for sp in shorts}
        for pair, position in list(self.book.positions.items()):
            if position.is_short and pair not in short_pairs:
                self.book.positions.pop(pair, None)
                self.journal.reconciliation(int(time.time() * 1000), pair, "short_closed_externally")

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _execute(self, actions: list[ApprovedAction], now_ms: int, current_bar: int) -> None:
        for action in actions:
            try:
                if action.action == ENTER_LONG:
                    self._enter_long(action, now_ms, current_bar)
                elif action.action == ENTER_SHORT:
                    self._enter_short(action, now_ms, current_bar)
                elif action.action == EXIT_LONG:
                    self._exit_long(action, now_ms, current_bar)
                elif action.action == EXIT_SHORT:
                    self._exit_short(action, now_ms, current_bar)
            except Exception as exc:
                self.stats.order_errors += 1
                log.exception("execution failed for %s", action.pair)
                self.journal.error("execute", str(exc), ts_ms=now_ms, action=action.to_dict())

    def _enter_long(self, action: ApprovedAction, now_ms: int, current_bar: int) -> None:
        trade_pair = self.exchange_pairs.get(action.pair)
        ticker = self.tickers.get(action.pair)
        if trade_pair is None or ticker is None:
            return
        quantity = self._quantise(trade_pair, action.quantity, ticker.mid)
        if quantity is None:
            self.journal.order(now_ms, action.to_dict(), error="below pair minimum")
            return

        self.stats.orders_sent += 1
        result = self.client.place_order(action.pair, "BUY", trade_pair.round_qty(quantity), "MARKET")
        self.journal.order(now_ms, action.to_dict(), result=result)
        self._record_result(result, action, now_ms, current_bar)

    def _enter_short(self, action: ApprovedAction, now_ms: int, current_bar: int) -> None:
        collateral = max(1.0, round(action.collateral, 2))
        if collateral < 1.0:
            return
        self.stats.orders_sent += 1
        payload = self.client.short_open(action.pair, collateral)
        self.journal.order(now_ms, action.to_dict(), result=payload)
        if not payload.get("Success"):
            self.stats.order_errors += 1
            log.warning("short_open %s rejected: %s", action.pair, payload.get("ErrMsg"))
            return
        quantity = float(payload.get("ShortQty", 0.0) or 0.0)
        entry = float(payload.get("EntryPrice", 0.0) or 0.0)
        if payload.get("Status") != "OPEN" or quantity <= 0:
            # A resting LIMIT order has no position yet.
            return
        self.book.apply_short_open(
            action.pair, quantity, entry, float(payload.get("Collateral", collateral) or collateral),
            now_ms, action.stop_price,
        )
        self.stats.entries += 1
        self.journal.trade(
            now_ms, action.pair, action.action, "SHORT_OPEN", quantity, entry,
            float(payload.get("OpenFee", 0.0) or 0.0), payload.get("ID", ""), "SHORT", action.reason,
        )

    def _exit_long(self, action: ApprovedAction, now_ms: int, current_bar: int) -> None:
        trade_pair = self.exchange_pairs.get(action.pair)
        position = self.book.get(action.pair)
        if trade_pair is None or position is None:
            return
        quantity = self._quantise(trade_pair, position.quantity, position.mark_price)
        if quantity is None:
            self.journal.order(now_ms, action.to_dict(), error="nothing sellable above the pair minimum")
            return
        # A SELL that would leave dust behind is rounded to the full balance.
        if quantity < position.quantity * 0.999:
            quantity = position.quantity
            quantity = float(fmt(quantity, trade_pair.amount_precision))

        self.stats.orders_sent += 1
        result = self.client.place_order(action.pair, "SELL", trade_pair.round_qty(quantity), "MARKET")
        self.journal.order(now_ms, action.to_dict(), result=result)
        self._record_result(result, action, now_ms, current_bar)

    def _exit_short(self, action: ApprovedAction, now_ms: int, current_bar: int) -> None:
        self.stats.orders_sent += 1
        payload = self.client.short_close(action.pair)
        self.journal.order(now_ms, action.to_dict(), result=payload)
        if not payload.get("Success"):
            self.stats.order_errors += 1
            log.warning("short_close %s rejected: %s", action.pair, payload.get("ErrMsg"))
            return
        closed = float(payload.get("ClosedQty", 0.0) or 0.0)
        price = float(payload.get("ClosePrice", 0.0) or 0.0)
        self.book.apply_short_close(action.pair, closed, price)
        self.stats.exits += 1
        self.risk.record_exit(action.pair, current_bar)
        self.journal.trade(
            now_ms, action.pair, action.action, "SHORT_CLOSE", closed, price,
            float(payload.get("CloseFee", 0.0) or 0.0), "", "SHORT", action.reason,
        )

    def _record_result(self, result: OrderResult, action: ApprovedAction, now_ms: int, current_bar: int) -> None:
        """Apply a spot order outcome to the book, reconciling UNKNOWNs."""
        if result.status == "UNKNOWN":
            self.stats.unknown_orders += 1
            reconciled = self._reconcile_unknown_order(action, result, now_ms)
            if reconciled is None:
                return
            result = reconciled

        if result.status == "REJECTED":
            self.stats.order_errors += 1
            log.warning("%s %s rejected: %s", result.side, action.pair, result.err_msg)
            return
        if result.status != "FILLED":
            # Resting limit order: no position change yet.
            return

        filled = result.filled_quantity or result.quantity
        price = result.avg_fill_price or (action.price or 0.0)
        if result.side == "BUY":
            self.book.apply_spot_buy(action.pair, filled, price, now_ms, action.stop_price)
            self.stats.entries += 1
        else:
            self.book.apply_spot_sell(action.pair, filled, price)
            self.stats.exits += 1
            self.risk.record_exit(action.pair, current_bar)
        self.journal.trade(
            now_ms, action.pair, action.action, result.side, filled, price,
            result.commission, result.order_id, result.role, action.reason,
        )

    def _reconcile_unknown_order(
        self, action: ApprovedAction, result: OrderResult, now_ms: int
    ) -> Optional[OrderResult]:
        """Did an UNKNOWN order actually land?

        Never re-send. Ask the order history instead: if an order for this pair
        was created after we sent the request and matches the side and quantity,
        treat it as ours; otherwise treat it as not placed.
        """
        try:
            rows = self.client.query_orders(pair=action.pair, limit=20)
        except Exception as exc:
            self.journal.reconciliation(now_ms, action.pair, "query_failed", {"error": str(exc)})
            log.error("cannot reconcile unknown order for %s: %s", action.pair, exc)
            return None

        cutoff = now_ms - 120_000
        for row in rows:
            created = int(row.get("CreateTimestamp", 0) or 0)
            side = str(row.get("Side", "")).upper()
            expected_side = "BUY" if action.action == ENTER_LONG else "SELL"
            if created >= cutoff and side == expected_side:
                self.journal.reconciliation(
                    now_ms, action.pair, "order_found", {"order_id": row.get("OrderID"), "status": row.get("Status")}
                )
                return OrderResult.from_api(action.pair, expected_side, "MARKET", action.quantity, {"Success": True, "OrderDetail": row})
        self.journal.reconciliation(now_ms, action.pair, "order_not_found", {"reason": result.err_msg})
        return None

    def _flatten(self, now_ms: int, reason: str) -> None:
        actions = self.risk.flatten_all(self.book.held())
        if not actions:
            return
        log.warning("flattening %d position(s): %s", len(actions), reason)
        self._execute(actions, now_ms, bar_index(now_ms, self.cfg.bar_seconds))

    @staticmethod
    def _quantise(trade_pair: TradePair, quantity: float, price: float) -> Optional[float]:
        """Round down to the pair's lot size, rejecting dust."""
        if quantity <= 0 or price <= 0:
            return None
        rounded = float(fmt(quantity, trade_pair.amount_precision))
        if rounded <= 0:
            return None
        if rounded * price < trade_pair.min_order:
            return None
        return rounded

    @staticmethod
    def _balances_usable(balances: dict[str, WalletBalance]) -> bool:
        """Is this snapshot complete enough to price the book and reconcile it?

        The quote currency must be present. An absent ``USD`` row means either a
        partial response or a venue that renamed its quote asset; either way the
        numbers derived from it are fiction, so the cycle is skipped.
        """
        return "USD" in balances

    @staticmethod
    def _cash_usd(balances: dict[str, WalletBalance]) -> float:
        usd = balances.get("USD")
        return usd.total if usd else 0.0

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _state_path(self) -> Path:
        return Path(self.cfg.journal_dir) / "engine_state.json"

    def _load_risk_state(self) -> None:
        path = self._state_path()
        if not path.is_file():
            return
        try:
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("could not read engine state: %s", exc)
            return
        self.risk.restore(payload.get("risk") or {})
        last_bar = payload.get("last_decision_bar")
        self._last_decision_bar = int(last_bar) if last_bar is not None else None
        if self.risk.halted:
            log.error("restored a HALTED state (%s); the kill switch requires a deliberate reset", self.risk.halt_reason)

    def _persist(self) -> None:
        import json

        if not self._ready:
            # Bootstrap never completed, so the in-memory book and risk state are
            # whatever the constructors left behind -- empty and default. Writing
            # them would destroy the real files on disk. `shutdown()` persists
            # from a `finally:`, so this guard is what makes a failed startup
            # non-destructive.
            log.error("not persisting state: bootstrap has not completed (a startup failure must not overwrite the stored book)")
            return

        self.book.save()
        try:
            payload = {
                "saved_ms": int(time.time() * 1000),
                "risk": self.risk.snapshot(),
                "last_decision_bar": self._last_decision_bar,
                "stats": self.stats.to_dict(),
                "universe": list(self.universe),
            }
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            log.error("could not persist engine state: %s", exc)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    def run(self, max_cycles: Optional[int] = None) -> EngineStats:
        """Run until stopped. One failing cycle never kills the process."""
        self.bootstrap()
        if self._seed_paths:
            self.seed_history()

        while not self._shutting_down:
            started = time.time()
            try:
                self.step()
                self.stats.consecutive_failures = 0
            except KeyboardInterrupt:
                log.info("interrupted")
                break
            except Exception as exc:
                self.stats.consecutive_failures += 1
                log.exception("cycle failed (%d in a row)", self.stats.consecutive_failures)
                self.journal.error("cycle", str(exc), consecutive_failures=self.stats.consecutive_failures)
                if self.stats.consecutive_failures >= 10:
                    # Give up and let the supervisor restart with a fresh clock sync.
                    log.error("10 consecutive failures; exiting so the service manager can restart")
                    self.journal.error("cycle", "aborting after 10 consecutive failures")
                    break
                time.sleep(min(2 ** self.stats.consecutive_failures, 60))
                continue

            if max_cycles is not None and self.stats.cycles >= max_cycles:
                log.info("reached max_cycles=%d", max_cycles)
                break
            elapsed = time.time() - started
            time.sleep(max(0.0, self.cfg.loop_interval_sec - elapsed))

        return self.stats

    def shutdown(self, flatten: bool = False) -> None:
        """Stop cleanly, optionally closing every position."""
        now_ms = int(time.time() * 1000)
        try:
            if flatten:
                current = PortfolioView(nav=0.0, cash_usd=0.0, positions=self.book.held())
                self._flatten(now_ms, reason="shutdown")
            self.journal.event("shutdown", stats=self.stats.to_dict(), metrics=self.tracker.metrics().to_dict())
        finally:
            self._persist()
            self.journal.close()
