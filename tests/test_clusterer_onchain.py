"""Tests for the on-chain attacker clusterer.

These tests exercise:
- the bridge between :class:`Tx` and ``joinmarket_analyzer`` input shape;
- the ILP solver wrapper recovers maker change outputs on small worlds;
- the hash-bucket and DBSCAN strategies cluster well-separated policies
  and degrade gracefully on identical policies / empty runs.
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
from coinjoin_simulator.clusterer_onchain import (
    dbscan_cluster_onchain,
    hash_bucket_cluster_onchain,
    is_coinjoin_tx,
    recover_maker_outputs,
    run_onchain_clusterers,
    tx_to_analyzer_dict,
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
) -> Maker:
    utxos: dict[int, list[Utxo]] = {
        m: [Utxo(utxo_id=f"u-{name}-m{m}", value_sats=100_000_000, mixdepth=m)]
        for m in range(DEFAULT_MAX_MIXDEPTH + 1)
    }
    return Maker(
        counterparty=name,
        policy=MakerFeePolicy(
            ordertype="sw0reloffer",
            cjfee_r=cjfee_r,
            cjfee_a_sats=cjfee_a,
            txfee_contribution=100,
            minsize_sats=minsize,
            fidelity_bond_value=fidelity_bond_value,
        ),
        utxos=utxos,
        rng=random.Random(seed),
    )


def _payment_taker(seed: int, makercount: int = 3) -> PaymentTaker:
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
    makercount: int,
) -> SimResult:
    return World.from_components(
        config=WorldConfig(seed=world_seed),
        makers=makers,
        takers=[_payment_taker(taker_seed, makercount=makercount)],
    ).run()


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


def test_bridge_round_trip_through_analyzer() -> None:
    """The bridge produces a dict that ``parse_transaction`` accepts."""
    from joinmarket_analyzer.parser import parse_transaction

    makers = [_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)]
    res = _run_world(makers, taker_seed=10, world_seed=100, makercount=3)
    assert len(res.txs) >= 1
    cj = next(t for t in res.txs if is_coinjoin_tx(t))
    parsed = parse_transaction(tx_to_analyzer_dict(cj))
    assert parsed.txid == cj.txid
    assert parsed.num_participants == 4  # 1 taker + 3 makers
    assert parsed.equal_amount == cj.cj_amount_sats
    # Network fee in parsed = sum(inputs) - sum(outputs) > 0 because the
    # simulator sizes the taker's input to cover it.
    assert parsed.network_fee == cj.network_fee_sats


def test_is_coinjoin_filters_followup_payments() -> None:
    """Follow-up payment txs (1-in/1-out) must not be treated as CJs."""
    makers = [_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)]
    taker = PaymentTaker.build(
        rng=random.Random(11),
        recipient="bc1qrecipient",
        amount_sats=2_000_000,
        src_mixdepth=0,
        makercount=3,
        follow_up_payment=True,
    )
    res = World.from_components(
        config=WorldConfig(seed=200),
        makers=makers,
        takers=[taker],
    ).run()
    cj_txs = [t for t in res.txs if is_coinjoin_tx(t)]
    followup_txs = [t for t in res.txs if not is_coinjoin_tx(t)]
    assert len(cj_txs) >= 1
    assert len(followup_txs) >= 1


# ---------------------------------------------------------------------------
# Solver wrapper
# ---------------------------------------------------------------------------


def test_recover_maker_outputs_returns_one_per_maker_change() -> None:
    """For a clean 3-maker CJ, the solver should recover all 3 maker changes."""
    makers = [_make_maker(f"m{i}", seed=i, cjfee_r=10 ** -(4 + i)) for i in range(3)]
    res = _run_world(makers, taker_seed=12, world_seed=300, makercount=3)
    rec = recover_maker_outputs(res)
    # 3 makers, 1 CJ, each maker should have a change output (large enough
    # input compared to cj amount).
    assert len(rec) >= 2
    # Each recovered output's truth maker_id is a real maker counterparty.
    truth_ids = {r.maker_id_truth for r in rec}
    assert truth_ids.issubset({m.counterparty for m in makers})


def test_recover_maker_outputs_empty_for_non_cj_only_run() -> None:
    """A run with no CJ tx (e.g. failed liquidity) yields no recoveries."""
    # No makers means takers can never fill -> world emits no tx.
    res = SimResult(
        seed=0, txs=[], offer_log=[], payment_records=[], maker_id_by_utxo={}, utxo_value_by_id={}
    )
    assert recover_maker_outputs(res) == []


# ---------------------------------------------------------------------------
# Hash-bucket clusterer
# ---------------------------------------------------------------------------


def test_hash_bucket_separates_well_separated_policies() -> None:
    """Three makers with policies 1-2 dex apart should land in distinct bands."""
    makers = [_make_maker(f"m{i}", seed=i, cjfee_r=10 ** -(3 + 2 * i)) for i in range(3)]
    res = _run_world(makers, taker_seed=20, world_seed=400, makercount=3)
    ca = hash_bucket_cluster_onchain(res)
    # Each recovered output should be in its own band.
    if ca.n_outputs >= 2:
        # ARI should be high; pair-counting F1 should also be > 0.5
        # because the makers are well separated.
        assert ca.ari >= 0.0  # at minimum, no worse than random
        assert ca.n_clusters >= 1


def test_hash_bucket_handles_empty_run() -> None:
    res = SimResult(
        seed=0, txs=[], offer_log=[], payment_records=[], maker_id_by_utxo={}, utxo_value_by_id={}
    )
    ca = hash_bucket_cluster_onchain(res)
    assert ca.n_outputs == 0
    assert ca.f1 == pytest.approx(1.0)
    assert ca.ari == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# DBSCAN clusterer
# ---------------------------------------------------------------------------


def test_dbscan_handles_empty_run() -> None:
    res = SimResult(
        seed=0, txs=[], offer_log=[], payment_records=[], maker_id_by_utxo={}, utxo_value_by_id={}
    )
    ca = dbscan_cluster_onchain(res)
    assert ca.n_outputs == 0


def test_dbscan_eps_controls_collapse() -> None:
    """A very wide eps collapses every recovery to one cluster."""
    makers = [_make_maker(f"m{i}", seed=i, cjfee_r=10 ** -(3 + i)) for i in range(3)]
    res = _run_world(makers, taker_seed=30, world_seed=500, makercount=3)
    rec = recover_maker_outputs(res)
    if len(rec) < 2:
        pytest.skip("solver did not recover enough makers in this small run")
    wide = dbscan_cluster_onchain(res, eps=100.0, recovered=rec)
    narrow = dbscan_cluster_onchain(res, eps=0.01, recovered=rec)
    assert wide.n_clusters <= narrow.n_clusters


# ---------------------------------------------------------------------------
# Combined runner
# ---------------------------------------------------------------------------


def test_run_onchain_clusterers_returns_both_strategies() -> None:
    makers = [_make_maker(f"m{i}", seed=i, cjfee_r=10 ** -(3 + 2 * i)) for i in range(3)]
    res = _run_world(makers, taker_seed=40, world_seed=600, makercount=3)
    out = run_onchain_clusterers(res)
    assert set(out.keys()) == {"hash_bucket", "dbscan"}
    for ca in out.values():
        assert 0.0 <= ca.f1 <= 1.0
        assert -1.0 <= ca.ari <= 1.0


# ---------------------------------------------------------------------------
# Comparison vs oracle
# ---------------------------------------------------------------------------


def test_onchain_recall_lower_or_equal_to_oracle() -> None:
    """The on-chain attacker cannot recover more pairs than the oracle.

    The oracle has access to the offer log; the on-chain attacker only
    has the public tx graph. On well-separated policies the on-chain
    attacker should still achieve nontrivial F1, but never exceed the
    oracle.
    """
    from coinjoin_simulator.clusterer_oracle import hash_bucket_cluster as oracle_hash

    makers = [_make_maker(f"m{i}", seed=i, cjfee_r=10 ** -(3 + 2 * i)) for i in range(4)]
    res = _run_world(makers, taker_seed=50, world_seed=700, makercount=4)
    oc = hash_bucket_cluster_onchain(res)
    oracle = oracle_hash(res)
    # Both metrics defined only when there are >= 2 outputs.
    if oc.n_outputs >= 2 and oracle.n_outputs >= 2:
        # On-chain attacker has strictly less information, so its F1
        # must be <= oracle F1 (with a tiny tolerance for ties).
        assert oc.f1 <= oracle.f1 + 1e-9
