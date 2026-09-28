# Findings — what the backtest actually says

Everything below comes from real Binance 30-minute candles over **120 days**
(2026-06-28 → 2026-09-28), 8 majors (BTC, ETH, SOL, BNB, XRP, DOGE, LINK,
AVAX), with the competition's cost model: **0.1% taker per side + 5 bps
slippage**, market orders filled at the **open of the next bar**.

Reproduce:

```bash
python scripts/fetch_history.py --days 120 --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT
python run_backtest.py --oos-frac 0.25
python scripts/analyze_backtest.py reports/trades_in-sample.csv
python run_sweep.py --config-grid "@reports/grid_stop.json"
python run_sweep.py --grid "@reports/grid_direction.json" --config-grid "@reports/grid_exit.json"
```

> **Configuration note.** Sections 1–3 were produced with **Rule 4's deviation
> gate enabled** (`enforce_min_deviation: true`), which is how those tables were
> obtained. That gate belongs to another owner, so it now ships **disabled** and
> the shipped default is worse on its own: in-sample −18.6% rather than −14.3%
> (section 4 has the full comparison, and it is the same conclusion either way).
> To reproduce these exact figures, add `"enforce_min_deviation": true` to
> `STRATEGY_PARAMS`, or pass `--params '{"enforce_min_deviation": true}'`.
> On Windows/PowerShell, inline JSON is mangled by the shell — use the `@file`
> form instead.

**Headline: as specified, Rules 1–3 lose money — and the entry logic is not the
reason.** Two configuration choices cause almost all of the damage, and both are
owned by teammates (Rules 5 and the short leg of Rule 2).

## 1. The default configuration loses

| | in-sample (90d) | out-of-sample (30d) |
|---|---|---|
| total return | −14.3% | −12.1% |
| Sharpe | −8.6 | −10.0 |
| Sortino | −8.9 | −10.0 |
| Calmar | −3.3 | −6.5 |
| composite | −7.1 | −8.9 |
| round trips | 338 | 149 |
| **fees paid** | **9,202** | **4,178** |

Fees alone are 9.2% of NAV. But gross P&L is negative too (−5,121 in-sample), so
cost is not the only problem.

## 2. The Rule 5 stop is where the money goes

`scripts/analyze_backtest.py` splits P&L by exit reason, and the picture is
unambiguous:

| exit | n | gross P&L | avg bars held | win rate |
|---|---|---|---|---|
| z-exit (Rule 2) | 89 | **+12,781** | 6.2 | **100%** |
| time stop (Rule 6) | 90 | +3,214 | 12.0 | 47.8% |
| **stop (Rule 5)** | 159 | **−21,116** | 3.8 | **0%** |

Every single trade that reached the mean-reversion target won. The ATR stop —
hit after an average of **3.8 bars** — converted 159 of them into realised losses
worth more than everything else earned combined.

Widening it improves the score monotonically, which is the signature of a stop
that sits inside the noise:

| `stop_atr_mult` | IS composite | OOS composite | IS return |
|---|---|---|---|
| `null` (disabled) | **−4.85** | **−7.86** | −11% |
| 5.0 | −5.02 | −8.77 | −10% |
| 3.0 | −6.08 | −8.91 | −14% |
| 1.5 (default) | **−7.13** | **−8.94** | −14% |

### Why Rules 2 and 5 are in direct conflict

This is structural, not a tuning accident. Rule 2 deliberately buys an asset that
has just moved hard *against* it (`Z ≤ −2`). Rule 5 then exits if the adverse move
exceeds `1.5 × ATR(14)` — and ATR is itself elevated *because of the very move
that triggered the entry*. The entry signal and the stop trigger are close
together by construction, so the stop fires while the deviation is still
widening, before there has been any time to revert.

A coherent alternative is to stop on the **same signal that generated the
entry**: exit if `Z` extends beyond, say, `−3.5` (the deviation failed to
revert and is now more extreme), rather than on an unrelated ATR multiple. That
keeps one author for the entry and the exit condition.

## 3. The short leg is the single biggest destroyer

Sweeping direction together with the time stop (stop disabled, to isolate them):

| shorts | time stop | IS composite | OOS composite | **IS return** | **OOS return** |
|---|---|---|---|---|---|
| allowed | 12 bars | −4.85 | −7.86 | −11% | −12% |
| allowed | off | −4.31 | −6.75 | −20% | −14% |
| **disabled** | 12 bars | −0.76 | −7.29 | **−1%** | −5% |
| **disabled** | off | **−0.60** | **−6.19** | **−0.6%** | **−4%** |

Turning the short leg off takes the in-sample result from −11% to **−0.6%** —
essentially flat before costs. By direction, in-sample gross P&L is −310 for
longs and −4,810 for shorts: the long book is already break-even.

