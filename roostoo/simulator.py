"""An in-process mock exchange that mirrors the Roostoo REST surface.

Why this exists: the team wants to build and tune the 12 strategy rules now,
but the competition keys are not issued yet. This simulator implements the same
methods, the same fee schedule and the same response shapes as the live API, so
the engine, the risk layer and the journal are exercised end-to-end offline.

Prices follow a seeded geometric random walk, so a given seed always replays the
same market -- that makes an integration test reproducible.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .client import PATH_PLACE_ORDER
from .config import Config
from .errors import APIError
from .models import (
    ExchangeInfo,
    OrderResult,
    ShortPosition,
    Ticker,
    TradePair,
    WalletBalance,
    fmt,
)

log = logging.getLogger(__name__)

SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass
class _Spec:
    """Static description of one simulated instrument."""

    pair: str
    coin: str
    price: float
    annual_vol: float
    annual_drift: float
    price_precision: int
    amount_precision: int
    spread_bps: float = 4.0
    min_order: float = 1.0


DEFAULT_UNIVERSE: tuple[_Spec, ...] = (
    _Spec("BTC/USD", "BTC", 95_000.0, 0.55, 0.10, 2, 6),
    _Spec("ETH/USD", "ETH", 3_400.0, 0.70, 0.08, 2, 5),
    _Spec("BNB/USD", "BNB", 610.0, 0.65, 0.05, 3, 3),
    _Spec("SOL/USD", "SOL", 185.0, 0.95, 0.05, 3, 3),
    _Spec("XRP/USD", "XRP", 0.62, 0.90, 0.00, 5, 2),
    _Spec("ADA/USD", "ADA", 0.45, 0.85, 0.00, 5, 2),
    _Spec("DOGE/USD", "DOGE", 0.12, 1.20, 0.00, 6, 2),
    _Spec("LINK/USD", "LINK", 15.5, 0.85, 0.02, 4, 3),
    _Spec("AVAX/USD", "AVAX", 31.0, 0.95, 0.02, 4, 3),
    _Spec("DOT/USD", "DOT", 6.1, 0.80, 0.00, 4, 3),
)

MIN_SHORT_COLLATERAL = 1.0
SHORT_FEE_RATE = 0.001  # the v6 short endpoints always charge 0.1%


@dataclass
class _PendingOrder:
    order_id: int
    pair: str
    side: str
    order_type: str
    price: float
    quantity: float
    created_ms: int
    locked_asset: str = ""
    locked_amount: float = 0.0


@dataclass
class _ShortBook:
    position_id: int
    pair: str
    entry_price: float
    quantity: float
    collateral: float
    created_ms: int


class MockRoostooClient:
    """Drop-in replacement for :class:`roostoo.client.RoostooClient`."""

    def __init__(
        self,
        config: Config,
        seed: int = 7,
        deterministic: bool = False,
        time_scale: float = 3600.0,
    ) -> None:
        self.cfg = config
        self.deterministic = deterministic
        self.time_scale = time_scale
        self._rng = random.Random(seed)
        # ``_Spec`` is mutable, so every instance gets its own copies. Sharing the
        # module-level objects would let one client's price walk -- or a test's
        # ``set_price`` -- leak into every other client in the process, silently
        # breaking the "a given seed always replays the same market" guarantee.
        self._specs: dict[str, _Spec] = {
            s.pair: _Spec(
                pair=s.pair,
                coin=s.coin,
                price=s.price,
                annual_vol=s.annual_vol,
                annual_drift=s.annual_drift,
                price_precision=s.price_precision,
                amount_precision=s.amount_precision,
                spread_bps=s.spread_bps,
                min_order=s.min_order,
            )
            for s in DEFAULT_UNIVERSE
        }
        self._last_step = time.time()
        self._order_seq = 1
        self._short_seq = 1
        self._orders: list[dict[str, Any]] = []
        self._pending: dict[int, _PendingOrder] = {}
        self._shorts: dict[str, _ShortBook] = {}
        self._wallet: dict[str, dict[str, float]] = {"USD": {"free": float(config.initial_capital), "lock": 0.0}}
        self._is_running = True
        for spec in self._specs.values():
            self._wallet.setdefault(spec.coin, {"free": 0.0, "lock": 0.0})

    # -- simulated market ----------------------------------------------
    def _step(self) -> None:
        """Advance every price by one random-walk step."""
        if self.deterministic:
            dt = self.time_scale
        else:
            now = time.time()
            dt = max(now - self._last_step, 1.0) * self.time_scale
            self._last_step = now
        dt = min(dt, SECONDS_PER_YEAR / 100.0)
        sqrt_dt = math.sqrt(dt / SECONDS_PER_YEAR)
        for spec in self._specs.values():
            shock = self._rng.gauss(0.0, 1.0)
            drift = (spec.annual_drift - 0.5 * spec.annual_vol**2) * (dt / SECONDS_PER_YEAR)
            spec.price = max(spec.price * math.exp(drift + spec.annual_vol * sqrt_dt * shock), 1e-8)
        self._settle_pending()

    def set_price(self, pair: str, price: float) -> None:
        """Test hook: pin a price exactly."""
        self._specs[pair].price = float(price)
        self._settle_pending()

    def _quote(self, spec: _Spec) -> tuple[float, float]:
        half = spec.price * spec.spread_bps / 2.0 / 10_000.0
        return (spec.price - half, spec.price + half)

    def _settle_pending(self) -> None:
        """Fill resting limit orders whose price the market has now reached."""
        for order_id in list(self._pending):
            order = self._pending[order_id]
            spec = self._specs[order.pair]
            bid, ask = self._quote(spec)
            crossed = (order.side == "BUY" and ask <= order.price) or (order.side == "SELL" and bid >= order.price)
            if not crossed:
                continue
            self._release_lock(order)
            del self._pending[order_id]
            self._execute(order.side, order.pair, order.quantity, order.price, "MAKER", order_id)

    # -- wallet helpers -------------------------------------------------
    def _free(self, asset: str) -> float:
        return self._wallet.setdefault(asset, {"free": 0.0, "lock": 0.0})["free"]

    def _lock(self, asset: str, amount: float) -> None:
        book = self._wallet.setdefault(asset, {"free": 0.0, "lock": 0.0})
        book["free"] -= amount
        book["lock"] += amount

    def _unlock(self, asset: str, amount: float) -> None:
        book = self._wallet.setdefault(asset, {"free": 0.0, "lock": 0.0})
        book["lock"] = max(book["lock"] - amount, 0.0)
        book["free"] += amount

    def _release_lock(self, order: _PendingOrder) -> None:
        if order.locked_asset and order.locked_amount:
            self._unlock(order.locked_asset, order.locked_amount)

    # -- client surface -------------------------------------------------
    def sync_time(self) -> int:
        return 0

    def exchange_info(self, retries: int = 1) -> ExchangeInfo:
        pairs = {
            s.pair: TradePair(
                pair=s.pair,
                coin=s.coin,
                unit="USD",
                can_trade=True,
                price_precision=s.price_precision,
                amount_precision=s.amount_precision,
                min_order=s.min_order,
            )
            for s in self._specs.values()
        }
        return ExchangeInfo(
            is_running=self._is_running,
            initial_wallet={"USD": float(self.cfg.initial_capital)},
            pairs=pairs,
        )

    def ticker(self, pair: Optional[str] = None, retries: int = 1) -> dict[str, Ticker]:
        self._step()
        ts = int(time.time() * 1000)
        selected = [self._specs[pair]] if pair and pair in self._specs else list(self._specs.values())
        out: dict[str, Ticker] = {}
        for spec in selected:
            bid, ask = self._quote(spec)
            out[spec.pair] = Ticker(
                pair=spec.pair,
                last=spec.price,
                max_bid=bid,
                min_ask=ask,
                change_24h=0.0,
                coin_volume=abs(self._rng.gauss(1_000_000, 200_000)),
                unit_volume=abs(self._rng.gauss(50_000_000, 5_000_000)),
                server_time_ms=ts,
            )
        return out

    def balance(self) -> dict[str, WalletBalance]:
        return {
            asset: WalletBalance(asset=asset, free=book["free"], locked=book["lock"])
            for asset, book in self._wallet.items()
        }

    def pending_count(self) -> tuple[int, dict[str, int]]:
        counts: dict[str, int] = {}
        for order in self._pending.values():
            counts[order.pair] = counts.get(order.pair, 0) + 1
        return len(self._pending), counts

    def place_order(
        self,
        pair: str,
        side: str,
        quantity: str | float,
        order_type: str = "MARKET",
        price: Optional[str | float] = None,
    ) -> OrderResult:
        side = side.upper()
        order_type = order_type.upper()
        qty = float(quantity)
        spec = self._specs.get(pair)
        order_id = self._order_seq
        self._order_seq += 1

        if spec is None:
            self._fail(pair, "pair not found")
        if qty <= 0:
            self._fail(pair, "quantity must be positive")
        if order_type == "LIMIT" and price is None:
            self._fail(pair, "limit order requires a price")
        # The venue refuses orders below the pair's MiniOrder. Not modelling it
        # meant the engine's long-path `_quantise` check was the only guard and
        # `--mock` happily accepted sizes the real venue rejects.
        if spec.min_order and qty * (float(price) if order_type == "LIMIT" and price else spec.price) < spec.min_order:
            self._fail(pair, "order value below MiniOrder")

        bid, ask = self._quote(spec)
        limit_price = float(price) if price is not None else 0.0
        if order_type == "MARKET":
            fill_price = ask if side == "BUY" else bid
            return self._immediate(spec, side, qty, fill_price, "TAKER", order_id)

        # LIMIT
        crossing = (side == "BUY" and ask <= limit_price) or (side == "SELL" and bid >= limit_price)
        if crossing:
            return self._immediate(spec, side, qty, limit_price, "TAKER", order_id)

        if side == "BUY":
            need = qty * limit_price
            if self._free("USD") < need:
                self._fail(pair, "insufficient balance")
            self._lock("USD", need)
            locked_asset, locked_amount = "USD", need
        else:
            if self._free(spec.coin) < qty:
                self._fail(pair, "insufficient balance")
            self._lock(spec.coin, qty)
            locked_asset, locked_amount = spec.coin, qty

        self._pending[order_id] = _PendingOrder(
            order_id=order_id,
            pair=pair,
            side=side,
            order_type="LIMIT",
            price=limit_price,
            quantity=qty,
            created_ms=int(time.time() * 1000),
            locked_asset=locked_asset,
            locked_amount=locked_amount,
        )
        detail = self._detail(
            spec,
            side,
            "LIMIT",
            qty,
            limit_price,
            filled=0.0,
            avg=0.0,
            status="PENDING",
            role="MAKER",
            order_id=order_id,
            commission=0.0,
        )
        self._orders.append(detail)
        return OrderResult.from_api(pair, side, "LIMIT", qty, {"Success": True, "ErrMsg": "", "OrderDetail": detail})

    def _immediate(
        self, spec: _Spec, side: str, qty: float, fill_price: float, role: str, order_id: int
    ) -> OrderResult:
        slip = spec.price * self.cfg.slippage_bps / 10_000.0
        fill_price = fill_price + slip if side == "BUY" else max(fill_price - slip, 0.0)
        ok, err = self._execute(side, spec.pair, qty, fill_price, role, order_id)
        if not ok:
            self._fail(spec.pair, err)
        return OrderResult.from_api(
            spec.pair, side, "MARKET", qty, {"Success": True, "ErrMsg": "", "OrderDetail": self._orders[-1]}
        )

    def _execute(self, side: str, pair: str, qty: float, price: float, role: str, order_id: int) -> tuple[bool, str]:
        spec = self._specs[pair]
        notional = qty * price
        fee_rate = self.cfg.maker_fee if role == "MAKER" else self.cfg.taker_fee
        fee = notional * fee_rate
        if side == "BUY":
            if self._free("USD") < notional + fee:
                return False, "insufficient balance"
            self._wallet["USD"]["free"] -= notional + fee
            self._wallet[spec.coin]["free"] += qty
            coin_change, unit_change = qty, -notional
        else:
            if self._free(spec.coin) < qty:
                return False, "insufficient balance"
            self._wallet[spec.coin]["free"] -= qty
            self._wallet["USD"]["free"] += notional - fee
            coin_change, unit_change = -qty, notional
        self._orders.append(
            self._detail(
                spec,
                side,
                "LIMIT" if role == "MAKER" else "MARKET",
                qty,
                price,
                filled=qty,
                avg=price,
                status="FILLED",
                role=role,
                order_id=order_id,
                commission=fee,
                coin_change=coin_change,
                unit_change=unit_change,
                fee_rate=fee_rate,
            )
        )
        return True, ""

    def _fail(self, pair: str, err: str) -> None:
        """Raise the same error the live client raises for a rejection.

        The live client turns `Success: false` into an `APIError`. The simulator
        used to *return* an `OrderResult(status="REJECTED")` instead, so the
        engine's `status == "REJECTED"` branch was exercised only under `--mock`
        and live rejections took a completely different path (caught by the broad
        handler in `_execute`). A mock that does not fail the way the venue fails
        is worse than no mock.
        """
        raise APIError(err, PATH_PLACE_ORDER, {"Success": False, "ErrMsg": err})

    @staticmethod
    def _detail(
        spec: _Spec,
        side: str,
        order_type: str,
        qty: float,
        price: float,
        filled: float,
        avg: float,
        status: str,
        role: str,
        order_id: int,
        commission: float,
        coin_change: float = 0.0,
        unit_change: float = 0.0,
        fee_rate: float = 0.0,
    ) -> dict[str, Any]:
        ts = int(time.time() * 1000)
        return {
            "Pair": spec.pair,
            "OrderID": order_id,
            "Status": status,
            "Role": role,
            "ServerTimeUsage": 0.001,
            "CreateTimestamp": ts,
            "FinishTimestamp": ts if status == "FILLED" else 0,
            "Side": side,
            "Type": order_type,
            "StopType": "GTC",
            "Price": price,
            "Quantity": qty,
            "FilledQuantity": filled,
            "FilledAverPrice": avg,
            "CoinChange": coin_change,
            "UnitChange": unit_change,
            "CommissionCoin": "USD",
            "CommissionChargeValue": commission,
            "CommissionPercent": fee_rate,
        }

    def query_orders(
        self,
        order_id: Optional[int | str] = None,
        pair: Optional[str] = None,
        pending_only: Optional[bool] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        rows = list(self._orders)
        if order_id is not None:
            return [r for r in rows if str(r.get("OrderID")) == str(order_id)]
        if pair:
            rows = [r for r in rows if r.get("Pair") == pair]
        if pending_only:
            rows = [r for r in rows if r.get("Status") == "PENDING"]
        rows.reverse()  # newest first, matching the live API
        if offset:
            rows = rows[int(offset) :]
        if limit:
            rows = rows[: int(limit)]
        return rows

    def cancel_order(self, order_id: Optional[int | str] = None, pair: Optional[str] = None) -> list[int]:
        targets: list[int] = []
        if order_id is not None:
            target = int(order_id)
            if target in self._pending:
                targets.append(target)
        else:
            for oid, order in self._pending.items():
                if pair is None or order.pair == pair:
                    targets.append(oid)
        for oid in targets:
            order = self._pending.pop(oid)
            self._release_lock(order)
            for row in reversed(self._orders):
                if row.get("OrderID") == oid and row.get("Status") == "PENDING":
                    row["Status"] = "CANCELED"
                    row["FinishTimestamp"] = int(time.time() * 1000)
                    break
        return targets

    # -- short side -----------------------------------------------------
    def short_open(self, pair: str, collateral: str | float, price: Optional[str | float] = None) -> dict[str, Any]:
        spec = self._specs.get(pair)
        coll = float(collateral)
        if spec is None:
            return {"Success": False, "ErrMsg": "pair not found"}
        if coll < MIN_SHORT_COLLATERAL:
            return {"Success": False, "ErrMsg": "minimum collateral is $1"}
        fee = coll * SHORT_FEE_RATE
        if self._free("USD") < coll + fee:
            return {"Success": False, "ErrMsg": "insufficient balance"}

        bid, ask = self._quote(spec)
        limit_price = float(price) if price is not None else None
        # Documented behaviour: a market short fills at the best bid and the fee
        # is charged when the request is accepted, even for a resting limit.
        entry = limit_price if limit_price is not None else bid
        qty = float(fmt(coll / entry, spec.amount_precision))

        self._lock("USD", coll)
        self._wallet["USD"]["free"] -= fee
        status = "PENDING" if limit_price is not None else "OPEN"

        if limit_price is not None:
            position_id = self._short_seq
            self._short_seq += 1
            return {
                "Success": True,
                "ID": position_id,
                "Pair": pair,
                "OrderType": "LIMIT",
                "EntryPrice": entry,
                "ShortQty": qty,
                "Collateral": coll,
                "OpenFee": fee,
                "Status": status,
                "CreateTimestamp": int(time.time() * 1000),
            }

        existing = self._shorts.get(pair)
        if existing:
            total_qty = existing.quantity + qty
            existing.entry_price = (existing.entry_price * existing.quantity + entry * qty) / total_qty
            existing.quantity = total_qty
            existing.collateral += coll
            position_id = existing.position_id
        else:
            position_id = self._short_seq
            self._short_seq += 1
            self._shorts[pair] = _ShortBook(position_id, pair, entry, qty, coll, int(time.time() * 1000))
        return {
            "Success": True,
            "ID": position_id,
            "Pair": pair,
            "OrderType": "MARKET",
            "EntryPrice": self._shorts[pair].entry_price,
            "ShortQty": self._shorts[pair].quantity,
            "Collateral": self._shorts[pair].collateral,
            "OpenFee": fee,
            "Status": "OPEN",
            "CreateTimestamp": int(time.time() * 1000),
        }

    def short_close(
        self, pair: str, close_qty: Optional[str | float] = None, close_pct: Optional[str | float] = None
    ) -> dict[str, Any]:
        book = self._shorts.get(pair)
        spec = self._specs.get(pair)
        if book is None or spec is None:
            return {"Success": False, "ErrMsg": "no open short position for this pair"}
        if close_qty is not None:
            qty = min(float(close_qty), book.quantity)
        elif close_pct is not None:
            qty = book.quantity * min(max(float(close_pct), 0.0), 100.0) / 100.0
        else:
            qty = book.quantity
        if qty <= 0:
            return {"Success": False, "ErrMsg": "close quantity must be positive"}

        close_price = self._quote(spec)[1]  # a close always fills at the best ask
        collateral_part = book.collateral * (qty / book.quantity) if book.quantity > 0 else 0.0
        realized = qty * (book.entry_price - close_price)
        realized = max(realized, -collateral_part)  # a short cannot lose more than its collateral
        fee = qty * close_price * SHORT_FEE_RATE
        return_amount = collateral_part + realized - fee

        self._unlock("USD", collateral_part)
        self._wallet["USD"]["free"] += realized - fee
        book.quantity -= qty
        book.collateral -= collateral_part
        fully = book.quantity <= 0 or fmt(book.quantity, spec.amount_precision) == fmt(0.0, spec.amount_precision)
        if fully:
            self._shorts.pop(pair, None)
        out: dict[str, Any] = {
            "Success": True,
            "ClosePrice": close_price,
            "RealizedPNL": realized,
            "CloseFee": fee,
            "ReturnAmount": return_amount,
            "ClosedQty": qty,
            "FullyClosed": fully,
        }
        if not fully:
            out["RemainingQty"] = book.quantity
            out["RemainingCollateral"] = book.collateral
        return out

    def short_positions(self) -> list[ShortPosition]:
        self._step()
        out: list[ShortPosition] = []
        for pair, book in self._shorts.items():
            spec = self._specs[pair]
            close_price = self._quote(spec)[1]
            unreal = book.quantity * (book.entry_price - close_price)
            out.append(
                ShortPosition(
                    position_id=book.position_id,
                    pair=pair,
                    entry_price=book.entry_price,
                    quantity=book.quantity,
                    collateral=book.collateral,
                    current_price=close_price,
                    unrealized_pnl=unreal,
                    unrealized_pct=(unreal / book.collateral) if book.collateral else 0.0,
                    position_value=book.collateral + unreal,
                    created_ts_ms=book.created_ms,
                )
            )
        return out

    # -- test conveniences ---------------------------------------------
    def equity(self) -> float:
        """Mark-to-market account value, matching `risk.portfolio_nav`.

        Two corrections over the previous formula. The locked USD is *included*
        (short collateral stays on the books at face value while its P&L floats),
        and the coin rows are valued as holdings which is what they are. The old
        version started from `free` USD only -- excluding the locked collateral --
        and then added that same collateral back, so a posted short collateral was
        counted twice and equity read roughly 10% high.
        """
        self._step()
        total = self._free("USD") + self._lock_balance("USD")
        for spec in self._specs.values():
            book = self._wallet[spec.coin]
            total += (book["free"] + book["lock"]) * spec.price
        total += sum(p.unrealized_pnl for p in self.short_positions())
        return total

    def _lock_balance(self, asset: str) -> float:
        return self._wallet[asset]["lock"]
