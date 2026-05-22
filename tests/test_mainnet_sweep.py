"""Tests for ``run_mainnet_sweep.py`` helpers.

The full sweep itself reads a 200 MB on-disk corpus and can't run in
unit tests, but the label-assignment + UTXO-detail helpers are pure
functions over already-loaded data so we exercise them against a tiny
synthetic corpus produced by the same loader.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from coinjoin_simulator.clusterer_onchain import (
    RecoveredMakerOutput,
    recover_maker_outputs_from_txs,
)
from coinjoin_simulator.clusterer_oracle import DEFAULT_LOG_STRIDE
from coinjoin_simulator.mainnet import load_corpus

# Reuse the synthetic-corpus fixture from test_mainnet.py.
from tests.test_mainnet import _make_maker, _run, _write_corpus  # noqa: PLC2701


def _load_sweep_module():  # type: ignore[no-untyped-def]
    """Load run_mainnet_sweep.py as a module without importing __main__."""
    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "run_mainnet_sweep",
        repo_root / "run_mainnet_sweep.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_mainnet_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sweep_module():  # type: ignore[no-untyped-def]
    return _load_sweep_module()


def test_hash_bucket_label_is_log_floored(sweep_module) -> None:  # type: ignore[no-untyped-def]
    """``_hash_bucket_label`` collapses fee rates to log10 floor bands."""
    assert sweep_module._hash_bucket_label(1e-4, 1.0) == -4
    assert sweep_module._hash_bucket_label(2e-4, 1.0) == -4  # same band
    assert sweep_module._hash_bucket_label(1e-5, 1.0) == -5
    # Sub-band stride: 0.5 splits each decade into halves.
    assert sweep_module._hash_bucket_label(1e-4, 0.5) == -8
    assert sweep_module._hash_bucket_label(0.0, 1.0) == -(10**9)


def test_assign_labels_hash_bucket_groups_by_band(sweep_module) -> None:  # type: ignore[no-untyped-def]
    """Outputs with the same log10 band share a cluster id."""
    rec = [
        RecoveredMakerOutput(
            output_id=f"out{i}",
            txid=f"tx{i}",
            cjfee_r=r,
            fee_sats=10,
            cj_amount_sats=100_000,
            maker_id_truth="",
            is_change=True,
        )
        for i, r in enumerate([1e-4, 2e-4, 1e-5, 9e-5])
    ]
    labels = sweep_module._assign_labels(
        rec,
        strategy="hash_bucket",
        log_stride=1.0,
        eps=0.3,
    )
    # 1e-4 and 2e-4 -> band -4; 1e-5 -> band -5; 9e-5 -> band -5
    assert labels["out0"] == labels["out1"]
    assert labels["out2"] == labels["out3"]
    assert labels["out0"] != labels["out2"]


def test_assign_labels_dbscan_runs(sweep_module) -> None:  # type: ignore[no-untyped-def]
    """DBSCAN path returns a label per output (no required equalities)."""
    rec = [
        RecoveredMakerOutput(
            output_id=f"out{i}",
            txid=f"tx{i}",
            cjfee_r=r,
            fee_sats=10,
            cj_amount_sats=100_000,
            maker_id_truth="",
            is_change=True,
        )
        for i, r in enumerate([1e-4, 1.05e-4, 1e-2])
    ]
    labels = sweep_module._assign_labels(
        rec,
        strategy="dbscan",
        log_stride=DEFAULT_LOG_STRIDE,
        eps=0.5,
    )
    assert set(labels.keys()) == {"out0", "out1", "out2"}
    # Close-together points share a cluster, distant one differs.
    assert labels["out0"] == labels["out1"]
    assert labels["out0"] != labels["out2"]


def test_assign_labels_unknown_strategy(sweep_module) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="unknown strategy"):
        sweep_module._assign_labels([], strategy="kmeans", log_stride=1.0, eps=0.3)


def test_dump_clusters_writes_per_cluster_json(
    tmp_path: Path,
    sweep_module,  # type: ignore[no-untyped-def]
) -> None:
    """End-to-end: dump cluster JSONs from a synthetic mini-corpus."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    txs = [e.tx for e in entries]
    rec = recover_maker_outputs_from_txs(txs, require_truth=False, mode="ilp")
    labels = sweep_module._assign_labels(
        rec,
        strategy="hash_bucket",
        log_stride=DEFAULT_LOG_STRIDE,
        eps=0.3,
    )
    # Build a minimal MainnetReport-shaped object for the dump helper.
    from coinjoin_simulator.mainnet import run_corpus

    report = run_corpus(entries, strategy="hash_bucket", mode="ilp")
    out_dir = tmp_path / "clusters"
    sweep_module._dump_clusters(
        rec,
        labels,
        report=report,
        cache_dir=cache_dir,
        out_dir=out_dir,
    )
    files = sorted(out_dir.glob("cluster_*.json"))
    assert files, "expected at least one cluster dump"
    payload = json.loads(files[0].read_text())
    for key in (
        "cluster_id",
        "n_outputs",
        "cjfee_r_mean",
        "log10_band",
        "n_distinct_addresses",
        "n_distinct_txs",
        "total_value_sats",
        "utxos",
    ):
        assert key in payload
    assert payload["n_outputs"] == len(payload["utxos"])
    for u in payload["utxos"]:
        assert "txid" in u and "vout" in u and "address" in u and "value_sats" in u
        assert u["value_sats"] >= 0


def test_dump_clusters_clears_stale_files(
    tmp_path: Path,
    sweep_module,  # type: ignore[no-untyped-def]
) -> None:
    """Re-running the dump nukes prior cluster_*.json so old runs don't linger."""
    out_dir = tmp_path / "clusters"
    out_dir.mkdir()
    stale = out_dir / "cluster_999999.json"
    stale.write_text("{}")
    # Empty rec -> no new files written, but stale should still be removed.
    from coinjoin_simulator.mainnet import MainnetReport

    empty_report = MainnetReport(
        strategy="hash_bucket",
        mode="ilp",
        n_txs_seen=0,
        n_txs_solved=0,
        n_recovered=0,
        n_clusters=0,
        log_stride=DEFAULT_LOG_STRIDE,
        eps=None,
        clusters=[],
    )
    sweep_module._dump_clusters(
        [],
        {},
        report=empty_report,
        cache_dir=tmp_path,
        out_dir=out_dir,
    )
    assert not stale.exists()
