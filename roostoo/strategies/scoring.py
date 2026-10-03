"""Pure scoring and execution economics for the long-only scored strategy."""

from __future__ import annotations

import math
from typing import Any, Optional

from ..config import Config

SCORING_VERSION = "mean_reversion_score_v1"


def factor_scores(
    *, z: float, delta_z: float, adx: float, atr: float,
    open_price: float, high: float, low: float, close: float,
    sma_change: float, shock: float, expansion: float,
    vwap: Optional[float] = None, relative_volume: Optional[float] = None,
) -> dict[str, int]:
    """Each bucket is exclusive; caps sum to 100 with economics."""
    deviation = (
        8 if -2 < z <= -1.5 else
        18 if -2.5 < z <= -2 else
        25 if -3 <= z <= -2.5 else
        12 if -4 <= z < -3 else 0
    )
    if vwap is not None and math.isfinite(vwap) and atr > 0:
        if 0.5 <= (vwap - close) / atr <= 1.5:
            deviation += 5
    reversal = 5 if 0 < delta_z < 0.15 else 10 if 0.15 <= delta_z < 0.35 else 15 if delta_z >= 0.35 else 0
    body = abs(close - open_price)
    if close > open_price and high > low and (close - low) / (high - low) >= 0.65:
        reversal += 5
    if body >= 0.1 * atr and min(open_price, close) - low >= 1.5 * body:
        reversal += 5
    regime = 15 if adx < 18 else 10 if adx < 23 else 5 if adx < 27 else 0
    if abs(sma_change) <= 0.5 * atr:
        regime += 5
    safety = 5 if abs(shock) < 1.5 else 3 if abs(shock) < 2.5 else 0
    safety += 5 if expansion < 1.3 else 3 if expansion < 1.8 else 0
    if relative_volume is not None and math.isfinite(relative_volume) and close > open_price:
        safety += 5 if 1.2 <= relative_volume < 3 else 2 if 0.8 <= relative_volume < 1.2 else 0
    return {"deviation": deviation, "reversal": reversal, "regime": regime, "safety": safety}


def economics(target: float, reference: float, distance: float, cost: float) -> dict[str, float]:
    """Use mid as reference; cost includes spread, fees and both slippages."""
    if not all(math.isfinite(x) for x in (target, reference, distance, cost)):
        raise ValueError("non-finite economics")
    if reference <= 0 or distance <= 0 or cost <= 0 or distance >= reference:
        raise ValueError("invalid economics")
    gain = target / reference - 1.0
    coverage = gain / cost
    rr = (gain - cost) / (distance / reference + cost)
    points = 10 if coverage >= 5 else 7 if coverage >= 4 else 4 if coverage >= 3 else 0
    return {"gain_pct": gain, "cost_pct": cost, "cost_coverage": coverage, "net_rr": rr, "cost_score": points}


def risk_fraction(score: float) -> float:
    return 0.005 if score >= 90 else 0.0035 if score >= 80 else 0.002


def execution_terms(
    cfg: Config, meta: dict[str, Any], *, reference: float,
    spread_bps: float, stop_price: Optional[float], now_ms: int,
) -> tuple[Optional[str], dict[str, float]]:
    """Recheck score, current economics, signal age and actual stop."""
    try:
        if meta.get("scoring_version") != SCORING_VERSION:
            return "unsupported scoring version", {}
        bar_close = float(meta["signal_close_ms"])
        age = now_ms - bar_close
        if not math.isfinite(bar_close) or age < 0 or age >= cfg.bar_seconds * 1000:
            return "scored signal expired or from the future", {}
        if stop_price is None or not math.isfinite(stop_price) or stop_price <= 0:
            return "scored entry needs a valid stop", {}
        if not math.isfinite(spread_bps) or spread_bps < 0:
            return "invalid scored spread", {}
        cost = 2 * cfg.taker_fee + 2 * cfg.slippage_bps / 10_000 + spread_bps / 10_000
        result = economics(float(meta["target_price"]), reference, reference - stop_price, cost)
        threshold = float(meta["score_threshold"])
        coverage_min = float(meta["min_cost_coverage"])
        rr_min = float(meta["min_net_rr"])
        score = float(meta["score_without_cost"]) + result["cost_score"]
        if not all(math.isfinite(x) for x in (threshold, coverage_min, rr_min, score)):
            return "invalid scored metadata", {}
        result["entry_score"] = score
        result["risk_per_trade_pct"] = min(cfg.risk_per_trade_pct, risk_fraction(score))
        if result["cost_coverage"] < coverage_min:
            return "scored target does not cover costs", result
        if result["net_rr"] < rr_min:
            return "scored net reward/risk too low", result
        if score < threshold:
            return "scored entry below threshold after repricing", result
        return None, result
    except (KeyError, TypeError, ValueError, OverflowError):
        return "invalid scored execution inputs", {}
