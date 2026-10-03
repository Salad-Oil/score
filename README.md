# Roostoo Quant Trading Hackathon 鈥?automated trading agent

An autonomous, dependency-free Python trading bot for the **HK vs AU vs IN Quant
Trading Hackathon** on [Roostoo](https://luma.com/coghwiyt)'s mock crypto
exchange, plus the backtesting and diagnostic tooling needed to tune it.

Scored as `0.40 脳 Sortino + 0.30 脳 Sharpe + 0.30 脳 Calmar`, so the design is
risk-first: downside deviation and drawdown are treated as the objective, not as
an afterthought.

```bash
python run_live.py --check                 # verify keys/clock/venue (read-only, no orders)
python run_live.py --mock --cycles 20      # full loop against the offline simulator
python scripts/fetch_history.py --days 365 --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT
python run_backtest.py --oos-frac 0.25     # in-sample vs out-of-sample
python scripts/analyze_backtest.py reports/trades_out-of-sample.csv   # why did it lose?
python run_sweep.py --config-grid "@reports/grid_stop.json"           # tune one decision at a time
```

> **Read [`docs/FINDINGS.md`](docs/FINDINGS.md) before trusting the default
> parameters.** The Rules 1鈥? entry logic works; the default Rule 5 stop does not,
> and the backtest says so unambiguously.

---

## 1. The data problem, and how it is solved

The Roostoo public API
([docs](https://github.com/roostoo/Roostoo-API-Documents)) exposes exactly one
market-data endpoint, `/v3/ticker`, returning:

`LastPrice`, `MaxBid`, `MinAsk`, 24h `Change`, 24h `CoinTradeValue`,
24h `UnitTradeValue`.

That is a **snapshot**. It is not enough for the team's playbook:

| The rules need | Roostoo provides | Resolution |
|---|---|---|
| 30-minute OHLCV candles (Rules 2鈥?) | nothing | `CandleBuilder` samples the ticker into 30-min bars live; `scripts/fetch_history.py` pulls real history from Binance for backtests |
| Per-bar volume (supp. Rules 3鈥?) | rolling 24h turnover only | live: the *change* in `UnitTradeValue` between samples; backtest: real Binance volume |
| Order-book depth within 卤0.5% (Rule 1) | **no endpoint** | pluggable `DepthProvider` 鈥?Binance L2 is now a legitimate proxy, since the venue is confirmed to track Binance |
| ADX / ATR / VWAP / VolExpansion | nothing | computed from the bars above, pure Python, no numpy |

The organisers' own **Data Sources Pack** recommends Binance Vision for bulk
history and notes CoinAPI as the only listed source with real L2 depth. Binance
is used here because it needs no account or key.

**The organisers confirmed the mock venue's prices follow Binance.** That single
fact is what makes this whole design valid, and it is now *measured* rather than
assumed. `roostoo/basis.py` compares every quoted pair against Binance on each
bar, journals the readings, and excludes any pair whose basis exceeds
`BASIS_MAX_PCT` (default 1%). A wide basis is not "a related but different
market" 鈥?it is the wrong symbol (USDT vs USD), the wrong pair, or a stale feed,
and fading a z-score computed against it is trading noise.

Two consequences worth stating plainly:

* **Rule 1's depth clause is now testable**, with one caveat: Binance's book
  measures *market* liquidity, not the mock venue's own depth. It answers "is this
  asset liquid", not "can Roostoo absorb my order". `DEPTH_PROVIDER=none` ships by
  default because the check **fails closed** 鈥?if Binance's depth endpoint is
  unreachable, every pair is excluded and the bot silently stops trading, which is
  a bad failure mode inside a scored window that requires 8 active trading days.
  Flip it to `binance` after `run_live.py --check` confirms connectivity.
* **Bar alignment is exact.** Binance 30-minute candles close on the UTC :00/:30
  grid and `CandleBuilder` floors samples to that same epoch-aligned grid, so a
  live sampled bar and its Binance counterpart describe the same window.

## 2. Layout

```
run_live.py                 live loop entry point (--check does read-only verification)
run_backtest.py             in-sample / out-of-sample backtest + report
run_sweep.py                parameter sweep & ablation, holdout-aware
scripts/fetch_history.py    Binance 30m klines -> CSV (pagination, retries, synthetic fallback)
scripts/analyze_backtest.py round-trip forensics: exit-reason mix, fee drag, holding time
deploy/                     systemd unit + AWS EC2 guide
roostoo/
  client.py       signed REST (HMAC-SHA256), retries, throttle, server-time sync
  simulator.py    in-process mock exchange: same surface, same fees, offline
  candles.py      OHLCV bars; live bar building and CSV loading
  basis.py        cross-venue basis monitor (the venue is confirmed to track Binance)
  indicators.py   SMA/EMA/RSI/z-score + Wilder ADX, ATR, VWAP, VolExpansion, ReturnShock
  metrics.py      Sharpe / Sortino / Calmar / drawdown + the competition composite
  universe.py     Rule 1: turnover ranking, spread ceiling, depth provider
  risk.py         NAV, position book, sizing, caps, protective exits, kill switch
  strategies/     strategy interface + the mean-reversion rules
  engine.py       the autonomous decision loop
  journal.py      append-only audit trail (JSONL + trades.csv)
  backtest.py     event-driven backtester sharing the live objects
tests/            405 unittest cases, stdlib only
scripts/          secret scanning, history fetch, sweep analysis
docs/SECURITY.md  how credentials are handled, and what to do if one leaks
```

**Never commit a key.** `.env` is git-ignored and is the only place credentials
live; `.env.example` is committed with empty values. Three guards enforce that --
a pre-commit hook, `scripts/publish.ps1` and a `secret-scan` CI job -- all
described in [docs/SECURITY.md](docs/SECURITY.md).

## 3. Rule map and ownership

Rules 1鈥? are this repository's implemented scope. Rules 4鈥?2 are **config-driven
defaults in `roostoo/risk.py`**, because the engine cannot run without a risk
layer, a sizing policy and some exit. They are written to be replaced wholesale by
whoever owns them: nothing in `engine.py` or `backtest.py` assumes the current
policy.

| Rule | Where | State |
|---|---|---|
| 1 top-8 by 24h turnover, spread 鈮?0.1%, depth > $X | `universe.py` | **implemented**; depth needs a provider |
| 2 SMA48/Std48 z-score, 卤2蟽 entry with 螖Z turn, exit at 鈭?.25 | `strategies/mean_reversion.py` | **implemented** |
| 3 ADX(14) < 25 trend filter | `indicators.adx` + strategy | **implemented** |
| 4 `|P鈭扴MA|/P > 0.6%` | `indicators.price_deviation_pct` | implemented, default on |
| 5 stop at 1.5 脳 ATR(14) | `risk.protective_exits` + `PositionSizer` | config-driven (`STOP_ATR_MULT`) |
| 6 time stop at 12 bars | `risk.protective_exits` | config-driven (`MAX_HOLD_BARS`) |
| 7 max loss 0.5% of NAV per trade | `PositionSizer` | risk-first sizing |
| 8 鈮?15% NAV per coin | `RiskManager.evaluate` | `MAX_PAIR_WEIGHT` |
| 9 鈮?60% NAV gross | `RiskManager.evaluate` | `MAX_GROSS_EXPOSURE` |
| 10 鈮?4 positions | `RiskManager.evaluate` | `MAX_OPEN_POSITIONS` |
| 11 鈭?% day halts entries | `RiskManager.observe` | `MAX_DAILY_LOSS_PCT`, UTC+8 day |
| 12 no re-entry for 2 bars | `RiskManager.record_exit` | `COOLDOWN_BARS` |
| supp. 1鈥? ReturnShock, VWAPGap, RelVolume, VolExpansion | `indicators.py` | maths implemented and tested; **all four switched off by default**, per the plan to add them one at a time |

`0.15 脳 4 == 0.60`: the per-coin cap and the gross cap bind at the same point, so
equal-weight sizing across four slots satisfies Rules 8, 9 and 10 at once.

## 4. What the backtest says

120 days of real Binance 30-minute data, 8 majors, 0.1% taker per side + 5bps
slippage, market orders filled at the **next bar's open**, with Rule 4's deviation
gate enabled (`enforce_min_deviation: true`) so these match `docs/FINDINGS.md`.
Regenerated after the correctness pass described in section 5 -- notably the fill
timestamps, the out-of-sample warm-up window and the entry gate below -- so these
supersede the earlier figures.

| | in-sample | out-of-sample |
|---|---|---|
| total return | 鈭?3.4% | 鈭?1.9% |
| Sharpe | 鈭?.1 | 鈮?鈭?0.0 |
| Sortino | 鈭?.5 | 鈮?鈭?0.0 |
| composite | 鈭?.8 | 鈭?.0 |
| round trips | 333 | 144 |
| fees paid | 9,121 | 4,055 |

`鈮?鈭?0.0` is not a typo: ratios are clamped at 卤10 for reporting, and the
out-of-sample figures reach the cap (the raw values are 鈭?4.5 and 鈭?1.8). The
clamped columns therefore understate how bad the out-of-sample result is, and
`run_backtest.py` now prints which figures were clamped.

The entry logic is **not** the problem. `scripts/analyze_backtest.py` splits the
P&L by exit reason:

| exit | n | gross P&L | avg bars held | win rate |
|---|---|---|---|---|
| z-exit (Rule 2) | 86 | **+12,176** | 6.0 | **100%** |
| time stop (Rule 6) | 102 | +2,955 | 12.0 | 49.0% |
| stop (Rule 5) | 145 | **鈭?9,460** | 4.2 | **0%** |

Every trade that reached the mean-reversion target won. The ATR stop, hit after
an average of 4.2 bars, turned 145 would-be mean-reversion trades into realised
losses and cost more than everything else earned.

**Rules 2 and 5 are in direct tension.** Rule 2 deliberately buys an asset that
has just moved hard against it; Rule 5 then exits if the adverse move exceeds
1.5 脳 ATR 鈥?and ATR is itself elevated precisely because of the move that
triggered the entry. The entry signal is close to the stop trigger by
construction.

Fees compound it: 0.2% round trip against an average gross of 鈭?3 per trip, while
the winners averaged +142. The strategy's edge is real but thin, so trade
frequency is a first-order parameter.

`docs/FINDINGS.md` records the ablations and the candidate fixes.

## 5. Design decisions worth knowing

* **The `Success: false` trap.** Roostoo returns application errors inside HTTP
  200. A client that only checks the status code keeps trading through
  rejections. `_parse` raises `APIError`, with a documented exception for
  `/v3/pending_count` and `/v3/query_order`, whose `Success: false` means "empty
  result".
* **`place_order` is never blind-retried.** A transport failure leaves the order
  genuinely unknown. The client returns `status="UNKNOWN"` and the engine
  reconciles against the order history 鈥?it does not re-send, which would risk a
  double position.
* **Live 鈮?backtest.** The backtester reuses `RiskManager`, `PositionSizer`,
  `PositionBook` and the same `Strategy.generate` call rather than reimplementing
  them. Both evaluate the strategy exactly once per **closed** bar and check stops
  on every loop.
* **No look-ahead.** Orders decided on bar *t* fill at the open of bar *t+1*.
  Stops are detected against the bar's high/low and filled at the stop level.
* **In-flight orders reserve capital.** Two consecutive bars must not each size a
  full position for the same pair. `RiskManager.evaluate` takes
  `committed_pairs`/`committed_notional` for exactly this, and Rule 9 is tested
  against the *projected* book so an exit approved earlier in the same batch frees
  its capital for a later entry. Both are covered by regression tests.
* **State survives restart, and a failed start is non-destructive.** `PositionBook`
  and the risk state persist to `journal/`, and every cycle reconciles local
  quantities against the exchange balances, which are authoritative. State is
  loaded *before* any network call, an empty book may not overwrite a real one,
  and nothing is persisted until bootstrap has completed -- so a transient failure
  during startup can no longer erase stops, cost basis, cooldowns or the kill
  switch.
* **A partial balance snapshot is not acted on.** If the venue's response has no
  quote-currency row the cycle is skipped and journalled, rather than pricing the
  book at zero (which would trip the permanent kill switch) and reconciling every
  missing row as a closed position. A *coin row* that is simply absent is treated
  the same way: only an explicit zero balance closes a position, because a
  truncated payload used to read as "the exchange holds nothing" and delete the
  whole book.
* **An unanswered question is not a negative answer.** A failed
  `/v6/short_positions` call once returned an empty list, which the reconciliation
  pass read as "the venue holds no shorts" and used to delete every short --
  losing the collateral, the Rule 5 stop and the Rule 6 open time. The call now
  reports "unknown" separately from "none", and shorts the venue holds that the
  book does not know about are adopted rather than ignored.
* **An UNKNOWN order is reconciled on side, size *and* status.** A history row is
  only accepted as ours when it is genuinely `FILLED`, matches the quantity to
  within a lot, and reports something filled. Matching on side and recency alone
  accepted a cancelled order of an unrelated size, and a row with no `Status`
  field used to arrive as `FILLED` with nothing filled -- which booked a
  full-size position at price zero and wrote it to `positions.json`.
* **A stop that cannot be computed refuses the trade.** With `STOP_ATR_MULT` set,
  a missing or non-finite ATR no longer approves a caps-only position with no
  stop at all (the largest size the caps allow, which is the opposite of
  risk-first). `REFUSE_ENTRY_WITHOUT_STOP=0` restores the old behaviour and
  reports it as `+no_stop`.
* **A position is always closable.** The Rule 6 time stop is evaluated before the
  mark is validated, and the execution layer falls back to the live quote and then
  the entry price before pricing an exit -- so a position whose mark is zero (an
  adopted holding, a restored bad state file, a venue that stopped quoting) can
  still be closed, by the kill switch if by nothing else.
* **Depth fails closed.** If a depth provider is configured and returns nothing,
  the pair is excluded rather than assumed liquid.

## 6. Operations

```bash
python run_live.py --check          # Oct 1-3: proves signing + clock + universe, sends no orders
python run_live.py --no-seed        # cold start, build bars from live samples
python run_live.py --flatten-on-exit  # deliberate stop: close the book
```

The competition requires **at least 8 active trading days with enough trades each
day**, so `run_live.py` is supervised by systemd with `Restart=always`, and
`deploy/AWS_DEPLOY.md` covers the EC2 setup. The service deliberately does *not*
pass `--flatten-on-exit`: a crash-loop must not liquidate the book.

The exception is a deliberate halt. When the kill switch fires, the halt is
persisted and a restart cannot recover it, so `run_live.py` exits with status `3`
and the unit declares that status a clean stop (`SuccessExitStatus=3`). Without
it, `Restart=always` restarted the halted bot about ten times before systemd
marked the unit failed. Clearing a halt is a manual edit of
`journal/engine_state.json`; that is the intent.

The host clock must be within 60 s of the exchange or every signed request is
rejected 鈥?`--check` verifies this, and `chrony` is configured in the deploy
guide.

## 7. Tests

```bash
python -m unittest discover -s tests -t .     # 405 tests, no network, no sleeping
```

Includes the HMAC signature reproduced byte-for-byte from Roostoo's published
test vector 鈥?the failure mode that would otherwise cost a day of the
competition to diagnose on live keys 鈥?and `tests/test_state_safety.py`, which
pins the guarantees that a bad startup or a malformed balance response cannot
destroy the stored book.

## 8. 涓枃蹇€熷紑濮?
```bash
# 1) 鏃犲瘑閽ュ厛璺戦€氭暣鏉￠摼璺紙鍐呯疆妯℃嫙浜ゆ槗鎵€锛?python run_live.py --mock --cycles 20

# 2) 鎷夌湡瀹炲巻鍙叉暟鎹紙鏈満宸查獙璇佸彲璁块棶 Binance锛屾棤闇€ API key锛?python scripts/fetch_history.py --days 365 --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT

# 3) 鍥炴祴锛堝墠 75% 鏍锋湰鍐?/ 鍚?25% 鏍锋湰澶栵級
python run_backtest.py --oos-frac 0.25

# 4) 鎷嗚В浜忔崯鍘熷洜锛氭寜鍑哄満鍘熷洜缁熻鐩堜簭銆佹墜缁垂鎷栫疮銆佹寔浠撴椂闀?python scripts/analyze_backtest.py reports/trades_out-of-sample.csv

# 5) 鍙傛暟鎵弿銆俉indows 涓?PowerShell 浼氭妸鍐呰仈 JSON 鐨勫紩鍙峰悆鎺夛紝
#    鎵€浠ョ敤 @鏂囦欢 鐨勫舰寮忎紶鍙傛暟锛坮eports/grid_stop.json 宸茬粰鍑虹ず渚嬶級
python run_sweep.py --config-grid "@reports/grid_stop.json"

# 6) 鎷垮埌瀵嗛挜鍚庯細鍏堝彧璇昏嚜妫€锛岀‘璁ょ鍚?鏃堕挓/浜ゆ槗瀵归兘姝ｅ父锛堜笉涓嬩换浣曞崟锛?copy .env.example .env    # 濉叆 ROOSTOO_API_KEY / ROOSTOO_SECRET_KEY
python run_live.py --check
```

**鍏堢湅 `docs/FINDINGS.md`**锛歊ule 1鈥? 鐨勫叆鍦洪€昏緫鏄湁鏁堢殑锛堝埌杈惧潎鍊肩洰鏍囩殑浜ゆ槗
100% 鐩堝埄锛夛紝浣嗛粯璁ょ殑 Rule 5 ATR 姝㈡崯涓?Rule 2 鐨勫叆鍦洪€昏緫浜掔浉鍐茬獊锛屽洖娴嬮噷瀹冩槸
浜忔崯鐨勪富瑕佹潵婧愩€傛鎹熷弬鏁扮敱璐熻矗 Rule 5 鐨勯槦鍙嬪喅瀹氾紝鏈粨搴撳彧鎻愪緵鍙厤缃」涓庢壂鎻忓伐鍏枫€?
## 9. License

MIT 鈥?see [LICENSE](LICENSE). The competition requires the submitted repository
to be open source; a public repository without a licence is legally
"all rights reserved", so this file is what makes that claim true. Swap it for
Apache-2.0 or GPL if the team prefers, but do not remove it.

> **Not investment advice, and not a live-money system.** This targets Roostoo's
> mock exchange with a virtual portfolio. The backtest results in
> `docs/FINDINGS.md` are negative; do not point this at real capital without
> independent validation.
