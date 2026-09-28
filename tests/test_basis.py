"""Tests for the cross-venue basis monitor.

Network is never touched: the Binance call goes through an injected transport, and
the arithmetic is exercised as a pure function.
"""

from __future__ import annotations

import json
import unittest

from roostoo.basis import (
    DEFAULT_BASIS_MAX_PCT,
    BasisMonitor,
    BasisReport,
    BasisRow,
    compute_basis,
    resolve_threshold,
)
from roostoo.models import Ticker


def make_ticker(pair: str, mid: float) -> Ticker:
    """A ticker whose bid/ask straddle ``mid`` so ``.mid`` is exact."""
    return Ticker(
        pair=pair,
        last=mid,
        max_bid=mid,
        min_ask=mid,
        change_24h=0.0,
        coin_volume=0.0,
        unit_volume=0.0,
        server_time_ms=0,
    )


class FakeTransport:
    """Returns a canned Binance response and records the URLs it was asked for."""

    def __init__(self, status: int = 200, payload: object = None, raise_error: bool = False) -> None:
        self.status = status
        self.payload = payload if payload is not None else []
        self.raise_error = raise_error
        self.urls: list[str] = []

    def send(self, method, url, body, headers, timeout):  # noqa: D102 - protocol shape
        self.urls.append(url)
        if self.raise_error:
            raise OSError("simulated network failure")
        return self.status, json.dumps(self.payload)


class TestComputeBasis(unittest.TestCase):
    def test_identical_prices_have_zero_basis(self) -> None:
        self.assertEqual(compute_basis(100.0, 100.0), 0.0)

    def test_venue_above_reference_is_positive(self) -> None:
        self.assertAlmostEqual(compute_basis(101.0, 100.0), 0.01, places=9)

    def test_venue_below_reference_is_negative(self) -> None:
        self.assertAlmostEqual(compute_basis(99.0, 100.0), -0.01, places=9)

    def test_unusable_inputs_return_none(self) -> None:
        for venue, reference in ((0.0, 100.0), (100.0, 0.0), (-1.0, 100.0), (100.0, -1.0)):
            self.assertIsNone(compute_basis(venue, reference), msg=f"{venue=} {reference=}")


class TestResolveThreshold(unittest.TestCase):
    def test_explicit_value_wins(self) -> None:
        self.assertAlmostEqual(resolve_threshold("0.002"), 0.002, places=9)

    def test_negative_value_is_made_positive(self) -> None:
        self.assertAlmostEqual(resolve_threshold("-0.002"), 0.002, places=9)

    def test_garbage_falls_back_to_the_default(self) -> None:
        self.assertEqual(resolve_threshold("not-a-number"), DEFAULT_BASIS_MAX_PCT)

    def test_blank_falls_back_to_the_default(self) -> None:
        self.assertEqual(resolve_threshold("   "), DEFAULT_BASIS_MAX_PCT)


class TestBasisRowAndReport(unittest.TestCase):
    def test_unverified_row_is_not_flagged(self) -> None:
        row = BasisRow("BTC/USD", "BTCUSDT", 100.0, None, None)
        self.assertFalse(row.verified)
        self.assertFalse(row.exceeds(0.001))

    def test_row_exceeding_tolerance_is_flagged_both_directions(self) -> None:
        for basis in (0.02, -0.02):
            row = BasisRow("BTC/USD", "BTCUSDT", 100.0, 100.0, basis)
            self.assertTrue(row.exceeds(0.01))

    def test_blocked_contains_only_verified_out_of_tolerance_pairs(self) -> None:
        report = BasisReport(threshold_pct=0.01)
        report.rows["BTC/USD"] = BasisRow("BTC/USD", "BTCUSDT", 100.0, 100.0, 0.0005)   # fine
        report.rows["ETH/USD"] = BasisRow("ETH/USD", "ETHUSDT", 100.0, 100.0, 0.05)     # too wide
        report.rows["XRP/USD"] = BasisRow("XRP/USD", "XRPUSDT", 100.0, None, None)      # unverified
        self.assertEqual(report.blocked(), {"ETH/USD"})
        self.assertEqual(report.unverified, {"XRP/USD"})

    def test_worst_picks_the_largest_magnitude_either_sign(self) -> None:
        report = BasisReport()
        report.rows["A/USD"] = BasisRow("A/USD", "AUSDT", 100.0, 100.0, -0.03)
        report.rows["B/USD"] = BasisRow("B/USD", "BUSDT", 100.0, 100.0, 0.01)
        self.assertEqual(report.worst().pair, "A/USD")

    def test_worst_is_none_when_nothing_is_verified(self) -> None:
        report = BasisReport()
        report.rows["A/USD"] = BasisRow("A/USD", "AUSDT", 100.0, None, None)
        self.assertIsNone(report.worst())

    def test_summary_reports_unavailability_rather_than_silence(self) -> None:
        self.assertIn("unavailable", BasisReport(error="boom").summary())


