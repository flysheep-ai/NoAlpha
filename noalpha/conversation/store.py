from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from noalpha.conversation.models import ChatTurn, ConversationMessage, ConversationSession


class ConversationStore:
    def __init__(self, db_path: str = ".cache/api/conversations.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("pragma journal_mode=WAL")
            conn.execute("pragma busy_timeout=5000")
            conn.execute("pragma foreign_keys=ON")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT 'New Chat',
                    created_at TEXT,
                    updated_at TEXT,
                    locale TEXT DEFAULT 'zh-CN',
                    report_language TEXT DEFAULT 'zh-CN',
                    active_subjects TEXT DEFAULT '[]',
                    linked_investigation_ids TEXT DEFAULT '[]',
                    linked_case_ids TEXT DEFAULT '[]',
                    status TEXT DEFAULT 'active'
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT DEFAULT '',
                    created_at TEXT,
                    language TEXT DEFAULT 'zh-CN',
                    status TEXT DEFAULT 'completed',
                    turn_id TEXT DEFAULT '',
                    metadata TEXT DEFAULT '{}',
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_message_id TEXT DEFAULT '',
                    assistant_message_id TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    started_at TEXT,
                    completed_at TEXT,
                    research_mode TEXT DEFAULT 'llm',
                    model TEXT DEFAULT '',
                    intent TEXT DEFAULT '',
                    plan TEXT DEFAULT '{}',
                    metrics TEXT DEFAULT '{}',
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
            """)

    # ── Session CRUD ──

    def create_session(self, session: ConversationSession) -> ConversationSession:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO sessions (session_id, title, created_at, updated_at, locale,
                   report_language, active_subjects, linked_investigation_ids, linked_case_ids, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session.session_id, session.title, session.created_at, session.updated_at,
                 session.locale, session.report_language,
                 json.dumps(session.active_subjects),
                 json.dumps(session.linked_investigation_ids),
                 json.dumps(session.linked_case_ids), session.status),
            )
        return session

    def get_session(self, session_id: str) -> ConversationSession | None:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        return ConversationSession(
            session_id=row[0], title=row[1], created_at=row[2], updated_at=row[3],
            locale=row[4], report_language=row[5],
            active_subjects=json.loads(row[6]) if row[6] else [],
            linked_investigation_ids=json.loads(row[7]) if row[7] else [],
            linked_case_ids=json.loads(row[8]) if row[8] else [],
            status=row[9],
        )

    def list_sessions(self) -> list[ConversationSession]:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 50"
            ).fetchall()
        return [ConversationSession(
            session_id=r[0], title=r[1], created_at=r[2], updated_at=r[3],
            locale=r[4], report_language=r[5],
            active_subjects=json.loads(r[6]) if r[6] else [],
            linked_investigation_ids=json.loads(r[7]) if r[7] else [],
            linked_case_ids=json.loads(r[8]) if r[8] else [],
            status=r[9],
        ) for r in rows]

    def update_session(self, session_id: str, **kwargs: Any) -> None:
        allowed = {"title", "updated_at", "locale", "report_language", "active_subjects",
                   "linked_investigation_ids", "linked_case_ids", "status"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [session_id]
        if "active_subjects" in updates:
            vals[list(updates.keys()).index("active_subjects")] = json.dumps(updates["active_subjects"])
        if "linked_investigation_ids" in updates:
            vals[list(updates.keys()).index("linked_investigation_ids")] = json.dumps(updates["linked_investigation_ids"])
        if "linked_case_ids" in updates:
            vals[list(updates.keys()).index("linked_case_ids")] = json.dumps(updates["linked_case_ids"])
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(f"UPDATE sessions SET {set_clause} WHERE session_id = ?", vals)

    def delete_session(self, session_id: str) -> None:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    # ── Message CRUD ──

    def add_message(self, msg: ConversationMessage) -> ConversationMessage:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO messages (message_id, session_id, role, content, created_at,
                   language, status, turn_id, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (msg.message_id, msg.session_id, msg.role, msg.content, msg.created_at,
                 msg.language, msg.status, msg.turn_id, json.dumps(msg.metadata)),
            )
        return msg

    def get_messages(self, session_id: str, limit: int = 50) -> list[ConversationMessage]:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [ConversationMessage(
            message_id=r[0], session_id=r[1], role=r[2], content=r[3], created_at=r[4],
            language=r[5], status=r[6], turn_id=r[7], metadata=json.loads(r[8]) if r[8] else {},
        ) for r in rows]

    def update_message(self, message_id: str, **kwargs: Any) -> None:
        allowed = {"content", "status", "metadata"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [message_id]
        for i, k in enumerate(updates):
            if k == "metadata":
                vals[i] = json.dumps(vals[i])
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(f"UPDATE messages SET {set_clause} WHERE message_id = ?", vals)

    # ── Turn CRUD ──

    def create_turn(self, turn: ChatTurn) -> ChatTurn:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO turns (turn_id, session_id, user_message_id, assistant_message_id,
                   status, started_at, completed_at, research_mode, model, intent, plan, metrics)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (turn.turn_id, turn.session_id, turn.user_message_id, turn.assistant_message_id,
                 turn.status, turn.started_at, turn.completed_at, turn.research_mode,
                 turn.model, turn.intent, json.dumps(turn.plan), json.dumps(turn.metrics)),
            )
        return turn

    def get_turn(self, turn_id: str) -> ChatTurn | None:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        if not row:
            return None
        return ChatTurn(
            turn_id=row[0], session_id=row[1], user_message_id=row[2],
            assistant_message_id=row[3], status=row[4], started_at=row[5] or "",
            completed_at=row[6] or "", research_mode=row[7], model=row[8],
            intent=row[9], plan=json.loads(row[10]) if row[10] else {},
            metrics=json.loads(row[11]) if row[11] else {},
        )

    def get_active_turn(self, session_id: str) -> ChatTurn | None:
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT turn_id FROM turns WHERE session_id = ? AND status IN ('pending', 'planning', 'executing') "
                "ORDER BY started_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return self.get_turn(row[0])

    def update_turn(self, turn_id: str, **kwargs: Any) -> None:
        allowed = {"status", "completed_at", "intent", "plan", "metrics", "assistant_message_id"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [turn_id]
        for i, k in enumerate(updates):
            if k in ("plan", "metrics"):
                vals[i] = json.dumps(vals[i])
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(f"UPDATE turns SET {set_clause} WHERE turn_id = ?", vals)
