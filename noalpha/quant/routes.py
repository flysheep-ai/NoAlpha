"""Quant API: model registry, experiments, latest predictions, factor/feature
metadata. Everything served here is real registry/experiment content — the
frontend renders predictions as predictions, never as facts (第九十六条)."""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from noalpha.quant.domain import ModelStatus
from noalpha.quant.experiment import ExperimentStore
from noalpha.quant.features import FACTOR_DEFINITIONS, FEATURE_DEFINITIONS
from noalpha.quant.labels import LABEL_DEFINITIONS
from noalpha.quant.promotion import PromotionEvidenceBundle, PromotionPolicy
from noalpha.quant.registry import PromotionError, QuantModelRegistry


def register_quant_routes(app: FastAPI, quant_root: str | Path = ".cache/quant") -> None:
    root = Path(quant_root)
    registry = QuantModelRegistry(root / "models")
    experiments = ExperimentStore(root / "experiments")

    @app.get("/api/quant/overview")
    def quant_overview() -> dict[str, Any]:
        models = registry.list_models()
        active = [m for m in models if m["status"] == ModelStatus.ACTIVE.value]
        return {
            "models_total": len(models),
            "active_models": [{k: m[k] for k in
                               ("model_id", "name", "version", "market_scope",
                                "horizon_days", "label_id", "promoted_at",
                                "promotion_evidence")}
                              for m in active],
            "experiments_total": len(experiments.load_all()),
            "feature_count": len(FEATURE_DEFINITIONS),
            "factor_count": len(FACTOR_DEFINITIONS),
            # Which models are live on unaudited promotions. Surfaced in the
            # overview rather than buried, because an ACTIVE model with no
            # recorded evidence is the single most misleading state the
            # registry can be in.
            "promotion_audit": registry.audit_promotions(),
        }

    @app.get("/api/quant/models")
    def quant_models() -> dict[str, Any]:
        return {"models": registry.list_models()}

    @app.post("/api/quant/models/{model_id}/promote/{status}")
    def promote_model(model_id: str, status: str,
                      body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Promote one stage. The body carries the PromotionEvidenceBundle;
        an empty body means "no evidence", which fails every gate — that is
        the intended behaviour, not an inconvenience."""
        body = body or {}
        known = {f.name for f in fields(PromotionEvidenceBundle)}
        evidence = PromotionEvidenceBundle(
            **{k: v for k, v in (body.get("evidence") or {}).items() if k in known})
        try:
            record = registry.promote(model_id, ModelStatus(status), evidence,
                                      override_reason=str(body.get("override_reason") or ""))
        except (PromotionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"model": record}

    @app.get("/api/quant/models/{model_id}/promotion-requirements")
    def promotion_requirements(model_id: str) -> dict[str, Any]:
        try:
            return registry.promotion_requirements(model_id)
        except PromotionError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/quant/promotion-policy")
    def promotion_policy() -> dict[str, Any]:
        policy = PromotionPolicy()
        return {"version": policy.version, "stages": policy.all_requirements()}

    @app.get("/api/quant/experiments")
    def quant_experiments() -> dict[str, Any]:
        rows = []
        for e in experiments.load_all():
            m = e.get("metrics") or {}
            rows.append({
                "experiment_id": e["experiment_id"], "name": e["name"],
                "model": e["model"], "status": e["status"],
                "dataset_version": e["dataset_version"],
                "label_version": e["label_version"],
                "test_period": e.get("test_period"),
                "n_folds": m.get("n_folds"), "seed": e.get("seed"),
                "signal": m.get("signal"), "trading": m.get("trading"),
                "equal_weight_benchmark": m.get("equal_weight_benchmark"),
                "survivorship_bias_risk": m.get("survivorship_bias_risk"),
                "created_at": e["created_at"],
            })
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return {"experiments": rows}

    @app.get("/api/quant/experiments/{experiment_id}")
    def quant_experiment_detail(experiment_id: str) -> dict[str, Any]:
        for e in experiments.load_all():
            if e["experiment_id"] == experiment_id:
                return e
        raise HTTPException(status_code=404, detail="experiment not found")

    @app.get("/api/quant/features")
    def quant_features() -> dict[str, Any]:
        return {
            "features": [
                {"feature_id": f.feature_id, "name": f.name, "version": f.version,
                 "category": f.category, "description": f.description,
                 "formula": f.formula, "lookback": f.lookback,
                 "availability_rule": f.availability_rule}
                for f in FEATURE_DEFINITIONS
            ],
            "factors": [
                {"factor_id": f.factor_id, "name": f.name,
                 "description": f.description, "feature_ids": list(f.feature_ids)}
                for f in FACTOR_DEFINITIONS
            ],
            "labels": [
                {"label_id": l.label_id, "kind": l.kind, "horizon_days": l.horizon_days,
                 "decision_time": l.decision_time, "execution_time": l.execution_time,
                 "formula": l.formula}
                for l in LABEL_DEFINITIONS
            ],
        }

    @app.get("/api/quant/results/{market}")
    def quant_results(market: str) -> dict[str, Any]:
        path = Path(f"experiments/results/phase_q_{market}.json")
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"no results for market {market}")
        return json.loads(path.read_text())

    @app.get("/api/quant/predictions/{session_id}")
    def quant_predictions(session_id: str) -> dict[str, Any]:
        """Latest inference of the trading session's loop. Prefers the live
        in-memory loop; falls back to the persisted quant snapshot so a
        restored session never contradicts the System Truth bar."""
        from noalpha.trading.autonomous import trading_scheduler
        loop = trading_scheduler.get_loop(session_id)
        result = (loop.quant_result or loop.quant.last_result) if loop else None
        if result is not None:
            return result.to_dict()
        # persisted fallback (from the quant_full snapshot emitted each cycle)
        store = getattr(app.state, "trading_store", None)
        if store is not None:
            snap = store.list_events(session_id, limit=1, event_type="quant_full")
            if snap:
                return snap[0]["payload"]
        return {"as_of": "", "model": {}, "predictions": [], "signals": [],
                "skipped": [], "error": "no inference has run yet"}
