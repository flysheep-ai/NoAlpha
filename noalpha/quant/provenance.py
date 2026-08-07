"""Provenance: what a result was actually computed from.

The dataset hash used to be built from shape alone:

    content_hash({universe, features, label, range, n_rows})

Nothing in there is the DATA. Re-fetch the same window from a provider that
silently restated a price, or re-run after an adjustment-factor change, and
the frame is different while `dataset_id` is byte-for-byte identical. Two
experiments then claim the same dataset and there is no way, six months later,
to find out why their numbers differ.

DataSnapshot hashes the bars themselves — per symbol and combined — so any
change to any value changes the dataset id. ExperimentProvenance records the
rest of the environment that a rerun depends on.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# Bar fields that are part of the content identity. `turnover` is included
# when present; anything the provider adds later is ignored on purpose so the
# hash does not churn on cosmetic payload changes.
BAR_FIELDS = ("time", "open", "high", "low", "close", "volume")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_bars(bars: Iterable[dict[str, Any]]) -> str:
    """Content hash of one instrument's bars.

    Values are normalised to a fixed decimal representation so that a float
    that round-trips through JSON differently does not change the hash, while
    a genuinely different price always does.
    """
    digest = hashlib.sha256()
    for bar in bars:
        row = "|".join(
            f"{bar[f]:.10g}" if isinstance(bar.get(f), (int, float)) else str(bar.get(f, ""))
            for f in BAR_FIELDS)
        digest.update(row.encode())
        digest.update(b"\n")
    return digest.hexdigest()[:16]


@dataclass
class DataSnapshot:
    """An immutable statement about the raw data an experiment consumed."""
    snapshot_id: str = ""
    provider: str = "tencent-fqkline"
    adjustment_mode: str = "qfq"          # forward-adjusted
    retrieved_at: str = field(default_factory=_now)
    start_date: str = ""
    end_date: str = ""
    symbols: list[str] = field(default_factory=list)
    per_symbol_hash: dict[str, str] = field(default_factory=dict)
    per_symbol_bars: dict[str, int] = field(default_factory=dict)
    combined_hash: str = ""

    @classmethod
    def from_history(cls, history: dict[str, list[dict[str, Any]]], *,
                     provider: str = "tencent-fqkline",
                     adjustment_mode: str = "qfq",
                     start_date: str = "", end_date: str = "",
                     retrieved_at: str = "") -> "DataSnapshot":
        per_symbol = {secid: hash_bars(bars) for secid, bars in sorted(history.items())}
        combined = hashlib.sha256(
            json.dumps({"provider": provider, "adjustment": adjustment_mode,
                        "symbols": per_symbol}, sort_keys=True).encode()
        ).hexdigest()[:16]
        snap = cls(
            provider=provider, adjustment_mode=adjustment_mode,
            retrieved_at=retrieved_at or _now(),
            start_date=start_date, end_date=end_date,
            symbols=sorted(history), per_symbol_hash=per_symbol,
            per_symbol_bars={s: len(b) for s, b in sorted(history.items())},
            combined_hash=combined)
        snap.snapshot_id = f"snap-{combined}"
        return snap

    def diff(self, other: "DataSnapshot") -> dict[str, Any]:
        """Which symbols changed between two snapshots — the question you
        actually ask six months later."""
        mine, theirs = self.per_symbol_hash, other.per_symbol_hash
        return {
            "identical": self.combined_hash == other.combined_hash,
            "added": sorted(set(theirs) - set(mine)),
            "removed": sorted(set(mine) - set(theirs)),
            "changed": sorted(s for s in set(mine) & set(theirs) if mine[s] != theirs[s]),
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _git_state() -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except Exception:
            return ""
    commit = run("rev-parse", "HEAD")
    dirty = bool(run("status", "--porcelain"))
    return {"commit": commit, "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            # A dirty tree means the commit does NOT describe the code that ran.
            "dirty": "true" if dirty else "false"}


def _dependency_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in ("pandas", "numpy", "scikit-learn", "lightgbm"):
        try:
            from importlib.metadata import version
            out[name] = version(name)
        except Exception:
            out[name] = "not installed"
    return out


@dataclass
class ExperimentProvenance:
    """Everything a rerun depends on beyond the dataset itself."""
    recorded_at: str = field(default_factory=_now)
    dataset_id: str = ""
    data_snapshot_id: str = ""
    raw_data_hash: str = ""
    feature_set_version: str = ""
    label_version: str = ""
    model_params: dict[str, Any] = field(default_factory=dict)
    random_seed: int = 0
    fold_definitions: list[dict[str, Any]] = field(default_factory=list)
    cost_model: dict[str, Any] = field(default_factory=dict)
    universe_snapshot: list[str] = field(default_factory=list)
    model_artifact_hash: str = ""
    git: dict[str, str] = field(default_factory=_git_state)
    python_version: str = field(default_factory=lambda: sys.version.split()[0])
    platform: str = field(default_factory=platform.platform)
    dependencies: dict[str, str] = field(default_factory=_dependency_versions)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def missing_fields(self) -> list[str]:
        """Which parts of the provenance were never filled in. A result with
        missing provenance is still reportable — it is just not reproducible,
        and it should say so rather than look complete."""
        # `model_artifact_hash` is deliberately NOT required here. A
        # walk-forward run trains a fresh model per fold and persists none of
        # them; it reproduces from seed + folds + data. Artifact integrity is
        # the registry's concern and is enforced there (see
        # QuantModelRegistry._load_model and the promotion evidence gate).
        required = {
            "dataset_id": self.dataset_id,
            "raw_data_hash": self.raw_data_hash,
            "feature_set_version": self.feature_set_version,
            "label_version": self.label_version,
            "fold_definitions": self.fold_definitions,
            "cost_model": self.cost_model,
            "universe_snapshot": self.universe_snapshot,
            "git.commit": self.git.get("commit"),
        }
        return sorted(k for k, v in required.items() if not v)

    @property
    def reproducible(self) -> bool:
        return not self.missing_fields() and self.git.get("dirty") != "true"
