"""Offline integration test for :mod:`roostoo.simulator`.

This is the only file that exercises a whole client surface end to end.  It uses
``MockRoostooClient`` with ``deterministic=True`` and a fixed seed, so the market
walk replays identically on every run.  Prices are additionally pinned with
``set_price`` wherever an exact balance change is asserted -- that removes the
random walk from the arithmetic without removing the integration.

Two simulator quirks are relied on and documented below:

* ``spec.spread_bps`` is set to 0 so the bid and ask equal the pinned price.  The
  simulator fills a MARKET BUY at the ask and a MARKET SELL at the bid, so a
  non-zero spread would make "the price has not moved" untrue for a short.
* ``ticker()``, ``short_positions()`` and ``equity()`` each call ``_step()`` and
  therefore *advance the random walk*.  Tests that pin a price must not call
  them in between two price-sensitive operations.
"""

from __future__ import annotations

import unittest

import roostoo.simulator as simulator
from roostoo.config import Config
from roostoo.errors import APIError
from roostoo.models import ExchangeInfo, OrderResult
from roostoo.simulator import MockRoostooClient

SEED = 1234
STABLE_PAIR = "BTC/USD"
STABLE_PRICE = 100.0


class SimulatorTestCase(unittest.TestCase):
    """Base class for the simulator tests.

    No shared-state reset is needed any more. ``MockRoostooClient`` used to alias
    the module-level ``DEFAULT_UNIVERSE`` ``_Spec`` objects, which leaked prices
    between instances; the constructor now copies each spec, and
    :class:`TestSimulatorIsolation` pins that down.
    """


def make_client(initial_capital: float = 100_000.0, **overrides: object) -> MockRoostooClient:
    """A deterministic simulator with a fixed seed and no bid/ask spread.

    A zero spread keeps the fill price equal to the pinned price, which is what
    lets the balance and P&L assertions be exact.
    """
    config_kwargs: dict[str, object] = dict(
        api_key="sim",
        secret_key="sim",
        mock=True,
        initial_capital=initial_capital,
        min_request_interval_sec=0.0,
    )
    config_kwargs.update(overrides)
    client = MockRoostooClient(Config(**config_kwargs), seed=SEED, deterministic=True)  # type: ignore[arg-type]
    for spec in client._specs.values():
        spec.spread_bps = 0.0
    client.set_price(STABLE_PAIR, STABLE_PRICE)
    return client


def free_usd(client: MockRoostooClient) -> float:
    return client.balance()["USD"].free


def locked_usd(client: MockRoostooClient) -> float:
    return client.balance()["USD"].locked


class TestExchangeInfo(SimulatorTestCase):
    def test_exchange_info_exposes_pairs(self) -> None:
        info = make_client().exchange_info()
        self.assertIsInstance(info, ExchangeInfo)
        self.assertTrue(info.is_running)
        self.assertIn("BTC/USD", info.pairs)
        self.assertGreaterEqual(len(info.pairs), 5)

    def test_every_pair_is_tradable_with_precisions(self) -> None:
        info = make_client().exchange_info()
        for name, pair in info.pairs.items():
            self.assertEqual(pair.pair, name)
            self.assertTrue(pair.can_trade)
            self.assertGreaterEqual(pair.price_precision, 0)
            self.assertGreaterEqual(pair.amount_precision, 0)

    def test_initial_wallet_is_the_configured_capital(self) -> None:
        info = make_client(initial_capital=50_000.0).exchange_info()
        self.assertAlmostEqual(info.initial_wallet["USD"], 50_000.0)

    def test_wallet_starts_with_capital_and_no_holdings(self) -> None:
        wallet = make_client().balance()
        self.assertAlmostEqual(wallet["USD"].free, 100_000.0)
        self.assertAlmostEqual(wallet["USD"].locked, 0.0)
        self.assertAlmostEqual(wallet["BTC"].free, 0.0)


