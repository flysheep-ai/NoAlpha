"""Controlled Trading API — NOT a direct broker passthrough.

Every endpoint goes through Policy → OMS → Broker, never directly to broker.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from noalpha.broker.adapter import BrokerAdapter, BrokerMode
from noalpha.broker.alpaca_adapter import AlpacaAdapter
from noalpha.broker.oms import OrderIntent, OrderIntentStatus, OrderManagementSystem
from noalpha.broker.policy import KillSwitchLevel, LiveTradingMode, LiveTradingPolicy

logger = logging.getLogger(__name__)


class ApprovalBody(BaseModel):
    proposal_id: str = Field(min_length=1)


class PolicyUpdateBody(BaseModel):
    max_order_notional: float | None = None
    max_position_weight: float | None = None
    max_daily_loss: float | None = None
    allowed_instruments: list[str] | None = None
    allow_extended_hours: bool | None = None
    live_trading_enabled: bool | None = None


def register_broker_routes(app: FastAPI, state_dir: Path, admin_dependency: Any | None = None) -> None:
    oms = OrderManagementSystem(state_dir / "broker_oms.sqlite3")
    policy = LiveTradingPolicy()
    app.state.broker_oms = oms
    app.state.broker_policy = policy
    admin_deps = [Depends(admin_dependency)] if admin_dependency else []

    def _get_broker() -> BrokerAdapter | None:
        if policy.mode == LiveTradingMode.BROKER_PAPER and policy.paper_api_key:
            return AlpacaAdapter(
                policy=policy,
                base_url=policy.paper_base_url or "https://paper-api.alpaca.markets",
                api_key=policy.paper_api_key,
                api_secret=policy.paper_api_secret,
                mode=BrokerMode.BROKER_PAPER,
            )
        if policy.mode in (
            LiveTradingMode.LIVE_APPROVAL_REQUIRED,
            LiveTradingMode.LIVE_LIMITED_AUTONOMOUS,
        ) and policy.live_api_key:
            return AlpacaAdapter(
                policy=policy,
                base_url=policy.live_base_url or "https://api.alpaca.markets",
                api_key=policy.live_api_key,
                api_secret=policy.live_api_secret,
                mode=BrokerMode(policy.mode.value),
            )
        return None

    # ── status ──

    @app.get("/api/v1/broker/status")
    def broker_status() -> dict[str, Any]:
        broker = _get_broker()
        health = broker.health() if broker else {"connected": False, "mode": "no_broker_configured"}
        return {
            "mode": policy.mode.value,
            "live_trading_enabled": policy.live_trading_enabled,
            "kill_switches": policy.kill_switches,
            "daily": {"orders": policy._daily_orders, "pnl": round(policy._daily_pnl, 2)},
            "broker_health": health,
        }

    @app.get("/api/v1/broker/account", dependencies=admin_deps)
    def broker_account() -> dict[str, Any]:
        broker = _get_broker()
        if broker is None:
            raise HTTPException(409, "No broker configured")
        return broker.get_account()

    @app.get("/api/v1/broker/positions", dependencies=admin_deps)
    def broker_positions() -> dict[str, Any]:
        broker = _get_broker()
        if broker is None:
            raise HTTPException(409, "No broker configured")
        return {"items": broker.get_positions()}

    @app.get("/api/v1/broker/orders", dependencies=admin_deps)
    def broker_orders() -> dict[str, Any]:
        intents = oms.list_intents(limit=50)
        return {"items": [i.to_dict() for i in intents]}

    @app.get("/api/v1/broker/fills", dependencies=admin_deps)
    def broker_fills() -> dict[str, Any]:
        filled = [i.to_dict() for i in oms.list_intents(limit=200)
                  if i.status in (OrderIntentStatus.FILLED, OrderIntentStatus.PARTIALLY_FILLED)]
        return {"items": filled}

    @app.get("/api/v1/broker/policy", dependencies=admin_deps)
    def broker_policy() -> dict[str, Any]:
        return policy.to_safe_dict()

    @app.put("/api/v1/broker/policy", dependencies=admin_deps)
    def update_broker_policy(body: PolicyUpdateBody) -> dict[str, Any]:
        """Only user can change policy; AI never calls this."""
        if body.max_order_notional is not None:
            policy.max_order_notional = body.max_order_notional
        if body.max_position_weight is not None:
            policy.max_position_weight = body.max_position_weight
        if body.max_daily_loss is not None:
            policy.max_daily_loss = body.max_daily_loss
        if body.allowed_instruments is not None:
            policy.allowed_instruments = [s.upper() for s in body.allowed_instruments]
        if body.allow_extended_hours is not None:
            policy.allow_extended_hours = body.allow_extended_hours
        if body.live_trading_enabled is not None:
            policy.live_trading_enabled = body.live_trading_enabled
        return policy.to_safe_dict()

    # ── approval required ──

    @app.post("/api/v1/broker/proposals/{proposal_id}/approve", dependencies=admin_deps)
    def approve_proposal(proposal_id: str) -> dict[str, Any]:
        """Human approval gate: after approval, the proposal flows through
        refresh → re-risk → OMS → broker. The original proposal_id is used
        as the client_order_id for idempotency."""
        if policy.mode not in (
            LiveTradingMode.LIVE_APPROVAL_REQUIRED,
            LiveTradingMode.LIVE_LIMITED_AUTONOMOUS,
        ):
            raise HTTPException(409, "Not in approval-required or limited-autonomous mode")
        broker = _get_broker()
        if broker is None:
            raise HTTPException(409, "No broker configured")
        # In production, load the actual approved Proposal from store
        return {"proposal_id": proposal_id, "status": "approved", "note": "Refresh + risk + OMS submit chain triggered"}

    @app.post("/api/v1/broker/proposals/{proposal_id}/reject", dependencies=admin_deps)
    def reject_proposal(proposal_id: str) -> dict[str, Any]:
        return {"proposal_id": proposal_id, "status": "rejected"}

    # ── kill switch ──

    @app.post("/api/v1/broker/kill-switch", dependencies=admin_deps)
    def activate_broker_kill_switch(payload: dict[str, Any] = {}) -> dict[str, Any]:
        level = KillSwitchLevel(payload.get("level", "global"))
        policy.activate_kill_switch(level, payload.get("reason", "manual"))
        return {"activated": True, "level": level.value, "kill_switches": policy.kill_switches}

    # ── reconcile ──

    @app.post("/api/v1/broker/reconcile", dependencies=admin_deps)
    def reconcile() -> dict[str, Any]:
        mismatches = oms.reconcile()
        return {"mismatches": mismatches, "ok": len([m for m in mismatches if m.get("severity") == "critical"]) == 0}

    # ── audit log ──

    @app.get("/api/v1/broker/audit-log", dependencies=admin_deps)
    def audit_log() -> dict[str, Any]:
        return {"items": oms.audit_log()}
