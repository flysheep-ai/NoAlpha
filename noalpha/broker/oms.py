"""Order Management System — the ONLY path to real money.

Write-ahead persistence, idempotency, state machine, timeout resolution.
LLM is NEVER given access to this module's submit/cancel/replace methods.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from noalpha.broker.adapter import BrokerAdapter, BrokerMode
from noalpha.broker.policy import KillSwitchLevel, LiveTradingPolicy

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrderIntentStatus(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    RISK_APPROVED = "risk_approved"
    PERSISTED = "persisted"            # write-ahead committed
    POLICY_APPROVED = "policy_approved"  # human approval (for LIVE_APPROVAL_REQUIRED)
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"                  # timeout — must reconcile before retry
    RECOVERY_REQUIRED = "recovery_required"
    FAILED = "failed"


@dataclass
class OrderIntent:
    intent_id: str = field(default_factory=lambda: str(uuid4()))
    client_order_id: str = ""            # stable idempotency key
    proposal_id: str = ""
    decision_id: str = ""
    account_id: str = ""
    symbol: str = ""
    side: str = ""                       # buy / sell
    order_type: str = "market"
    quantity: float = 0.0
    limit_price: float | None = None
    time_in_force: str = "day"
    created_at: str = field(default_factory=_now)
    expires_at: str = ""
    broker_order_id: str = ""
    risk_result: dict[str, Any] = field(default_factory=dict)
    policy_version: str = ""
    status: OrderIntentStatus = OrderIntentStatus.CREATED
    error: str = ""
    fill_quantity: float = 0.0
    fill_avg_price: float = 0.0
    commission: float = 0.0
    broker_response: dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    last_error_at: str = ""

    def idempotency_key(self) -> str:
        return self.client_order_id or f"{self.proposal_id}-{self.created_at}"

    def is_terminal(self) -> bool:
        return self.status in (
            OrderIntentStatus.FILLED, OrderIntentStatus.CANCELLED,
            OrderIntentStatus.REJECTED, OrderIntentStatus.EXPIRED,
            OrderIntentStatus.FAILED,
        )

    def is_in_flight(self) -> bool:
        return self.status in (
            OrderIntentStatus.SUBMITTING, OrderIntentStatus.SUBMITTED,
            OrderIntentStatus.ACKNOWLEDGED, OrderIntentStatus.PARTIALLY_FILLED,
            OrderIntentStatus.UNKNOWN, OrderIntentStatus.RECOVERY_REQUIRED,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id, "client_order_id": self.client_order_id,
            "proposal_id": self.proposal_id, "decision_id": self.decision_id,
            "account_id": self.account_id, "symbol": self.symbol,
            "side": self.side, "order_type": self.order_type,
            "quantity": self.quantity, "limit_price": self.limit_price,
            "time_in_force": self.time_in_force,
            "created_at": self.created_at, "expires_at": self.expires_at,
            "broker_order_id": self.broker_order_id,
            "risk_result": self.risk_result,
            "status": self.status.value,
            "error": self.error,
            "fill_quantity": self.fill_quantity,
            "fill_avg_price": self.fill_avg_price,
            "commission": self.commission,
            "retry_count": self.retry_count,
        }


# ── OMS ──

class OrderManagementSystem:
    """The ONLY path to real-money order submission.

    Guarantees:
    1. Write-ahead: Intent is persisted BEFORE broker call
    2. Idempotency: client_order_id prevents duplicates
    3. Timeout resolution: UNKNOWN → reconcile → decide (never blind retry)
    """

    def __init__(self, db_path: Path | str, broker: BrokerAdapter | None = None,
                 policy: LiveTradingPolicy | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.broker = broker
        self.policy = policy or LiveTradingPolicy()
        self._lock = threading.Lock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript("""
                create table if not exists order_intents (
                    intent_id text primary key,
                    client_order_id text not null,
                    proposal_id text, decision_id text,
                    account_id text, symbol text, side text,
                    order_type text, quantity real, limit_price real,
                    time_in_force text,
                    created_at text, expires_at text,
                    broker_order_id text,
                    risk_result text,
                    status text not null default 'created',
                    error text,
                    fill_quantity real default 0,
                    fill_avg_price real default 0,
                    commission real default 0,
                    broker_response text,
                    retry_count integer default 0,
                    last_error_at text
                );
                create index if not exists idx_intents_status on order_intents(status);
                create index if not exists idx_intents_client_id on order_intents(client_order_id);
                create unique index if not exists uq_intents_client_id
                  on order_intents(client_order_id)
                  where client_order_id <> '';
                create table if not exists broker_audit_log (
                    audit_id text primary key,
                    timestamp text not null,
                    event_type text not null,
                    intent_id text,
                    account_id text,
                    user_id text,
                    details text
                );
            """)

    # ── intent persistence ──

    def save_intent(self, intent: OrderIntent) -> OrderIntent:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """insert or replace into order_intents
                   (intent_id,client_order_id,proposal_id,decision_id,account_id,symbol,side,
                    order_type,quantity,limit_price,time_in_force,created_at,expires_at,
                    broker_order_id,risk_result,status,error,fill_quantity,fill_avg_price,
                    commission,broker_response,retry_count,last_error_at)
                   values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (intent.intent_id, intent.client_order_id, intent.proposal_id,
                 intent.decision_id, intent.account_id, intent.symbol, intent.side,
                 intent.order_type, intent.quantity, intent.limit_price,
                 intent.time_in_force, intent.created_at, intent.expires_at,
                 intent.broker_order_id, json.dumps(intent.risk_result),
                 intent.status.value, intent.error, intent.fill_quantity,
                 intent.fill_avg_price, intent.commission,
                 json.dumps(intent.broker_response), intent.retry_count,
                 intent.last_error_at),
            )
        return intent

    def load_intent(self, intent_id: str) -> OrderIntent | None:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "select * from order_intents where intent_id=?", (intent_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_intent(row)

    def load_intent_by_client_id(self, client_order_id: str) -> OrderIntent | None:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "select * from order_intents where client_order_id=? order by created_at desc limit 1",
                (client_order_id,),
            ).fetchone()
        if not row:
            return None
        return self._row_to_intent(row)

    def list_intents(self, status: str | None = None, limit: int = 50) -> list[OrderIntent]:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            if status:
                rows = conn.execute(
                    "select * from order_intents where status=? order by created_at desc limit ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select * from order_intents order by created_at desc limit ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_intent(r) for r in rows]

    def _row_to_intent(self, row: Any) -> OrderIntent:
        return OrderIntent(
            intent_id=row[0], client_order_id=row[1], proposal_id=row[2],
            decision_id=row[3], account_id=row[4], symbol=row[5], side=row[6],
            order_type=row[7], quantity=row[8], limit_price=row[9],
            time_in_force=row[10], created_at=row[11], expires_at=row[12],
            broker_order_id=row[13],
            risk_result=json.loads(row[14] or "{}"),
            status=OrderIntentStatus(row[15]),
            error=row[16] or "",
            fill_quantity=row[17] or 0, fill_avg_price=row[18] or 0,
            commission=row[19] or 0,
            broker_response=json.loads(row[20] or "{}"),
            retry_count=row[21] or 0, last_error_at=row[22] or "",
        )

    # ── write-ahead submit ──

    def submit(self, intent: OrderIntent, broker: BrokerAdapter | None = None) -> OrderIntent:
        """Write-ahead → submit → resolve. The ONLY real-money entry point."""
        # 0. Idempotency is a hard safety boundary. A repeated client_order_id
        # must return the existing order intent instead of creating a new one.
        if intent.client_order_id:
            existing = self.load_intent_by_client_id(intent.client_order_id)
            if existing is not None and existing.intent_id != intent.intent_id:
                self._audit("idempotent_replay", existing.intent_id, intent.client_order_id)
                return existing

        # 1. Policy + kill-switch check (ALWAYS first)
        if not self.policy.allows_order(intent):
            intent.status = OrderIntentStatus.REJECTED
            intent.error = "Policy denied"
            self.save_intent(intent)
            self._audit("policy_denied", intent.intent_id, intent.error)
            return intent

        br = broker or self.broker
        if br is None:
            intent.status = OrderIntentStatus.FAILED
            intent.error = "No broker configured"
            self.save_intent(intent)
            return intent

        # 2. Write-ahead persistence
        intent.status = OrderIntentStatus.PERSISTED
        self.save_intent(intent)

        # 3. Submit to broker
        intent.status = OrderIntentStatus.SUBMITTING
        self.save_intent(intent)

        try:
            result = br.submit_order(intent.to_dict())
        except Exception as exc:
            intent.status = OrderIntentStatus.UNKNOWN
            intent.error = f"Submit timeout or error: {exc}"
            intent.last_error_at = _now()
            self.save_intent(intent)
            self._audit("submit_timeout", intent.intent_id, intent.error)
            return intent

        # 4. Map broker response
        broker_id = result.get("id") or result.get("order_id") or ""
        broker_status = result.get("status", "")

        if broker_id:
            intent.broker_order_id = broker_id
            intent.broker_response = result
            if broker_status in ("accepted", "new", "pending_new", "acknowledged"):
                intent.status = OrderIntentStatus.ACKNOWLEDGED
            elif broker_status in ("filled",):
                intent.status = OrderIntentStatus.FILLED
                intent.fill_quantity = float(result.get("filled_qty", intent.quantity))
                intent.fill_avg_price = float(result.get("filled_avg_price", 0))
                self.policy.record_order(intent.quantity * intent.fill_avg_price)
            elif broker_status in ("partially_filled",):
                intent.status = OrderIntentStatus.PARTIALLY_FILLED
                intent.fill_quantity = float(result.get("filled_qty", 0))
                intent.fill_avg_price = float(result.get("filled_avg_price", 0))
                self.policy.record_order(intent.fill_quantity * intent.fill_avg_price)
            elif broker_status in ("rejected", "canceled", "expired"):
                intent.status = OrderIntentStatus.REJECTED
                intent.error = result.get("reject_reason", broker_status)
            self.save_intent(intent)
            self._audit("broker_response", intent.intent_id, f"status={broker_status} id={broker_id}")
        else:
            intent.status = OrderIntentStatus.UNKNOWN
            intent.error = f"Broker returned no order id: {result}"
            intent.last_error_at = _now()
            self.save_intent(intent)

        return intent

    # ── cancel ──

    def cancel(self, intent_id: str) -> OrderIntent:
        intent = self.load_intent(intent_id)
        if intent is None:
            raise ValueError(f"intent {intent_id} not found")
        if intent.is_terminal():
            return intent
        br = self.broker
        if br is None or not intent.broker_order_id:
            intent.status = OrderIntentStatus.CANCELLED
            intent.error = "No broker to cancel with"
            self.save_intent(intent)
            return intent

        intent.status = OrderIntentStatus.CANCEL_PENDING
        self.save_intent(intent)
        try:
            result = br.cancel_order(intent.broker_order_id)
            if result.get("status") == "canceled":
                intent.status = OrderIntentStatus.CANCELLED
            else:
                intent.status = OrderIntentStatus.UNKNOWN
                intent.error = f"Cancel uncertain: {result}"
                intent.last_error_at = _now()
        except Exception as exc:
            intent.status = OrderIntentStatus.UNKNOWN
            intent.error = f"Cancel failed: {exc}"
            intent.last_error_at = _now()
        self.save_intent(intent)
        return intent

    # ── reconcile ──

    def reconcile(self) -> list[dict[str, Any]]:
        """Reconcile all in-flight intents against the broker."""
        if self.broker is None:
            return []
        mismatches: list[dict[str, Any]] = []
        open_orders = self.broker.get_open_orders()
        broker_order_map = {o.get("id", ""): o for o in open_orders}

        for intent in self.list_intents(limit=200):
            if not intent.is_in_flight() and intent.status != OrderIntentStatus.UNKNOWN:
                continue
            bid = intent.broker_order_id
            if bid and bid in broker_order_map:
                bo = broker_order_map[bid]
                broker_status = bo.get("status", "")
                broker_filled = float(bo.get("filled_qty", 0))
                # Sync local state to broker truth
                if broker_status in ("filled",) and intent.fill_quantity != intent.quantity:
                    intent.status = OrderIntentStatus.FILLED
                    intent.fill_quantity = intent.quantity
                    intent.fill_avg_price = float(bo.get("filled_avg_price", 0)) if bo.get("filled_avg_price") else intent.fill_avg_price
                    self.save_intent(intent)
                elif broker_status in ("canceled",) and intent.status != OrderIntentStatus.CANCELLED:
                    intent.status = OrderIntentStatus.CANCELLED
                    self.save_intent(intent)
            elif bid and bid not in broker_order_map:
                # Order not at broker but we think it's live → RECOVERY_REQUIRED
                mismatches.append({
                    "type": "LOCAL_ORDER_MISSING_AT_BROKER",
                    "intent_id": intent.intent_id,
                    "broker_order_id": bid,
                    "local_status": intent.status.value,
                })
                intent.status = OrderIntentStatus.RECOVERY_REQUIRED
                self.save_intent(intent)

        return mismatches

    # ── audit ──

    def _audit(self, event_type: str, intent_id: str, details: str, user_id: str = "system") -> None:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "insert into broker_audit_log(audit_id,timestamp,event_type,intent_id,account_id,user_id,details)"
                " values(?,?,?,?,?,?,?)",
                (str(uuid4())[:12], _now(), event_type, intent_id, "", user_id, details),
            )

    def audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from broker_audit_log order by timestamp desc limit ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
