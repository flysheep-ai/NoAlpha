"""Load a local .env file into the process environment.

Secrets reach the app through three layers. Highest priority first:

  1. .cache/api/credentials.json — set at runtime via the Settings UI, never
     committed (see api.resolve_llm_config, which prefers it over the env)
  2. real environment variables — what CI and containers set
  3. .env — a local file for development, never committed

`load_dotenv(override=False)` implements 2 > 3: something already exported in
your shell beats the file, so `LLM_API_KEY=... python -m noalpha` still works
for one-off overrides. Layer 1 is applied later, at config-resolution time.

Call `load_env()` at process entry points only. Importing this module has no
side effects, so tests and libraries stay in control of their own environment.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def load_env(path: str | Path | None = None) -> bool:
    """Load .env if present. Returns True if a file was read.

    Never overrides a variable that is already set, so repeat calls are harmless.
    """
    env_path = Path(path) if path else Path(os.environ.get("NOALPHA_ENV_FILE", ".env"))
    if not env_path.is_file():
        return False

    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("%s exists but python-dotenv is not installed; "
                       "install it or export the variables yourself", env_path)
        return False

    load_dotenv(env_path, override=False)
    return True
