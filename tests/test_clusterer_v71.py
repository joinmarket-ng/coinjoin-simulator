"""Unit tests for v7.1 maker clusterer (non-CJ CIOH edges).

Covers:

* Positive case: <=2-output non-CJ spender co-spending two maker
  change UTXOs from different CJs unions the two slots.
* Same-CJ pair is rejected (no precision violation).
* >2-output spenders are dropped by the conservative filter.
* Singleton non-CJ spends (<2 maker outpoints) are dropped.
* Unknown outpoints (not in slot store) are silently ignored.
* v7 attribution edges still fire alongside v7.1.
* v6 chain edges still fire alongside v7.1.
"""

from __future__ import annotations

from coinjoin_simulator.clusterer_v7 import MakerSlotV7
from coinjoin_simulator.clusterer_v71 import (
    NonCjSpender,
    cluster_v71,
)


def _slot(
    txid: str,
    owner: str,
    inputs: tuple[str, ...] = (),
    eq: str | None = None,
    ch: str | None = None,
    eq_amt: int = 1_000_000,
    fee: int = 100,
) -> MakerSlotV7:
    return MakerSlotV7(
        txid=txid,
        owner_id=owner,
        inputs=inputs,
        equal_output=eq,
        change_output=ch,
        equal_amt_sats=eq_amt,
        fee_sats=fee,
    )


def _same_cluster(labels: dict[int, int], a: int, b: int) -> bool:
    return labels[a] == labels[b]


# ---------------------------------------------------------------------------
# Positive case
# ---------------------------------------------------------------------------


def test_non_cj_cospend_unions_cross_cj_slots() -> None:
    """Two maker slots in distinct CJs whose change UTXOs are co-spent
    by a 2-output non-CJ tx should be unioned."""
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), ch="T1:7"),
        _slot("T2", "M1", inputs=("in2:0",), ch="T2:7"),
        _slot("T3", "M2", inputs=("in3:0",), ch="T3:7"),
    ]
    non_cj = [
        NonCjSpender(
            spender_txid="N1",
            n_outputs=2,
            maker_outpoints=("T1:7", "T2:7"),
        ),
    ]
    res = cluster_v71(slots, equal_outpoints_by_tx={}, non_cj_spenders=non_cj)
    assert _same_cluster(res.labels, 0, 1)
    assert not _same_cluster(res.labels, 0, 2)
    assert res.v71.n_qualifying_spenders == 1
    assert res.v71.n_cross_cj_unions == 1


# ---------------------------------------------------------------------------
# Same-CJ rejection
# ---------------------------------------------------------------------------


def test_same_cj_pair_is_rejected() -> None:
    """If a non-CJ tx co-spends two change UTXOs from the same CJ,
    the pair must NOT be unioned (precision contract)."""
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), ch="T1:7"),
        _slot("T1", "M2", inputs=("in2:0",), ch="T1:8"),
    ]
    non_cj = [
        NonCjSpender(
            spender_txid="N1",
            n_outputs=2,
            maker_outpoints=("T1:7", "T1:8"),
        ),
    ]
    res = cluster_v71(slots, equal_outpoints_by_tx={}, non_cj_spenders=non_cj)
    assert not _same_cluster(res.labels, 0, 1)
    assert res.v71.n_same_cj_pairs_dropped == 1
    assert res.v71.n_cross_cj_unions == 0


# ---------------------------------------------------------------------------
# Output-count filter
# ---------------------------------------------------------------------------


def test_spender_with_more_than_two_outputs_is_dropped() -> None:
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), ch="T1:7"),
        _slot("T2", "M1", inputs=("in2:0",), ch="T2:7"),
    ]
    non_cj = [
        NonCjSpender(
            spender_txid="N1",
            n_outputs=5,
            maker_outpoints=("T1:7", "T2:7"),
        ),
    ]
    res = cluster_v71(slots, equal_outpoints_by_tx={}, non_cj_spenders=non_cj)
    assert not _same_cluster(res.labels, 0, 1)
    assert res.v71.n_dropped_by_outputs == 1
    assert res.v71.n_cross_cj_unions == 0


def test_max_spender_outputs_param_loosened() -> None:
    """Lifting the cap to 3 should accept a 3-output spender."""
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), ch="T1:7"),
        _slot("T2", "M1", inputs=("in2:0",), ch="T2:7"),
    ]
    non_cj = [
        NonCjSpender(
            spender_txid="N1",
            n_outputs=3,
            maker_outpoints=("T1:7", "T2:7"),
        ),
    ]
    res = cluster_v71(
        slots,
        equal_outpoints_by_tx={},
        non_cj_spenders=non_cj,
        max_spender_outputs=3,
    )
    assert _same_cluster(res.labels, 0, 1)
    assert res.v71.n_cross_cj_unions == 1