class TestMarketBuy(SimulatorTestCase):
    def test_market_buy_fills(self) -> None:
        client = make_client()
        result = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="MARKET")
        self.assertIsInstance(result, OrderResult)
        self.assertEqual(result.status, "FILLED")
        self.assertAlmostEqual(result.filled_quantity, 1.0)
        self.assertGreater(result.order_id, 0)

    def test_market_buy_reduces_usd_by_notional_plus_fee(self) -> None:
        client = make_client()
        qty = 1.0
        before = free_usd(client)
        result = client.place_order(STABLE_PAIR, "BUY", qty, order_type="MARKET")

        spend = before - free_usd(client)
        expected = result.avg_fill_price * qty * (1.0 + client.cfg.taker_fee)
        self.assertAlmostEqual(spend, expected, places=6)

    def test_market_buy_spend_is_approximately_qty_times_price_times_fee(self) -> None:
        """The economics, stated the way the task describes them."""
        client = make_client()
        qty = 2.0
        before = free_usd(client)
        client.place_order(STABLE_PAIR, "BUY", qty, order_type="MARKET")
        spend = before - free_usd(client)

        taker_fee = client.cfg.taker_fee
        slippage = STABLE_PRICE * client.cfg.slippage_bps / 10_000.0
        fill_price = STABLE_PRICE + slippage  # a MARKET BUY pays the ask plus slippage
        self.assertAlmostEqual(spend, qty * fill_price * (1.0 + taker_fee), places=6)

    def test_market_buy_credits_the_coin(self) -> None:
        client = make_client()
        client.place_order(STABLE_PAIR, "BUY", 1.5, order_type="MARKET")
        self.assertAlmostEqual(client.balance()["BTC"].free, 1.5, places=9)

    def test_market_buy_charges_the_taker_fee(self) -> None:
        client = make_client()
        result = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="MARKET")
        self.assertAlmostEqual(result.commission, result.avg_fill_price * 1.0 * client.cfg.taker_fee, places=9)
        self.assertEqual(result.role, "TAKER")

    def test_market_buy_beyond_the_balance_is_rejected(self) -> None:
        """A rejection raises, exactly as the live client does.

        The live client converts `Success: false` into an `APIError`; the
        simulator used to return `status="REJECTED"` instead, so the engine's
        rejection branch was exercised only under `--mock` and live rejections
        took a different path entirely.
        """
        client = make_client(initial_capital=1_000.0)
        with self.assertRaises(APIError) as caught:
            client.place_order(STABLE_PAIR, "BUY", 1_000.0, order_type="MARKET")
        self.assertIn("insufficient balance", str(caught.exception))

    def test_rejected_market_buy_leaves_the_balance_untouched(self) -> None:
        client = make_client(initial_capital=1_000.0)
        before = free_usd(client)
        with self.assertRaises(APIError):
            client.place_order(STABLE_PAIR, "BUY", 1_000.0, order_type="MARKET")
        self.assertAlmostEqual(free_usd(client), before, places=9)


