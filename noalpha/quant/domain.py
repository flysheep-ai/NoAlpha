"""First-class quant concepts: definitions, versions, predictions, signals.

Every artifact is versioned and carries enough provenance to answer
"what did the AI see, with which model, at which time" after the fact.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ── definitions ──


@dataclass(frozen=True)
class FeatureDefinition:
    """A single versioned feature. `formula` is the human-auditable spec;
    the implementation lives in features.py keyed by `name:version`."""
    name: str
    version: int
    description: str
    formula: str
    required_fields: tuple[str, ...]
    lookback: int                      # trading days of history required
    availability_rule: str             # when the value becomes known
    market_scope: str = "ALL"          # ALL | CN | US
    category: str = ""                 # returns/momentum/volatility/...
    created_at: str = "2026-07-07"

    @property
    def feature_id(self) -> str:
        return f"{self.name}:v{self.version}"


@dataclass(frozen=True)
class FactorDefinition:
    """A factor is a named combination of features (e.g. MomentumFactor)."""
    name: str
    version: int
    description: str
    feature_ids: tuple[str, ...]
    combine: str = "equal_weight_zscore"

    @property
    def factor_id(self) -> str:
        return f"{self.name}:v{self.version}"


@dataclass(frozen=True)
class LabelDefinition:
    """Versioned prediction target with explicit execution assumptions."""
    name: str
    version: int
    description: str
    horizon_days: int
    kind: str                          # forward_return | excess_return | cs_rank | direction
    decision_time: str                 # e.g. "T close (after close auction)"
    execution_time: str                # e.g. "T+1 open"
    formula: str

    @property
    def label_id(self) -> str:
        return f"{self.name}:v{self.version}"


@dataclass(frozen=True)
class DecisionTimeProtocol:
    """The contract every experiment must satisfy (第二十三条)."""
    decision_time: str = "T close"
    information_cutoff: str = "T close: only bars with date <= T"
    execution_time: str = "T+1 open"
    prediction_horizon_days: int = 5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── dataset / experiment ──


@dataclass
class DatasetVersion:
    dataset_id: str
    universe: list[str]
    start_date: str
    end_date: str
    feature_ids: list[str]
    label_id: str
    protocol: dict[str, Any]
    n_rows: int = 0
    n_features: int = 0
    survivorship_bias_risk: bool = True   # universe is today's list, not PIT constituents
    content_hash: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuantExperiment:
    experiment_id: str = field(default_factory=lambda: _uid("exp"))
    name: str = ""
    dataset_version: str = ""
    feature_set_version: str = ""
    label_version: str = ""
    model: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    train_period: list[str] = field(default_factory=list)
    validation_period: list[str] = field(default_factory=list)
    test_period: list[str] = field(default_factory=list)
    folds: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    seed: int = 42
    code_version: str = ""
    notes: str = ""
    status: str = "created"            # created | running | done | failed
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestRun:
    run_id: str = field(default_factory=lambda: _uid("bt"))
    experiment_id: str = ""
    strategy: str = ""                 # e.g. topk_equal_weight
    period: list[str] = field(default_factory=list)
    cost_model: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    daily_returns: list[float] = field(default_factory=list)
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── model lifecycle ──


class ModelStatus(str, Enum):
    EXPERIMENTAL = "experimental"
    VALIDATED = "validated"
    SHADOW = "shadow"
    PAPER = "paper"
    ACTIVE = "active"
    RETIRED = "retired"
    REJECTED = "rejected"


# Promotion gate (第七十九条): a model may only move one step at a time and
# never straight from training to live decisions.
PROMOTION_ORDER = [ModelStatus.EXPERIMENTAL, ModelStatus.VALIDATED, ModelStatus.SHADOW,
                   ModelStatus.PAPER, ModelStatus.ACTIVE]


@dataclass
class ModelVersion:
    model_id: str = field(default_factory=lambda: _uid("mdl"))
    name: str = ""                     # e.g. lightgbm_rank
    version: int = 1
    status: ModelStatus = ModelStatus.EXPERIMENTAL
    experiment_id: str = ""
    dataset_version: str = ""
    feature_ids: list[str] = field(default_factory=list)
    label_id: str = ""
    horizon_days: int = 5
    market_scope: str = "ALL"
    artifact_path: str = ""
    train_period: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    calibration: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    promoted_at: str = ""
    status_history: list[dict[str, str]] = field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.name} v{self.version}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


# ── runtime artifacts ──


@dataclass
class FeatureSnapshot:
    """What the model saw at decision time (第十八条)."""
    snapshot_id: str = field(default_factory=lambda: _uid("fsnap"))
    secid: str = ""
    as_of: str = ""
    feature_ids: list[str] = field(default_factory=list)
    values: dict[str, float | None] = field(default_factory=dict)
    bars_used: int = 0
    last_bar_time: str = ""
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Prediction:
    prediction_id: str = field(default_factory=lambda: _uid("pred"))
    instrument_id: str = ""            # secid
    symbol: str = ""
    model_version: str = ""
    as_of: str = ""
    horizon_days: int = 5
    predicted_return: float | None = None      # expected excess return over horizon
    predicted_rank: int | None = None           # 1 = best in universe
    universe_size: int | None = None
    probability_up: float | None = None
    uncertainty: float | None = None
    feature_snapshot_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


@dataclass
class QuantSignal:
    signal_id: str = field(default_factory=lambda: _uid("sig"))
    instrument_id: str = ""
    symbol: str = ""
    signal_type: str = "cross_sectional_rank"
    horizon_days: int = 5
    direction: SignalDirection = SignalDirection.NEUTRAL
    strength: float = 0.0              # 0..1, rank-derived
    confidence: float = 0.0            # calibrated (see calibration.py) or raw with flag
    calibrated: bool = False
    model_version: str = ""
    prediction_id: str = ""
    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction.value
        return d


class ConflictResolution(str, Enum):
    REDUCE_POSITION = "reduce_position"
    REQUEST_INVESTIGATION = "request_investigation"
    NO_ACTION = "no_action"


@dataclass
class SignalConflict:
    conflict_id: str = field(default_factory=lambda: _uid("conf"))
    symbol: str = ""
    quant_view: str = ""
    llm_view: str = ""
    detail: str = ""
    resolution: ConflictResolution = ConflictResolution.NO_ACTION
    as_of: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["resolution"] = self.resolution.value
        return d


@dataclass
class PortfolioTarget:
    target_id: str = field(default_factory=lambda: _uid("ptgt"))
    as_of: str = field(default_factory=_now)
    method: str = "equal_weight"
    weights: dict[str, float] = field(default_factory=dict)   # symbol -> target weight
    turnover: float = 0.0
    constraints: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]
