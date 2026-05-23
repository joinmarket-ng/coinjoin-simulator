"""Tests for the v6 state-machine maker clusterer."""

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
from coinjoin_simulator.clusterer_state_machine import (
    MakerSlot,
    _ConstrainedUnionFind,
    cluster_state_machine,
    state_machine_cluster,
)
from coinjoin_simulator.world import World, WorldConfig

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_world.py)
# ---------------------------------------------------------------------------


def _make_maker(
    name: str,
    *,
    seed: int,
    balances: dict[int, int],
    fidelity_bond_value: float = 1e9,
    max_mixdepth: int = DEFAULT_MAX_MIXDEPTH,
) -> Maker:
    utxos: dict[int, list[Utxo]] = {}
    for mix, sats in balances.items():
        utxos[mix] = [Utxo(utxo_id=f"u-{name}-m{mix}", value_sats=sats, mixdepth=mix)]
    return Maker(
        counterparty=name,
        policy=MakerFeePolicy(
            ordertype="sw0reloffer",
            cjfee_r=2e-5,
            cjfee_a_sats=500,
            txfee_contribution=100,
            minsize_sats=10_000,
            fidelity_bond_value=fidelity_bond_value,
        ),
        utxos=utxos,
        max_mixdepth=max_mixdepth,
        rng=random.Random(seed),
    )


def _build_makers(n: int, *, seed: int = 0) -> list[Maker]:
    makers: list[Maker] = []
    for i in range(n):
        balances = {m: 500_000_000 for m in range(DEFAULT_MAX_MIXDEPTH + 1)}
        makers.append(
            _make_maker(
                f"m{i}",
                seed=seed + i,
                balances=balances,
                fidelity_bond_value=1e9 + i * 1e6,
            ),
        )
    return makers


# ---------------------------------------------------------------------------
# Union-find unit tests
# ---------------------------------------------------------------------------


def test_uf_basic_union() -> None:
    uf = _ConstrainedUnionFind()
    for x in range(5):
        uf.make(x)
    assert uf.union(0, 1)
    assert uf.union(1, 2)
    assert uf.find(0) == uf.find(2)
    assert uf.find(3) != uf.find(0)


def test_uf_forbid_blocks_direct_union() -> None:
    uf = _ConstrainedUnionFind()
    for x in range(3):
        uf.make(x)
    uf.forbid(0, 1)
    assert not uf.union(0, 1)
    assert uf.find(0) != uf.find(1)


def test_uf_forbid_propagates_through_transitive_merges() -> None:
    """0 and 2 are forbidden; merging 0-1 then 1-2 must fail at the second step."""
    uf = _ConstrainedUnionFind()
    for x in range(3):
        uf.make(x)
    uf.forbid(0, 2)
    assert uf.union(0, 1)
    assert not uf.union(1, 2)
    assert uf.find(0) != uf.find(2)


def test_uf_forbid_survives_intermediate_merge() -> None:
    """0-A forbidden. Merge 0-1, then 1-A must be rejected."""
    uf = _ConstrainedUnionFind()
    for x in range(4):
        uf.make(x)
    uf.forbid(0, 3)
    assert uf.union(0, 1)
    assert uf.union(2, 3)
    assert not uf.union(1, 2)
    assert uf.find(0) != uf.find(3)


# ---------------------------------------------------------------------------
# cluster_state_machine logic
# ---------------------------------------------------------------------------


def test_cluster_state_machine_singletons() -> None:
    slots = [
        MakerSlot("t1", "m0", ("u1",), "out1", "ch1"),
        MakerSlot("t1", "m1", ("u2",), "out2", "ch2"),
    ]
    labels = cluster_state_machine(slots)
    assert labels[0] != labels[1]  # same CJ -> must-not-link


def test_cluster_state_machine_change_chain_merges() -> None:
    """Slot 0 emits ch1; slot 1 in a later tx consumes ch1 as input."""
    slots = [
        MakerSlot("t1", "m0", ("u-seed",), "out1", "ch1"),
        MakerSlot("t2", "m0?", ("ch1",), "out2", "ch2"),
    ]
    labels = cluster_state_machine(slots)
    assert labels[0] == labels[1]