class TestMarketSellRejection(SimulatorTestCase):
    def test_selling_more_than_the_free_balance_is_rejected(self) -> None:
        """No holdings at all, so any SELL above zero must be refused."""
        client = make_client()
        with self.assertRaises(APIError) as caught:
            client.place_order(STABLE_PAIR, "SELL", 1.0, order_type="MARKET")
        self.assertIn("insufficient balance", str(caught.exception))

    def test_selling_slightly_more_than_held_is_rejected(self) -> None:
        """Owning 0.5 and selling 0.51 must be refused."""
        client = make_client()
        client.place_order(STABLE_PAIR, "BUY", 0.5, order_type="MARKET")
        with self.assertRaises(APIError) as caught:
            client.place_order(STABLE_PAIR, "SELL", 0.51, order_type="MARKET")
        self.assertIn("insufficient balance", str(caught.exception))

    def test_selling_exactly_the_free_balance_is_accepted(self) -> None:
        """The boundary case: the whole holding is sellable."""
        client = make_client()
        client.place_order(STABLE_PAIR, "BUY", 0.5, order_type="MARKET")
        result = client.place_order(STABLE_PAIR, "SELL", 0.5, order_type="MARKET")
        self.assertEqual(result.status, "FILLED")

    def test_rejected_sell_leaves_the_holding_untouched(self) -> None:
        client = make_client()
        client.place_order(STABLE_PAIR, "BUY", 0.5, order_type="MARKET")
        before = client.balance()["BTC"].free
        with self.assertRaises(APIError):
            client.place_order(STABLE_PAIR, "SELL", 5.0, order_type="MARKET")
        self.assertAlmostEqual(client.balance()["BTC"].free, before, places=9)

    def test_sell_of_an_unknown_pair_is_rejected(self) -> None:
        client = make_client()
        with self.assertRaises(APIError) as caught:
            client.place_order("NOPE/USD", "BUY", 1.0, order_type="MARKET")
        self.assertIn("pair not found", str(caught.exception))

    def test_an_order_below_the_pair_minimum_is_rejected(self) -> None:
        """The venue enforces `MiniOrder`; the mock must too.

        Without this the simulator filled sizes the real venue refuses, so the
        only guard was the engine's long-path `_quantise` and `--mock` looked
        healthy for orders that could never be placed.
        """
        client = make_client()
        with self.assertRaises(APIError) as caught:
            client.place_order(STABLE_PAIR, "BUY", 0.000001, order_type="MARKET")
        self.assertIn("MiniOrder", str(caught.exception))


class TestRestingLimitOrder(SimulatorTestCase):
    def test_non_crossing_limit_buy_is_pending(self) -> None:
        """A bid well below the market rests instead of filling."""
        client = make_client()
        result = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)
        self.assertEqual(result.status, "PENDING")
        self.assertEqual(result.role, "MAKER")
        self.assertAlmostEqual(result.filled_quantity, 0.0)
        self.assertAlmostEqual(result.price, STABLE_PRICE - 10.0)

    def test_pending_limit_buy_locks_usd(self) -> None:
        """The notional is reserved at the limit price, not the market price."""
        client = make_client()
        before_free = free_usd(client)
        result = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)

        locked_notional = 1.0 * (STABLE_PRICE - 10.0)
        self.assertAlmostEqual(locked_usd(client), locked_notional, places=6)
        self.assertAlmostEqual(free_usd(client), before_free - locked_notional, places=6)
        self.assertEqual(result.status, "PENDING")

    def test_pending_order_appears_in_pending_count(self) -> None:
        client = make_client()
        client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)
        total, pairs = client.pending_count()
        self.assertEqual(total, 1)
        self.assertEqual(pairs[STABLE_PAIR], 1)

    def test_cancel_releases_the_lock_and_returns_the_order_id(self) -> None:
        """Cancelling must hand the reserved cash straight back."""
        client = make_client()
        before_free = free_usd(client)
        placed = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)

        canceled = client.cancel_order(order_id=placed.order_id)
        self.assertEqual(canceled, [placed.order_id])
        self.assertAlmostEqual(locked_usd(client), 0.0, places=9)
        self.assertAlmostEqual(free_usd(client), before_free, places=6)

    def test_cancel_marks_the_order_canceled(self) -> None:
        client = make_client()
        placed = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)
        client.cancel_order(order_id=placed.order_id)
        row = client.query_orders(order_id=placed.order_id)[0]
        self.assertEqual(row["Status"], "CANCELED")

    def test_cancel_of_an_unknown_order_returns_nothing(self) -> None:
        client = make_client()
        self.assertEqual(client.cancel_order(order_id=999_999), [])

    def test_cancel_all_for_a_pair(self) -> None:
        client = make_client()
        first = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)
        second = client.place_order(STABLE_PAIR, "BUY", 2.0, order_type="LIMIT", price=STABLE_PRICE - 20.0)
        canceled = client.cancel_order(pair=STABLE_PAIR)
        self.assertEqual(sorted(canceled), sorted([first.order_id, second.order_id]))
        self.assertEqual(client.pending_count()[0], 0)

    def test_limit_buy_without_a_price_is_rejected(self) -> None:
        client = make_client()
        with self.assertRaises(APIError) as caught:
            client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT")
        self.assertIn("price", str(caught.exception))

    def test_crossing_limit_buy_fills_as_taker(self) -> None:
        """A bid at or above the ask crosses and fills immediately."""
        client = make_client()
        result = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.role, "TAKER")


