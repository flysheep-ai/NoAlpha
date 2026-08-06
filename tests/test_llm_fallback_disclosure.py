from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from noalpha.api import create_app
from noalpha.research_mode import ResearchMode
from tests.test_api import FakePipeline


def _wait_completed(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/investigations/{job_id}").json()
        if body["status"] in {"completed", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError("investigation did not finish")


class RecordingFactory:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return FakePipeline()


class LLMFallbackDisclosureTests(unittest.TestCase):
    """A job must never report research_mode='llm' when no LLM key is configured."""

    def test_llm_request_without_key_is_disclosed_as_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LLM_API_KEY": ""}):
            app = create_app(Path(tmp))
            factory = RecordingFactory()
            app.state.pipeline_factory = factory
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/investigations",
                    json={"symbol": "AAPL", "research_mode": "llm"},
                )
                self.assertEqual(created.status_code, 202)
                body = _wait_completed(client, created.json()["investigation_id"])

                self.assertEqual(body["research_mode"], "deterministic")
                self.assertEqual(body["llm_model"], "")
                warnings = body["provider_warnings"]
                self.assertTrue(any("no LLM API key" in w for w in warnings), warnings)
                self.assertTrue(any("no LLM API key" in u for u in body["report"]["unknowns"]))
                # The pipeline itself must have been built in deterministic mode.
                self.assertEqual(factory.calls[0]["research_mode"], ResearchMode.DETERMINISTIC)
                self.assertIsNone(factory.calls[0]["llm_config"])

    def test_llm_request_with_key_keeps_llm_label(self) -> None:
        env = {"LLM_API_KEY": "sk-test", "LLM_MODEL": "test-model"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            app = create_app(Path(tmp))
            factory = RecordingFactory()
            app.state.pipeline_factory = factory
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/investigations",
                    json={"symbol": "AAPL", "research_mode": "llm"},
                )
                body = _wait_completed(client, created.json()["investigation_id"])

                self.assertEqual(body["research_mode"], "llm")
                self.assertEqual(body["llm_model"], "test-model")
                self.assertFalse(any("no LLM API key" in w for w in body["provider_warnings"]))
                self.assertEqual(factory.calls[0]["research_mode"], ResearchMode.LLM)
                self.assertIsNotNone(factory.calls[0]["llm_config"])


if __name__ == "__main__":
    unittest.main()
