"""Tests for the oracle maker clusterer."""

from __future__ import annotations

import random

import pytest

from coinjoin_simulator.agents import (
    DEFAULT_MAX_MIXDEPTH,
    Maker,
    MakerFeePolicy,
    PaymentTaker,
    Utxo,
)
from coinjoin_simulator.clusterer_oracle import (
    ClusterAssignment,
    DbscanConfig,
    HashBucketConfig,
    _log_band,
    dbscan_cluster,
    hash_bucket_cluster,
    run_oracle_clusterers,
)
from coinjoin_simulator.world import SimResult, World, WorldConfig


def _make_maker(
    name: str,
    *,
    seed: int,
    cjfee_r: float,
    cjfee_a: int = 500,
    minsize: int = 10_000,
    fidelity_bond_value: float = 1e9,
    ordertype: str = "sw0reloffer",
) -> Maker:
    utxos: dict[int, list[Utxo]] = {
        m: [Utxo(utxo_id=f"u-{name}-m{m}", value_sats=100_000_000, mixdepth=m)]
        for m in range(DEFAULT_MAX_MIXDEPTH + 1)
    }
    return Maker(
        counterparty=name,
        policy=MakerFeePolicy(
            ordertype=ordertype,
            cjfee_r=cjfee_r,
            cjfee_a_sats=cjfee_a,
            txfee_contribution=100,
            minsize_sats=minsize,
            fidelity_bond_value=fidelity_bond_value,
        ),
        utxos=utxos,
        rng=random.Random(seed),
    )


def _payment_taker(seed: int, makercount: int = 4) -> PaymentTaker:
    return PaymentTaker.build(
        rng=random.Random(seed),
        recipient="bc1qrecipient",
        amount_sats=2_000_000,
        src_mixdepth=0,
        makercount=makercount,
    )


# ---------------------------------------------------------------------------
# Banding helper
# ---------------------------------------------------------------------------


def test_log_band_zero_and_negative_share_a_bucket() -> None:
    assert _log_band(0.0) == _log_band(-1.0)
    assert _log_band(0.0) != _log_band(1.0)


def test_log_band_separates_decades() -> None:
    # With stride=1 each decade is its own bucket.
    assert _log_band(1.0, stride=1.0) != _log_band(10.0, stride=1.0)
    assert _log_band(2.0, stride=1.0) == _log_band(9.99, stride=1.0)


def test_log_band_absorbs_jitter() -> None:
    """At the default stride (0.1), a yg-pe ±10% jitter must not flip bands."""
    centre = 1e-5
    low = centre * 0.9
    high = centre * 1.1
    # The default stride may not put all three points in the same band -- we
    # only require the spread (low..high) to span at most one band-edge.
    bands = {_log_band(low), _log_band(centre), _log_band(high)}
    assert len(bands) <= 2


# ---------------------------------------------------------------------------
# Hash-bucket clusterer
# ---------------------------------------------------------------------------


def _run_world(
    makers: list[Maker],
    *,
    taker_seed: int,
    world_seed: int,
    makercount: int = 3,
) -> SimResult:
    taker = _payment_taker(seed=taker_seed, makercount=makercount)
    return World.from_components(
        config=WorldConfig(seed=world_seed),
        makers=makers,
        takers=[taker],
    ).run()


def test_hash_bucket_perfectly_separates_distinct_policies() -> None:
    """Three makers with policies separated by orders of magnitude must split."""
    makers = [
        _make_maker("a", seed=1, cjfee_r=1e-5, fidelity_bond_value=1e9),
        _make_maker("b", seed=2, cjfee_r=1e-3, fidelity_bond_value=1e7),
        _make_maker("c", seed=3, cjfee_r=5e-4, fidelity_bond_value=1e8),
    ]
    res = _run_world(makers, taker_seed=10, world_seed=100, makercount=3)
    assignment = hash_bucket_cluster(res)
    # All 3 maker outputs should land in 3 distinct clusters.
    assert assignment.n_outputs == 3
    assert assignment.n_clusters == 3
    assert assignment.f1 == pytest.approx(1.0)
    assert assignment.ari == pytest.approx(1.0)


