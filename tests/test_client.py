"""Request-level tests for :class:`roostoo.client.RoostooClient`.

Everything here runs offline: a ``FakeTransport`` satisfies the ``Transport``
protocol, a ``FakeClock`` replaces ``time.time`` and a ``FakeSleeper`` replaces
``time.sleep``.  No test opens a socket or waits on a real timer.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field
from typing import Any, Optional

from roostoo.client import (
    PATH_BALANCE,
    PATH_PENDING_COUNT,
    PATH_PLACE_ORDER,
    PATH_SERVER_TIME,
    RoostooClient,
    canonical_params,
    sign_payload,
)
from roostoo.config import Config
from roostoo.errors import APIError, TransportError

API_KEY = "test-api-key"
SECRET_KEY = "S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep"
BASE_URL = "https://mock-api.roostoo.com"
CLOCK_START = 1_700_000_000.0


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    """One outbound call, captured so assertions can inspect the wire format."""

    method: str
    url: str
    body: Optional[bytes]
    headers: dict[str, str]
    timeout: float

    @property
    def path(self) -> str:
        return self.url.split("?", 1)[0]

    @property
    def query(self) -> dict[str, str]:
        """Query-string parameters as a dict (empty when there is no query)."""
        if "?" not in self.url:
            return {}
        return dict(part.split("=", 1) for part in self.url.split("?", 1)[1].split("&") if part)

    @property
    def form(self) -> dict[str, str]:
        """Form-encoded body parameters as a dict (empty when there is no body)."""
        if not self.body:
            return {}
        text = self.body.decode("utf-8")
        return dict(part.split("=", 1) for part in text.split("&") if part)

    @property
    def body_text(self) -> str:
        return self.body.decode("utf-8") if self.body else ""


class FakeTransport:
    """A ``Transport`` that returns canned ``(status, text)`` tuples."""

    def __init__(
        self,
        responses: Optional[list[tuple[int, str]]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.requests: list[RecordedRequest] = []

    @property
    def attempts(self) -> int:
        return len(self.requests)

    def send(
        self, method: str, url: str, body: Optional[bytes], headers: dict[str, str], timeout: float
    ) -> tuple[int, str]:
        self.requests.append(
            RecordedRequest(method=method, url=url, body=body, headers=dict(headers), timeout=timeout)
        )
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError(f"FakeTransport ran out of canned responses for {method} {url}")
        return self.responses.pop(0)


class FakeClock:
    """Deterministic stand-in for ``time.time``."""

    def __init__(self, start: float = CLOCK_START) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleeper:
    """Records requested sleeps instead of performing them."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


class SequencedClock:
    """A clock that returns a fixed list of readings, in order.

    Used to make the two halves of a measured round trip exactly known, which
    is what ``sync_time`` needs in order to be checked arithmetically.
    """

    def __init__(self, readings: list[float]) -> None:
        self._readings = list(readings)
        self._last = self._readings[-1]

    def __call__(self) -> float:
        if self._readings:
            self._last = self._readings.pop(0)
        return self._last


@dataclass
class Harness:
    """A client wired to the three test doubles."""

    client: RoostooClient
    transport: FakeTransport
    clock: FakeClock
    sleeper: FakeSleeper
    config: Config = field(default_factory=Config)

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)


def make_harness(
    responses: Optional[list[tuple[int, str]]] = None,
    error: Optional[Exception] = None,
    **config_overrides: Any,
) -> Harness:
    """Build an offline client with fakes injected (never touches the network)."""
    defaults: dict[str, Any] = dict(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
        base_url=BASE_URL,
        request_timeout_sec=15.0,
        min_request_interval_sec=0.25,
    )
    defaults.update(config_overrides)
    config = Config(**defaults)
    transport = FakeTransport(responses=responses, error=error)
    clock = FakeClock()
    sleeper = FakeSleeper()
    client = RoostooClient(config, transport=transport, clock=clock, sleeper=sleeper)
    return Harness(client=client, transport=transport, clock=clock, sleeper=sleeper, config=config)


def ok(payload: dict[str, Any]) -> tuple[int, str]:
    """A canned HTTP 200 response carrying ``payload`` as JSON."""
    return 200, json.dumps(payload)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction(unittest.TestCase):
    def test_missing_credentials_are_rejected(self) -> None:
        """A client without keys cannot sign anything, so construction fails early."""
        with self.assertRaises(ValueError):
            RoostooClient(Config(api_key="", secret_key=""))

    def test_injected_transport_and_clock_are_used(self) -> None:
        """The fakes are honoured -- proof that no test here can hit the network."""
        harness = make_harness([ok({"Success": True, "Wallet": {}})])
        harness.client.balance()
        self.assertEqual(harness.transport.attempts, 1)
        self.assertEqual(harness.client.request_count, 1)


