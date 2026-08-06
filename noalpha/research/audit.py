from __future__ import annotations

import json
import logging
from pathlib import Path

from noalpha.research.context_fields import ResearchRunMetadata

logger = logging.getLogger(__name__)


class ResearchAuditTrail:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(".cache/api/llm_audit.jsonl")

    def record(self, metadata: ResearchRunMetadata) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "research_mode": metadata.research_mode,
                "llm_provider": metadata.llm_provider,
                "model": metadata.model,
                "prompt_versions": metadata.prompt_versions,
                "started_at": metadata.started_at.isoformat() if metadata.started_at else None,
                "completed_at": metadata.completed_at.isoformat() if metadata.completed_at else None,
                "latency_ms": metadata.latency_ms,
                "token_usage": {
                    "input_tokens": metadata.token_usage.input_tokens,
                    "output_tokens": metadata.token_usage.output_tokens,
                },
                "context_id": metadata.context_id,
                "selected_evidence_count": metadata.selected_evidence_count,
                "excluded_evidence_count": metadata.excluded_evidence_count,
            }
            with open(self.path, "a") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        except Exception:
            logger.warning("Failed to write audit trail entry", exc_info=True)
