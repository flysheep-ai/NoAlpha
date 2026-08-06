from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass
class ConversationSession:
    session_id: str = field(default_factory=lambda: str(uuid4()))
    title: str = "New Chat"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    locale: str = "zh-CN"
    report_language: str = "zh-CN"
    active_subjects: list[str] = field(default_factory=list)
    linked_investigation_ids: list[str] = field(default_factory=list)
    linked_case_ids: list[str] = field(default_factory=list)
    status: str = "active"  # active / archived

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "locale": self.locale,
            "report_language": self.report_language,
            "active_subjects": self.active_subjects,
            "linked_investigation_ids": self.linked_investigation_ids,
            "linked_case_ids": self.linked_case_ids,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConversationSession":
        return cls(
            session_id=d.get("session_id", str(uuid4())),
            title=d.get("title", "New Chat"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            locale=d.get("locale", "zh-CN"),
            report_language=d.get("report_language", "zh-CN"),
            active_subjects=d.get("active_subjects", []),
            linked_investigation_ids=d.get("linked_investigation_ids", []),
            linked_case_ids=d.get("linked_case_ids", []),
            status=d.get("status", "active"),
        )


@dataclass
class ConversationMessage:
    message_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    role: str = "user"  # user / assistant / system / tool
    content: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    language: str = "zh-CN"
    status: str = "completed"  # pending / streaming / completed / failed / cancelled
    turn_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
            "language": self.language,
            "status": self.status,
            "turn_id": self.turn_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConversationMessage":
        return cls(
            message_id=d.get("message_id", str(uuid4())),
            session_id=d.get("session_id", ""),
            role=d.get("role", "user"),
            content=d.get("content", ""),
            created_at=d.get("created_at", ""),
            language=d.get("language", "zh-CN"),
            status=d.get("status", "completed"),
            turn_id=d.get("turn_id", ""),
            metadata=d.get("metadata", {}),
        )


@dataclass
class ChatTurn:
    turn_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    user_message_id: str = ""
    assistant_message_id: str = ""
    status: str = "pending"  # pending / planning / executing / streaming / completed / failed / cancelled
    started_at: str = ""
    completed_at: str = ""
    research_mode: str = "llm"
    model: str = ""
    intent: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "research_mode": self.research_mode,
            "model": self.model,
            "intent": self.intent,
            "plan": self.plan,
            "metrics": self.metrics,
        }
