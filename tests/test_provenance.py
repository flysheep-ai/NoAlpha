"""Dataset identity must be the DATA, not the shape of the data.

The bug: `dataset_id` hashed {universe, features, label, range, n_rows}. Change
a single close price, keep the row count, and two materially different
datasets share an id — after which no experiment comparison means anything.
"""
from __future__ import annotations

import copy

from noalpha.quant.dataset import QuantDatasetBuilder
from noalpha.quant.provenance import (
    DataSnapshot, ExperimentProvenance, hash_bars,
)

from tests.test_quant_core import make_history


# ── the core regression ──


def test_changing_one_price_changes_the_dataset_hash():
    history = make_history()
    baseline = QuantDatasetBuilder().build(history, "2023-06-01", "2024-02-01")

    tampered = copy.deepcopy(history)
    secid = sorted(tampered)[0]
    tampered[secid][100]["close"] *= 1.01          # one bar, one field, same row count
    after = QuantDatasetBuilder().build(tampered, "2023-06-01", "2024-02-01")

    assert baseline.version.n_rows == after.version.n_rows, \
        "the test must keep the shape identical or it proves nothing"
    assert baseline.version.raw_data_hash != after.version.raw_data_hash
    assert baseline.version.dataset_id != after.version.dataset_id


def test_identical_history_produces_an_identical_dataset_id():
    history = make_history()
    a = QuantDatasetBuilder().build(history, "2023-06-01", "2024-02-01")
    b = QuantDatasetBuilder().build(copy.deepcopy(history), "2023-06-01", "2024-02-01")
    assert a.version.dataset_id == b.version.dataset_id


def test_adjustment_mode_and_provider_are_part_of_the_identity():
    """Re-fetching the same window unadjusted is a different dataset even when
    the bars happen to be identical in this run."""
    history = make_history()
    qfq = QuantDatasetBuilder().build(history, "2023-06-01", "2024-02-01")
    raw = QuantDatasetBuilder().build(history, "2023-06-01", "2024-02-01",
                                      adjustment_mode="none")
    other = QuantDatasetBuilder().build(history, "2023-06-01", "2024-02-01",
                                        provider="other-vendor")
    assert len({qfq.version.dataset_id, raw.version.dataset_id,
                other.version.dataset_id}) == 3


# ── DataSnapshot ──


def test_per_symbol_hashes_localise_the_change():
    history = make_history()
    before = DataSnapshot.from_history(history)
    tampered = copy.deepcopy(history)
    victim = sorted(tampered)[1]
    tampered[victim][5]["volume"] += 1
    after = DataSnapshot.from_history(tampered)

    diff = before.diff(after)
    assert diff["identical"] is False
    assert diff["changed"] == [victim]
    assert diff["added"] == [] and diff["removed"] == []


def test_snapshot_records_what_the_data_came_from():
    snap = DataSnapshot.from_history(make_history(), provider="tencent-fqkline",
                                     adjustment_mode="qfq",
                                     start_date="2023-01-01", end_date="2024-02-01")
    payload = snap.to_dict()
    for key in ("provider", "adjustment_mode", "retrieved_at", "start_date",
                "end_date", "per_symbol_hash", "per_symbol_bars", "combined_hash"):
        assert payload[key] not in ("", None, {})
    assert snap.snapshot_id.startswith("snap-")


def test_hash_is_stable_across_a_json_round_trip():
    import json
    bars = make_history()[sorted(make_history())[0]]
    assert hash_bars(bars) == hash_bars(json.loads(json.dumps(bars)))


def test_a_dropped_symbol_is_visible_in_the_diff():
    history = make_history()
    before = DataSnapshot.from_history(history)
    reduced = {k: v for k, v in history.items() if k != sorted(history)[0]}
    diff = before.diff(DataSnapshot.from_history(reduced))
    assert diff["removed"] == [sorted(history)[0]]


# ── ExperimentProvenance ──


def test_provenance_names_what_is_missing_instead_of_looking_complete():
    empty = ExperimentProvenance()
    missing = empty.missing_fields()
    assert {"dataset_id", "raw_data_hash", "fold_definitions", "cost_model",
            "universe_snapshot", "label_version"} <= set(missing)
    assert empty.reproducible is False


def test_provenance_captures_the_environment_a_rerun_depends_on():
    p = ExperimentProvenance(dataset_id="ds-1", raw_data_hash="abc",
                             feature_set_version="fs-1", label_version="lb-1",
                             fold_definitions=[{"fold_id": 0}],
                             cost_model={"bps": 15}, universe_snapshot=["A"],
                             model_artifact_hash="deadbeef", random_seed=42)
    payload = p.to_dict()
    assert payload["python_version"]
    assert payload["platform"]
    assert "pandas" in payload["dependencies"]
    assert "commit" in payload["git"]
    assert p.missing_fields() == [] or p.missing_fields() == ["git.commit"]


def test_a_dirty_working_tree_is_not_reproducible():
    p = ExperimentProvenance(dataset_id="ds-1", raw_data_hash="abc",
                             feature_set_version="fs-1", label_version="lb-1",
                             fold_definitions=[{"fold_id": 0}],
                             cost_model={"bps": 15}, universe_snapshot=["A"],
                             model_artifact_hash="deadbeef")
    p.git = {"commit": "abc123", "branch": "main", "dirty": "true"}
    assert p.reproducible is False, \
        "a dirty tree means the recorded commit does not describe the code that ran"
    p.git = {"commit": "abc123", "branch": "main", "dirty": "false"}
    assert p.reproducible is True