# ---------------------------------------------------------------------------
# Signed GET
# ---------------------------------------------------------------------------


class TestSignedGet(unittest.TestCase):
    def test_balance_sends_api_key_and_hex_signature_headers(self) -> None:
        harness = make_harness([ok({"Success": True, "Wallet": {}})])
        harness.client.balance()

        request = harness.transport.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path, BASE_URL + PATH_BALANCE)
        self.assertEqual(request.headers["RST-API-KEY"], API_KEY)
        signature = request.headers["MSG-SIGNATURE"]
        self.assertEqual(len(signature), 64)
        self.assertEqual(signature, signature.lower())
        int(signature, 16)  # raises ValueError if not hex

    def test_balance_signature_covers_exactly_the_sent_query_string(self) -> None:
        """The signature is HMAC over the query string, timestamp included."""
        harness = make_harness([ok({"Success": True, "Wallet": {}})])
        harness.client.balance()

        request = harness.transport.requests[0]
        expected = sign_payload(request.url.split("?", 1)[1], SECRET_KEY)
        self.assertEqual(request.headers["MSG-SIGNATURE"], expected)

    def test_balance_puts_params_in_the_query_string(self) -> None:
        """GET parameters travel in the URL; there is no request body."""
        harness = make_harness([ok({"Success": True, "Wallet": {}})])
        harness.client.balance()

        request = harness.transport.requests[0]
        self.assertIsNone(request.body)
        self.assertIn("timestamp", request.query)
        self.assertEqual(list(request.query), ["timestamp"])

    def test_balance_timestamp_is_clock_milliseconds(self) -> None:
        """With no measured offset the timestamp is the raw local clock."""
        harness = make_harness([ok({"Success": True, "Wallet": {}})])
        harness.client.balance()

        expected = str(int(CLOCK_START * 1000))
        self.assertEqual(harness.transport.requests[0].query["timestamp"], expected)

    def test_balance_is_unsigned_for_extra_params(self) -> None:
        """No undocumented parameter is added: the query holds only the timestamp."""
        harness = make_harness([ok({"Success": True, "Wallet": {}})])
        harness.client.balance()
        request = harness.transport.requests[0]
        self.assertEqual(set(request.query), {"timestamp"})
        self.assertEqual(request.body_text, "")

    def test_balance_parses_free_and_locked_amounts(self) -> None:
        """The wallet payload is normalised into WalletBalance objects."""
        harness = make_harness(
            [
                ok(
                    {
                        "Success": True,
                        "Wallet": {"USD": {"Free": 1000.5, "Lock": 20.0}, "BTC": {"Free": 0.25, "Lock": 0.0}},
                    }
                )
            ]
        )
        wallet = harness.client.balance()
        self.assertAlmostEqual(wallet["USD"].free, 1000.5)
        self.assertAlmostEqual(wallet["USD"].locked, 20.0)
        self.assertAlmostEqual(wallet["BTC"].total, 0.25)


# ---------------------------------------------------------------------------
# Signed POST
# ---------------------------------------------------------------------------


@dataclass
class OrderDetailFixture:
    """The documented ``OrderDetail`` shape for a fully filled MARKET order."""

    @staticmethod
    def filled(pair: str = "BNB/USD", side: str = "BUY", qty: float = 1.0, price: float = 610.5) -> dict[str, Any]:
        return {
            "Pair": pair,
            "OrderID": 4242,
            "Status": "FILLED",
            "Role": "TAKER",
            "Side": side,
            "Type": "MARKET",
            "Price": price,
            "Quantity": qty,
            "FilledQuantity": qty,
            "FilledAverPrice": price,
            "CommissionChargeValue": qty * price * 0.001,
            "CommissionCoin": "USD",
        }


