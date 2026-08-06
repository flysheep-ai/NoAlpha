"""QuantModelRegistry: versioned model lifecycle with a promotion gate.

EXPERIMENTAL → VALIDATED → SHADOW → PAPER → ACTIVE (one step at a time,
第七十九条). Training something never auto-activates it.
"""
from __future__ import annotations

import json
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path

from noalpha.quant.domain import PROMOTION_ORDER, ModelStatus, ModelVersion
from noalpha.quant.models import QuantModel


class PromotionError(Exception):
    pass


class QuantModelRegistry:
    def __init__(self, root: str | Path = ".cache/quant/models") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "registry.json"

    # ── persistence ──

    def _load_index(self) -> dict[str, dict]:
        if self.index_path.exists():
            return json.loads(self.index_path.read_text())
        return {}

    def _save_index(self, index: dict[str, dict]) -> None:
        self.index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1, default=str))

    # ── API ──

    def register(self, version: ModelVersion, model: QuantModel) -> ModelVersion:
        artifact = self.root / f"{version.model_id}.pkl"
        model.save(artifact)
        version.artifact_path = str(artifact)
        version.status_history.append({"status": version.status.value,
                                       "at": datetime.now(timezone.utc).isoformat()})
        index = self._load_index()
        record = version.to_dict()
        record["artifact_sha256"] = self._sha256(artifact)
        index[version.model_id] = record
        self._save_index(index)
        return version

    def list_models(self) -> list[dict]:
        return sorted(self._load_index().values(), key=lambda m: m["created_at"], reverse=True)

    def get(self, model_id: str) -> dict | None:
        return self._load_index().get(model_id)

    def promote(self, model_id: str, target: ModelStatus) -> dict:
        index = self._load_index()
        record = index.get(model_id)
        if record is None:
            raise PromotionError(f"unknown model {model_id}")
        current = ModelStatus(record["status"])
        if target in (ModelStatus.RETIRED, ModelStatus.REJECTED):
            pass  # always allowed
        else:
            try:
                cur_i = PROMOTION_ORDER.index(current)
                tgt_i = PROMOTION_ORDER.index(target)
            except ValueError as exc:
                raise PromotionError(f"cannot promote from {current} to {target}") from exc
            if tgt_i != cur_i + 1:
                raise PromotionError(
                    f"promotion gate: {current.value} → {target.value} skips stages; "
                    f"one step at a time")
        # only one ACTIVE model per (name, market_scope)
        if target == ModelStatus.ACTIVE:
            for other_id, other in index.items():
                if (other_id != model_id and other["status"] == ModelStatus.ACTIVE.value
                        and other["market_scope"] == record["market_scope"]):
                    other["status"] = ModelStatus.RETIRED.value
                    other["status_history"].append({
                        "status": "retired", "at": datetime.now(timezone.utc).isoformat(),
                        "reason": f"superseded by {model_id}"})
        record["status"] = target.value
        record["promoted_at"] = datetime.now(timezone.utc).isoformat()
        record["status_history"].append({"status": target.value, "at": record["promoted_at"]})
        self._save_index(index)
        return record

    def active_model(self, market_scope: str = "ALL") -> tuple[dict, QuantModel] | None:
        """The single model allowed to feed live decisions for a market.
        SHADOW/PAPER models also load for parallel (non-binding) inference."""
        for record in self._load_index().values():
            if record["status"] == ModelStatus.ACTIVE.value and record["market_scope"] in (market_scope, "ALL"):
                return record, self._load_model(record)
        return None

    def models_in_status(self, statuses: list[ModelStatus], market_scope: str = "ALL") -> list[tuple[dict, QuantModel]]:
        out = []
        wanted = {s.value for s in statuses}
        for record in self._load_index().values():
            if record["status"] in wanted and record["market_scope"] in (market_scope, "ALL"):
                try:
                    out.append((record, self._load_model(record)))
                except Exception:
                    continue
        return out

    def _load_model(self, record: dict) -> QuantModel:
        artifact = Path(record["artifact_path"]).resolve()
        root = self.root.resolve()
        if root not in artifact.parents and artifact != root:
            raise PromotionError(f"artifact path escapes registry root: {artifact}")
        expected = record.get("artifact_sha256")
        if expected and self._sha256(artifact) != expected:
            raise PromotionError(f"artifact hash mismatch for {record.get('model_id')}")
        return QuantModel.load(artifact)

    def _sha256(self, path: Path) -> str:
        return sha256(path.read_bytes()).hexdigest()
