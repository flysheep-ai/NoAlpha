from __future__ import annotations

from enum import Enum


class ResearchMode(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    HYBRID = "hybrid"
