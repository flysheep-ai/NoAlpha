"""Contract for .env loading.

The precedence rule matters for secrets: a value exported in the shell must beat
a stale value sitting in a developer's .env file, or debugging becomes guesswork
about which key was actually used.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from noalpha.env import load_env


class LoadEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".env"
        self._saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._saved)))

    def test_reads_values_into_environment(self):
        self.path.write_text("NOALPHA_TEST_TOKEN=from-dotenv\n")
        os.environ.pop("NOALPHA_TEST_TOKEN", None)

        self.assertTrue(load_env(self.path))
        self.assertEqual(os.environ["NOALPHA_TEST_TOKEN"], "from-dotenv")

    def test_existing_environment_variable_wins(self):
        self.path.write_text("NOALPHA_TEST_TOKEN=from-dotenv\n")
        os.environ["NOALPHA_TEST_TOKEN"] = "from-shell"

        load_env(self.path)
        self.assertEqual(os.environ["NOALPHA_TEST_TOKEN"], "from-shell")

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(load_env(Path(self.tmp.name) / "nope.env"))

    def test_repeat_calls_do_not_change_the_result(self):
        self.path.write_text("NOALPHA_TEST_TOKEN=from-dotenv\n")
        os.environ.pop("NOALPHA_TEST_TOKEN", None)

        load_env(self.path)
        load_env(self.path)
        self.assertEqual(os.environ["NOALPHA_TEST_TOKEN"], "from-dotenv")

    def test_comments_and_blank_lines_are_ignored(self):
        self.path.write_text("# a comment\n\nNOALPHA_TEST_TOKEN=value\n")
        os.environ.pop("NOALPHA_TEST_TOKEN", None)

        load_env(self.path)
        self.assertEqual(os.environ["NOALPHA_TEST_TOKEN"], "value")


class EnvExampleTests(unittest.TestCase):
    """.env.example is the only place a newcomer learns what to configure."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent
        self.example = self.root / ".env.example"

    def test_env_example_exists(self):
        self.assertTrue(self.example.is_file())

    def test_env_example_carries_no_real_secret(self):
        for line in self.example.read_text().splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip().endswith(("KEY", "TOKEN")):
                self.assertEqual(value.strip(), "",
                                 f"{key.strip()} in .env.example must be blank")

    def test_env_example_documents_every_variable_the_code_reads(self):
        documented = {
            line.partition("=")[0].strip()
            for line in self.example.read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        # read by noalpha/ at runtime; RUN_ONLINE_SMOKE is a test-only switch
        required = {
            "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_TRADING_MODEL",
            "LLM_TIMEOUT", "LLM_MAX_TOKENS", "LLM_TEMPERATURE",
            "MARKETDATA_APP_TOKEN",
            "NOALPHA_API_PORT", "NOALPHA_API_STATE_DIR", "NOALPHA_ADMIN_TOKEN",
        }
        self.assertEqual(required - documented, set())


if __name__ == "__main__":
    unittest.main()