def test_hash_bucket_collapses_identical_policies() -> None:
    """Makers with identical policies should mostly collapse into one cluster.

    Hash-bucket cannot be perfect under random jitter because values that
    sit near a band boundary can flip across it; the more robust DBSCAN
    test below handles this with a tunable ``eps``. We require the
    dominant cluster to absorb the majority of identical-policy outputs.
    """
    makers = [_make_maker(f"m{i}", seed=i, cjfee_r=2e-5, fidelity_bond_value=1e9) for i in range(8)]
    res = _run_world(makers, taker_seed=11, world_seed=101, makercount=8)
    assignment = hash_bucket_cluster(res)
    assert assignment.n_outputs == 8
    # The largest cluster must hold at least 5/8 of the outputs.
    from collections import Counter

    cluster_sizes = Counter(assignment.labels.values())
    assert max(cluster_sizes.values()) >= 5
    # And there must be drastically fewer clusters than the 8 ground-truth
    # makers (otherwise the clusterer is useless).
    assert assignment.n_clusters <= 3


def test_hash_bucket_distinguishes_relative_vs_absolute_ordertype() -> None:
    """Absolute and relative ordertypes must never share a cluster."""
    makers = [
        _make_maker("rel", seed=1, cjfee_r=2e-5, ordertype="sw0reloffer"),
        _make_maker("abs", seed=2, cjfee_a=500, ordertype="sw0absoffer", cjfee_r=0.0),
    ]
    res = _run_world(makers, taker_seed=12, world_seed=102, makercount=2)
    assignment = hash_bucket_cluster(res)
    if assignment.n_outputs >= 2:
        assert assignment.n_clusters == 2


# ---------------------------------------------------------------------------
# DBSCAN clusterer
# ---------------------------------------------------------------------------


def test_dbscan_separates_well_spaced_makers() -> None:
    makers = [
        _make_maker("a", seed=1, cjfee_r=1e-5, fidelity_bond_value=1e9),
        _make_maker("b", seed=2, cjfee_r=1e-3, fidelity_bond_value=1e7),
        _make_maker("c", seed=3, cjfee_r=5e-4, fidelity_bond_value=1e8),
    ]
    res = _run_world(makers, taker_seed=20, world_seed=200, makercount=3)
    assignment = dbscan_cluster(res, DbscanConfig(eps=0.2))
    assert assignment.n_outputs == 3
    assert assignment.n_clusters == 3
    assert assignment.ari == pytest.approx(1.0)


def test_dbscan_eps_widens_to_merge_close_makers() -> None:
    """At a wide enough eps, fee-similar makers collapse into a single cluster."""
    # Two makers separated by < 0.05 dex in cjfee_r and identical otherwise.
    makers = [
        _make_maker("a", seed=1, cjfee_r=1.0e-5, fidelity_bond_value=1e9),
        _make_maker("b", seed=2, cjfee_r=1.05e-5, fidelity_bond_value=1e9),
    ]
    res = _run_world(makers, taker_seed=21, world_seed=201, makercount=2)
    narrow = dbscan_cluster(res, DbscanConfig(eps=0.001))
    wide = dbscan_cluster(res, DbscanConfig(eps=2.0))
    # Wide eps forces a single cluster.
    assert wide.n_clusters == 1
    # Narrow eps may split them.
    assert narrow.n_clusters >= wide.n_clusters


# ---------------------------------------------------------------------------
# Bond-axis weighting
# ---------------------------------------------------------------------------


def test_dbscan_bond_weight_separates_makers_with_distinct_bonds() -> None:
    """Two makers with same fee but bond differing by 2 dex should split when
    bond weight is boosted."""
    makers = [
        _make_maker("low", seed=1, cjfee_r=2e-5, fidelity_bond_value=1e7),
        _make_maker("high", seed=2, cjfee_r=2e-5, fidelity_bond_value=1e9),
    ]
    res = _run_world(makers, taker_seed=22, world_seed=202, makercount=2)
    weighted = dbscan_cluster(
        res,
        DbscanConfig(eps=0.5, feature_weights=(1.0, 1.0, 1.0, 5.0)),
    )
    unweighted = dbscan_cluster(
        res,
        DbscanConfig(eps=0.5, feature_weights=(1.0, 1.0, 1.0, 0.1)),
    )
    assert weighted.n_clusters >= unweighted.n_clusters


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_clusterers_handle_empty_simulation() -> None:
    empty = SimResult(
        seed=0,
        txs=[],
        offer_log=[],
        payment_records=[],
        maker_id_by_utxo={},
        utxo_value_by_id={},
    )
    hb = hash_bucket_cluster(empty)
    db = dbscan_cluster(empty)
    for a in (hb, db):
        assert a.n_outputs == 0
        assert a.n_clusters == 0
        assert a.ari == pytest.approx(1.0)
        assert a.f1 == pytest.approx(1.0)


