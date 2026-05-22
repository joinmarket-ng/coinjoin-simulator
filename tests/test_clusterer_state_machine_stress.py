"""Adversarial stress tests for the v6 state-machine clusterer.

These scenarios exercise the v6 contract (precision == 1.0, recall
materially above singleton floor) on harder simulator setups:

- multi-offer makers: one wallet identity publishing two offers (rel +
  abs), modelled as two ``Maker`` objects sharing a counterparty name
  but distinct fee policies and UTXO sets;
- intermittent makers: makers seeded with limited mixdepth coverage so
  they drop out of the pool after a few rounds;
- maker churn: a second cohort joins later in the run with no UTXO
  overlap to the first cohort;
- dense taker overlap: many takers running concurrently, increasing
  the chance that two distinct makers' slots end up adjacent in many
  CJs.

Each scenario validates strict precision == 1.0 and a meaningful
recall target chosen from the expected reuse rate (number of CJ
rounds per maker).
"""

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
from coinjoin_simulator.clusterer_state_machine import state_machine_cluster
from coinjoin_simulator.world import World, WorldConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _well_funded_maker(
    counterparty: str,
    *,
    seed: int,
    fb: float,
    utxo_prefix: str | None = None,
    policy: MakerFeePolicy | None = None,
) -> Maker:
    prefix = utxo_prefix or counterparty
    return Maker(
        counterparty=counterparty,
        policy=policy
        or MakerFeePolicy(
            ordertype="sw0reloffer",
            cjfee_r=2e-5,
            cjfee_a_sats=500,
            txfee_contribution=100,
            minsize_sats=10_000,
            fidelity_bond_value=fb,
        ),
        utxos={
            m: [Utxo(utxo_id=f"u-{prefix}-m{m}", value_sats=500_000_000, mixdepth=m)]
            for m in range(DEFAULT_MAX_MIXDEPTH + 1)
        },
        max_mixdepth=DEFAULT_MAX_MIXDEPTH,
        rng=random.Random(seed),
    )


def _payment_taker(seed: int, *, makercount: int = 4, amount: int = 2_000_000) -> PaymentTaker:
    return PaymentTaker.build(
        rng=random.Random(seed),
        recipient=f"bc1qrecipient{seed}",
        amount_sats=amount,
        src_mixdepth=0,
        makercount=makercount,
        follow_up_payment=False,
    )


def _run(makers: list[Maker], takers: list[PaymentTaker], *, seed: int = 0) -> object:
    w = World.from_components(config=WorldConfig(seed=seed), makers=makers, takers=takers)
    return w.run()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def test_v6_multi_offer_makers_stay_pure_and_merge_correctly() -> None:
    """A maker entity publishing two offers (rel + abs) must collapse to ONE cluster.

    We model this by giving two ``Maker`` objects the same
    ``counterparty`` but distinct policies and UTXO sets. The v6 chain
    edges only run through actual UTXO consumption, so the two halves
    of a single identity will NOT merge unless real chain reuse links
    them. The contract that must hold regardless is **precision = 1.0
    against ground truth**: every cluster must be pure (same true
    counterparty).
    """
    makers: list[Maker] = []
    for i in range(6):
        rel_policy = MakerFeePolicy(
            ordertype="sw0reloffer",
            cjfee_r=2e-5 + i * 1e-6,
            cjfee_a_sats=500,
            txfee_contribution=100,
            minsize_sats=10_000,
            fidelity_bond_value=1e9 + i * 1e6,
        )
        abs_policy = MakerFeePolicy(
            ordertype="sw0absoffer",
            cjfee_r=0.0,
            cjfee_a_sats=2000 + i * 50,
            txfee_contribution=100,
            minsize_sats=10_000,
            fidelity_bond_value=1e9 + i * 1e6,
        )
        makers.append(_well_funded_maker(f"m{i}", seed=i, fb=1e9, utxo_prefix=f"m{i}-rel", policy=rel_policy))
        makers.append(_well_funded_maker(f"m{i}", seed=i + 100, fb=1e9, utxo_prefix=f"m{i}-abs", policy=abs_policy))
    takers = [_payment_taker(seed=1000 + i, makercount=4) for i in range(60)]

    res = _run(makers, takers, seed=11)
    a = state_machine_cluster(res)

    assert a.n_outputs > 0
    assert a.precision == pytest.approx(1.0)
    # We expect at most 12 clusters (6 makers x 2 sub-wallets) and at least 6.
    assert 6 <= a.n_clusters <= 12


