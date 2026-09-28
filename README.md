# Roostoo Quant Trading Hackathon — automated trading agent

An autonomous, dependency-free Python trading bot for the **HK vs AU vs IN Quant
Trading Hackathon** on [Roostoo](https://luma.com/coghwiyt)'s mock crypto
exchange, plus the backtesting and diagnostic tooling needed to tune it.

Scored as `0.40 × Sortino + 0.30 × Sharpe + 0.30 × Calmar`, so the design is
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
> parameters.** The Rules 1–3 entry logic works; the default Rule 5 stop does not,
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
| 30-minute OHLCV candles (Rules 2–6) | nothing | `CandleBuilder` samples the ticker into 30-min bars live; `scripts/fetch_history.py` pulls real history from Binance for backtests |
| Per-bar volume (supp. Rules 3–4) | rolling 24h turnover only | live: the *change* in `UnitTradeValue` between samples; backtest: real Binance volume |
| Order-book depth within ±0.5% (Rule 1) | **no endpoint** | pluggable `DepthProvider` — off in backtest, Binance L2 as a live proxy, or a Roostoo path the moment the organisers confirm one |
| ADX / ATR / VWAP / VolExpansion | nothing | computed from the bars above, pure Python, no numpy |

The organisers' own **Data Sources Pack** recommends Binance Vision for bulk
history and notes CoinAPI as the only listed source with real L2 depth. Binance
klines are used here because they need no account or key.

**The caveat that matters:** orders fill on Roostoo while signals may be computed
from Binance data. Those are different books. The bot therefore refuses to seed
its indicators from a CSV whose last close is more than 2% away from the venue's
live mid (`engine.seed_history`) and journals the decision either way. Check the
basis per pair before trusting an external feed.

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
  indicators.py   SMA/EMA/RSI/z-score + Wilder ADX, ATR, VWAP, VolExpansion, ReturnShock
  metrics.py      Sharpe / Sortino / Calmar / drawdown + the competition composite
  universe.py     Rule 1: turnover ranking, spread ceiling, depth provider
  risk.py         NAV, position book, sizing, caps, protective exits, kill switch
  strategies/     strategy interface + the mean-reversion rules
  engine.py       the autonomous decision loop
  journal.py      append-only audit trail (JSONL + trades.csv)
  backtest.py     event-driven backtester sharing the live objects
tests/            334 unittest cases, stdlib only
```

## 3. Rule map and ownership

Rules 1–3 are this repository's implemented scope. Rules 4–12 are **config-driven
defaults in `roostoo/risk.py`**, because the engine cannot run without a risk
layer, a sizing policy and some exit. They are written to be replaced wholesale by
whoever owns them: nothing in `engine.py` or `backtest.py` assumes the current
policy.

| Rule | Where | State |
|---|---|---|
| 1 top-8 by 24h turnover, spread ≤ 0.1%, depth > $X | `universe.py` | **implemented**; depth needs a provider |
| 2 SMA48/Std48 z-score, ±2σ entry with ΔZ turn, exit at ∓0.25 | `strategies/mean_reversion.py` | **implemented** |
| 3 ADX(14) < 25 trend filter | `indicators.adx` + strategy | **implemented** |
| 4 `|P−SMA|/P > 0.6%` | `indicators.price_deviation_pct` | implemented, default on |
| 5 stop at 1.5 × ATR(14) | `risk.protective_exits` + `PositionSizer` | config-driven (`STOP_ATR_MULT`) |
| 6 time stop at 12 bars | `risk.protective_exits` | config-driven (`MAX_HOLD_BARS`) |
| 7 max loss 0.5% of NAV per trade | `PositionSizer` | risk-first sizing |
| 8 ≤ 15% NAV per coin | `RiskManager.evaluate` | `MAX_PAIR_WEIGHT` |
| 9 ≤ 60% NAV gross | `RiskManager.evaluate` | `MAX_GROSS_EXPOSURE` |
| 10 ≤ 4 positions | `RiskManager.evaluate` | `MAX_OPEN_POSITIONS` |
| 11 −2% day halts entries | `RiskManager.observe` | `MAX_DAILY_LOSS_PCT`, UTC+8 day |
| 12 no re-entry for 2 bars | `RiskManager.record_exit` | `COOLDOWN_BARS` |
| supp. 1–4 ReturnShock, VWAPGap, RelVolume, VolExpansion | `indicators.py` | maths implemented and tested; **all four switched off by default**, per the plan to add them one at a time |

`0.15 × 4 == 0.60`: the per-coin cap and the gross cap bind at the same point, so
equal-weight sizing across four slots satisfies Rules 8, 9 and 10 at once.

## 4. What the backtest says

120 days of real Binance 30-minute data, 8 majors, 0.1% taker per side + 5bps
slippage, market orders filled at the **next bar's open**.

| | in-sample | out-of-sample |
|---|---|---|
| total return | −14.3% | −12.1% |
| Sortino | −8.9 | −10.0 |
| composite | −7.1 | −8.9 |
| round trips | 338 | 149 |
| fees paid | 9,202 | 4,178 |

The entry logic is **not** the problem. `scripts/analyze_backtest.py` splits the
P&L by exit reason:

| exit | n | gross P&L | avg bars held | win rate |
|---|---|---|---|---|
| z-exit (Rule 2) | 89 | **+12,781** | 6.2 | **100%** |
| time stop (Rule 6) | 90 | +3,214 | 12.0 | 47.8% |
| stop (Rule 5) | 159 | **−21,116** | 3.8 | **0%** |

Every trade that reached the mean-reversion target won. The ATR stop, hit after
an average of 3.8 bars, turned 159 would-be mean-reversion trades into realised
losses and cost more than everything else earned.

**Rules 2 and 5 are in direct tension.** Rule 2 deliberately buys an asset that
has just moved hard against it; Rule 5 then exits if the adverse move exceeds
1.5 × ATR — and ATR is itself elevated precisely because of the move that
triggered the entry. The entry signal is close to the stop trigger by
construction.

Fees compound it: 0.2% round trip against an average gross of −15 per trip, while
the winners averaged +143. The strategy's edge is real but thin, so trade
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
  reconciles against the order history — it does not re-send, which would risk a
  double position.
* **Live ≡ backtest.** The backtester reuses `RiskManager`, `PositionSizer`,
  `PositionBook` and the same `Strategy.generate` call rather than reimplementing
  them. Both evaluate the strategy exactly once per **closed** bar and check stops
  on every loop.
* **No look-ahead.** Orders decided on bar *t* fill at the open of bar *t+1*.
  Stops are detected against the bar's high/low and filled at the stop level.
* **In-flight orders reserve capital.** Two consecutive bars must not each size a
  full position for the same pair. `RiskManager.evaluate` takes
  `committed_pairs`/`committed_notional` for exactly this; the bug is covered by
  a regression test.
* **State survives restart.** `PositionBook` and the risk state persist to
  `journal/`, and every cycle reconciles local quantities against the exchange
  balances, which are authoritative.
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

The host clock must be within 60 s of the exchange or every signed request is
rejected — `--check` verifies this, and `chrony` is configured in the deploy
guide.

## 7. Tests

```bash
python -m unittest discover -s tests -t .     # 334 tests, no network, no sleeping
```

Includes the HMAC signature reproduced byte-for-byte from Roostoo's published
test vector — the failure mode that would otherwise cost a day of the
competition to diagnose on live keys.

## 8. 中文快速开始

```bash
# 1) 无密钥先跑通整条链路（内置模拟交易所）
python run_live.py --mock --cycles 20

# 2) 拉真实历史数据（本机已验证可访问 Binance，无需 API key）
python scripts/fetch_history.py --days 365 --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT

# 3) 回测（前 75% 样本内 / 后 25% 样本外）
python run_backtest.py --oos-frac 0.25

# 4) 拆解亏损原因：按出场原因统计盈亏、手续费拖累、持仓时长
python scripts/analyze_backtest.py reports/trades_out-of-sample.csv

# 5) 参数扫描。Windows 下 PowerShell 会把内联 JSON 的引号吃掉，
#    所以用 @文件 的形式传参数（reports/grid_stop.json 已给出示例）
python run_sweep.py --config-grid "@reports/grid_stop.json"

# 6) 拿到密钥后：先只读自检，确认签名/时钟/交易对都正常（不下任何单）
copy .env.example .env    # 填入 ROOSTOO_API_KEY / ROOSTOO_SECRET_KEY
python run_live.py --check
```

**先看 `docs/FINDINGS.md`**：Rule 1–3 的入场逻辑是有效的（到达均值目标的交易
100% 盈利），但默认的 Rule 5 ATR 止损与 Rule 2 的入场逻辑互相冲突，回测里它是
亏损的主要来源。止损参数由负责 Rule 5 的队友决定，本仓库只提供可配置项与扫描工具。

## 9. License

MIT — see [LICENSE](LICENSE). The competition requires the submitted repository
to be open source; a public repository without a licence is legally
"all rights reserved", so this file is what makes that claim true. Swap it for
Apache-2.0 or GPL if the team prefers, but do not remove it.

> **Not investment advice, and not a live-money system.** This targets Roostoo's
> mock exchange with a virtual portfolio. The backtest results in
> `docs/FINDINGS.md` are negative; do not point this at real capital without
> independent validation.