class TestSignedPost(unittest.TestCase):
    def test_place_order_sends_form_content_type_on_post(self) -> None:
        """Order placement is a form-encoded POST, not a JSON body."""
        harness = make_harness([ok({"Success": True, "OrderDetail": OrderDetailFixture.filled()})])
        harness.client.place_order("BNB/USD", "BUY", 1.0)

        request = harness.transport.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.path, BASE_URL + PATH_PLACE_ORDER)
        self.assertEqual(request.headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(request.query, {})

    def test_place_order_body_equals_the_canonical_param_string(self) -> None:
        """What is signed is exactly what is sent: body == canonical params."""
        harness = make_harness([ok({"Success": True, "OrderDetail": OrderDetailFixture.filled()})])
        harness.client.place_order("BNB/USD", "BUY", 1.0)

        request = harness.transport.requests[0]
        self.assertEqual(request.body_text, canonical_params(request.form))

    def test_place_order_signature_covers_the_body(self) -> None:
        """MSG-SIGNATURE is HMAC over the form body, not over the URL."""
        harness = make_harness([ok({"Success": True, "OrderDetail": OrderDetailFixture.filled()})])
        harness.client.place_order("BNB/USD", "BUY", 1.0)

        request = harness.transport.requests[0]
        self.assertEqual(request.headers["MSG-SIGNATURE"], sign_payload(request.body_text, SECRET_KEY))

    def test_market_order_body_has_no_price_field(self) -> None:
        """A MARKET order must not carry ``price``.

        The server rebuilds the canonical string from the fields it recognises,
        so sending ``price`` on a MARKET order would invalidate the signature.
        """
        harness = make_harness([ok({"Success": True, "OrderDetail": OrderDetailFixture.filled()})])
        harness.client.place_order("BNB/USD", "BUY", 2.0, order_type="MARKET")

        form = harness.transport.requests[0].form
        self.assertNotIn("price", form)
        self.assertEqual(form["type"], "MARKET")
        self.assertEqual(form["side"], "BUY")
        self.assertEqual(form["pair"], "BNB/USD")
        self.assertEqual(form["quantity"], "2.0")
        self.assertEqual(set(form), {"pair", "side", "type", "quantity", "timestamp"})

    def test_limit_order_body_includes_price(self) -> None:
        """A LIMIT order carries its price, and the params stay lexicographic."""
        harness = make_harness([ok({"Success": True, "OrderDetail": OrderDetailFixture.filled()})])
        harness.client.place_order("BNB/USD", "BUY", 2.0, order_type="LIMIT", price="600.25")

        request = harness.transport.requests[0]
        self.assertEqual(request.form["price"], "600.25")
        self.assertEqual(request.form["type"], "LIMIT")
        self.assertEqual(request.body_text, canonical_params(request.form))
        self.assertTrue(request.body_text.startswith("pair=BNB/USD&price=600.25"))

    def test_limit_order_without_price_raises_before_the_wire(self) -> None:
        """A LIMIT order with no price is a caller bug, caught client-side."""
        harness = make_harness([])
        with self.assertRaises(ValueError):
            harness.client.place_order("BNB/USD", "BUY", 1.0, order_type="LIMIT")
        self.assertEqual(harness.transport.attempts, 0)

    def test_place_order_lowercases_nothing_and_uppercases_enums(self) -> None:
        """side/type are upper-cased on the wire; the pair is passed through."""
        harness = make_harness([ok({"Success": True, "OrderDetail": OrderDetailFixture.filled()})])
        harness.client.place_order("BNB/USD", "buy", 1.0, order_type="market")

        form = harness.transport.requests[0].form
        self.assertEqual(form["side"], "BUY")
        self.assertEqual(form["type"], "MARKET")

    def test_place_order_returns_parsed_order_result(self) -> None:
        """The documented TAKER response becomes a FILLED OrderResult."""
        harness = make_harness([ok({"Success": True, "OrderDetail": OrderDetailFixture.filled()})])
        result = harness.client.place_order("BNB/USD", "BUY", 1.0)

        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.order_id, 4242)
        self.assertEqual(result.role, "TAKER")
        self.assertAlmostEqual(result.filled_quantity, 1.0)
        self.assertTrue(result.is_live)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestApplicationErrors(unittest.TestCase):
    def test_success_false_on_http_200_raises_api_error(self) -> None:
        """HTTP 200 is not success: ``Success: false`` must raise."""
        harness = make_harness([ok({"Success": False, "ErrMsg": "invalid signature"})])
        with self.assertRaises(APIError) as ctx:
            harness.client.balance()
        self.assertIn("invalid signature", str(ctx.exception))
        self.assertEqual(ctx.exception.endpoint, PATH_BALANCE)

    def test_api_error_is_not_retried(self) -> None:
        """An application rejection is deterministic -- one attempt only."""
        harness = make_harness([ok({"Success": False, "ErrMsg": "invalid signature"})])
        with self.assertRaises(APIError):
            harness.client.balance()
        self.assertEqual(harness.transport.attempts, 1)

    def test_pending_count_empty_state_is_not_an_error(self) -> None:
        """``no pending order`` is a documented empty result, not a failure.

        The endpoint answers ``Success: false`` when the account simply has no
        working orders; treating that as an error would stall the loop.
        """
        harness = make_harness(
            [ok({"Success": False, "ErrMsg": "no pending order under this account", "TotalPending": 0})]
        )
        total, pairs = harness.client.pending_count()
        self.assertEqual(total, 0)
        self.assertEqual(pairs, {})

    def test_pending_count_real_error_still_raises(self) -> None:
        """Only the documented empty-state token is swallowed."""
        harness = make_harness([ok({"Success": False, "ErrMsg": "invalid API key"})])
        with self.assertRaises(APIError):
            harness.client.pending_count()

    def test_pending_count_parses_counts(self) -> None:
        harness = make_harness(
            [ok({"Success": True, "TotalPending": 3, "OrderPairs": {"BNB/USD": 2, "BTC/USD": 1}})]
        )
        total, pairs = harness.client.pending_count()
        self.assertEqual(total, 3)
        self.assertEqual(pairs["BNB/USD"], 2)

    def test_query_orders_no_match_is_not_an_error(self) -> None:
        """``no order matched`` is the other documented empty state."""
        harness = make_harness(
            [ok({"Success": False, "ErrMsg": "no order matched, please check your input"})]
        )
        self.assertEqual(harness.client.query_orders(pair="BNB/USD"), [])

    def test_place_order_application_rejection_raises_api_error(self) -> None:
        """Unlike a transport failure, a definite rejection is a hard error.

        ``place_order`` only converts ``TransportError`` into ``status='UNKNOWN'``;
        an ``APIError`` means the exchange told us the order does not exist, so it
        must propagate to the caller instead of looking like an ambiguous state.
        """
        harness = make_harness([ok({"Success": False, "ErrMsg": "insufficient balance"})])
        with self.assertRaises(APIError):
            harness.client.place_order("BNB/USD", "BUY", 1.0)
        self.assertEqual(harness.transport.attempts, 1)