class TestShortSide(SimulatorTestCase):
    def test_short_open_then_close_at_the_same_price_is_flat(self) -> None:
        """With the price unchanged the short realises exactly zero P&L.

        A market short enters at the best bid and closes at the best ask; with a
        zero spread (set by :func:`make_client`) those are the same price.
        """
        client = make_client()
        opened = client.short_open(STABLE_PAIR, collateral=1_000.0)
        self.assertTrue(opened["Success"])

        closed = client.short_close(STABLE_PAIR)
        self.assertTrue(closed["Success"])
        self.assertAlmostEqual(closed["RealizedPNL"], 0.0, places=6)

    def test_short_open_reports_a_positive_quantity_and_open_status(self) -> None:
        client = make_client()
        opened = client.short_open(STABLE_PAIR, collateral=1_000.0)
        self.assertEqual(opened["Status"], "OPEN")
        self.assertEqual(opened["OrderType"], "MARKET")
        self.assertGreater(opened["ShortQty"], 0.0)
        self.assertAlmostEqual(opened["EntryPrice"], STABLE_PRICE, places=6)

    def test_short_open_charges_the_open_fee(self) -> None:
        client = make_client()
        opened = client.short_open(STABLE_PAIR, collateral=1_000.0)
        self.assertAlmostEqual(opened["OpenFee"], 1_000.0 * 0.001, places=9)

    def test_short_open_locks_the_collateral(self) -> None:
        client = make_client()
        client.short_open(STABLE_PAIR, collateral=1_000.0)
        self.assertAlmostEqual(locked_usd(client), 1_000.0, places=9)

    def test_short_appears_in_short_positions(self) -> None:
        client = make_client()
        client.short_open(STABLE_PAIR, collateral=1_000.0)
        positions = client.short_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].pair, STABLE_PAIR)
        self.assertAlmostEqual(positions[0].collateral, 1_000.0, places=9)

    def test_short_close_removes_the_position(self) -> None:
        client = make_client()
        client.short_open(STABLE_PAIR, collateral=1_000.0)
        client.short_close(STABLE_PAIR)
        self.assertEqual(client.short_positions(), [])

    def test_short_profits_when_the_price_falls(self) -> None:
        client = make_client()
        client.short_open(STABLE_PAIR, collateral=1_000.0)
        client.set_price(STABLE_PAIR, STABLE_PRICE - 10.0)

        closed = client.short_close(STABLE_PAIR)
        self.assertGreater(closed["RealizedPNL"], 0.0)

    def test_short_loses_when_the_price_rises(self) -> None:
        client = make_client()
        client.short_open(STABLE_PAIR, collateral=1_000.0)
        client.set_price(STABLE_PAIR, STABLE_PRICE + 10.0)

        closed = client.short_close(STABLE_PAIR)
        self.assertLess(closed["RealizedPNL"], 0.0)

    def test_short_loss_is_capped_at_its_collateral(self) -> None:
        """A 10,000% adverse move must still lose at most the collateral."""
        client = make_client()
        collateral = 1_000.0
        client.short_open(STABLE_PAIR, collateral=collateral)
        client.set_price(STABLE_PAIR, STABLE_PRICE * 100.0)

        closed = client.short_close(STABLE_PAIR)
        self.assertGreaterEqual(closed["RealizedPNL"], -collateral)
        self.assertLessEqual(closed["ReturnAmount"], collateral)

    def test_short_loss_never_exceeds_collateral_across_many_moves(self) -> None:
        """Property check over many adverse prices, all deterministic."""
        collateral = 500.0
        for multiplier in (1.5, 2.0, 5.0, 10.0, 50.0, 1_000.0):
            with self.subTest(multiplier=multiplier):
                client = make_client()
                client.short_open(STABLE_PAIR, collateral=collateral)
                client.set_price(STABLE_PAIR, STABLE_PRICE * multiplier)
                closed = client.short_close(STABLE_PAIR)
                self.assertGreaterEqual(closed["RealizedPNL"], -collateral)

    def test_short_below_minimum_collateral_is_refused(self) -> None:
        client = make_client()
        opened = client.short_open(STABLE_PAIR, collateral=0.5)
        self.assertFalse(opened["Success"])
        self.assertIn("minimum collateral", opened["ErrMsg"])

    def test_short_without_funds_is_refused(self) -> None:
        client = make_client(initial_capital=100.0)
        opened = client.short_open(STABLE_PAIR, collateral=1_000.0)
        self.assertFalse(opened["Success"])
        self.assertIn("insufficient balance", opened["ErrMsg"])

    def test_close_without_a_position_is_refused(self) -> None:
        client = make_client()
        closed = client.short_close(STABLE_PAIR)
        self.assertFalse(closed["Success"])
        self.assertIn("no open short position", closed["ErrMsg"])


