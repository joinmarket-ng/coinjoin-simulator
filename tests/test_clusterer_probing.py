"""Tests for the probing-attacker maker clusterer."""

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
from coinjoin_simulator.clusterer_oracle import hash_bucket_cluster
from coinjoin_simulator.clusterer_probing import (
    ProbingClustererReport,
    ProbingConfig,
    probing_cluster,
    run_probing_clusterer,
)
from coinjoin_simulator.world import SimResult, World, WorldConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_maker(
    name: str,
    *,
    seed: int,
    cjfee_r: float = 2e-5,
    cjfee_a: int = 500,
    minsize: int = 10_000,
    fidelity_bond_value: float = 1e8,
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


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_probing_clusterer_handles_empty_simulation() -> None:
    empty = SimResult(
        seed=0,
        txs=[],
        offer_log=[],
        payment_records=[],
        maker_id_by_utxo={},
        utxo_value_by_id={},
    )
    a = probing_cluster(empty)
    assert a.n_outputs == 0
    assert a.n_clusters == 0
    assert a.ari == pytest.approx(1.0)
    assert a.f1 == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Perfect probe -> oracle-equivalent recovery
# ---------------------------------------------------------------------------


def test_full_probe_full_resolution_recovers_ground_truth() -> None:
    """probe_success_rate=1, subset_sum_resolution_rate=1 -> perfect labels."""
    makers = [_make_maker(f"m{i}", seed=i) for i in range(4)]
    res = _run_world(makers, taker_seed=10, world_seed=100, makercount=4)
    a = probing_cluster(
        res,
        ProbingConfig(probe_success_rate=1.0, subset_sum_resolution_rate=1.0, seed=0),
    )
    # Every output gets its true counterparty -> perfect F1 / ARI.
    assert a.n_outputs > 0
    assert a.n_noise == 0
    assert a.precision == pytest.approx(1.0)
    assert a.recall == pytest.approx(1.0)
    assert a.f1 == pytest.approx(1.0)
    assert a.ari == pytest.approx(1.0)


def test_full_probe_recovers_at_least_as_well_as_oracle_hash_bucket() -> None:
    """The probing attacker with 100% probe coverage is strictly stronger
    than any fee-fingerprinting attacker, so its F1 must be >= the oracle
    hash-bucket F1 on the same simulation."""
    # Two makers with identical fee policy: oracle hash-bucket cannot
    # separate them, but the probe label can.
    makers = [
        _make_maker("twin1", seed=1, cjfee_r=2e-5, fidelity_bond_value=1e8),
        _make_maker("twin2", seed=2, cjfee_r=2e-5, fidelity_bond_value=1e8),
    ]
    res = _run_world(makers, taker_seed=11, world_seed=110, makercount=2)
    probe = probing_cluster(
        res,
        ProbingConfig(probe_success_rate=1.0, subset_sum_resolution_rate=1.0, seed=0),
    )
    oracle = hash_bucket_cluster(res)
    assert probe.f1 >= oracle.f1 - 1e-9


# ---------------------------------------------------------------------------
# Coverage degradation
# ---------------------------------------------------------------------------


def test_zero_probe_coverage_yields_all_noise() -> None:
    makers = [_make_maker(f"m{i}", seed=i) for i in range(3)]
    res = _run_world(makers, taker_seed=12, world_seed=120, makercount=3)
    a = probing_cluster(
        res,
        ProbingConfig(probe_success_rate=0.0, subset_sum_resolution_rate=1.0, seed=0),
    )
    assert a.n_outputs > 0
    # Every output unlabelled -> all noise.
    assert a.n_noise == a.n_outputs
    # Pair-counting on all-noise predictions: no positive predictions
    # but ground-truth has positives (multiple outputs per maker), so
    # recall must be 0 and F1 must be 0.
    assert a.recall == pytest.approx(0.0)


def test_partial_probe_coverage_is_monotone_in_rate() -> None:
    """Higher probe_success_rate must not reduce recall (in expectation).
    We seed both runs to keep the comparison deterministic and run multi-tx
    so there are pairs to score."""
    makers = [_make_maker(f"m{i}", seed=i, cjfee_r=10 ** -(4 + i)) for i in range(4)]
    res = _run_world(makers, taker_seed=13, world_seed=130, makercount=4)
    low = probing_cluster(
        res,
        ProbingConfig(probe_success_rate=0.25, subset_sum_resolution_rate=1.0, seed=42),
    )
    high = probing_cluster(
        res,
        ProbingConfig(probe_success_rate=1.0, subset_sum_resolution_rate=1.0, seed=42),
    )
    assert high.recall >= low.recall


# ---------------------------------------------------------------------------
# Subset-sum ambiguity
# ---------------------------------------------------------------------------


def test_subset_sum_resolution_rate_below_one_drops_some_labels() -> None:
    makers = [_make_maker(f"m{i}", seed=i) for i in range(4)]
    res = _run_world(makers, taker_seed=14, world_seed=140, makercount=4)
    a = probing_cluster(
        res,
        ProbingConfig(probe_success_rate=1.0, subset_sum_resolution_rate=0.5, seed=7),
    )
    assert 0 < a.n_noise < a.n_outputs


# ---------------------------------------------------------------------------
# Reporting wrapper
# ---------------------------------------------------------------------------


def test_run_probing_clusterer_returns_full_report() -> None:
    makers = [_make_maker(f"m{i}", seed=i) for i in range(3)]
    res = _run_world(makers, taker_seed=15, world_seed=150, makercount=3)
    report = run_probing_clusterer(
        res,
        config=ProbingConfig(probe_success_rate=1.0, subset_sum_resolution_rate=1.0, seed=0),
    )
    assert isinstance(report, ProbingClustererReport)
    assert report.n_true_makers == 3
    # All makers probed and resolved -> n_probed_makers == n_true_makers.
    assert report.n_probed_makers == 3


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_probing_clusterer_is_deterministic_under_seed() -> None:
    makers = [_make_maker(f"m{i}", seed=i) for i in range(4)]
    res = _run_world(makers, taker_seed=16, world_seed=160, makercount=4)
    cfg = ProbingConfig(probe_success_rate=0.5, subset_sum_resolution_rate=0.5, seed=99)
    a = probing_cluster(res, cfg)
    b = probing_cluster(res, cfg)
    assert a.labels == b.labels
    assert a.f1 == pytest.approx(b.f1)
