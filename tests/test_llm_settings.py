from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from noalpha.api import CredentialStore, create_app, resolve_llm_config
from noalpha.conversation.agent import ChatAgent
from noalpha.conversation.store import ConversationStore
from noalpha.conversation.models import ConversationMessage
from noalpha.domain import AsOfContext, Evidence, EvidenceType, Instrument, Market
from noalpha.llm.config import LLMConfig
from noalpha.pit import filter_evidence_as_of
from noalpha.research.deterministic import ResearchOrchestrator


class ResolveLLMConfigTests(unittest.TestCase):
    def test_credentials_override_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
            "LLM_API_KEY": "env-key", "LLM_MODEL": "env-model",
        }):
            creds = CredentialStore(Path(tmp) / "credentials.json")
            creds.set("LLM_API_KEY", "cred-key")
            cfg = resolve_llm_config(creds)
            self.assertEqual(cfg.api_key, "cred-key")   # credential wins
            self.assertEqual(cfg.model, "env-model")     # env fallback per-field

    def test_env_fallback_when_no_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LLM_API_KEY": "env-key"}):
            creds = CredentialStore(Path(tmp) / "credentials.json")
            self.assertEqual(resolve_llm_config(creds).api_key, "env-key")


class SettingsEndpointTests(unittest.TestCase):
    def test_llm_settings_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LLM_API_KEY": ""}):
            app = create_app(Path(tmp))
            with TestClient(app) as client:
                settings = client.get("/api/v1/settings").json()
                self.assertFalse(settings["llm"]["api_key_configured"])
                self.assertEqual(settings["llm"]["api_key_source"], "none")

                resp = client.put("/api/v1/settings/credentials",
                                  json={"key": "LLM_API_KEY", "value": "sk-user-configured"})
                self.assertTrue(resp.json()["configured"])
                client.put("/api/v1/settings/credentials",
                           json={"key": "LLM_MODEL", "value": "my-model"})

                settings = client.get("/api/v1/settings").json()
                self.assertTrue(settings["llm"]["api_key_configured"])
                self.assertEqual(settings["llm"]["api_key_source"], "credentials")
                self.assertEqual(settings["llm"]["model"], "my-model")
                # preview must never contain the full key
                self.assertNotIn("sk-user-configured", settings["llm"]["api_key_preview"])

                # empty value clears the credential
                resp = client.put("/api/v1/settings/credentials", json={"key": "LLM_API_KEY", "value": ""})
                self.assertFalse(resp.json()["configured"])
                settings = client.get("/api/v1/settings").json()
                self.assertFalse(settings["llm"]["api_key_configured"])


class ChatContextTests(unittest.TestCase):
    def test_general_messages_include_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(str(Path(tmp) / "conv.db"))
            agent = ChatAgent(store=store, llm_config=LLMConfig(base_url="x", api_key="k", model="m"))
            sid = "s1"
            for i, (role, text) in enumerate([
                ("user", "分析 AAPL 当前最重要的风险"),
                ("assistant", "第一风险是A；第二风险是B [E2]；第三风险是C"),
                ("user", "你刚才第二个风险的证据是什么?"),
            ]):
                store.add_message(ConversationMessage(
                    message_id=f"m{i}", session_id=sid, role=role, content=text,
                    created_at=f"2026-07-07T00:00:0{i}Z",
                ))
            messages = agent._build_general_messages(sid, "lang", history_limit=12)
            self.assertEqual(messages[0].role, "system")
            self.assertEqual([m.role for m in messages[1:]], ["user", "assistant", "user"])
            self.assertIn("第二风险是B", messages[2].content)


class PitEventTimeGuardTests(unittest.TestCase):
    def test_future_event_time_is_blocked_even_with_past_available_at(self) -> None:
        as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
        past = as_of - timedelta(days=5)
        future = as_of + timedelta(days=5)
        item = Evidence(
            evidence_id="x", evidence_type=EvidenceType.NEWS, source="s", source_url="u",
            event_time=future, published_at=past, available_at=past, retrieved_at=past,
            content="c", metadata={},
        )
        self.assertEqual(filter_evidence_as_of([item], AsOfContext(as_of)), [])

    def test_future_retrieved_at_is_allowed_for_replay(self) -> None:
        as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
        past = as_of - timedelta(days=5)
        item = Evidence(
            evidence_id="x", evidence_type=EvidenceType.NEWS, source="s", source_url="u",
            event_time=past, published_at=past, available_at=past,
            retrieved_at=as_of + timedelta(days=30),
            content="c", metadata={},
        )
        self.assertEqual(len(filter_evidence_as_of([item], AsOfContext(as_of))), 1)


class DeterministicLanguageTests(unittest.TestCase):
    def test_zh_report_is_chinese(self) -> None:
        instrument = Instrument(symbol="AAPL", company_name="Apple Inc.", exchange="NASDAQ",
                                market=Market.US, currency="USD")
        report = ResearchOrchestrator().build_report(
            instrument, AsOfContext.now(), [], (), (), [], {}, report_language="zh-CN",
        )
        self.assertIn("本次调查", report.summary)
        self.assertTrue(any("看涨" in line or "证据不足" in line for line in report.bull_case))
        self.assertTrue(any("as-of" in u or "不可用" in u for u in report.unknowns))

    def test_default_report_stays_english(self) -> None:
        instrument = Instrument(symbol="AAPL", company_name="Apple Inc.", exchange="NASDAQ",
                                market=Market.US, currency="USD")
        report = ResearchOrchestrator().build_report(
            instrument, AsOfContext.now(), [], (), (), [], {},
        )
        self.assertIn("investigation used", report.summary)


if __name__ == "__main__":
    unittest.main()
