from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout: int = 60
    max_tokens: int = 4096
    temperature: float = 0.3

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", "deepseek-chat"),
            timeout=int(os.environ.get("LLM_TIMEOUT", "60")),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "4096")),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.3")),
        )
