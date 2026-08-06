"""Broker Gateway tests — OMS state machine, policy gates, adapter contract."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from noalpha.broker.oms import OrderIntent, OrderIntentStatus, OrderManagementSystem
from noalpha.broker.policy import KillSwitchLevel, LiveTradingMode, LiveTradingPolicy
from noalpha.broker.alpaca_adapter import AlpacaAdapter
from noalpha.broker.adapter import BrokerCapabilities, BrokerMode


class PolicyGateTests(unittest.TestCase):
    def test_default_policy_blocks_live(self) -> None:
        p = LiveTradingPolicy()
        self.assertFalse(p.live_trading_enabled)
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=10)
        p.mode = LiveTradingMode.LIVE_LIMITED_AUTONOMOUS
        self.assertFalse(p.allows_order(intent))  # live_trading_enabled=False

    def test_kill_switch_blocks_everything(self) -> None:
        p = LiveTradingPolicy(live_trading_enabled=True,
                              mode=LiveTradingMode.LIVE_LIMITED_AUTONOMOUS)
        p.activate_kill_switch(KillSwitchLevel.GLOBAL, "test")
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=1)
        self.assertFalse(p.allows_order(intent))

    def test_blocked_instrument_rejected(self) -> None:
        p = LiveTradingPolicy(live_trading_enabled=True,
                              mode=LiveTradingMode.LIVE_LIMITED_AUTONOMOUS,
                              blocked_instruments=["TSLA"])
        intent = OrderIntent(symbol="TSLA", side="buy", quantity=1)
        self.assertFalse(p.allows_order(intent))

    def test_allowed_instrument_passes(self) -> None:
        p = LiveTradingPolicy(live_trading_enabled=True,
                              mode=LiveTradingMode.LIVE_LIMITED_AUTONOMOUS,
                              allowed_instruments=["AAPL"])
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=1)
        self.assertTrue(p.allows_order(intent))

    def test_max_order_notional_rejected(self) -> None:
        p = LiveTradingPolicy(live_trading_enabled=True,
                              mode=LiveTradingMode.LIVE_LIMITED_AUTONOMOUS,
                              max_order_notional=100)
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=10,
                             limit_price=150)  # $1500 > $100
        self.assertFalse(p.allows_order(intent))

    def test_daily_order_limit(self) -> None:
        p = LiveTradingPolicy(live_trading_enabled=True,
                              mode=LiveTradingMode.LIVE_LIMITED_AUTONOMOUS,
                              max_orders_per_day=2)
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=1)
        self.assertTrue(p.allows_order(intent))
        p.record_order()
        self.assertTrue(p.allows_order(intent))
        p.record_order()
        self.assertFalse(p.allows_order(intent))  # limit=2, already 2

    def test_secrets_excluded_from_safe_dict(self) -> None:
        p = LiveTradingPolicy(paper_api_key="PK_TEST_SECRET", live_api_key="LK_TEST_SECRET")
        safe = p.to_safe_dict()
        self.assertNotIn("paper_api_key", safe)
        self.assertNotIn("live_api_key", safe)
        self.assertNotIn("PK_TEST_SECRET", str(safe))


class OMSStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.oms = OrderManagementSystem(Path(self.tmp.name) / "oms.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_ahead_persistence(self) -> None:
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=10,
                             client_order_id="test-wa-001")
        self.oms.save_intent(intent)
        loaded = self.oms.load_intent(intent.intent_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.symbol, "AAPL")
        self.assertEqual(loaded.client_order_id, "test-wa-001")

    def test_idempotency_via_client_order_id(self) -> None:
        intent1 = OrderIntent(symbol="AAPL", side="buy", quantity=10,
                              client_order_id="idem-001")
        self.oms.save_intent(intent1)
        intent2 = OrderIntent(symbol="AAPL", side="buy", quantity=10,
                              client_order_id="idem-001")  # same client id
        self.oms.save_intent(intent2)  # should replace
        loaded = self.oms.load_intent_by_client_id("idem-001")
        self.assertIsNotNone(loaded)

    def test_status_transition_created_to_persisted(self) -> None:
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=10,
                             client_order_id="st-001")
        self.assertFalse(intent.is_terminal())
        intent.status = OrderIntentStatus.PERSISTED
        self.oms.save_intent(intent)
        loaded = self.oms.load_intent(intent.intent_id)
        self.assertEqual(loaded.status, OrderIntentStatus.PERSISTED)

    def test_terminal_statuses(self) -> None:
        for s in (OrderIntentStatus.FILLED, OrderIntentStatus.CANCELLED,
                  OrderIntentStatus.REJECTED, OrderIntentStatus.EXPIRED,
                  OrderIntentStatus.FAILED):
            intent = OrderIntent(status=s)
            self.assertTrue(intent.is_terminal(), f"{s} should be terminal")

    def test_unknown_status_is_not_terminal(self) -> None:
        intent = OrderIntent(status=OrderIntentStatus.UNKNOWN)
        self.assertFalse(intent.is_terminal())
        self.assertTrue(intent.is_in_flight())

    def test_submit_with_no_broker_returns_failed(self) -> None:
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=10)
        result = self.oms.submit(intent)  # no broker configured
        self.assertEqual(result.status, OrderIntentStatus.FAILED)
        self.assertIn("No broker configured", result.error)

    def test_policy_denied_before_broker_call(self) -> None:
        policy = LiveTradingPolicy(live_trading_enabled=False,
                                   mode=LiveTradingMode.LIVE_LIMITED_AUTONOMOUS)
        oms = OrderManagementSystem(Path(self.tmp.name) / "oms2.db", policy=policy)
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=10)
        result = oms.submit(intent)
        self.assertEqual(result.status, OrderIntentStatus.REJECTED)
        self.assertEqual(result.error, "Policy denied")

    def test_audit_log_recorded(self) -> None:
        intent = OrderIntent(symbol="AAPL", side="buy", quantity=10)
        self.oms.save_intent(intent)
        self.oms._audit("test_event", intent.intent_id, "test details")
        log = self.oms.audit_log(limit=10)
        self.assertTrue(any(e["event_type"] == "test_event" for e in log))

    def test_submit_is_idempotent_by_client_order_id(self) -> None:
        class FakeBroker:
            calls = 0

            def submit_order(self, intent):
                self.calls += 1
                return {"id": f"broker-{self.calls}", "status": "accepted"}

        policy = LiveTradingPolicy(mode=LiveTradingMode.SIMULATION)
        broker = FakeBroker()
        oms = OrderManagementSystem(Path(self.tmp.name) / "oms-idem.db", broker=broker, policy=policy)
        first = oms.submit(OrderIntent(symbol="AAPL", side="buy", quantity=1, client_order_id="same-001"))
        second = oms.submit(OrderIntent(symbol="AAPL", side="buy", quantity=1, client_order_id="same-001"))
        self.assertEqual(broker.calls, 1)
        self.assertEqual(second.intent_id, first.intent_id)
        self.assertEqual(second.broker_order_id, first.broker_order_id)


class AlpacaAdapterContractTests(unittest.TestCase):
    def test_capabilities_contract(self) -> None:
        cap = AlpacaAdapter().capabilities()
        self.assertEqual(cap.broker_name, "Alpaca Markets")
        self.assertIn("US", cap.markets)
        self.assertTrue(cap.paper_supported)
        self.assertTrue(cap.live_supported)
        self.assertEqual(cap.currency, "USD")

    def test_disconnected_health(self) -> None:
        adapter = AlpacaAdapter()
        health = adapter.health()
        self.assertFalse(health["connected"])

    def test_credentials_never_in_health(self) -> None:
        adapter = AlpacaAdapter(api_key="PK_TEST", api_secret="SK_TEST")
        health = adapter.health()
        health_str = str(health)
        self.assertNotIn("PK_TEST", health_str)
        self.assertNotIn("SK_TEST", health_str)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_missing_credentials_returns_401_on_real_connect(self) -> None:
        adapter = AlpacaAdapter(api_key="", api_secret="")
        self.assertFalse(adapter.connect())


class BrokerInfrastructureE2E(unittest.TestCase):
    """Full chain: Policy → OMS → (mock) Broker, without real money."""

    def test_full_chain_simulation(self) -> None:
        policy = LiveTradingPolicy(
            mode=LiveTradingMode.SIMULATION,
            live_trading_enabled=False,
            max_order_notional=5000,
            allowed_instruments=["AAPL", "NVDA"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            oms = OrderManagementSystem(Path(tmp) / "oms.db", policy=policy)
            # Create intent through the chain
            intent = OrderIntent(
                symbol="AAPL", side="buy", quantity=10,
                client_order_id="e2e-sim-001",
                proposal_id="prop-1", decision_id="dec-1",
            )
            # Submit (no broker in sim mode → failed)
            result = oms.submit(intent)
            self.assertEqual(result.status, OrderIntentStatus.FAILED)
            # Policy check passes but no broker → fails safely
            self.assertIn("No broker configured", result.error)

    def test_kill_switch_before_anything(self) -> None:
        policy = LiveTradingPolicy(
            live_trading_enabled=True,
            mode=LiveTradingMode.LIVE_LIMITED_AUTONOMOUS,
        )
        policy.activate_kill_switch(KillSwitchLevel.GLOBAL, "audit test")
        with tempfile.TemporaryDirectory() as tmp:
            oms = OrderManagementSystem(Path(tmp) / "oms.db", policy=policy)
            intent = OrderIntent(symbol="AAPL", side="buy", quantity=1)
            result = oms.submit(intent)
            self.assertEqual(result.status, OrderIntentStatus.REJECTED)
            self.assertEqual(result.error, "Policy denied")


if __name__ == "__main__":
    unittest.main()
