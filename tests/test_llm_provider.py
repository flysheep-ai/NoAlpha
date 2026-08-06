import os
import unittest

from noalpha.llm.config import LLMConfig
from noalpha.llm.provider import OpenAICompatibleProvider
from noalpha.llm.schemas import LLMMessage


class TestLLMConfig(unittest.TestCase):
    def test_config_creation(self):
        config = LLMConfig(
            base_url="https://test.api",
            api_key="sk-test",
            model="test-model",
        )
        self.assertEqual(config.base_url, "https://test.api")
        self.assertEqual(config.model, "test-model")
        self.assertEqual(config.temperature, 0.3)
        self.assertEqual(config.max_tokens, 4096)

    def test_from_env_defaults(self):
        config = LLMConfig(base_url="https://a.com", api_key="k", model="m")
        self.assertEqual(config.base_url, "https://a.com")


class TestOpenAICompatibleProviderIntegration(unittest.TestCase):
    """Integration tests requiring LLM_API_KEY env var."""

    @classmethod
    def setUpClass(cls):
        cls.api_key = os.environ.get("LLM_API_KEY", "")
        if not cls.api_key:
            return
        cls.provider = OpenAICompatibleProvider(LLMConfig(
            base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            api_key=cls.api_key,
            model=os.environ.get("LLM_MODEL", "deepseek-chat"),
        ))

    def test_generate_simple(self):
        if not self.api_key:
            self.skipTest("LLM_API_KEY not set")
        messages = [
            LLMMessage(role="system", content="Reply with only the word OK."),
            LLMMessage(role="user", content="Hi"),
        ]
        resp = self.provider.generate(messages, max_tokens=10)
        self.assertIn("OK", resp.content.upper())
        self.assertGreater(resp.input_tokens, 0)
        self.assertGreaterEqual(resp.output_tokens, 1)

    def test_generate_structured(self):
        if not self.api_key:
            self.skipTest("LLM_API_KEY not set")
        schema = {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["answer", "confidence"],
        }
        messages = [
            LLMMessage(role="system", content="You are a helpful assistant."),
            LLMMessage(role="user", content="Is water a liquid at room temperature?"),
        ]
        result = self.provider.generate_structured(messages, schema)
        self.assertIn("answer", result)
        self.assertIn("confidence", result)
        self.assertIsInstance(result["confidence"], (int, float))

    def test_usage_recorded(self):
        if not self.api_key:
            self.skipTest("LLM_API_KEY not set")
        messages = [
            LLMMessage(role="user", content="Say hello"),
        ]
        self.provider.generate(messages, max_tokens=5)
        usage = self.provider.usage
        self.assertIsNotNone(usage)
        self.assertGreater(usage.input_tokens, 0)
        self.assertGreater(usage.output_tokens, 0)
