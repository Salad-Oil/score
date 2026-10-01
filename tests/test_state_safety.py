"""Regression tests for the state-safety, risk-budget and secret-scanning fixes.

These are the tests whose absence let a bad startup wipe the position book, let a
malformed balance response empty it, and let an exit fail to free its capital.
Three of the four bugs below could destroy real money or real evidence, and every
one of them lived in a module with **zero** test coverage before.

On scratch directories: `tempfile.mkdtemp` is deliberately not used. It chmods the
new directory to 0700, and in a sandboxed process that sets a DACL the confined
token cannot then write through, so every test here would fail for an
environmental reason. A plain `mkdir` under the system temp directory works
everywhere including CI. Set ``ROOSTOO_TEST_SCRATCH`` to redirect the base if the
system temp area is itself read-only.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scan_secrets  # noqa: E402  (deliberately after the sys.path fix-up)

from roostoo.config import Config  # noqa: E402
from roostoo.engine import TradingEngine  # noqa: E402
from roostoo.models import (  # noqa: E402
    ExchangeInfo,
    Position,
    Ticker,
    TradePair,
    WalletBalance,
)
from roostoo.risk import PortfolioView, PositionBook, PositionSizer, RiskManager  # noqa: E402
from roostoo.strategies.base import ENTER_LONG, EXIT_LONG, Signal  # noqa: E402
from roostoo.universe import RoostooDepthProvider  # noqa: E402

PAIR = "BTC/USD"
#: A credential-shaped value that is *not* on the allowlist, assembled from two
#: fragments so the literal never appears as one token in this file. That is not
#: paranoia for its own sake: `scripts/scan_secrets.py` scans the worktree, so a
#: single-line 60-character credential here would make every commit fail the
#: pre-commit hook -- and the tempting "fix" would be to allowlist this file,
#: which is exactly the hole that let a real key reach a public repository.
_FAKE_HEAD = "Qw3rTy9ZxCv2Bn4Mk6Lp8"
_FAKE_TAIL = "Rt1Yu3Io5Pa7Sd9Fg2Hj4Kl6Zx8Cv0Bn"
FAKE_SECRET = _FAKE_HEAD + _FAKE_TAIL


@contextlib.contextmanager
def scratch_dir() -> Iterator[Path]:
    """A writable scratch directory that works under a restricted sandbox."""
    base = Path(os.environ.get("ROOSTOO_TEST_SCRATCH") or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"roostoo-test-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_config(directory: Path) -> Config:
    cfg = Config()
    cfg.mock = True
    cfg.api_key = "MOCK"
    cfg.secret_key = "MOCK"
    cfg.journal_dir = str(directory)
    cfg.log_dir = str(directory)
    return cfg


def trade_pair(pair: str = PAIR) -> TradePair:
    return TradePair(
        pair=pair,
        coin=pair.split("/")[0],
        unit="USD",
        can_trade=True,
        price_precision=2,
        amount_precision=6,
        min_order=10.0,
    )


def ticker(pair: str, mid: float = 100.0, volume: float = 1_000.0) -> Ticker:
    return Ticker(
        pair=pair,
        last=mid,
        max_bid=mid,
        min_ask=mid,
        change_24h=0.0,
        coin_volume=0.0,
        unit_volume=volume,
        server_time_ms=0,
    )


def write_book(path: Path, positions: dict) -> None:
    path.write_text(json.dumps({"positions": positions}), encoding="utf-8")


def book_fixture(quantity: float = 0.5, stop: float = 57_000.0) -> dict:
    return {
        "quantity": quantity,
        "avg_price": 60_000.0,
        "mark_price": 61_000.0,
        "is_short": False,
        "collateral": 0.0,
        "stop_price": stop,
        "take_profit_price": None,
        "peak_price": 62_000.0,
        "opened_ts_ms": 1_700_000_000_000,
    }


def write_state(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "saved_ms": 1_700_000_000_000,
                "risk": {
                    "peak_nav": 123_456.0,
                    "day_id": 19_700,
                    "day_start_nav": 120_000.0,
                    "daily_halt": False,
                    "halted": True,
                    "halt_reason": "kill switch test",
                    "last_exit_bar": {"ETH/USD": 41},
                },
                "last_decision_bar": 56_666,
                "stats": {},
                "universe": [PAIR],
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
class TestPositionBookCannotBeClobbered(unittest.TestCase):
    """``save()`` must never let an unloaded, empty book overwrite a real one."""

    def test_save_refuses_to_clobber_a_book_that_was_never_loaded(self) -> None:
        with scratch_dir() as d:
            path = d / "positions.json"
            write_book(path, {PAIR: book_fixture()})
            before = path.read_bytes()

            # A fresh book, exactly as TradingEngine constructs one. `load()` is
            # never called, so `positions` is empty by default.
            PositionBook(path).save()

            self.assertEqual(path.read_bytes(), before, "an unloaded empty book overwrote a real one")

    def test_save_writes_normally_once_load_has_run(self) -> None:
        with scratch_dir() as d:
            path = d / "positions.json"
            write_book(path, {PAIR: book_fixture()})
            book = PositionBook(path)
            self.assertTrue(book.load())
            self.assertIn(PAIR, book.positions)

            # Closing the last position is a legitimate reason to persist an
            # empty book: load() ran, so the emptiness is real information.
            book.positions.clear()
            book.save()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["positions"], {})

    def test_save_works_when_no_book_exists_yet(self) -> None:
        with scratch_dir() as d:
            path = d / "positions.json"
            PositionBook(path).save()
            self.assertTrue(path.is_file())

    def test_a_corrupt_book_is_not_clobbered_either(self) -> None:
        with scratch_dir() as d:
            path = d / "positions.json"
            path.write_text("{ this is not json", encoding="utf-8")
            before = path.read_bytes()

            book = PositionBook(path)
            self.assertFalse(book.load())  # refuses to guess
            book.save()

            # Unreadable is not the same as empty: leave it for a human.
            self.assertEqual(path.read_bytes(), before)


# ---------------------------------------------------------------------------
class FlakyStartupClient:
    """Fails during bootstrap exactly the way a transient network error does."""

    def sync_time(self) -> int:
        raise RuntimeError("simulated transient network failure")

    def exchange_info(self) -> ExchangeInfo:
        raise AssertionError("must not be reached once sync_time has failed")

    def ticker(self, pair: str | None = None) -> dict:
        return {}

    def balance(self) -> dict:
        return {}

    def pending_count(self) -> tuple[int, dict]:
        return 0, {}

    def place_order(self, *args, **kwargs):  # pragma: no cover - no orders expected
        raise AssertionError("no orders expected")

    def query_orders(self, **kwargs) -> list:
        return []

    def cancel_order(self, *args, **kwargs) -> list:
        return []

    def short_positions(self) -> list:
        return []


class TestFailedStartupIsNonDestructive(unittest.TestCase):
    """The bug this pins: a startup blip used to erase the whole book."""

    def test_run_failure_leaves_the_stored_state_untouched(self) -> None:
        with scratch_dir() as d:
            book_path = d / "positions.json"
            state_path = d / "engine_state.json"
            write_book(book_path, {PAIR: book_fixture()})
            write_state(state_path)
            book_before = book_path.read_bytes()
            state_before = state_path.read_bytes()

            engine = TradingEngine(make_config(d), client=FlakyStartupClient())
            try:
                engine.run(max_cycles=1)
                self.fail("run() should have propagated the startup failure")
            except RuntimeError:
                pass
            finally:
                # Exactly what run_live.py does from its `finally:` block.
                engine.shutdown(flatten=False)

            self.assertEqual(book_path.read_bytes(), book_before, "the position book was wiped by a failed startup")
            self.assertEqual(state_path.read_bytes(), state_before, "the risk state was reset by a failed startup")

    def test_the_kill_switch_survives_a_failed_startup(self) -> None:
        """`halted` must not be cleared by a blip: that would un-halt a halted bot."""
        with scratch_dir() as d:
            write_book(d / "positions.json", {PAIR: book_fixture()})
            write_state(d / "engine_state.json")

            engine = TradingEngine(make_config(d), client=FlakyStartupClient())
            try:
                engine.run(max_cycles=1)
            except RuntimeError:
                pass
            finally:
                engine.shutdown(flatten=False)

            restored = json.loads((d / "engine_state.json").read_text(encoding="utf-8"))
            self.assertTrue(restored["risk"]["halted"])
            self.assertEqual(restored["risk"]["peak_nav"], 123_456.0)
            self.assertEqual(restored["risk"]["last_exit_bar"], {"ETH/USD": 41})

    def test_persist_is_gated_before_bootstrap(self) -> None:
        with scratch_dir() as d:
            engine = TradingEngine(make_config(d), client=FlakyStartupClient())
            engine._persist()
            self.assertFalse((d / "positions.json").exists())
            self.assertFalse((d / "engine_state.json").exists())


# ---------------------------------------------------------------------------
class NoUsdBalanceClient:
    """A partial balance snapshot: assets are reported, the quote currency is not."""

    def sync_time(self) -> int:
        return 0

    def exchange_info(self) -> ExchangeInfo:
        return ExchangeInfo(is_running=True, initial_wallet={"USD": 100_000.0}, pairs={PAIR: trade_pair()})

    def ticker(self, pair: str | None = None) -> dict:
        return {PAIR: ticker(PAIR)}

    def balance(self) -> dict:
        # No "USD" row at all. `_cash_usd` would return 0.0, NAV would collapse to
        # the position marks, and the drawdown check would trip the permanent
        # kill switch; reconciliation would also read every missing row as "the
        # exchange holds nothing" and delete the book.
        return {"BTC": WalletBalance(asset="BTC", free=0.5, locked=0.0)}

    def pending_count(self) -> tuple[int, dict]:
        return 0, {}

    def place_order(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("no orders expected")

    def query_orders(self, **kwargs) -> list:
        return []

    def cancel_order(self, *args, **kwargs) -> list:
        return []

    def short_positions(self) -> list:
        return []


class TestIncompleteBalanceSnapshotIsNotActedOn(unittest.TestCase):
    def _engine(self, directory: Path) -> TradingEngine:
        engine = TradingEngine(make_config(directory), client=NoUsdBalanceClient())
        self.addCleanup(engine.journal.close)  # do not leak the journal handles
        engine.exchange_pairs = {PAIR: trade_pair()}
        engine.book.positions[PAIR] = Position(
            pair=PAIR, quantity=0.5, avg_price=60_000.0, mark_price=61_000.0, stop_price=57_000.0
        )
        return engine

    def test_cycle_is_skipped_and_the_book_is_preserved(self) -> None:
        with scratch_dir() as d:
            engine = self._engine(d)
            engine.step()

            self.assertIn(PAIR, engine.book.positions, "a partial balance snapshot deleted the position")
            self.assertEqual(engine.book.positions[PAIR].stop_price, 57_000.0)
            self.assertFalse(engine.risk.halted, "a partial snapshot tripped the permanent kill switch")

    def test_the_skip_is_journalled(self) -> None:
        with scratch_dir() as d:
            engine = self._engine(d)
            engine.step()

            from roostoo.journal import read_events

            events = []
            for path in sorted(Path(d).glob("decisions-*.jsonl")):
                events += read_events(path)
            self.assertTrue(
                any(e.get("kind") == "error" and "USD" in str(e.get("message", "")) for e in events),
                f"expected a journalled reason for the skipped cycle, got {[e.get('kind') for e in events]}",
            )


# ---------------------------------------------------------------------------
class TestGrossBudgetIsFreedBySameBatchExits(unittest.TestCase):
    """Rule 10 freed the slot but Rule 9 kept rejecting the entry."""

    HELD = ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD")
    NEW = "ADA/USD"

    def _view(self) -> PortfolioView:
        # 4 x 15% == exactly the 60% gross cap.
        positions = {
            p: Position(pair=p, quantity=150.0, avg_price=100.0, mark_price=100.0) for p in self.HELD
        }
        return PortfolioView(nav=100_000.0, cash_usd=40_000.0, positions=positions)

    def test_an_exit_earlier_in_the_batch_frees_capital_for_a_later_entry(self) -> None:
        cfg = make_config(Path(tempfile.gettempdir()))
        risk = RiskManager(cfg, PositionSizer(cfg))
        tickers = {p: ticker(p) for p in self.HELD + (self.NEW,)}

        decision = risk.evaluate(
            [
                Signal("BTC/USD", EXIT_LONG, reason="stop hit"),
                Signal(self.NEW, ENTER_LONG, meta={"atr": 1.0}),
            ],
            view=self._view(),
            tickers=tickers,
            now_ms=0,
            bar_idx=1,
        )
        approved = {a.pair: a.action for a in decision.approved}
        self.assertEqual(approved.get("BTC/USD"), EXIT_LONG)
        self.assertEqual(
            approved.get(self.NEW),
            ENTER_LONG,
            f"the exit freed a slot and 15k of gross, but the entry was still rejected: {decision.rejected}",
        )

    def test_a_batch_cannot_overshoot_the_gross_cap(self) -> None:
        """The fix must free budget *without* letting a batch exceed Rule 9."""
        cfg = make_config(Path(tempfile.gettempdir()))
        risk = RiskManager(cfg, PositionSizer(cfg))
        pairs = ["A/USD", "B/USD", "C/USD", "D/USD", "E/USD"]
        tickers = {p: ticker(p) for p in pairs}
        view = PortfolioView(nav=100_000.0, cash_usd=100_000.0, positions={})

        decision = risk.evaluate(
            [Signal(p, ENTER_LONG, meta={"atr": 1.0}) for p in pairs],
            view=view,
            tickers=tickers,
            now_ms=0,
            bar_idx=1,
        )
        total = sum(a.notional for a in decision.approved if a.is_entry)
        self.assertLessEqual(total, 100_000.0 * cfg.max_gross_exposure + 1e-6)
        self.assertLessEqual(len(decision.approved), cfg.max_open_positions)


# ---------------------------------------------------------------------------
class TestInFlightOrdersReserveCapital(unittest.TestCase):
    """A pending order is not a position yet, but its capital must be committed.

    Without this, two consecutive bars each see a flat account and each size a
    full position for the same pair, ending at twice the per-pair cap.
    """

    def _evaluate(self, committed_pairs: set[str]):
        cfg = make_config(Path(tempfile.gettempdir()))
        risk = RiskManager(cfg, PositionSizer(cfg))
        tickers = {PAIR: ticker(PAIR)}
        view = PortfolioView(nav=100_000.0, cash_usd=100_000.0, positions={})
        return risk.evaluate(
            [Signal(PAIR, ENTER_LONG, meta={"atr": 1.0})],
            view=view,
            tickers=tickers,
            now_ms=0,
            bar_idx=1,
            committed_pairs=committed_pairs,
            committed_notional=15_000.0 if committed_pairs else 0.0,
        )

    def test_an_entry_is_approved_when_nothing_is_in_flight(self) -> None:
        decision = self._evaluate(set())
        self.assertEqual([a.pair for a in decision.approved], [PAIR])

    def test_an_entry_is_refused_for_a_pair_already_in_flight(self) -> None:
        decision = self._evaluate({PAIR})
        self.assertEqual(decision.approved, [], "a second full-size entry was approved for an in-flight pair")
        self.assertTrue(
            any("already holding" in reason for _, reason in decision.rejected),
            f"expected a reservation rejection, got {decision.rejected}",
        )


# ---------------------------------------------------------------------------
class RecordingTransport:
    """Captures the exact request the depth provider sends."""

    def __init__(self, payload: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self.payload = payload if payload is not None else {"Bids": [[99.0, 2.0]], "Asks": [[101.0, 2.0]]}

    def send(self, method, url, body, headers, timeout):
        self.calls.append((method, url, body, dict(headers), timeout))
        return 200, json.dumps(self.payload)


class SigningClientStub:
    base_url = "https://example.invalid"

    def __init__(self) -> None:
        self.signed: list[str | None] = []

    def timestamp_ms(self) -> str:
        return "1580774512000"

    def sign_headers(self, params, canonical=None):
        self.signed.append(canonical)
        return {"RST-API-KEY": "KEY", "MSG-SIGNATURE": "SIGNED"}


class TestDepthProviderSignsItsRequests(unittest.TestCase):
    """It used to probe for a private `_sign_headers` that never existed."""

    def test_signature_covers_the_exact_query_string_that_is_sent(self) -> None:
        client = SigningClientStub()
        transport = RecordingTransport()
        provider = RoostooDepthProvider(client, "/v3/depth", transport=transport, timeout=5.0)

        snapshot = provider.snapshot(PAIR, 0.005)

        self.assertIsNotNone(snapshot)
        self.assertEqual(len(transport.calls), 1)
        _, url, _, headers, _ = transport.calls[0]
        self.assertEqual(headers.get("MSG-SIGNATURE"), "SIGNED", "the request was sent unsigned")
        self.assertEqual(headers.get("RST-API-KEY"), "KEY")
        # The signed string must be byte-identical to the query actually sent,
        # otherwise the server rebuilds a different canonical string and rejects it.
        self.assertEqual(client.signed[0], url.split("?", 1)[1])

    def test_a_client_without_signing_still_works(self) -> None:
        class UnsignedClient:
            base_url = "https://example.invalid"

        transport = RecordingTransport()
        provider = RoostooDepthProvider(UnsignedClient(), "/v3/depth", transport=transport, timeout=5.0)
        self.assertIsNotNone(provider.snapshot(PAIR, 0.005))
        self.assertNotIn("MSG-SIGNATURE", transport.calls[0][3])


# ---------------------------------------------------------------------------
class TestSecretScanner(unittest.TestCase):
    """The old guard read filenames; this one reads bytes."""

    def test_a_hardcoded_credential_is_caught(self) -> None:
        findings = scan_secrets.scan_text(f'API_KEY = "{FAKE_SECRET}"', "roostoo/whatever.py")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "credential-assignment")

    def test_the_finding_never_echoes_the_secret(self) -> None:
        """A redaction that truncates first would print the head of the key."""
        findings = scan_secrets.scan_text(f'API_KEY = "{FAKE_SECRET}"', "x.py")
        self.assertNotIn(FAKE_SECRET, findings[0].excerpt)
        self.assertNotIn(FAKE_SECRET[:16], findings[0].excerpt)
        self.assertIn("<redacted>", findings[0].excerpt)

    def test_the_published_docs_vector_is_allowlisted(self) -> None:
        doc_secret = "S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep"
        self.assertIn(doc_secret, scan_secrets.ALLOWED_VALUES)
        self.assertEqual(scan_secrets.scan_text(f'SECRET_KEY = "{doc_secret}"', "tests/test_client.py"), [])

    def test_placeholders_and_templates_are_not_flagged(self) -> None:
        for line in (
            'API_KEY = "your_api_key_here_placeholder"',
            "ROOSTOO_SECRET_KEY=",
            "SECRET_KEY = ''",
            'api_key = "${ROOSTOO_API_KEY}"',
        ):
            with self.subTest(line=line):
                self.assertEqual(scan_secrets.scan_text(line, "x.py"), [])

    #: Assembled at runtime so this file does not itself contain a literal PEM
    #: header. A literal one is a permanent false positive for the worktree scan
    #: *and* the history scan, and the tempting cure for that is to allowlist
    #: this file -- which is precisely how a real key would get through.
    _PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"

    def test_private_key_blocks_are_caught(self) -> None:
        findings = scan_secrets.scan_text(self._PEM_HEADER, "id_rsa")
        self.assertEqual([f.kind for f in findings], ["private-key-block"])

    def test_the_scanners_own_pattern_list_is_not_a_finding(self) -> None:
        """It contains the word `secret` and a PEM header; it must skip itself."""
        self.assertTrue(scan_secrets._skip("scripts/scan_secrets.py"))
        self.assertEqual(scan_secrets.scan_worktree(), [])


if __name__ == "__main__":
    unittest.main()