class TestTransportErrors(unittest.TestCase):
    def test_non_json_body_raises_transport_error(self) -> None:
        """An HTML error page or truncated body is a transport failure."""
        harness = make_harness([(200, "<html>gateway timeout</html>")])
        with self.assertRaises(TransportError):
            harness.client.balance()

    def test_empty_body_raises_transport_error(self) -> None:
        harness = make_harness([(200, "")])
        with self.assertRaises(TransportError):
            harness.client.balance()

    def test_json_array_body_raises_transport_error(self) -> None:
        """A well-formed but wrongly shaped payload is also a transport failure."""
        harness = make_harness([(200, "[1, 2, 3]")])
        with self.assertRaises(TransportError):
            harness.client.balance()

    def test_http_500_is_retried_then_raises_transport_error(self) -> None:
        """A 5xx is transient: retry ``retries`` times, then give up.

        ``balance()`` itself does not retry (``retries=0``), so the retrying
        path is exercised through ``_call`` with an explicit budget.
        """
        harness = make_harness([(500, "boom"), (500, "boom"), (503, "still down")])
        with self.assertLogs("roostoo.client", level="WARNING"):
            with self.assertRaises(TransportError):
                harness.client._call("GET", PATH_BALANCE, {}, signed=True, retries=2)
        self.assertEqual(harness.transport.attempts, 3)

    def test_http_500_retry_can_succeed(self) -> None:
        """The retry is genuinely attempted and a later success is returned."""
        harness = make_harness([(500, "boom"), ok({"Success": True, "Wallet": {}})])
        with self.assertLogs("roostoo.client", level="WARNING"):
            payload = harness.client._call("GET", PATH_BALANCE, {}, signed=True, retries=1)
        self.assertTrue(payload["Success"])
        self.assertEqual(harness.transport.attempts, 2)

    def test_http_400_is_not_retried(self) -> None:
        """A 4xx is our own fault (bad signature/params): retrying cannot help."""
        harness = make_harness([(400, "bad request"), (200, "{}")])
        with self.assertRaises(TransportError):
            harness.client.balance()
        self.assertEqual(harness.transport.attempts, 1)

    def test_transport_exception_is_retried_when_asked(self) -> None:
        """A raised TransportError is retried only when the endpoint allows it."""
        harness = make_harness(error=TransportError("connection reset"))
        with self.assertRaises(TransportError):
            harness.client._call("GET", PATH_BALANCE, {}, signed=True, retries=2)
        self.assertEqual(harness.transport.attempts, 3)

    def test_backoff_sleeps_between_retries(self) -> None:
        """Retries wait through the injected sleeper -- never a real timer.

        Every attempt also pays the throttle floor (``min_request_interval_sec``),
        so the backoff wait is singled out as the sleep strictly longer than that
        floor.  Attempt 1 backs off ``0.5 * jitter`` in ``[0.35, 0.65]`` seconds;
        attempt 2 backs off ``1.0 * jitter`` in ``[0.7, 1.3]`` seconds.
        """
        harness = make_harness([(500, "boom"), (500, "boom"), ok({"Success": True, "Wallet": {}})])
        with self.assertLogs("roostoo.client", level="WARNING"):
            harness.client._call("GET", PATH_BALANCE, {}, signed=True, retries=2)

        backoff_waits = [delay for delay in harness.sleeper.calls if delay > harness.config.min_request_interval_sec]
        self.assertEqual(len(backoff_waits), 2)
        self.assertGreaterEqual(backoff_waits[0], 0.35)
        self.assertLessEqual(backoff_waits[0], 0.65)
        self.assertGreaterEqual(backoff_waits[1], 0.70)
        self.assertLessEqual(backoff_waits[1], 1.30)
        for delay in harness.sleeper.calls:
            self.assertLessEqual(delay, 8.0)


