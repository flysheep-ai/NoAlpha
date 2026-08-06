"""Alpaca Markets Broker Adapter — REST API (no SDK dependency).

Paper: https://paper-api.alpaca.markets
Live:  https://api.alpaca.markets
Docs:  https://docs.alpaca.markets/reference

Credentials are NEVER passed to LLM context or frontend.
"""
from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from typing import Any

from noalpha.broker.adapter import BrokerAdapter, BrokerCapabilities, BrokerMode
from noalpha.broker.policy import LiveTradingPolicy

logger = logging.getLogger(__name__)

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL = "https://api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"


class AlpacaAdapter(BrokerAdapter):
    """Alpaca Markets — paper + live, US equities only."""

    def __init__(self, policy: LiveTradingPolicy | None = None,
                 base_url: str = "", api_key: str = "", api_secret: str = "",
                 mode: BrokerMode = BrokerMode.SIMULATION) -> None:
        self.policy = policy or LiveTradingPolicy()
        self.base_url = base_url or ALPACA_PAPER_URL
        self.api_key = api_key or self.policy.paper_api_key
        self.api_secret = api_secret or self.policy.paper_api_secret
        self.mode = mode
        self._connected = False

    # ── BrokerAdapter impl ──

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker_name="Alpaca Markets",
            markets=["US"],
            paper_supported=True,
            live_supported=True,
            order_types=["market", "limit", "stop", "stop_limit", "trailing_stop"],
            time_in_force=["day", "gtc", "opg", "cls", "ioc", "fok"],
            fractional=True,
            shorting=True,
            stream_orders=False,
            stream_fills=False,
            replace_order=False,
            extended_hours=False,
            currency="USD",
            requires_desktop_client=False,
            mode=self.mode,
        )

    def connect(self) -> bool:
        try:
            resp = self._request("GET", "/v2/account")
            self._connected = resp.get("status") in ("ACTIVE", "ONBOARDING", "SUBMISSION_FAILED")
            return self._connected
        except Exception as exc:
            logger.warning("Alpaca connect failed: %s", exc)
            return False

    def disconnect(self) -> None:
        self._connected = False

    def health(self) -> dict[str, Any]:
        try:
            acc = self.get_account() if self._connected else None
            return {
                "connected": self._connected,
                "mode": self.mode.value,
                "base_url": self.base_url.replace(ALPACA_LIVE_URL, "***live***").replace(ALPACA_PAPER_URL, "paper-api"),
                "account_status": acc.get("status", "unknown") if acc else "disconnected",
                "currency": acc.get("currency", "USD") if acc else "USD",
                "pattern_day_trader": acc.get("pattern_day_trader", False) if acc else False,
            }
        except Exception as exc:
            return {"connected": False, "error": str(exc)}

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/v2/account")

    def get_buying_power(self) -> float:
        acc = self.get_account()
        return float(acc.get("buying_power", 0) or 0)

    def get_positions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/positions") or []

    def get_open_orders(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/orders?status=open") or []

    def get_order(self, broker_order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v2/orders/{broker_order_id}")

    def submit_order(self, intent: dict[str, Any]) -> dict[str, Any]:
        symbol = intent.get("symbol", "")
        qty = intent.get("quantity", 0)
        side = intent.get("side", "buy")
        order_type = intent.get("order_type", "market")
        client_id = intent.get("client_order_id", "")
        limit_price = intent.get("limit_price")
        time_in_force = intent.get("time_in_force", "day")

        order_type_normalized = order_type if order_type in ("market", "limit", "stop", "stop_limit", "trailing_stop") else "market"
        body: dict[str, Any] = {
            "symbol": symbol.upper(),
            "qty": str(qty),
            "side": side.lower(),
            "type": order_type_normalized,
            "time_in_force": time_in_force if time_in_force in ("day", "gtc", "opg", "cls", "ioc", "fok") else "day",
        }
        if client_id:
            body["client_order_id"] = client_id
        if limit_price is not None and order_type_normalized in ("limit", "stop_limit"):
            body["limit_price"] = str(limit_price)

        return self._request("POST", "/v2/orders", body)

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v2/orders/{broker_order_id}")

    def replace_order(self, broker_order_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Alpaca replace_order is not implemented in this adapter")

    def reconcile(self, local_state: dict[str, Any]) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        try:
            broker_positions = {p["symbol"]: p for p in self.get_positions()}
            local_positions = local_state.get("positions", {})
            for sym, lp in (local_positions or {}).items():
                bp = broker_positions.get(sym)
                if not bp and float(lp.get("quantity", 0)) > 0:
                    mismatches.append({
                        "type": "LOCAL_POSITION_MISSING_AT_BROKER",
                        "symbol": sym, "local_qty": lp.get("quantity"),
                        "severity": "high",
                    })
                elif bp and abs(float(lp.get("quantity", 0)) - float(bp.get("qty", 0))) > 0.001:
                    mismatches.append({
                        "type": "POSITION_MISMATCH",
                        "symbol": sym,
                        "local_qty": lp.get("quantity"),
                        "broker_qty": bp.get("qty"),
                        "severity": "critical" if abs(float(lp.get("quantity", 0)) - float(bp.get("qty", 0))) > 10 else "high",
                    })
        except Exception as exc:
            logger.warning("Reconciliation failed: %s", exc)
            mismatches.append({"type": "RECONCILIATION_ERROR", "error": str(exc)})
        return mismatches

    # ── helpers ──

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 timeout: int = 30) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body else None
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            body_raw = exc.read().decode("utf-8", errors="replace")
            logger.error("Alpaca HTTP %s %s → %s: %s", method, path, exc.code, body_raw[:200])
            raise RuntimeError(f"Alpaca {method} {path} returned {exc.code}: {body_raw[:200]}") from exc
        except Exception:
            raise
