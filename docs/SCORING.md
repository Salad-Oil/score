# Scored mean-reversion entries, version 1

Opt-in long-only strategy using the existing Strategy/Signal interfaces,
universe, portfolio controls, journal and mean exits. The original strategy
and default selection remain available. Weights are uncalibrated hypotheses.

## Enable

Replace BOTH strategy lines in your local uncommitted `.env`:

```dotenv
STRATEGY=roostoo.strategies.scored_mean_reversion:ScoredMeanReversionStrategy
STRATEGY_PARAMS={"score_threshold":70,"min_deviation_score":18,"min_reversal_score":15,"volume_factors_enabled":false}
```

Do not retain old entry parameters (z_entry, allow_short, require_turn,
enforce_min_deviation or supplementary-filter switches). They are rejected.
Both runners call optional configure_execution(cfg) to share actual costs and
stops. Direct callers must do so too. HISTORY_WINDOW must be >=125 by default.
An ATR or percentage stop is required; no-stop configurations are rejected.

## Rules

Require 120 valid contiguous closed bars, valid quotes within MAX_SPREAD_BPS,
live quote age <=two polling intervals, -4<=current Z<=-1.5, ADX<30, positive
delta Z AND rising close, absolute shock<4, ATR expansion<2.5 and valid stop.
This intentionally uses current Z after the turn, unlike the old previous-Z
trigger. Entry needs total>=70, deviation>=18 and reversal>=15.

| Category | Exclusive buckets | Points |
|---|---|---|
| Deviation (30) | Z: (-2,-1.5] / (-2.5,-2] / [-3,-2.5] / [-4,-3) | 8 / 18 / 25 / 12 |
| Deviation | (VWAP24-close)/ATR14 in [0.5,1.5] | +5 |
| Reversal (25) | delta Z: (0,.15) / [.15,.35) / >=.35 | 5 / 10 / 15 |
| Reversal | bullish candle, close in upper 35% | +5 |
| Reversal | lower wick>=1.5*body AND body>=.1*ATR | +5 |
| Regime (20) | ADX: <18 / [18,23) / [23,27) / [27,30) | 15 / 10 / 5 / 0 |
| Regime | absolute 4-bar SMA48 change<=.5*ATR | +5 |
| Safety/volume (15) | absolute shock: <1.5 / [1.5,2.5) / [2.5,4) | 5 / 3 / 0 |
| Safety/volume | expansion: <1.3 / [1.3,1.8) / [1.8,2.5) | +5 / +3 / +0 |
| Safety/volume | bullish AND relative volume: [1.2,3) / [.8,1.2) / other | +5 / +2 / +0 |
| Economics (10) | target distance/cost: >=5 / [4,5) / [3,4) | 10 / 7 / 4 |

Missing optional data earn zero without redistributing points. Volume factors
default OFF for both live and backtest (maximum 90/100). Only enable with
reliable volume: rolling 24-hour turnover differences are not true bar volume.
Default deviation floor also prevents Z below -3 or above -2 qualifying,
because those buckets cannot reach 18 even with the VWAP bonus.

Z uses sample standard deviation. Shock is current log return divided by
PREVIOUS 48 log-return standard deviation, excluding current. Expansion is
current Wilder ATR14 / mean of PREVIOUS 48 Wilder ATR14 readings. These differ
intentionally from legacy simple-return shock and high-low-range expansion.
VWAP uses 24 typical-price bars; relative volume uses PREVIOUS 20 volume bars.

## Economics and execution

Entry-time estimated target=SMA48+z_exit_long*Std48. Actual inherited mean exit
recalculates Z, so this is not a fixed profit order or expected profit.
Use MID P: G=target/P-1; C=2*TAKER_FEE+2*SLIPPAGE_BPS/10000+spread_bps/10000;
L=(P-stop)/P. Require G/C>=3 and (G-C)/(L+C)>=1.2.
One full spread estimates round-trip spread; using ask as P would double-count.
The existing backtest fill model charges fees/slippage but does not separately
charge this assumed spread. This PR preserves that convention and uses the
spread conservatively in the entry gate.

Risk tiers: score<80 =>.20% NAV; [80,90)=>.35%; >=90=>.50%, always capped by
RISK_PER_TRADE_PCT. Cap notional by NAV*risk/(L+C), then existing pair/gross caps.
Planned risk includes estimated costs; actual slippage/gaps can exceed it.

Sort entries by score, net reward/risk, lower cost, then pair name. Exits first.
A lower score after entry does not itself trigger an exit. Versioned signal
metadata includes a deterministic audit ID; existing once-per-bar decisions
and pending-order reservations still provide duplicate protection.

RiskManager rechecks current price, Config costs, stop, score and age. Expire
one full bar after source-bar CLOSE; normal next-open fill has age zero.
Backtester checks again at next open and can cancel or shrink, never enlarge.
It uses only the new open and original target/stop, not future OHLC/indicators.
Existing basis/depth behavior is preserved, including fail-open basis checks
on provider outages. Live sampled OHLC still differs from true backtest OHLC.

## Verify

```bash
python -m unittest discover -s tests -t .
python -m compileall -q roostoo tests run_live.py run_backtest.py run_sweep.py scripts
python scripts/check_encoding.py
python scripts/scan_secrets.py
python run_live.py --mock --cycles 2 --loop-interval 5 --no-seed --strategy roostoo.strategies.scored_mean_reversion:ScoredMeanReversionStrategy
python run_backtest.py --env .env.scored --oos-frac 0.25 --journal --out-dir reports/scored
```

Compare using separate baseline/scored env files and journal directories; fix
the same costs, stops, holding time, universe and volume capability. Use a
long-only baseline to isolate scoring. Process environment overrides dotenv.
The old edge_analysis.py hardcodes old entry rules and DOES NOT test scoring.
Use the actual Backtester, chronological holdouts and score-bin net results.

Verification: 444 tests including 26 new cases passed on the tested checkout.
The committed ETH sample (1200 bars) produced ZERO trades with default scoring;
this confirms a smoke run, not profitability. Inspect rejection diagnostics
before lowering thresholds. Check activity days as well as risk-adjusted returns.
Journal includes raw factors, category scores, economics, source score, repriced
score, risk fraction and rejection reasons. Score is not a win probability.