The plausible reason is drift. Over this sample crypto rose, so shorting a
`+2σ` stretch is fighting a persistent trend, while the equivalent long trade is
fading *into* it.

## 4. Rule 3 (ADX) and Rule 4 (deviation) both earn their place

Full z-entry × ADX × deviation grid, in-sample return:

| `z_entry` | ADX filter | dev gate | IS return | OOS return | fills (IS/OOS) |
|---|---|---|---|---|---|
| 2.0 | on | on | −14.3% | −12.0% | 676 / 298 |
| 2.0 | **off** | on | −19.5% | −20.2% | 882 / 606 |
| 2.5 | on | on | −10.4% | −7.6% | 400 / 180 |
| 2.5 | **off** | on | −15.9% | −16.1% | 1070 / 433 |
| 3.0 | on | on | −5.6% | −5.4% | 220 / 98 |
| 3.0 | **off** | on | −7.1% | −8.3% | 560 / 251 |

* **The ADX filter helps consistently**, at every z-entry level, by refusing to
  fade genuine trends. It roughly halves the trade count and cuts the loss.
* **The deviation gate (Rule 4) helps modestly** at every level.
* **Higher z-entry is better**: `3.0` loses about a third as much as `2.0`,
  because it trades far less often against a fixed 0.2% round-trip cost.

All 12 cells still lose, so no parameter choice rescues the current exit policy.

## 5. The out-of-sample result does not generalise

Every in-sample improvement collapses out-of-sample: OOS composites cluster in a
narrow band (−8.4 … −9.7) no matter how the parameters are set. **A tight OOS
cluster around a loss means the problem is structural, not parametric** — there is
no plateau to find. Any tuning that only looks good in-sample is noise.

Also note the OOS window is only **30 days** (1,441 bars of the 5,761), which is
too short to conclude anything about regime robustness. Re-run on 6–12 months and
check the result holds in both a rising and a falling market before committing.

## 6. What to change first, in order of expected impact

1. **`allow_short: false`** — worth roughly +10 percentage points in-sample.
2. **`STOP_ATR_MULT=none`** (or ≥ 3× ATR, or a structural `Z`-based stop) — worth
   roughly +2 to +3 points, and it is the difference between a 0% and a ~40% win
   rate on stopped trades.
3. **`z_entry: 2.5` or `3.0`** — fewer, more selective trades, materially smaller
   losses at a fixed 0.2% round-trip cost.
4. Keep **ADX < 25** and the **0.6% deviation gate**.
5. Re-examine the **12-bar time stop**: it is roughly neutral in-sample and
   slightly harmful out-of-sample; the 90 time-stop trades averaged exactly 12.0
   bars, so the cap is binding often. Try 18–24 bars.
6. Then test the **supplementary filters** one at a time (see `run_sweep.py`).
   Their maths is implemented and unit-tested; all four are off by default.

Even after all of that the best cell is **−4% out-of-sample**, so the honest
expectation is that this needs a genuine edge improvement, not just tuning. The
one encouraging number is that the reversion target itself is reached reliably:
when a trade is allowed to reach the mean, it wins 100% of the time. The problem
is how many trades get killed before they get there.

## 7. Method caveats (read before quoting any of this)

* **Removing the stop also removes risk-based sizing.** With no stop distance,
  Rule 7's 0.5% risk budget cannot be expressed, so `PositionSizer` falls back to
  the 15% per-pair cap and positions get *larger*. The `stop_atr_mult: null` rows
  therefore change two things at once. The clean version of that experiment is
  `STOP_ATR_MULT=none` **plus** `STOP_LOSS_PCT=0.05`, which keeps sizing
  risk-based with a wide stop.
* **Rule 1's spread filter cannot discriminate in a backtest.** The CSV has no
  book, so one assumed spread (`--spread-bps`, default 5) is applied to every
  pair. The ranking and the code path are exercised; the *filter* is not
  validated. Depth is not checked at all offline.
* **Bars are sampled, not true OHLC, on the live venue.** Live bars come from
  polling the ticker, so an intrabar spike between two samples is invisible. The
  backtest uses real Binance OHLC, so live results will differ from these.
* **Gap risk through a stop is understated.** A stop fills at its own level plus
  slippage; a genuine gap through it would be worse.
* **`stdev` uses the sample (n−1) denominator**, and ADX follows Wilder's
  original seeding. Both are defensible conventions, but a different choice moves
  the last decimal of a z-score and can flip a trade right at the threshold.
* **The sweep's `OOS/IS ratio` flag is suppressed when |IS composite| < 0.5**, so
  it will print `n/a` for near-zero baselines instead of reporting a meaningless
  ratio like "10.34, ok".