# ---------------------------------------------------------------------------
# Size filter
# ---------------------------------------------------------------------------


def test_single_maker_outpoint_is_dropped() -> None:
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("in2:0",), ch="T2:7"),
    ]
    non_cj = [
        NonCjSpender(
            spender_txid="N1",
            n_outputs=2,
            maker_outpoints=("T1:7",),
        ),
    ]
    res = cluster_v71(slots, equal_outpoints_by_tx={}, non_cj_spenders=non_cj)
    assert not _same_cluster(res.labels, 0, 1)
    assert res.v71.n_dropped_by_size == 1


def test_unknown_outpoints_silently_ignored() -> None:
    """If a caller passes outpoints not present in the slot store
    (e.g. non-maker inputs), they should be silently dropped and the
    spender then falls into n_dropped_by_size."""
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), ch="T1:7"),
    ]
    non_cj = [
        NonCjSpender(
            spender_txid="N1",
            n_outputs=2,
            maker_outpoints=("UNKNOWN:0", "ALSO_UNKNOWN:1"),
        ),
    ]
    res = cluster_v71(slots, equal_outpoints_by_tx={}, non_cj_spenders=non_cj)
    assert res.v71.n_dropped_by_size == 1
    assert res.v71.n_cross_cj_unions == 0


# ---------------------------------------------------------------------------
# Interaction with v6/v7 edges
# ---------------------------------------------------------------------------


def test_v6_change_chain_still_fires() -> None:
    """A slot's change_output consumed as maker input in a later CJ
    must still produce a union under v7.1 (inherited v6 edge)."""
    slots = [
        _slot("T1", "M1", inputs=("seed:0",), ch="T1:7"),
        _slot("T2", "M1", inputs=("T1:7",), ch="T2:7"),
    ]
    res = cluster_v71(slots, equal_outpoints_by_tx={}, non_cj_spenders=())
    assert _same_cluster(res.labels, 0, 1)


def test_v7_attribution_still_fires() -> None:
    """v7 fee-fingerprint equal-output edge must still fire."""
    # Producer CJ T1 has two maker slots with distinguishable fees.
    # The consumer slot in T2 fingerprints to producer slot 0.
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), eq="T1:5", fee=100, eq_amt=1_000_000),
        _slot("T1", "M2", inputs=("in2:0",), eq="T1:6", fee=200, eq_amt=1_000_000),
        _slot("T2", "M3", inputs=("T1:5",), fee=100, eq_amt=1_000_000),
    ]
    equal_outpoints_by_tx = {"T1": ["T1:5", "T1:6"]}
    res = cluster_v71(
        slots,
        equal_outpoints_by_tx=equal_outpoints_by_tx,
        non_cj_spenders=(),
    )
    # Producer slot 0 (M1 in T1) and consumer slot 2 (M3 in T2) merge.
    assert _same_cluster(res.labels, 0, 2)
    assert not _same_cluster(res.labels, 1, 2)


def test_v71_unions_in_addition_to_v7() -> None:
    """A single corpus exercising both v7 attribution and v7.1 CIOH."""
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), eq="T1:5", ch="T1:6", fee=100),
        _slot("T1", "M2", inputs=("in2:0",), eq="T1:7", ch="T1:8", fee=200),
        _slot("T2", "M3", inputs=("T1:5",), ch="T2:9", fee=100),  # v7 edge
        _slot("T3", "M4", inputs=("in3:0",), ch="T3:9", fee=300),
    ]
    equal_outpoints_by_tx = {"T1": ["T1:5", "T1:7"]}
    # Non-CJ spender co-spends T2:9 (slot 2 change) and T3:9 (slot 3 change).
    non_cj = [
        NonCjSpender(
            spender_txid="N1",
            n_outputs=2,
            maker_outpoints=("T2:9", "T3:9"),
        ),
    ]
    res = cluster_v71(
        slots,
        equal_outpoints_by_tx=equal_outpoints_by_tx,
        non_cj_spenders=non_cj,
    )
    # v7: slot 0 (T1 M1) <-> slot 2 (T2 M3) via equal-output fp.
    # v7.1: slot 2 <-> slot 3 via non-CJ CIOH.
    # Transitively: 0,2,3 should be one cluster; slot 1 alone.
    assert _same_cluster(res.labels, 0, 2)
    assert _same_cluster(res.labels, 2, 3)
    assert _same_cluster(res.labels, 0, 3)
    assert not _same_cluster(res.labels, 1, 0)
