#!/usr/bin/env python3
"""Historical OHLCV acquisition for the Roostoo quant bot.

WHY THIS FILE EXISTS
--------------------
The strategy layer needs 30-minute OHLCV bars: an SMA(48)/Std(48) z-score for
mean reversion, ADX(14) and ATR(14) for regime and stop sizing, a session VWAP
and a volume filter.  The Roostoo mock exchange's public API exposes only a
ticker *snapshot* -- ``LastPrice``, ``MaxBid``, ``MinAsk``, 24h change and 24h
CoinTradeValue/UnitTradeValue.  There is:

  * no candlestick / OHLCV endpoint, and
  * no order-book depth endpoint.

So bars cannot be reconstructed from Roostoo at all: a snapshot polled once a
minute gives a single price path, never a real high/low or a real traded volume
for a bar.  Until the strategy accumulates its own 30-minute samples *and*
Roostoo offers history, candles must come from an external venue.  Binance's
free public REST endpoint (``/api/v3/klines``) needs no API key and no signing,
so it is the pragmatic source.

.. warning::
   **Live execution happens on Roostoo; the signals may be computed from
   Binance data.**  Those are two different order books with different
   liquidity, different fee tiers and, on a mock venue, possibly deliberately
   different prices.  Before trusting a signal you MUST sanity-check the two
   feeds against each other -- pull one Roostoo ticker and one Binance ticker
   for the same pair and compare ``LastPrice`` (binance mid = (MaxBid+MinAsk)/2
   is a fairer comparison).  If the basis is wider than a few tens of basis
   points, or if it drifts, do not trade that pair on external signals: the
   mock exchange may be pricing off a different index or a seeded RNG.  A
   mean-reversion entry at a z-score of -2 measured on Binance is worthless if
   Roostoo's price is not the same random variable.

WHAT IT WRITES
--------------
One CSV per symbol, e.g. ``data/BTC-USD_30m.csv``, with the exact columns::

    ts_ms,open,high,low,close,volume,quote_volume,trades

``ts_ms`` is the bar's *open* time as integer milliseconds since the Unix epoch
in **UTC** -- no timezone offset is stored, and CSV consumers must not assume
local time.  Rows are sorted ascending and de-duplicated on ``ts_ms``.

USAGE
-----
Synthetic (no network, deterministic, for tests and demos)::

    python scripts/fetch_history.py --source synthetic --days 60 \
        --symbols BTCUSDT,ETHUSDT --verify

Live Binance (paginated, 1000 bars per request)::

    python scripts/fetch_history.py --days 180 --symbols BTCUSDT,ETHUSDT

Only the standard library is used (``urllib``), so this runs on a bare box.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

CSV_COLUMNS = ["ts_ms", "open", "high", "low", "close", "volume", "quote_volume", "trades"]

#: A sensible basket of liquid majors that also exist on the Roostoo mock feed.
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,LTCUSDT"

MS_PER_SECOND = 1000
MS_PER_MINUTE = 60 * MS_PER_SECOND
MS_PER_HOUR = 60 * MS_PER_MINUTE
MS_PER_DAY = 24 * MS_PER_HOUR

#: Binance is happy with 1000; asking for more is silently capped.
PAGE_LIMIT = 1000
#: Requests to retry a *transaction* before giving up. Transient failures only.
MAX_ATTEMPTS = 5
#: Exponential backoff ceiling, seconds.
MAX_BACKOFF_SEC = 30.0

_QUOTES = ("USDT", "USDC", "FDUSD", "TUSD", "BUSD", "BTC", "ETH", "BNB", "EUR", "TRY", "USD")

#: Fallback start prices for the synthetic walk, roughly log-uniform across the
#: market caps the bot actually trades. Unknown symbols get a deterministic one.
_SYNTHETIC_START_PRICE = {
    "BTC": 68_000.0,
    "ETH": 3_500.0,
    "BNB": 580.0,
    "SOL": 165.0,
    "XRP": 0.62,
    "ADA": 0.48,
    "DOGE": 0.14,
    "LINK": 16.5,
    "AVAX": 36.0,
    "LTC": 85.0,
    "DOT": 7.2,
    "MATIC": 0.72,
    "TRX": 0.12,
    "ATOM": 9.0,
    "UNI": 9.5,
    "NEAR": 5.5,
    "APT": 9.0,
    "ARB": 1.05,
    "OP": 2.3,
    "FIL": 5.6,
}


class FetchError(RuntimeError):
    """A symbol could not be downloaded (retries exhausted or fatal HTTP error)."""


# ---------------------------------------------------------------------------
# Symbol / interval / time helpers
# ---------------------------------------------------------------------------


def parse_interval_ms(interval: str) -> int:
    """Convert a Binance interval (``30m``, ``1h``, ``4h``, ``1d``) to milliseconds."""
    text = (interval or "").strip().lower()
    if text.endswith("mo"):  # Binance month interval, e.g. "1mo"
        body, factor = text[:-2], 30 * MS_PER_DAY
    else:
        unit = text[-1:]
        if unit == "s":
            body, factor = text[:-1], MS_PER_SECOND
        elif unit == "m":
            body, factor = text[:-1], MS_PER_MINUTE
        elif unit == "h":
            body, factor = text[:-1], MS_PER_HOUR
        elif unit == "d":
            body, factor = text[:-1], MS_PER_DAY
        elif unit == "w":
            body, factor = text[:-1], 7 * MS_PER_DAY
        else:
            body = ""
            factor = 0
    try:
        count = int(body)
    except ValueError as exc:
        raise ValueError(f"unsupported interval {interval!r}; expected e.g. 1m, 30m, 4h, 1d") from exc
    if count <= 0:
        raise ValueError(f"interval count must be positive, got {interval!r}")
    return count * factor


def split_symbol(symbol: str) -> tuple[str, str]:
    """Split ``BTCUSDT`` or ``BTC/USDT`` into ``("BTC", "USDT")``."""
    text = (symbol or "").strip().upper().replace("-", "/").replace("_", "/")
    if not text:
        raise ValueError("empty symbol")
    if "/" in text:
        base, _, quote = text.partition("/")
        if not base or not quote:
            raise ValueError(f"cannot parse symbol {symbol!r}")
        return base, quote
    for quote in _QUOTES:
        if text.endswith(quote) and len(text) > len(quote):
            return text[: -len(quote)], quote
    # No recognisable quote suffix: treat the whole thing as a base asset.
    return text, "USD"


def pair_label(symbol: str) -> str:
    """Display label used across the bot: ``BTCUSDT`` -> ``BTC/USD``.

    Roostoo quotes everything against ``USD``, so the Binance ``USDT`` leg is
    intentionally collapsed to ``USD`` for the label and the file name. USDT is
    a close-but-not-perfect USD proxy -- see the module docstring caveat.
    """
    base, quote = split_symbol(symbol)
    if quote in ("USDT", "USDC", "BUSD", "FDUSD", "TUSD"):
        quote = "USD"
    return f"{base}/{quote}"


def file_stem(symbol: str, interval: str) -> str:
    """``BTCUSDT`` + ``30m`` -> ``BTC-USD_30m`` (the CSV stem)."""
    return f"{pair_label(symbol).replace('/', '-')}_{interval.strip().lower()}"


def csv_path(out_dir: Path, symbol: str, interval: str) -> Path:
    return Path(out_dir) / f"{file_stem(symbol, interval)}.csv"


def to_ms(moment: datetime) -> int:
    """Aware or naive datetime -> epoch milliseconds (naive is read as UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def parse_iso_date(text: str, *, end_of_day: bool = False) -> datetime:
    """Accept ``YYYY-MM-DD`` or a full ISO-8601 timestamp; assume UTC when naive."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty date")
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        try:
            moment = datetime.combine(date.fromisoformat(raw), datetime.min.time())
        except ValueError as exc:
            raise ValueError(f"cannot parse date {text!r}; use YYYY-MM-DD or ISO-8601") from exc
        if end_of_day:
            moment = moment + timedelta(days=1) - timedelta(milliseconds=1)
        moment = moment.replace(tzinfo=timezone.utc)
        return moment
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def iso_utc(ts_ms: int) -> str:
    """Epoch milliseconds -> ``2024-05-01T00:00:00Z``."""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_ts(ts_ms: int) -> str:
    return iso_utc(ts_ms)


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def write_csv(path: Path, rows: Iterable[Iterable]) -> int:
    """Write bars sorted ascending and de-duplicated on ``ts_ms``.

    Later duplicates win, which makes a re-run over a partially written file
    idempotent. Returns the number of rows written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deduped: dict[int, list] = {}
    for row in rows:
        values = list(row)
        ts_ms = int(values[0])
        deduped[ts_ms] = [
            ts_ms,
            float(values[1]),
            float(values[2]),
            float(values[3]),
            float(values[4]),
            float(values[5]),
            float(values[6]),
            int(values[7]),
        ]
    ordered = [deduped[key] for key in sorted(deduped)]
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for row in ordered:
            writer.writerow(
                [
                    row[0],
                    f"{row[1]:.8f}",
                    f"{row[2]:.8f}",
                    f"{row[3]:.8f}",
                    f"{row[4]:.8f}",
                    f"{row[5]:.8f}",
                    f"{row[6]:.8f}",
                    row[7],
                ]
            )
    tmp.replace(path)
    return len(ordered)


def read_csv(path: Path) -> list[dict]:
    """Read a bar CSV back as a list of dicts with numeric values."""
    rows: list[dict] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in CSV_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
        for record in reader:
            rows.append(
                {
                    "ts_ms": int(record["ts_ms"]),
                    "open": float(record["open"]),
                    "high": float(record["high"]),
                    "low": float(record["low"]),
                    "close": float(record["close"]),
                    "volume": float(record["volume"]),
                    "quote_volume": float(record["quote_volume"]),
                    "trades": int(float(record["trades"])),
                }
            )
    return rows


def verify_csv(path: Path) -> tuple[bool, list[str], dict]:
    """Re-read a written CSV and check schema, ordering and OHLC consistency.

    Returns ``(ok, problems, stats)``. Never raises on bad data -- the caller
    decides the exit code.
    """
    problems: list[str] = []
    stats: dict = {"rows": 0, "first_ts_ms": None, "last_ts_ms": None, "min_close": None, "max_close": None}
    try:
        rows = read_csv(path)
    except Exception as exc:  # noqa: BLE001 - surfaced as a verification problem
        return False, [f"cannot read {path}: {exc}"], stats

    stats["rows"] = len(rows)
    if not rows:
        return False, [f"{path}: no data rows"], stats

    stats["first_ts_ms"] = rows[0]["ts_ms"]
    stats["last_ts_ms"] = rows[-1]["ts_ms"]
    stats["min_close"] = min(row["close"] for row in rows)
    stats["max_close"] = max(row["close"] for row in rows)

    seen: set[int] = set()
    previous: Optional[int] = None
    for index, row in enumerate(rows):
        ts_ms = row["ts_ms"]
        if ts_ms in seen:
            problems.append(f"row {index + 2}: duplicate ts_ms {ts_ms} ({_fmt_ts(ts_ms)})")
        seen.add(ts_ms)
        if previous is not None and ts_ms <= previous:
            problems.append(f"row {index + 2}: ts_ms not ascending ({previous} -> {ts_ms})")
        previous = ts_ms

        high, low, open_, close = row["high"], row["low"], row["open"], row["close"]
        for name, value in (("open", open_), ("high", high), ("low", low), ("close", close)):
            if not math.isfinite(value) or value <= 0:
                problems.append(f"row {index + 2}: {name}={value!r} is not a positive finite price")
        if high < low:
            problems.append(f"row {index + 2}: high {high} < low {low}")
        if high < max(open_, close):
            problems.append(f"row {index + 2}: high {high} < max(open, close) {max(open_, close)}")
        if low > min(open_, close):
            problems.append(f"row {index + 2}: low {low} > min(open, close) {min(open_, close)}")
        if row["volume"] < 0 or row["quote_volume"] < 0:
            problems.append(f"row {index + 2}: negative volume")
        if len(problems) > 40:
            problems.append("... further problems suppressed")
            break

    return (not problems), problems, stats


def report_verification(path: Path, ok: bool, problems: list[str], stats: dict) -> None:
    print(f"  verify {path}")
    print(f"    rows        : {stats['rows']}")
    print(f"    first ts    : {_fmt_ts(stats['first_ts_ms'])}" if stats["first_ts_ms"] is not None else "    first ts    : -")
    print(f"    last ts     : {_fmt_ts(stats['last_ts_ms'])}" if stats["last_ts_ms"] is not None else "    last ts     : -")
    if stats["min_close"] is not None:
        print(f"    min close   : {stats['min_close']:.8f}")
        print(f"    max close   : {stats['max_close']:.8f}")
    print(f"    consistent  : {'yes' if ok else 'NO'}")
    for problem in problems:
        print(f"    ! {problem}")


# ---------------------------------------------------------------------------
# Binance source
# ---------------------------------------------------------------------------


def _retry_after_seconds(headers) -> Optional[float]:
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None


def _sleep_backoff(attempt: int, retry_after: Optional[float], quiet: bool, why: str) -> None:
    delay = min(MAX_BACKOFF_SEC, 2.0 ** (attempt - 1))
    if retry_after is not None:
        delay = min(MAX_BACKOFF_SEC, max(delay, retry_after))
    if not quiet:
        print(f"      {why}; sleeping {delay:.2f}s (attempt {attempt}/{MAX_ATTEMPTS})", flush=True)
    time.sleep(delay)


def fetch_klines_page(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    timeout: float = 20.0,
    quiet: bool = False,
) -> list[list]:
    """One ``/api/v3/klines`` request with retry/backoff. Raises FetchError."""
    query = urllib.parse.urlencode(
        {
            "symbol": symbol.upper(),
            "interval": interval,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "limit": PAGE_LIMIT,
        }
    )
    url = f"{BINANCE_KLINES_URL}?{query}"
    last_error = "unknown error"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"User-Agent": "roostoo-quant-bot/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            if not isinstance(payload, list):
                raise FetchError(f"unexpected klines payload type {type(payload).__name__}")
            return payload
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code in (429, 418):
                # Rate limited / banned: honour Retry-After and slow down hard.
                _sleep_backoff(attempt, _retry_after_seconds(exc.headers), quiet, last_error)
                continue
            if 500 <= exc.code < 600:
                _sleep_backoff(attempt, _retry_after_seconds(exc.headers), quiet, last_error)
                continue
            # 400/404/451... are permanent for this request: fail fast, no retry.
            raise FetchError(f"{symbol}: {last_error} (not retryable)") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _sleep_backoff(attempt, None, quiet, last_error)
            continue
        except json.JSONDecodeError as exc:
            last_error = f"bad JSON: {exc}"
            _sleep_backoff(attempt, None, quiet, last_error)
            continue

    raise FetchError(f"{symbol}: giving up after {MAX_ATTEMPTS} attempts ({last_error})")


def fetch_symbol_history(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    sleep_sec: float = 0.25,
    timeout: float = 20.0,
    quiet: bool = False,
) -> list[list]:
    """Page through Binance klines until ``end_ms`` (or the venue runs dry)."""
    step = parse_interval_ms(interval)
    rows: list[list] = []
    cursor = start_ms
    seen_last: Optional[int] = None
    page = 0

    while cursor < end_ms:
        page += 1
        batch = fetch_klines_page(symbol, interval, cursor, end_ms, timeout=timeout, quiet=quiet)
        if not batch:
            if not quiet:
                print(f"      page {page}: no more data", flush=True)
            break

        for kline in batch:
            # Binance kline: [openTime, open, high, low, close, volume,
            #                 closeTime, quoteVolume, trades, ...]
            rows.append(
                [
                    int(kline[0]),
                    float(kline[1]),
                    float(kline[2]),
                    float(kline[3]),
                    float(kline[4]),
                    float(kline[5]),
                    float(kline[7]),
                    int(kline[8]),
                ]
            )

        last_open = int(batch[-1][0])
        if seen_last is not None and last_open <= seen_last:
            # Defensive: never loop forever if the venue repeats a page.
            raise FetchError(f"{symbol}: pagination stalled at {_fmt_ts(last_open)}")
        seen_last = last_open

        if not quiet:
            print(f"      page {page}: {len(batch)} bars -> {_fmt_ts(last_open)}", flush=True)

        if len(batch) < PAGE_LIMIT:
            break  # short page == end of available history
        cursor = last_open + step
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    # Only keep bars whose open time falls inside the requested window.
    return [row for row in rows if start_ms <= row[0] <= end_ms]


# ---------------------------------------------------------------------------
# Synthetic source (no network)
# ---------------------------------------------------------------------------


def _synthetic_start_price(base: str, seed: int) -> float:
    known = _SYNTHETIC_START_PRICE.get(base.upper())
    if known is not None:
        return known
    # Deterministic pseudo-price for unknown tickers: log-uniform $0.01-$1000.
    rng = random.Random(f"{seed}:{base.upper()}")
    return round(math.exp(rng.uniform(math.log(0.01), math.log(1000.0))), 6)


def _price_decimals(price: float) -> int:
    if price >= 1000:
        return 2
    if price >= 1:
        return 4
    if price >= 0.01:
        return 6
    return 8


def generate_synthetic_bars(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    seed: int = 7,
) -> list[list]:
    """Deterministic OHLCV bars from a seeded geometric random walk.

    The walk is deliberately *not* a pure random walk: it mean-reverts toward a
    slowly drifting anchor, so a z-score/SMA mean-reversion strategy has
    something real to find and a pure trend strategy does not get a free lunch.
    Volatility clusters (GARCH-ish) so ATR/ADX are not constant, and volume is
    lognormal and correlated with absolute return.

    Internally consistent by construction:
    ``high >= max(open, close)``, ``low <= min(open, close)``,
    ``quote_volume = volume * typical_price``.
    """
    step = parse_interval_ms(interval)
    steps = max(0, (end_ms - start_ms) // step)
    if steps <= 0:
        return []

    base, _ = split_symbol(symbol)
    rng = random.Random(f"{seed}:{symbol.upper()}:{interval.lower()}:{start_ms}")
    decimals = _price_decimals(_synthetic_start_price(base, seed))

    # Per-bar volatility ~ scaled from a 1.1%/day-ish anchor. 30m bars get
    # ~1.1%/sqrt(48) ~ 0.16% * the vol_multiplier, which keeps ATR sane.
    bars_per_day = max(1.0, MS_PER_DAY / step)
    base_sigma = 0.011 / math.sqrt(bars_per_day)

    price = _synthetic_start_price(base, seed)
    anchor = price
    anchor_drift = rng.uniform(-0.00015, 0.00025)  # slow regime drift per bar
    reversion = rng.uniform(0.010, 0.045)  # pull toward the anchor
    vol_multiplier = 1.0
    notional_scale = math.exp(rng.uniform(14.0, 17.5))  # ~$1.2M-$40M per bar

    rows: list[list] = []
    for index in range(int(steps)):
        ts_ms = start_ms + index * step

        # Volatility clustering: mean-reverting log-volatility.
        vol_multiplier += 0.12 * (1.0 - vol_multiplier) + rng.gauss(0.0, 0.16)
        vol_multiplier = min(4.0, max(0.25, vol_multiplier))
        sigma = base_sigma * vol_multiplier

        anchor *= 1.0 + anchor_drift
        anchor *= 1.0 + rng.gauss(0.0, base_sigma * 0.35)
        if index and index % 500 == 0:
            # Occasionally shift regime so multi-month backtests see trends.
            anchor_drift = rng.uniform(-0.00035, 0.00045)

        log_pull = math.log(anchor / price) if price > 0 else 0.0
        shock = rng.gauss(0.0, sigma)
        # Student-t-ish fat tails: crypto does not move in Gaussians.
        if rng.random() < 0.02:
            shock *= 2.5
        new_price = price * math.exp(reversion * log_pull + shock)
        new_price = max(new_price, price * 0.80)  # no >20% single-bar collapse

        open_ = price
        close = new_price
        span = abs(close - open_)
        wick = sigma * open_ * rng.uniform(0.20, 1.30)
        high = max(open_, close) + wick
        low = min(open_, close) - wick
        # A small fraction of bars are near-marubozu (tiny wicks).
        if rng.random() < 0.15:
            high = max(open_, close) * (1.0 + abs(rng.gauss(0.0, sigma * 0.12)))
            low = min(open_, close) * (1.0 - abs(rng.gauss(0.0, sigma * 0.12)))
        high = max(high, max(open_, close))
        low = min(low, min(open_, close))
        low = max(low, 1e-12)

        typical = (high + low + close) / 3.0
        volume = notional_scale / max(typical, 1e-12)
        volume *= math.exp(rng.gauss(0.0, 0.45)) * (1.0 + 6.0 * min(1.0, span / max(open_, 1e-12)))
        quote_volume = volume * typical
        # Trade count implied by an average trade size of $500-$20k.
        avg_trade_notional = math.exp(rng.uniform(math.log(500.0), math.log(20_000.0)))
        trades = max(1, min(int(quote_volume / avg_trade_notional), 5_000_000))

        price = close
        rows.append(
            [
                ts_ms,
                round(open_, decimals),
                round(high, decimals),
                round(low, decimals),
                round(close, decimals),
                round(volume, 8),
                round(quote_volume, 8),
                trades,
            ]
        )

    # Rounding can in principle clip a wick; re-assert consistency defensively.
    for row in rows:
        row[2] = max(row[2], row[1], row[4])
        row[3] = min(row[3], row[1], row[4])
    return rows


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _parse_symbols(raw: str) -> list[str]:
    parts = [part.strip() for part in (raw or "").replace(";", ",").split(",")]
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        base, quote = split_symbol(part)
        out.append(f"{base}{quote}")
    # De-duplicate, keep order.
    return list(dict.fromkeys(out))


def resolve_window(args: argparse.Namespace, now_ms: int, interval: str) -> tuple[int, int]:
    """Work out ``[start_ms, end_ms]`` from ``--start``/``--end``/``--days``."""
    if args.end:
        try:
            end_ms = to_ms(parse_iso_date(args.end, end_of_day=True))
        except ValueError as exc:
            raise SystemExit(f"--end: {exc}") from exc
    else:
        end_ms = now_ms

    if args.start:
        try:
            start_ms = to_ms(parse_iso_date(args.start))
        except ValueError as exc:
            raise SystemExit(f"--start: {exc}") from exc
    else:
        start_ms = end_ms - int(args.days) * MS_PER_DAY

    if start_ms >= end_ms:
        raise SystemExit("start must be before end (check --start/--end/--days)")
    # Align to the interval grid so pagination never straddles a bar.
    step = parse_interval_ms(interval)
    start_ms = (start_ms // step) * step
    return start_ms, end_ms


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch_history.py",
        description="Download 30m OHLCV bars for the Roostoo bot (Binance public REST or synthetic).",
        epilog=(
            "Roostoo has no candle endpoint, so bars come from Binance. Live orders still "
            "execute on Roostoo -- always sanity-check the two price feeds against each other."
        ),
    )
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS, help=f"comma-separated symbols (default: {DEFAULT_SYMBOLS})")
    parser.add_argument("--interval", default="30m", help="bar interval (default: 30m)")
    parser.add_argument("--days", type=float, default=180, help="days of history ending now (default: 180)")
    parser.add_argument("--start", default=None, help="ISO start date, overrides --days")
    parser.add_argument("--end", default=None, help="ISO end date (default: now)")
    parser.add_argument("--out-dir", default="data", help="output directory (default: data)")
    parser.add_argument("--source", choices=("binance", "synthetic"), default="binance", help="data source (default: binance)")
    parser.add_argument("--sleep", type=float, default=0.25, help="seconds between requests (default: 0.25)")
    parser.add_argument("--seed", type=int, default=7, help="seed for the synthetic generator (default: 7)")
    parser.add_argument("--verify", action="store_true", help="re-read each CSV and assert schema/OHLC consistency")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output (errors still print)")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    quiet = bool(args.quiet)

    try:
        interval_step = parse_interval_ms(args.interval)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print("error: no symbols given", file=sys.stderr)
        return 2

    now_ms = int(time.time() * 1000)
    start_ms, end_ms = resolve_window(args, now_ms, args.interval)
    out_dir = Path(args.out_dir)

    if not quiet:
        print(f"source      : {args.source}")
        print(f"interval    : {args.interval} ({interval_step} ms)")
        print(f"window      : {_fmt_ts(start_ms)} -> {_fmt_ts(end_ms)}")
        print(f"symbols     : {', '.join(symbols)}")
        print(f"out dir     : {out_dir.resolve()}")

    written: list[tuple[str, Path, int]] = []
    failures: list[tuple[str, str]] = []
    verifications: list[tuple[Path, bool, list[str], dict]] = []

    for symbol in symbols:
        label = pair_label(symbol)
        path = csv_path(out_dir, symbol, args.interval)
        if not quiet:
            print(f"\n[{label}] {symbol} ({args.source})")

        try:
            if args.source == "synthetic":
                rows = generate_synthetic_bars(symbol, args.interval, start_ms, end_ms, seed=args.seed)
            else:
                rows = fetch_symbol_history(
                    symbol,
                    args.interval,
                    start_ms,
                    end_ms,
                    sleep_sec=max(0.0, args.sleep),
                    quiet=quiet,
                )
        except (FetchError, ValueError) as exc:
            failures.append((label, str(exc)))
            print(f"  FAILED {label}: {exc}", file=sys.stderr)
            continue

        if not rows:
            failures.append((label, "no bars returned"))
            print(f"  FAILED {label}: no bars returned for the requested window", file=sys.stderr)
            continue

        count = write_csv(path, rows)
        written.append((label, path, count))
        if not quiet:
            print(f"  wrote {count} bars -> {path}")

        if args.verify:
            ok, problems, stats = verify_csv(path)
            verifications.append((path, ok, problems, stats))
            if not quiet:
                report_verification(path, ok, problems, stats)
            if not ok:
                failures.append((label, f"verification failed ({len(problems)} problem(s))"))

    if not quiet or args.verify:
        print("\n--- summary ---")
    for label, path, count in written:
        print(f"  ok    {label:10s} {count:6d} bars  {path}")
    for label, reason in failures:
        print(f"  fail  {label:10s} {reason}", file=sys.stderr)

    if not written:
        print("error: every symbol failed", file=sys.stderr)
        return 1
    if failures:
        # Partial success is exit 0 (some symbols failed), per the CLI contract,
        # but a verification failure still signals a corrupt artefact.
        if any("verification failed" in reason for _, reason in failures):
            return 1
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