class TestQueryOrders(SimulatorTestCase):
    def test_query_orders_by_pair_is_newest_first(self) -> None:
        client = make_client()
        placed_ids = []
        for _ in range(3):
            result = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)
            placed_ids.append(result.order_id)

        rows = client.query_orders(pair=STABLE_PAIR)
        returned_ids = [row["OrderID"] for row in rows]
        self.assertEqual(returned_ids, list(reversed(placed_ids)))

    def test_query_orders_by_pair_excludes_other_pairs(self) -> None:
        client = make_client()
        client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)
        client.place_order("ETH/USD", "BUY", 1.0, order_type="LIMIT", price=1.0)

        rows = client.query_orders(pair=STABLE_PAIR)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["Pair"], STABLE_PAIR)

    def test_query_orders_by_id_returns_exactly_one(self) -> None:
        client = make_client()
        placed = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)
        rows = client.query_orders(order_id=placed.order_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["OrderID"], placed.order_id)

    def test_query_orders_pending_only_filters_filled_orders(self) -> None:
        client = make_client()
        client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="MARKET")  # fills
        client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)
        rows = client.query_orders(pair=STABLE_PAIR, pending_only=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Status"], "PENDING")

    def test_limit_applies_after_the_newest_first_reversal(self) -> None:
        client = make_client()
        newest = None
        for _ in range(3):
            newest = client.place_order(STABLE_PAIR, "BUY", 1.0, order_type="LIMIT", price=STABLE_PRICE - 10.0)
        rows = client.query_orders(pair=STABLE_PAIR, limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["OrderID"], newest.order_id)


class TestDeterminism(SimulatorTestCase):
    def test_same_seed_replays_the_same_market(self) -> None:
        """Two clients with the same seed must agree ticker-for-ticker.

        This only holds because ``SimulatorTestCase.setUp`` hands each client a
        freshly built universe -- see ``TestKnownSourceBug1``.
        """
        first = make_client()
        second = make_client()
        first_prices = {pair: t.last for pair, t in first.ticker().items()}
        second_prices = {pair: t.last for pair, t in second.ticker().items()}
        self.assertEqual(first_prices, second_prices)

    def test_different_seeds_diverge(self) -> None:
        first = MockRoostooClient(Config(api_key="s", secret_key="s", mock=True), seed=1, deterministic=True)
        second = MockRoostooClient(Config(api_key="s", secret_key="s", mock=True), seed=2, deterministic=True)
        first_prices = {pair: t.last for pair, t in first.ticker().items()}
        second_prices = {pair: t.last for pair, t in second.ticker().items()}
        self.assertNotEqual(first_prices, second_prices)

    def test_a_pinned_price_moves_with_each_step(self) -> None:
        """The random walk keeps moving: a pinned price is a starting point only."""
        client = make_client()
        client.ticker()
        client.ticker()
        self.assertNotAlmostEqual(client._specs[STABLE_PAIR].price, STABLE_PRICE, places=9)

    def test_deterministic_mode_does_not_read_the_wall_clock(self) -> None:
        """With ``deterministic=True`` each step is exactly one ``time_scale``."""
        client = make_client()
        before = client._last_step
        client.ticker()
        self.assertEqual(client._last_step, before)

    def test_same_seed_produces_the_same_step_count_from_a_fresh_universe(self) -> None:
        """Replaying from pristine prices reproduces the exact same path."""
        first = make_client()
        second = make_client()
        for _ in range(3):
            first.ticker()
            second.ticker()
        self.assertEqual(
            {pair: spec.price for pair, spec in first._specs.items()},
            {pair: spec.price for pair, spec in second._specs.items()},
        )


class TestSimulatorIsolation(SimulatorTestCase):
    """Regression tests: every simulator instance owns its own market.

    ``MockRoostooClient.__init__`` used to do
    ``{s.pair: s for s in DEFAULT_UNIVERSE}``, which stores *references* to the
    module-level ``_Spec`` objects. ``_Spec`` is mutable, so one client's price
    walk -- or a test's ``set_price`` -- mutated the market every other client in
    the process was looking at, and the documented guarantee ("a given seed
    always replays the same market") was false across instances.

    The constructor now copies each spec. These tests pin the guarantee down so
    it cannot regress.
    """

    def test_two_clients_with_the_same_seed_replay_identically(self) -> None:
        first = MockRoostooClient(Config(api_key="s", secret_key="s", mock=True), seed=7, deterministic=True)
        second = MockRoostooClient(Config(api_key="s", secret_key="s", mock=True), seed=7, deterministic=True)

        self.assertIsNot(first._specs["BTC/USD"], second._specs["BTC/USD"])
        first_prices = {pair: t.last for pair, t in first.ticker().items()}
        second_prices = {pair: t.last for pair, t in second.ticker().items()}
        self.assertEqual(first_prices, second_prices)

    def test_set_price_does_not_leak_between_instances(self) -> None:
        first = MockRoostooClient(Config(api_key="s", secret_key="s", mock=True), seed=7, deterministic=True)
        second = MockRoostooClient(Config(api_key="s", secret_key="s", mock=True), seed=7, deterministic=True)

        first.set_price("ETH/USD", 12_345.0)
        self.assertAlmostEqual(first._specs["ETH/USD"].price, 12_345.0, places=9)
        # The second client still starts from the documented price.
        self.assertAlmostEqual(second._specs["ETH/USD"].price, 3_400.0, places=9)

    def test_module_level_universe_is_never_mutated(self) -> None:
        before = tuple(spec.price for spec in simulator.DEFAULT_UNIVERSE)
        client = MockRoostooClient(Config(api_key="s", secret_key="s", mock=True), seed=7, deterministic=True)
        client.ticker()
        client.set_price("BTC/USD", 1.0)
        self.assertEqual(tuple(spec.price for spec in simulator.DEFAULT_UNIVERSE), before)

    def test_instances_do_not_share_spec_objects(self) -> None:
        client = MockRoostooClient(Config(api_key="s", secret_key="s", mock=True), seed=7, deterministic=True)
        for spec in simulator.DEFAULT_UNIVERSE:
            self.assertIsNot(client._specs[spec.pair], spec)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