def test_cluster_state_machine_eq_chain_merges() -> None:
    slots = [
        MakerSlot("t1", "m0", ("u-seed",), "out1", "ch1"),
        MakerSlot("t2", "m0?", ("out1",), "out2", "ch2"),
    ]
    labels = cluster_state_machine(slots)
    assert labels[0] == labels[1]


def test_cluster_state_machine_must_not_link_blocks_chain_through_same_cj() -> None:
    """Two slots in t2 are same-CJ-forbidden; chain edges from t1 cannot merge them."""
    slots = [
        MakerSlot("t1", "a", ("u-a-seed",), "a-out1", "a-ch1"),
        MakerSlot("t1", "b", ("u-b-seed",), "b-out1", "b-ch1"),
        # In t2, two slots both consume from t1, one from a, one from b.
        MakerSlot("t2", "x", ("a-ch1",), "x-out", "x-ch"),
        MakerSlot("t2", "y", ("b-ch1",), "y-out", "y-ch"),
    ]
    labels = cluster_state_machine(slots)
    # a-chain (0,2) merged, b-chain (1,3) merged.
    assert labels[0] == labels[2]
    assert labels[1] == labels[3]
    # But the two clusters must stay distinct: a and b are different makers.
    assert labels[0] != labels[1]


# ---------------------------------------------------------------------------
# End-to-end: simulator -> ClusterAssignment
# ---------------------------------------------------------------------------


def _tumbler(seed: int, *, makercount: int = 4, mixdepth_count: int = 3) -> PaymentTaker:
    # Use a PaymentTaker as a one-shot CJ generator. Many of them against a
    # small maker pool gives the maker-reuse signal the v6 clusterer needs.
    return PaymentTaker.build(
        rng=random.Random(seed),
        recipient=f"bc1qrecipient{seed}",
        amount_sats=2_000_000,
        src_mixdepth=0,
        makercount=makercount,
        follow_up_payment=False,
    )


def test_state_machine_cluster_on_simulator_corpus_high_precision() -> None:
    """Run a small mixed corpus; state-machine clusterer must hit precision == 1.0."""
    makers = _build_makers(8, seed=42)
    takers = [_tumbler(seed=i, makercount=4, mixdepth_count=3) for i in range(6)]
    cfg = WorldConfig(seed=123)
    w = World.from_components(config=cfg, makers=makers, takers=takers)
    res = w.run()
    assert len(res.txs) > 0

    assignment = state_machine_cluster(res)
    assert assignment.n_outputs > 0
    # Hard contract: under-clustering is acceptable, over-clustering is not.
    # Every cluster must be pure (precision == 1.0).
    assert assignment.precision == pytest.approx(1.0)


def test_state_machine_cluster_recovers_meaningful_recall() -> None:
    """With long maker reuse, recall should rise well above the singleton floor."""
    makers = _build_makers(4, seed=7)
    # With richest-mixdepth selection (matching joinmarket-ng), the
    # maker's reuse pool fragments across mixdepths and v6 needs more
    # rounds to recover the same recall the legacy lowest-mixdepth
    # simulator hit with 12 rounds.
    takers = [_tumbler(seed=i, makercount=3, mixdepth_count=4) for i in range(48)]
    cfg = WorldConfig(seed=99)
    w = World.from_components(config=cfg, makers=makers, takers=takers)
    res = w.run()

    assignment = state_machine_cluster(res)
    # Singleton recall floor is 1 / max_cluster_size; with 4 makers,
    # makercount=3, and 12 rounds, the v6 chain edge (state-machine
    # clusterer here is v6) still has to fire enough to lift recall
    # above the singleton floor. Recall is sensitive to the maker's
    # richest-mixdepth selection (matching joinmarket-ng) which spreads
    # reuse across mixdepths, so the lower bound is set conservatively
    # at 1.4x the singleton floor.
    singleton_floor = 1.0 / max(1, assignment.n_outputs / assignment.n_clusters)
    assert assignment.recall > singleton_floor * 1.4
    assert assignment.precision == pytest.approx(1.0)