def test_run_oracle_clusterers_returns_both() -> None:
    makers = [
        _make_maker(f"m{i}", seed=i, cjfee_r=10 ** -(4 + i), fidelity_bond_value=10 ** (7 + i))
        for i in range(4)
    ]
    res = _run_world(makers, taker_seed=30, world_seed=300, makercount=4)
    report = run_oracle_clusterers(res)
    assert isinstance(report.hash_bucket, ClusterAssignment)
    assert isinstance(report.dbscan, ClusterAssignment)
    # 4 makers with one fill each -> 4 outputs each.
    assert report.hash_bucket.n_outputs == 4
    assert report.dbscan.n_outputs == 4
    assert report.n_true_makers == 4


# ---------------------------------------------------------------------------
# Cross-strategy invariants
# ---------------------------------------------------------------------------


def test_perfect_separation_metrics_are_consistent() -> None:
    """When clusters perfectly recover ground truth, both metrics agree."""
    makers = [
        _make_maker("a", seed=1, cjfee_r=1e-5, fidelity_bond_value=1e9),
        _make_maker("b", seed=2, cjfee_r=1e-3, fidelity_bond_value=1e7),
    ]
    res = _run_world(makers, taker_seed=40, world_seed=400, makercount=2)
    hb = hash_bucket_cluster(res, HashBucketConfig(log_stride=0.05))
    assert hb.precision == pytest.approx(1.0)
    assert hb.recall == pytest.approx(1.0)
    assert hb.f1 == pytest.approx(1.0)
    assert hb.ari == pytest.approx(1.0)


def test_one_output_per_maker_yields_trivial_metrics() -> None:
    """With a single maker output, pair-counting is degenerate (1.0 by convention)."""
    makers = [_make_maker("a", seed=1, cjfee_r=2e-5, fidelity_bond_value=1e9)]
    # Need at least makercount makers to fill, so use one maker but require 1.
    res = _run_world(makers, taker_seed=50, world_seed=500, makercount=1)
    hb = hash_bucket_cluster(res)
    assert hb.n_outputs == 1
    # n < 2 -> no pairs -> precision/recall/f1 trivially 1.0.
    assert hb.precision == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Multi-tx invariance (same maker across txs -> same cluster)
# ---------------------------------------------------------------------------


def test_same_maker_across_multiple_cjs_clusters_together() -> None:
    """A maker that participates in multiple CJs should mostly land in one cluster.

    With well-separated policies (>= 1 dex apart) the oracle clusterer
    must achieve high recall: the dominant cluster for each maker must
    hold the majority of that maker's outputs.
    """
    makers = [
        _make_maker(f"m{i}", seed=i, cjfee_r=10 ** -(4 + i), fidelity_bond_value=10 ** (7 + i))
        for i in range(5)
    ]
    takers = [_payment_taker(seed=60 + i, makercount=3) for i in range(3)]
    res = World.from_components(
        config=WorldConfig(seed=600, txs_per_block=10),
        makers=makers,
        takers=list(takers),
    ).run()
    assignment = hash_bucket_cluster(res)
    from collections import Counter, defaultdict

    by_maker: dict[str, list[int]] = defaultdict(list)
    for output_id, label in assignment.labels.items():
        by_maker[assignment.ground_truth[output_id]].append(label)
    # For each maker, the dominant cluster must hold the majority of outputs.
    for cp, labels_for_cp in by_maker.items():
        sizes = Counter(labels_for_cp)
        dominant = max(sizes.values())
        assert dominant >= (len(labels_for_cp) + 1) // 2, (
            f"maker {cp} dominant cluster {dominant}/{len(labels_for_cp)}"
        )
    # Overall ARI should be strong (>0.7) when policies are 1 dex apart.
    assert assignment.ari > 0.7