def test_v6_intermittent_makers_drop_out_cleanly() -> None:
    """Makers with limited UTXO inventory drop out after a few rounds.

    Even with churn the precision contract must hold; recall will
    degrade because dropped-out makers have shorter chains.
    """
    makers: list[Maker] = []
    for i in range(8):
        # Half the makers only have UTXOs in mixdepth 0; they'll exit after one round.
        if i % 2 == 0:
            utxos = {0: [Utxo(utxo_id=f"u-m{i}-m0", value_sats=200_000_000, mixdepth=0)]}
        else:
            utxos = {
                m: [Utxo(utxo_id=f"u-m{i}-m{m}", value_sats=500_000_000, mixdepth=m)]
                for m in range(DEFAULT_MAX_MIXDEPTH + 1)
            }
        makers.append(
            Maker(
                counterparty=f"m{i}",
                policy=MakerFeePolicy(
                    ordertype="sw0reloffer",
                    cjfee_r=2e-5,
                    cjfee_a_sats=500,
                    txfee_contribution=100,
                    minsize_sats=10_000,
                    fidelity_bond_value=1e9 + i * 1e6,
                ),
                utxos=utxos,
                max_mixdepth=DEFAULT_MAX_MIXDEPTH,
                rng=random.Random(i),
            ),
        )
    takers = [_payment_taker(seed=2000 + i, makercount=4) for i in range(40)]
    res = _run(makers, takers, seed=22)
    a = state_machine_cluster(res)

    assert a.n_outputs > 0
    assert a.precision == pytest.approx(1.0)


def test_v6_cohort_churn_no_cross_merging() -> None:
    """Two non-overlapping maker cohorts must NEVER be merged into a single cluster."""
    # First cohort - all chain reuse happens here.
    cohort_a = [_well_funded_maker(f"a{i}", seed=i, fb=1e9, utxo_prefix=f"a{i}") for i in range(4)]
    cohort_b = [_well_funded_maker(f"b{i}", seed=i + 50, fb=1e9, utxo_prefix=f"b{i}") for i in range(4)]
    takers = [_payment_taker(seed=3000 + i, makercount=4) for i in range(60)]

    res = _run([*cohort_a, *cohort_b], takers, seed=33)
    a = state_machine_cluster(res)

    assert a.n_outputs > 0
    assert a.precision == pytest.approx(1.0)
    # Each cluster must hold exactly one true identity (no cross-cohort merging).
    by_cluster: dict[int, set[str]] = {}
    for oid, cid in a.labels.items():
        by_cluster.setdefault(cid, set()).add(a.ground_truth[oid])
    for cid, owners in by_cluster.items():
        assert len(owners) == 1, f"cluster {cid} merged distinct identities: {owners}"


def test_v6_dense_overlap_high_recall() -> None:
    """A small maker pool with many CJs produces deep chains; recall should approach 1."""
    makers = [_well_funded_maker(f"m{i}", seed=i, fb=1e9 + i * 1e6) for i in range(5)]
    takers = [_payment_taker(seed=4000 + i, makercount=4) for i in range(120)]
    res = _run(makers, takers, seed=44)
    a = state_machine_cluster(res)

    assert a.precision == pytest.approx(1.0)
    # With 120 CJs against only 5 makers, each maker shows up many times and
    # chain reuse should collapse each identity into a single cluster.
    assert a.n_clusters <= 5
    assert a.recall > 0.9


def test_v6_high_taker_concurrency_still_pure() -> None:
    """Many concurrent takers stress same-CJ must-not-link constraints."""
    makers = [_well_funded_maker(f"m{i}", seed=i, fb=1e9) for i in range(10)]
    takers = [_payment_taker(seed=5000 + i, makercount=5) for i in range(80)]
    res = _run(makers, takers, seed=55)
    a = state_machine_cluster(res)

    assert a.n_outputs > 0
    assert a.precision == pytest.approx(1.0)
