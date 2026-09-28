# `data/` — historical OHLCV bars for the strategy layer

## Why this directory exists

The Roostoo mock exchange's public API returns a **ticker snapshot only**
(`LastPrice`, `MaxBid`, `MinAsk`, 24h change, 24h `CoinTradeValue` /
`UnitTradeValue`). It has:

- **no candlestick / OHLCV endpoint**, and
- **no order-book depth endpoint**.

That is not enough to build the indicators the strategy needs — SMA(48),
Std(48), z-score, ADX(14), ATR(14), VWAP, volume. A snapshotted price path can
never produce a real bar *high*, *low* or *traded volume*, so bars are sourced
externally and cached here as CSV.

## Where the data comes from

`scripts/fetch_history.py` downloads 30-minute klines from Binance's free public
REST endpoint:

```
GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=30m&startTime=...&limit=1000
```

- No API key, no signing, no third-party packages (`urllib.request` only).
- Paginated 1000 bars per request, advancing by last open time + interval.
- Retries HTTP 429/418/5xx and network errors with exponential backoff (capped
  at 30 s); on 429/418 it honours `Retry-After`. Five attempts per symbol, then
  that symbol is skipped and the others continue.

A `--source synthetic` mode produces deterministic seeded bars instead, with no
network access at all — use it for tests, CI and demos.

### Binance ≈ Roostoo, but **not guaranteed identical**

Live orders execute on **Roostoo**; the signals may be computed from **Binance**
data. These are different venues with different liquidity and possibly a
different reference index, and a mock exchange might even be driven by a seeded
RNG. **Sanity-check the feeds against each other before trading**: pull one
Roostoo ticker and one Binance ticker for the same pair and compare. If the
basis is more than a few tens of basis points, or if it drifts over time, treat
that pair's external signals as unreliable.

Two further mismatches to keep in mind:

- **Symbol naming.** Binance spells it `BTCUSDT`; Roostoo and this repo use
  `BTC/USD`, and files are `BTC-USD_30m.csv`. The `USDT` leg is collapsed to
  `USD` because Roostoo quotes in USD. USDT is a close-but-imperfect USD proxy.
- **Timeframe.** Binance bars are much higher-resolution in volume than the
  mock feed, so **never** feed Binance *volume* into a Roostoo execution
  decision as if it were the size you can actually trade.

## Files

| File | Purpose |
| --- | --- |
| `sample_BTC-USD_30m.csv` | 1200 synthetic bars, seed 7 — committed, offline |
| `sample_ETH-USD_30m.csv` | same, for ETH |
| `BTC-USD_30m.csv`, `ETH-USD_30m.csv`, … | real downloads (gitignored, `data/*.csv` except `sample_*`) |

## CSV schema (exact)

Header row always present, rows sorted ascending by `ts_ms` and de-duplicated:

```csv
ts_ms,open,high,low,close,volume,quote_volume,trades
1714003200000,63412.15000000,63580.90000000,63390.00000000,63501.40000000,412.73100000,26205000.00000000,3184
```

| Column | Type | Meaning |
| --- | --- | --- |
| `ts_ms` | int | Bar **open** time, milliseconds since the Unix epoch, **UTC** |
| `open` | float | First trade price of the bar |
| `high` | float | Highest trade price in the bar |
| `low` | float | Lowest trade price in the bar |
| `close` | float | Last trade price of the bar |
| `volume` | float | Base-asset volume, e.g. BTC |
| `quote_volume` | float | Quote-asset (USDT≈USD) volume |
| `trades` | int | Number of trades in the bar |

Guarantees, asserted by `--verify`:

- `high >= low`, `high >= max(open, close)`, `low <= min(open, close)`
- no duplicate and no out-of-order `ts_ms`
- `quote_volume = volume * typical_price` for the synthetic source

### `ts_ms` is UTC — no exceptions

Bars are aligned to the UTC epoch grid (a 30m bar opens at `:00` or `:30` UTC).
The value is stored with no offset, so **a consumer that interprets it as local
time will silently shift every bar**. Convert with
`datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)`.

## How to refresh the data

```bash
# 12 months of 30m bars for the whole default basket of majors
python scripts/fetch_history.py --days 365 --symbols BTCUSDT,ETHUSDT,SOLUSDT --verify

# explicit window, both ISO dates
python scripts/fetch_history.py --start 2024-01-01 --end 2024-07-01 --verify

# offline / CI: deterministic synthetic bars, no network
python scripts/fetch_history.py --source synthetic --days 60 --symbols BTCUSDT,ETHUSDT --verify

# regenerate the committed samples (~1200 bars each, seed 7)
python scripts/make_sample_data.py

# a different interval (file name follows the interval: BTC-USD_1h.csv)
python scripts/fetch_history.py --interval 1h --days 365
```

Useful flags: `--out-dir` (default `data`), `--sleep` (default `0.25` s between
requests — do not lower it, Binance rate-limits by IP), `--seed` (synthetic
RNG), `--quiet`.

Exit codes: `0` on success (including partial success where only some symbols
failed), `1` if **every** symbol failed or a `--verify` check found a corrupt
file, `2` on bad CLI arguments.

## How many days for a 30-minute mean-reversion backtest?

**At least 6–12 months (roughly 8,600–17,500 bars per symbol).** Rationale:

- Crypto regimes are the whole game for mean reversion. A 30m z-score strategy
  that looks brilliant in a choppy range gets destroyed in a trend, and vice
  versa. Six months is the practical minimum to span a rally, a drawdown and a
  flat stretch; 12 months usually covers a real stress event.
- 180 days (~8,600 bars) is enough to estimate SMA(48)/Std(48) and ADX(14), but
  *not* enough to trust the equity curve — that is the CLI default only because
  it is a fast download.
- More than ~24 months buys little for a 30m model and invites structural
  breaks (different market makers, different volatility floor).

Then **reserve an out-of-sample slice** (e.g. the last 20–25% of bars) and
refit nothing on it, or the backtest is a curve-fit. Note that fees dominate a
30m mean-reversion edge: at 0.1% taker per side, a round trip costs 0.2%, so the
average target move must clear that comfortably.

## Caveats

- **Volume on some intervals.** Binance always returns `volume` and
  `quote_volume` for spot klines, but some venues/intervals report zero volume
  for illiquid pairs or during outages. Bars with `volume == 0` should be
  dropped or flagged before computing VWAP or a volume filter.
- **USDT ≠ USD.** Small persistent basis, usually well under 1%.
- **Bar alignment.** Binance's first bar of a request can be partial if
  `startTime` lands mid-bar; `--verify` catches structural problems, not
  partial-bar bias. Prefer whole-day windows.
- **Rate limits.** Bulk multi-year downloads across many symbols are throttled
  by IP, not by key. Keep `--sleep` at the default and expect a while.
- **The newest bar is incomplete** until its close; `make_sample_data.py` snaps
  its window to the interval grid for this reason.