class TestOrderPlacementIsNotIdempotent(unittest.TestCase):
    def test_transport_failure_returns_unknown_status(self) -> None:
        """A transport failure during place_order must not be blind-retried.

        We do not know whether the order reached the matching engine, so the
        client reports ``UNKNOWN`` and the engine reconciles via query_order.
        """
        harness = make_harness(error=TransportError("connection reset"))
        result = harness.client.place_order("BNB/USD", "BUY", 1.0)

        self.assertEqual(result.status, "UNKNOWN")
        self.assertIn("connection reset", result.err_msg)

    def test_transport_failure_records_exactly_one_attempt(self) -> None:
        """The dangerous path is the one that must never double-send an order."""
        harness = make_harness(error=TransportError("connection reset"))
        harness.client.place_order("BNB/USD", "BUY", 1.0)
        self.assertEqual(harness.transport.attempts, 1)

    def test_http_500_during_place_order_also_returns_unknown(self) -> None:
        """Even a 5xx is not retried for order placement."""
        harness = make_harness([(500, "boom")])
        result = harness.client.place_order("BNB/USD", "BUY", 1.0)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(harness.transport.attempts, 1)

    def test_unknown_result_keeps_the_requested_parameters(self) -> None:
        """The returned OrderResult still describes what we *tried* to do."""
        harness = make_harness(error=TransportError("timeout"))
        result = harness.client.place_order("ETH/USD", "SELL", 2.5, order_type="LIMIT", price="3000")
        self.assertEqual(result.pair, "ETH/USD")
        self.assertEqual(result.side, "SELL")
        self.assertEqual(result.order_type, "LIMIT")
        self.assertAlmostEqual(result.quantity, 2.5)
        self.assertAlmostEqual(result.price, 3000.0)
        self.assertFalse(result.is_live)


# ---------------------------------------------------------------------------
# query_orders exclusivity
# ---------------------------------------------------------------------------


