from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from noalpha.domain import Instrument, Market, stable_id
from noalpha.watch_models import CaseStatus, InvestigationCase, ScanPolicy, ThesisSnapshot, WatchItem, Watchlist


def _dt(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _instrument_to_json(instrument: Instrument) -> str:
    return json.dumps(
        {
            "symbol": instrument.symbol,
            "exchange": instrument.exchange,
            "market": instrument.market.value,
            "currency": instrument.currency,
            "company_name": instrument.company_name,
            "sector": instrument.sector,
            "industry": instrument.industry,
            "cik": instrument.cik,
        },
        sort_keys=True,
    )


def _instrument_from_json(data: str) -> Instrument:
    raw = json.loads(data)
    return Instrument(
        symbol=raw["symbol"],
        exchange=raw["exchange"],
        market=Market(raw["market"]),
        currency=raw["currency"],
        company_name=raw["company_name"],
        sector=raw.get("sector"),
        industry=raw.get("industry"),
        cik=raw.get("cik"),
    )


class SQLiteRepository:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=WAL")
        self.conn.execute("pragma busy_timeout=5000")
        self.conn.execute("pragma foreign_keys=ON")
        self._migrate()

    def __enter__(self) -> "SQLiteRepository":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            create table if not exists watchlists (
              watchlist_id text primary key,
              name text not null unique,
              created_at text not null,
              updated_at text not null
            );
            create table if not exists watch_items (
              watchlist_id text not null,
              symbol text not null,
              instrument_json text not null,
              enabled integer not null,
              scan_policy_json text not null,
              priority integer not null,
              added_at text not null,
              last_scanned_at text,
              cooldown_until text,
              primary key (watchlist_id, symbol)
            );
            create table if not exists cases (
              case_id text primary key,
              symbol text not null,
              instrument_json text not null,
              status text not null,
              opened_at text not null,
              updated_at text not null,
              closed_at text,
              trigger_cluster_ids_json text not null,
              primary_trigger text not null,
              severity real not null,
              current_thesis text not null,
              counter_thesis text not null,
              unknowns_json text not null,
              confidence real not null,
              latest_report_id text,
              evidence_ids_json text not null,
              cooldown_until text
            );
            create index if not exists idx_cases_symbol_status on cases(symbol, status);
            create table if not exists thesis_snapshots (
              snapshot_id text primary key,
              case_id text not null,
              created_at text not null,
              summary text not null,
              bull_case_json text not null,
              bear_case_json text not null,
              risks_json text not null,
              unknowns_json text not null,
              confidence real not null,
              evidence_ids_json text not null,
              report_json text not null,
              thesis_change text not null
            );
            """
        )
        self.conn.commit()

    def create_watchlist(self, name: str) -> Watchlist:
        now = datetime.now(timezone.utc)
        watchlist = Watchlist(stable_id("watchlist", name), name, now, now)
        self.conn.execute(
            "insert or ignore into watchlists(watchlist_id,name,created_at,updated_at) values(?,?,?,?)",
            (watchlist.watchlist_id, watchlist.name, _dt(watchlist.created_at), _dt(watchlist.updated_at)),
        )
        self.conn.commit()
        return self.get_watchlist(name) or watchlist

    def get_watchlist(self, name: str = "default") -> Watchlist | None:
        row = self.conn.execute("select * from watchlists where name=?", (name,)).fetchone()
        if not row:
            return None
        return Watchlist(row["watchlist_id"], row["name"], _parse_dt(row["created_at"]), _parse_dt(row["updated_at"]))  # type: ignore[arg-type]

    def add_watch_item(
        self,
        watchlist: Watchlist,
        instrument: Instrument,
        policy: ScanPolicy | None = None,
        *,
        priority: int = 0,
        enabled: bool = True,
    ) -> WatchItem:
        now = datetime.now(timezone.utc)
        item = WatchItem(watchlist.watchlist_id, instrument, enabled, policy or ScanPolicy(), priority, now)
        self.conn.execute(
            """
            insert into watch_items(watchlist_id,symbol,instrument_json,enabled,scan_policy_json,priority,added_at,last_scanned_at,cooldown_until)
            values(?,?,?,?,?,?,?,?,?)
            on conflict(watchlist_id, symbol) do update set
              instrument_json=excluded.instrument_json,
              enabled=excluded.enabled,
              scan_policy_json=excluded.scan_policy_json,
              priority=excluded.priority
            """,
            (
                item.watchlist_id,
                item.instrument.symbol,
                _instrument_to_json(item.instrument),
                1 if item.enabled else 0,
                json.dumps(item.scan_policy.to_dict(), sort_keys=True),
                item.priority,
                _dt(item.added_at),
                None,
                None,
            ),
        )
        self.conn.commit()
        return item

    def list_watch_items(self, watchlist_id: str) -> list[WatchItem]:
        rows = self.conn.execute(
            "select * from watch_items where watchlist_id=? order by priority desc, symbol",
            (watchlist_id,),
        ).fetchall()
        return [self._watch_item_from_row(row) for row in rows]

    def update_watch_item_scan(self, item: WatchItem, scanned_at: datetime, cooldown_until: datetime | None) -> None:
        self.conn.execute(
            "update watch_items set last_scanned_at=?, cooldown_until=? where watchlist_id=? and symbol=?",
            (_dt(scanned_at), _dt(cooldown_until), item.watchlist_id, item.instrument.symbol),
        )
        self.conn.commit()

    def _watch_item_from_row(self, row: sqlite3.Row) -> WatchItem:
        return WatchItem(
            watchlist_id=row["watchlist_id"],
            instrument=_instrument_from_json(row["instrument_json"]),
            enabled=bool(row["enabled"]),
            scan_policy=ScanPolicy.from_dict(json.loads(row["scan_policy_json"])),
            priority=int(row["priority"]),
            added_at=_parse_dt(row["added_at"]),  # type: ignore[arg-type]
            last_scanned_at=_parse_dt(row["last_scanned_at"]),
            cooldown_until=_parse_dt(row["cooldown_until"]),
        )

    def create_case(self, case: InvestigationCase) -> InvestigationCase:
        self.conn.execute(
            """
            insert into cases(case_id,symbol,instrument_json,status,opened_at,updated_at,closed_at,trigger_cluster_ids_json,
              primary_trigger,severity,current_thesis,counter_thesis,unknowns_json,confidence,latest_report_id,evidence_ids_json,cooldown_until)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            self._case_values(case),
        )
        self.conn.commit()
        return case

    def get_case(self, case_id: str) -> InvestigationCase | None:
        row = self.conn.execute("select * from cases where case_id=?", (case_id,)).fetchone()
        return self._case_from_row(row) if row else None

    def find_open_case(self, symbol: str) -> InvestigationCase | None:
        rows = self.conn.execute(
            "select * from cases where symbol=? and status in ('DETECTED','INVESTIGATING','OPEN','MONITORING','UPDATED','BLOCKED') order by updated_at desc",
            (symbol.upper(),),
        ).fetchall()
        return self._case_from_row(rows[0]) if rows else None

    def update_case(self, case: InvestigationCase) -> InvestigationCase:
        self.conn.execute(
            """
            update cases set status=?, updated_at=?, closed_at=?, trigger_cluster_ids_json=?, primary_trigger=?, severity=?,
              current_thesis=?, counter_thesis=?, unknowns_json=?, confidence=?, latest_report_id=?, evidence_ids_json=?, cooldown_until=?
            where case_id=?
            """,
            (
                case.status.value,
                _dt(case.updated_at),
                _dt(case.closed_at),
                json.dumps(list(case.trigger_cluster_ids), sort_keys=True),
                case.primary_trigger,
                case.severity,
                case.current_thesis,
                case.counter_thesis,
                json.dumps(list(case.unknowns), sort_keys=True),
                case.confidence,
                case.latest_report_id,
                json.dumps(list(case.evidence_ids), sort_keys=True),
                _dt(case.cooldown_until),
                case.case_id,
            ),
        )
        self.conn.commit()
        return case

    def list_cases(self) -> list[InvestigationCase]:
        rows = self.conn.execute("select * from cases order by updated_at desc").fetchall()
        return [self._case_from_row(row) for row in rows]

    def close_case(self, case_id: str, closed_at: datetime) -> InvestigationCase:
        case = self.get_case(case_id)
        if case is None:
            raise KeyError(case_id)
        updated = replace(case, status=CaseStatus.CLOSED, updated_at=closed_at, closed_at=closed_at)
        return self.update_case(updated)

    def add_snapshot(self, snapshot: ThesisSnapshot) -> ThesisSnapshot:
        self.conn.execute(
            """
            insert into thesis_snapshots(snapshot_id,case_id,created_at,summary,bull_case_json,bear_case_json,risks_json,
              unknowns_json,confidence,evidence_ids_json,report_json,thesis_change)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot.snapshot_id,
                snapshot.case_id,
                _dt(snapshot.created_at),
                snapshot.summary,
                json.dumps(list(snapshot.bull_case), sort_keys=True),
                json.dumps(list(snapshot.bear_case), sort_keys=True),
                json.dumps(list(snapshot.risks), sort_keys=True),
                json.dumps(list(snapshot.unknowns), sort_keys=True),
                snapshot.confidence,
                json.dumps(list(snapshot.evidence_ids), sort_keys=True),
                json.dumps(snapshot.report, sort_keys=True),
                snapshot.thesis_change,
            ),
        )
        self.conn.commit()
        return snapshot

    def list_snapshots(self, case_id: str) -> list[ThesisSnapshot]:
        rows = self.conn.execute(
            "select * from thesis_snapshots where case_id=? order by created_at",
            (case_id,),
        ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def _case_values(self, case: InvestigationCase) -> tuple[Any, ...]:
        return (
            case.case_id,
            case.instrument.symbol,
            _instrument_to_json(case.instrument),
            case.status.value,
            _dt(case.opened_at),
            _dt(case.updated_at),
            _dt(case.closed_at),
            json.dumps(list(case.trigger_cluster_ids), sort_keys=True),
            case.primary_trigger,
            case.severity,
            case.current_thesis,
            case.counter_thesis,
            json.dumps(list(case.unknowns), sort_keys=True),
            case.confidence,
            case.latest_report_id,
            json.dumps(list(case.evidence_ids), sort_keys=True),
            _dt(case.cooldown_until),
        )

    def _case_from_row(self, row: sqlite3.Row) -> InvestigationCase:
        return InvestigationCase(
            case_id=row["case_id"],
            instrument=_instrument_from_json(row["instrument_json"]),
            status=CaseStatus(row["status"]),
            opened_at=_parse_dt(row["opened_at"]),  # type: ignore[arg-type]
            updated_at=_parse_dt(row["updated_at"]),  # type: ignore[arg-type]
            closed_at=_parse_dt(row["closed_at"]),
            trigger_cluster_ids=tuple(json.loads(row["trigger_cluster_ids_json"])),
            primary_trigger=row["primary_trigger"],
            severity=float(row["severity"]),
            current_thesis=row["current_thesis"],
            counter_thesis=row["counter_thesis"],
            unknowns=tuple(json.loads(row["unknowns_json"])),
            confidence=float(row["confidence"]),
            latest_report_id=row["latest_report_id"],
            evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
            cooldown_until=_parse_dt(row["cooldown_until"]),
        )

    def _snapshot_from_row(self, row: sqlite3.Row) -> ThesisSnapshot:
        return ThesisSnapshot(
            snapshot_id=row["snapshot_id"],
            case_id=row["case_id"],
            created_at=_parse_dt(row["created_at"]),  # type: ignore[arg-type]
            summary=row["summary"],
            bull_case=tuple(json.loads(row["bull_case_json"])),
            bear_case=tuple(json.loads(row["bear_case_json"])),
            risks=tuple(json.loads(row["risks_json"])),
            unknowns=tuple(json.loads(row["unknowns_json"])),
            confidence=float(row["confidence"]),
            evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
            report=json.loads(row["report_json"]),
            thesis_change=row["thesis_change"],
        )
