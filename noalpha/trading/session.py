"""TradingSession model + SQLite persistence for the autonomous engine.

Everything the autonomous loop decides is persisted here:
  trading_sessions — session lifecycle/config/state
  decision_events  — the AI decision feed (scan/candidate/research/proposal/
                     risk/order/fill/no_action/...), source of truth for UI
  equity_curve     — per-cycle equity snapshots (+ benchmark)
  broker_state     — full serialized VirtualBroker for crash recovery
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..i18n import DEFAULT_LANG


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStatus(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    HALTED = "halted"  # emergency stop
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class TradingSession:
    session_id: str = field(default_factory=lambda: str(uuid4())[:12])
    mode: str = "simulation"
    status: SessionStatus = SessionStatus.CREATED
    initial_capital: float = 100000.0
    current_equity: float = 100000.0
    cash: float = 100000.0
    currency: str = "USD"
    started_at: str = ""
    stopped_at: str = ""
    created_at: str = field(default_factory=_now)
    strategy_version: str = "autonomous-v1"
    research_mode: str = "deterministic"
    allowed_markets: list[str] = field(default_factory=lambda: ["CN", "US"])
    universe: str = "watchlist"
    risk_profile: str = "balanced"
    scan_interval: int = 300
    max_position_weight: float = 0.10
    max_daily_loss_pct: float = 0.05
    max_orders_per_day: int = 20
    allow_closed_market_fills: bool = False
    # Language for narratives this session generates. Applies at write time, so
    # changing it affects future cycles only — see noalpha/i18n.py.
    lang: str = DEFAULT_LANG
    cycle_count: int = 0
    risk_rejections: int = 0
    last_cycle_at: str = ""
    next_cycle_at: str = ""
    error: str = ""
    # Trading-day risk counters (TradingDayRiskState). Persisted so a restart
    # cannot reset today's realized-loss limit — "restart to clear the risk
    # limit" would otherwise be a working bypass of a hard control.
    risk_day_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "mode": self.mode, "status": self.status.value,
            "initial_capital": self.initial_capital, "current_equity": round(self.current_equity, 2),
            "cash": round(self.cash, 2), "currency": self.currency,
            "started_at": self.started_at, "stopped_at": self.stopped_at,
            "created_at": self.created_at, "strategy_version": self.strategy_version,
            "research_mode": self.research_mode, "allowed_markets": self.allowed_markets,
            "universe": self.universe, "risk_profile": self.risk_profile,
            "scan_interval": self.scan_interval,
            "max_position_weight": self.max_position_weight,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_orders_per_day": self.max_orders_per_day,
            "allow_closed_market_fills": self.allow_closed_market_fills,
            "lang": self.lang,
            "cycle_count": self.cycle_count, "risk_rejections": self.risk_rejections,
            "last_cycle_at": self.last_cycle_at, "next_cycle_at": self.next_cycle_at,
            "error": self.error,
            "risk_day_state": self.risk_day_state,
        }


@dataclass
class DecisionEvent:
    event_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    cycle: int = 0
    timestamp: str = field(default_factory=_now)
    event_type: str = ""  # scan / candidate / research / proposal / risk / order / fill / position / no_action / exit / error / session
    secid: str = ""
    symbol: str = ""
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "session_id": self.session_id, "cycle": self.cycle,
            "timestamp": self.timestamp, "event_type": self.event_type,
            "secid": self.secid, "symbol": self.symbol, "message": self.message,
            "payload": self.payload,
        }


class TradingStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                create table if not exists trading_sessions (
                  session_id text primary key,
                  status text not null,
                  created_at text not null,
                  session_json text not null
                );
                create table if not exists decision_events (
                  event_id text primary key,
                  session_id text not null,
                  cycle integer not null,
                  timestamp text not null,
                  event_type text not null,
                  secid text,
                  symbol text,
                  message text,
                  payload_json text
                );
                create index if not exists idx_events_session on decision_events(session_id, timestamp);
                create table if not exists equity_curve (
                  session_id text not null,
                  cycle integer not null,
                  timestamp text not null,
                  equity real not null,
                  cash real not null,
                  market_value real not null,
                  benchmark_value real,
                  primary key (session_id, cycle, timestamp)
                );
                create table if not exists broker_state (
                  session_id text primary key,
                  updated_at text not null,
                  state_json text not null
                );
                create table if not exists daily_reports (
                  report_id text primary key,
                  session_id text not null,
                  date text not null,
                  market text not null,
                  report_json text not null,
                  created_at text not null
                );
                create index if not exists idx_reports_session on daily_reports(session_id);
                create table if not exists overnight_events (
                  event_id text primary key,
                  session_id text not null,
                  event_type text not null,
                  summary text,
                  affected_symbols text,
                  created_at text not null
                );
                create table if not exists pending_actions (
                  plan_id text primary key,
                  session_id text not null,
                  symbol text not null,
                  action text not null,
                  reason text,
                  revalidated integer default 0,
                  created_at text not null
                );
                create table if not exists proposals (
                  proposal_id text primary key,
                  session_id text not null,
                  cycle integer,
                  symbol text,
                  action text,
                  target_weight real,
                  confidence real,
                  decision_mode text,
                  status text not null,
                  created_at text,
                  updated_at text,
                  proposal_json text not null
                );
                create index if not exists idx_proposals_session on proposals(session_id, created_at);
                create table if not exists theses (
                  thesis_id text primary key,
                  session_id text not null,
                  symbol text not null,
                  status text not null,
                  thesis_text text,
                  supporting_evidence_ids text,
                  risks text,
                  invalidation_conditions text,
                  expected_horizon text,
                  confidence real,
                  proposal_id text,
                  created_at text,
                  updated_at text,
                  closed_at text,
                  exit_reason text,
                  exit_rationale text,
                  realized_pnl real
                );
                create index if not exists idx_theses_session on theses(session_id);
                """
            )

    # ── sessions ──

    def save_session(self, session: TradingSession) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into trading_sessions(session_id,status,created_at,session_json) values(?,?,?,?)"
                " on conflict(session_id) do update set status=excluded.status, session_json=excluded.session_json",
                (session.session_id, session.status.value, session.created_at,
                 json.dumps(session.to_dict(), sort_keys=True)),
            )

    def load_session(self, session_id: str) -> TradingSession | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "select session_json from trading_sessions where session_id=?", (session_id,)
            ).fetchone()
        if not row:
            return None
        return self._session_from_json(row["session_json"])

    def list_sessions(self, limit: int = 20) -> list[TradingSession]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select session_json from trading_sessions order by created_at desc limit ?", (limit,)
            ).fetchall()
        return [self._session_from_json(r["session_json"]) for r in rows]

    @staticmethod
    def _session_from_json(raw: str) -> TradingSession:
        d = json.loads(raw)
        session = TradingSession(
            session_id=d["session_id"], mode=d.get("mode", "simulation"),
            status=SessionStatus(d.get("status", "created")),
            initial_capital=d.get("initial_capital", 100000.0),
            current_equity=d.get("current_equity", 100000.0),
            cash=d.get("cash", 100000.0), currency=d.get("currency", "USD"),
            started_at=d.get("started_at", ""), stopped_at=d.get("stopped_at", ""),
            created_at=d.get("created_at", ""),
            strategy_version=d.get("strategy_version", "autonomous-v1"),
            research_mode=d.get("research_mode", "deterministic"),
            allowed_markets=d.get("allowed_markets", ["CN", "US"]),
            universe=d.get("universe", "watchlist"),
            risk_profile=d.get("risk_profile", "balanced"),
            scan_interval=d.get("scan_interval", 300),
            max_position_weight=d.get("max_position_weight", 0.10),
            max_daily_loss_pct=d.get("max_daily_loss_pct", 0.05),
            max_orders_per_day=d.get("max_orders_per_day", 20),
            allow_closed_market_fills=d.get("allow_closed_market_fills", False),
            lang=d.get("lang", DEFAULT_LANG),
            cycle_count=d.get("cycle_count", 0),
            risk_rejections=d.get("risk_rejections", 0),
            last_cycle_at=d.get("last_cycle_at", ""), next_cycle_at=d.get("next_cycle_at", ""),
            error=d.get("error", ""),
            risk_day_state=d.get("risk_day_state") or {},
        )
        return session

    # ── decision feed ──

    def add_event(self, event: DecisionEvent) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into decision_events(event_id,session_id,cycle,timestamp,event_type,secid,symbol,message,payload_json)"
                " values(?,?,?,?,?,?,?,?,?)",
                (event.event_id, event.session_id, event.cycle, event.timestamp,
                 event.event_type, event.secid, event.symbol, event.message,
                 json.dumps(event.payload, sort_keys=True, default=str)),
            )

    def list_events(self, session_id: str, limit: int = 200, event_type: str | None = None) -> list[dict[str, Any]]:
        query = "select * from decision_events where session_id=?"
        params: list[Any] = [session_id]
        if event_type:
            query += " and event_type=?"
            params.append(event_type)
        query += " order by timestamp desc limit ?"
        params.append(limit)
        with self._lock, self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "event_id": r["event_id"], "session_id": r["session_id"], "cycle": r["cycle"],
                "timestamp": r["timestamp"], "event_type": r["event_type"],
                "secid": r["secid"], "symbol": r["symbol"], "message": r["message"],
                "payload": json.loads(r["payload_json"] or "{}"),
            }
            for r in rows
        ]

    def events_for_proposal(self, session_id: str, proposal_id: str) -> list[dict[str, Any]]:
        events = self.list_events(session_id, limit=2000)
        return [e for e in events if e["payload"].get("proposal_id") == proposal_id]

    # ── equity curve ──

    def add_equity_point(self, session_id: str, cycle: int, equity: float, cash: float,
                         market_value: float, benchmark_value: float | None = None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert or replace into equity_curve(session_id,cycle,timestamp,equity,cash,market_value,benchmark_value)"
                " values(?,?,?,?,?,?,?)",
                (session_id, cycle, _now(), equity, cash, market_value, benchmark_value),
            )

    def equity_curve(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from equity_curve where session_id=? order by timestamp", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── proposals (first-class, status kept in sync by the loop) ──

    def save_proposal(self, session_id: str, cycle: int, proposal: Any) -> None:
        """Upsert the full proposal INCLUDING its current status. Called at
        every lifecycle stage so an executed proposal can never stay
        'pending' in any reader."""
        with self._lock, self._conn() as conn:
            conn.execute(
                """insert into proposals(proposal_id,session_id,cycle,symbol,action,
                     target_weight,confidence,decision_mode,status,created_at,updated_at,proposal_json)
                   values(?,?,?,?,?,?,?,?,?,?,?,?)
                   on conflict(proposal_id) do update set
                     status=excluded.status, target_weight=excluded.target_weight,
                     decision_mode=excluded.decision_mode,
                     updated_at=excluded.updated_at, proposal_json=excluded.proposal_json""",
                (proposal.proposal_id, session_id, cycle, proposal.symbol,
                 proposal.action.value, proposal.target_weight, proposal.confidence,
                 proposal.decision_mode, proposal.status, proposal.created_at,
                 _now(), json.dumps(proposal.to_dict(), sort_keys=True, default=str)),
            )

    def list_proposals(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from proposals where session_id=? order by created_at desc limit ?",
                (session_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["proposal_json"])
            d["cycle"] = r["cycle"]
            d["updated_at"] = r["updated_at"]
            out.append(d)
        return out

    def active_proposal(self, session_id: str, symbol: str) -> dict[str, Any] | None:
        """Most recent non-terminal proposal for one instrument (dedup guard)."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "select proposal_json from proposals where session_id=? and symbol=?"
                " and status in ('pending','approved','adjusted')"
                " order by created_at desc limit 1",
                (session_id, symbol),
            ).fetchone()
        return json.loads(row["proposal_json"]) if row else None

    # ── theses ──

    def save_thesis(self, thesis: Any, session_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """insert into theses(thesis_id,session_id,symbol,status,thesis_text,
                   supporting_evidence_ids,risks,invalidation_conditions,expected_horizon,
                   confidence,proposal_id,created_at,updated_at,closed_at,exit_reason,
                   exit_rationale,realized_pnl)
                   values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   on conflict(thesis_id) do update set
                     status=excluded.status,thesis_text=excluded.thesis_text,
                     supporting_evidence_ids=excluded.supporting_evidence_ids,
                     risks=excluded.risks,
                     invalidation_conditions=excluded.invalidation_conditions,
                     updated_at=excluded.updated_at,closed_at=excluded.closed_at,
                     exit_reason=excluded.exit_reason,exit_rationale=excluded.exit_rationale,
                     realized_pnl=excluded.realized_pnl""",
                (thesis.thesis_id, session_id, thesis.symbol, thesis.status.value,
                 thesis.thesis_text, json.dumps(thesis.supporting_evidence_ids),
                 json.dumps(thesis.risks), json.dumps(thesis.invalidation_conditions),
                 thesis.expected_horizon, thesis.confidence, thesis.proposal_id,
                 thesis.created_at, thesis.updated_at, thesis.closed_at,
                 thesis.exit_reason, thesis.exit_rationale, thesis.realized_pnl),
            )

    def list_theses(self, session_id: str, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            if symbol:
                rows = conn.execute(
                    "select * from theses where session_id=? and symbol=? order by created_at desc",
                    (session_id, symbol),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select * from theses where session_id=? order by created_at desc",
                    (session_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    # ── broker state snapshots ──

    def save_broker_state(self, session_id: str, state: dict[str, Any]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into broker_state(session_id,updated_at,state_json) values(?,?,?)"
                " on conflict(session_id) do update set updated_at=excluded.updated_at, state_json=excluded.state_json",
                (session_id, _now(), json.dumps(state, sort_keys=True, default=str)),
            )

    def load_broker_state(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "select state_json from broker_state where session_id=?", (session_id,)
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    # ── daily reports ──

    def save_daily_report(self, report: dict[str, Any]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into daily_reports(report_id,session_id,date,market,report_json,created_at)"
                " values(?,?,?,?,?,?)"
                " on conflict(report_id) do update set report_json=excluded.report_json",
                (report["report_id"], report["session_id"], report["date"],
                 report["market"], json.dumps(report, sort_keys=True),
                 datetime.now(timezone.utc).isoformat()),
            )

    def list_daily_reports(self, session_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            if session_id:
                rows = conn.execute(
                    "select report_json from daily_reports where session_id=? order by date desc limit ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select report_json from daily_reports order by date desc limit ?",
                    (limit,),
                ).fetchall()
        return [json.loads(r["report_json"]) for r in rows]

    # ── overnight events ──

    def save_overnight_event(self, session_id: str, event_type: str, summary: str,
                            affected_symbols: list[str]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into overnight_events(event_id,session_id,event_type,summary,affected_symbols,created_at)"
                " values(?,?,?,?,?,?)",
                (str(uuid4())[:12], session_id, event_type, summary,
                 json.dumps(affected_symbols), datetime.now(timezone.utc).isoformat()),
            )

    def list_overnight_events(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from overnight_events where session_id=? order by created_at desc limit ?",
                (session_id, limit),
            ).fetchall()
        return [{
            "event_id": r["event_id"], "event_type": r["event_type"],
            "summary": r["summary"],
            "affected_symbols": json.loads(r["affected_symbols"] or "[]"),
            "created_at": r["created_at"],
        } for r in rows]

    # ── pending actions ──

    def save_pending_action(self, session_id: str, plan_id: str, symbol: str,
                            action: str, reason: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into pending_actions(plan_id,session_id,symbol,action,reason,revalidated,created_at)"
                " values(?,?,?,?,?,0,?)"
                " on conflict(plan_id) do update set reason=excluded.reason, revalidated=excluded.revalidated",
                (plan_id, session_id, symbol, action, reason,
                 datetime.now(timezone.utc).isoformat()),
            )

    def list_pending_actions(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from pending_actions where session_id=? order by created_at desc",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_pending_revalidated(self, plan_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("update pending_actions set revalidated=1 where plan_id=?", (plan_id,))