class TestQueryOrdersParameters(unittest.TestCase):
    def test_order_id_with_pair_raises_value_error(self) -> None:
        """order_id is exclusive: the API rejects a combined query."""
        harness = make_harness([])
        with self.assertRaises(ValueError):
            harness.client.query_orders(order_id=1, pair="BNB/USD")
        self.assertEqual(harness.transport.attempts, 0)

    def test_order_id_with_pending_only_raises_value_error(self) -> None:
        harness = make_harness([])
        with self.assertRaises(ValueError):
            harness.client.query_orders(order_id=1, pending_only=True)
        self.assertEqual(harness.transport.attempts, 0)

    def test_order_id_with_limit_raises_value_error(self) -> None:
        harness = make_harness([])
        with self.assertRaises(ValueError):
            harness.client.query_orders(order_id=1, limit=10)
        self.assertEqual(harness.transport.attempts, 0)

    def test_order_id_alone_is_accepted(self) -> None:
        """The exclusive form on its own is valid and returns the matched rows."""
        harness = make_harness([ok({"Success": True, "OrderMatched": [{"OrderID": 7}]})])
        rows = harness.client.query_orders(order_id=7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(harness.transport.requests[0].form["order_id"], "7")

    def test_pair_query_sends_pending_only_flag(self) -> None:
        harness = make_harness([ok({"Success": True, "OrderMatched": []})])
        harness.client.query_orders(pair="BNB/USD", pending_only=True, limit=5)
        form = harness.transport.requests[0].form
        self.assertEqual(form["pair"], "BNB/USD")
        self.assertEqual(form["pending_only"], "TRUE")
        self.assertEqual(form["limit"], "5")

    def test_cancel_order_rejects_order_id_and_pair_together(self) -> None:
        harness = make_harness([])
        with self.assertRaises(ValueError):
            harness.client.cancel_order(order_id=1, pair="BNB/USD")


# ---------------------------------------------------------------------------
# Clock synchronisation
# ---------------------------------------------------------------------------


class TestServerTimeSync(unittest.TestCase):
    def test_sync_time_stores_server_time_minus_local_midpoint(self) -> None:
        """The offset is ``ServerTime - local midpoint`` of the round trip."""
        # A tiny, fully deterministic round trip: the host clock reads 1000.5 ms
        # before the call and 1001.5 ms after it, so the midpoint is exactly
        # 1001.0 ms -- a whole millisecond, which keeps the int() truncation in
        # sync_time() from losing a millisecond.  The server is 100_000 ms ahead.
        local_before_ms = 1_000.5
        local_after_ms = 1_001.5
        offset_ms = 100_000
        midpoint = (local_before_ms + local_after_ms) / 2.0  # 1001.0
        server_ms = int(midpoint + offset_ms)  # 101001

        harness = make_harness([ok({"Success": True, "ServerTime": server_ms})])
        harness.client._clock = SequencedClock([local_before_ms / 1000.0, local_after_ms / 1000.0])

        measured = harness.client.sync_time()
        self.assertEqual(midpoint, 1_001.0)
        self.assertEqual(measured, offset_ms)
        self.assertEqual(harness.client.server_offset_ms, offset_ms)

    def test_sync_time_request_is_unsigned(self) -> None:
        """serverTime needs no credentials, so it carries no signature header."""
        harness = make_harness([ok({"Success": True, "ServerTime": 1})])
        harness.client.sync_time()
        request = harness.transport.requests[0]
        self.assertEqual(request.path, BASE_URL + PATH_SERVER_TIME)
        self.assertNotIn("MSG-SIGNATURE", request.headers)
        self.assertNotIn("RST-API-KEY", request.headers)

    def test_offset_is_applied_to_later_timestamps(self) -> None:
        """Once measured, the offset corrects every subsequent signed request."""
        harness = make_harness([ok({"Success": True, "ServerTime": int(CLOCK_START * 1000) + 2_000})])
        harness.client.sync_time()
        self.assertAlmostEqual(harness.client.server_offset_ms, 2_000, delta=1)

        offset = harness.client.server_offset_ms
        harness.advance(1.5)
        self.assertEqual(harness.client.timestamp_ms(), str(int(harness.clock.now * 1000) + offset))

    def test_missing_server_time_leaves_offset_at_zero(self) -> None:
        """A payload without ``ServerTime`` must not corrupt the offset."""
        harness = make_harness([ok({"Success": True})])
        self.assertEqual(harness.client.sync_time(), 0)
        self.assertEqual(harness.client.server_offset_ms, 0)


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


class TestThrottle(unittest.TestCase):
    def test_throttle_sleeps_to_respect_the_floor(self) -> None:
        """Two immediate requests are spaced by ``min_request_interval_sec``."""
        harness = make_harness(
            [ok({"Success": True, "Wallet": {}}), ok({"Success": True, "Wallet": {}})],
            min_request_interval_sec=0.25,
        )
        harness.client.balance()
        harness.client.balance()
        self.assertEqual(len(harness.sleeper.calls), 1)
        self.assertAlmostEqual(harness.sleeper.calls[0], 0.25, places=6)

    def test_no_sleep_once_the_interval_has_elapsed(self) -> None:
        """A slow caller is never delayed."""
        harness = make_harness(
            [ok({"Success": True, "Wallet": {}}), ok({"Success": True, "Wallet": {}})],
            min_request_interval_sec=0.25,
        )
        harness.client.balance()
        harness.advance(5.0)
        harness.client.balance()
        self.assertEqual(harness.sleeper.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