class TestBasisMonitor(unittest.TestCase):
    def test_check_maps_symbols_and_computes_the_basis(self) -> None:
        transport = FakeTransport(payload=[{"symbol": "BTCUSDT", "price": "100.00"}])
        monitor = BasisMonitor(transport=transport, cache_seconds=0.0)
        report = monitor.check({"BTC/USD": make_ticker("BTC/USD", 101.0)})
        self.assertTrue(report.source_ok)
        self.assertAlmostEqual(report.rows["BTC/USD"].basis_pct, 0.01, places=9)
        self.assertIn("BTCUSDT", transport.urls[0])

    def test_missing_symbol_is_reported_unverified_not_blocked(self) -> None:
        transport = FakeTransport(payload=[{"symbol": "BTCUSDT", "price": "100.00"}])
        monitor = BasisMonitor(transport=transport, cache_seconds=0.0)
        report = monitor.check(
            {"BTC/USD": make_ticker("BTC/USD", 100.0), "DOGE/USD": make_ticker("DOGE/USD", 1.0)}
        )
        self.assertIsNone(report.rows["DOGE/USD"].basis_pct)
        self.assertEqual(report.blocked(), set())
        self.assertEqual(report.unverified, {"DOGE/USD"})

    def test_network_failure_fails_open_with_an_error(self) -> None:
        monitor = BasisMonitor(transport=FakeTransport(raise_error=True), cache_seconds=0.0)
        report = monitor.check({"BTC/USD": make_ticker("BTC/USD", 100.0)})
        self.assertFalse(report.source_ok)
        self.assertTrue(report.error)
        self.assertEqual(report.blocked(), set())

    def test_http_error_fails_open(self) -> None:
        monitor = BasisMonitor(transport=FakeTransport(status=451, payload={}), cache_seconds=0.0)
        report = monitor.check({"BTC/USD": make_ticker("BTC/USD", 100.0)})
        self.assertFalse(report.source_ok)

    def test_binance_error_object_is_not_treated_as_prices(self) -> None:
        transport = FakeTransport(payload={"code": -1121, "msg": "Invalid symbol."})
        monitor = BasisMonitor(transport=transport, cache_seconds=0.0)
        report = monitor.check({"BTC/USD": make_ticker("BTC/USD", 100.0)})
        self.assertFalse(report.source_ok)
        self.assertIn("no reference prices", report.error)

    def test_one_request_covers_the_whole_universe(self) -> None:
        transport = FakeTransport(payload=[{"symbol": "BTCUSDT", "price": "100"}, {"symbol": "ETHUSDT", "price": "50"}])
        monitor = BasisMonitor(transport=transport, cache_seconds=0.0)
        monitor.check({"BTC/USD": make_ticker("BTC/USD", 100.0), "ETH/USD": make_ticker("ETH/USD", 50.0)})
        self.assertEqual(len(transport.urls), 1, "the universe must cost one request, not one per pair")

    def test_malformed_rows_are_skipped_without_raising(self) -> None:
        transport = FakeTransport(payload=[{"symbol": "BTCUSDT"}, {"nope": 1}, {"symbol": "ETHUSDT", "price": "abc"}])
        monitor = BasisMonitor(transport=transport, cache_seconds=0.0)
        report = monitor.check({"BTC/USD": make_ticker("BTC/USD", 100.0)})
        self.assertFalse(report.source_ok)  # nothing usable parsed

    def test_to_dict_is_json_serialisable(self) -> None:
        transport = FakeTransport(payload=[{"symbol": "BTCUSDT", "price": "100.00"}])
        monitor = BasisMonitor(transport=transport, cache_seconds=0.0)
        report = monitor.check({"BTC/USD": make_ticker("BTC/USD", 100.5)})
        json.dumps(report.to_dict())  # must not raise: the journal will serialise this


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
