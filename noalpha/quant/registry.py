"""QuantModelRegistry: versioned model lifecycle behind an evidence gate.

EXPERIMENTAL → VALIDATED → SHADOW → PAPER → ACTIVE, one step at a time
(第七十九条) AND only with the evidence that stage requires (see
noalpha/quant/promotion.py). Training something never auto-activates it, and
neither does calling promote() five times.
"""
from __future__ import annotations

import json
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path

from noalpha.quant.domain import PROMOTION_ORDER, ModelStatus, ModelVersion
from noalpha.quant.models import QuantModel
from noalpha.quant.promotion import PromotionEvidenceBundle, PromotionPolicy


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
        models = sorted(self._load_index().values(),
                        key=lambda m: m["created_at"], reverse=True)
        for record in models:
            record["promotion_evidence"] = self._evidence_state(record)
        return models

    @staticmethod
    def _evidence_state(record: dict) -> str:
        """How the model's CURRENT status was reached.

        Models promoted before the evidence gate existed carry no decision in
        their history. Their status is real but unaudited, and silently
        rendering them next to evidenced models would misstate what is known.
        """
        last = (record.get("status_history") or [{}])[-1]
        decision = last.get("promotion_decision")
        if record["status"] == ModelStatus.EXPERIMENTAL.value:
            return "NOT_PROMOTED"
        if not decision:
            return "UNVERIFIED_LEGACY"
        if decision.get("override"):
            return "OVERRIDDEN"
        return "EVIDENCED"

    def artifact_health(self, model_id: str) -> dict:
        """Can this model actually be loaded right now?

        A registration is a claim; the artifact is the fact. A model can sit at
        ACTIVE for months with an artifact that no longer unpickles (a renamed
        package, a dependency bump) while every dashboard reports a healthy
        quant layer and the loop quietly runs on the heuristic fallback.
        """
        record = self.get(model_id)
        if record is None:
            return {"model_id": model_id, "loadable": False, "error": "unknown model"}
        try:
            self._load_model(record)
            return {"model_id": model_id, "loadable": True, "error": ""}
        except Exception as exc:
            return {"model_id": model_id, "loadable": False,
                    "error": f"{type(exc).__name__}: {exc}"}

    def audit_promotions(self) -> dict:
        """Every model whose current status is not backed by recorded evidence,
        plus every model whose artifact cannot actually be loaded. This is
        meant to be uncomfortable reading."""
        models = self.list_models()
        by_state: dict[str, list[str]] = {}
        for m in models:
            by_state.setdefault(m["promotion_evidence"], []).append(m["model_id"])
        unverified = [m for m in models
                      if m["promotion_evidence"] in ("UNVERIFIED_LEGACY", "OVERRIDDEN")]
        broken = [h for h in (self.artifact_health(m["model_id"]) for m in models)
                  if not h["loadable"]]
        broken_ids = {h["model_id"] for h in broken}
        return {
            "models_total": len(models),
            "by_evidence_state": by_state,
            "unverified": [{"model_id": m["model_id"], "name": m["name"],
                            "status": m["status"], "market_scope": m["market_scope"],
                            "evidence": m["promotion_evidence"]}
                           for m in unverified],
            "active_unverified": [m["model_id"] for m in unverified
                                  if m["status"] == ModelStatus.ACTIVE.value],
            "unloadable_artifacts": broken,
            "active_but_unloadable": [m["model_id"] for m in models
                                      if m["status"] == ModelStatus.ACTIVE.value
                                      and m["model_id"] in broken_ids],
        }

    def get(self, model_id: str) -> dict | None:
        return self._load_index().get(model_id)

    def promote(self, model_id: str, target: ModelStatus,
                evidence: PromotionEvidenceBundle | None = None,
                *, override_reason: str = "") -> dict:
        """Promote one stage, against evidence.

        Two independent gates, both hard:
          1. stage order — never skip a stage (第七十九条)
          2. evidence    — the target stage's requirements must be satisfied

        `override_reason` exists so a human can force a promotion, but it is
        recorded permanently in status_history alongside every requirement it
        bypassed. An override is a documented decision, not a way to make a
        denial disappear.
        """
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

        # ── evidence gate ──
        decision = PromotionPolicy().evaluate(
            model_id=model_id, current=current, target=target, evidence=evidence)
        if not decision.approved:
            if not override_reason:
                raise PromotionError(
                    f"promotion evidence gate: {current.value} → {target.value} denied. "
                    f"{decision.reason}")
            decision.override = True
            decision.override_reason = override_reason
            decision.approved = True

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
        # The full decision — every requirement, passed or not — is part of the
        # permanent record. A promotion is auditable after the fact or it is
        # not a promotion gate at all.
        record["status_history"].append({
            "status": target.value, "at": record["promoted_at"],
            "promotion_decision": decision.to_dict(),
        })
        record["last_promotion_decision"] = decision.to_dict()
        self._save_index(index)
        return record

    def promotion_requirements(self, model_id: str) -> dict:
        """What the NEXT stage would require of this model."""
        record = self.get(model_id)
        if record is None:
            raise PromotionError(f"unknown model {model_id}")
        current = ModelStatus(record["status"])
        try:
            nxt = PROMOTION_ORDER[PROMOTION_ORDER.index(current) + 1]
        except (ValueError, IndexError):
            return {"model_id": model_id, "current": current.value,
                    "next": None, "requirements": []}
        policy = PromotionPolicy()
        return {"model_id": model_id, "current": current.value, "next": nxt.value,
                "policy_version": policy.version,
                "requirements": policy.requirements_for(current, nxt)}

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
