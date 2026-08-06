from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from noalpha.conversation.agent import ChatAgent
from noalpha.conversation.events import ChatEvent, event_bus
from noalpha.conversation.models import ChatTurn, ConversationMessage, ConversationSession
from noalpha.conversation.store import ConversationStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_conversation_routes(app: FastAPI, store: ConversationStore, agent: ChatAgent) -> None:

    @app.post("/api/v1/conversation/sessions")
    def create_session(payload: dict[str, Any] = {}) -> dict[str, Any]:
        session = ConversationSession(
            locale=payload.get("locale", "zh-CN"),
            report_language=payload.get("report_language", payload.get("locale", "zh-CN")),
            linked_investigation_ids=payload.get("linked_investigation_ids", []),
            linked_case_ids=payload.get("linked_case_ids", []),
        )
        store.create_session(session)
        return session.to_dict()

    @app.get("/api/v1/conversation/sessions")
    def list_sessions() -> dict[str, Any]:
        sessions = store.list_sessions()
        return {"items": [s.to_dict() for s in sessions]}

    @app.get("/api/v1/conversation/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        session = store.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        return session.to_dict()

    @app.delete("/api/v1/conversation/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, Any]:
        store.delete_session(session_id)
        event_bus.clear(session_id)
        return {"ok": True}

    @app.post("/api/v1/conversation/sessions/{session_id}/messages")
    def send_message(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = store.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")

        content = payload.get("content", "")
        if not content.strip():
            raise HTTPException(status_code=400, detail="content required")

        lang = payload.get("language", session.locale)
        user_msg = ConversationMessage(
            session_id=session_id, role="user", content=content,
            language=lang, status="completed",
        )
        store.add_message(user_msg)

        # Update session
        title = content[:50].replace("\n", " ")
        store.update_session(session_id, title=title, updated_at=_now())

        # Run agent in background
        linked_inv = payload.get("linked_investigation_id", "")
        linked_claim = payload.get("linked_claim_id", "")
        linked_evidence = payload.get("linked_evidence_id", "")

        thread = threading.Thread(
            target=agent.run_turn,
            args=(session, user_msg, linked_inv, linked_claim, linked_evidence),
            daemon=True,
        )
        thread.start()

        return {"ok": True, "message": user_msg.to_dict()}

    @app.get("/api/v1/conversation/sessions/{session_id}/messages")
    def get_messages(session_id: str) -> dict[str, Any]:
        messages = store.get_messages(session_id)
        return {"items": [m.to_dict() for m in messages]}

    @app.get("/api/v1/conversation/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request, last_event_id: str = ""):
        session = store.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")

        async def event_generator():
            async for event in event_bus.subscribe(session_id, last_event_id=last_event_id):
                if await request.is_disconnected():
                    break
                yield event.to_sse()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/conversation/sessions/{session_id}/cancel")
    def cancel_turn(session_id: str) -> dict[str, Any]:
        turn = store.get_active_turn(session_id)
        if turn and agent.cancel_turn(turn.turn_id):
            return {"ok": True, "turn_id": turn.turn_id}
        return {"ok": False, "detail": "no active turn found"}
