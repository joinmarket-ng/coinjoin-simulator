"""Unit tests for v7.2 maker clusterer (non-CJ round-trip CIOH edges)."""

from __future__ import annotations

from coinjoin_simulator.clusterer_v7 import MakerSlotV7
from coinjoin_simulator.clusterer_v71 import NonCjSpender
from coinjoin_simulator.clusterer_v72 import NonCjHop, cluster_v72


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


def _same(labels: dict[int, int], a: int, b: int) -> bool:
    return labels[a] == labels[b]


# ---------------------------------------------------------------------------
# Positive round-trip
# ---------------------------------------------------------------------------


def test_round_trip_unions_producer_and_consumer() -> None:
    """Maker change -> non-CJ hop -> maker-slot input round trip."""
    slots = [
        # Producer slot in T1.
        _slot("T1", "M1", inputs=("seed:0",), ch="T1:7"),
        # Consumer slot in T2 whose first input is hop output N1:0.
        _slot("T2", "M2", inputs=("N1:0",), ch="T2:7"),
        # An unrelated slot.
        _slot("T3", "M3", inputs=("in3:0",), ch="T3:7"),
    ]
    hops = [
        NonCjHop(
            hop_txid="N1",
            n_outputs=2,
            consumed_maker_outpoints=("T1:7",),
            output_outpoints=("N1:0", "N1:1"),
        ),
    ]
    res = cluster_v72(slots, equal_outpoints_by_tx={}, non_cj_spenders=(), non_cj_hops=hops)
    assert _same(res.labels, 0, 1)
    assert not _same(res.labels, 0, 2)
    assert res.v72.n_qualifying_hops == 1
    assert res.v72.n_cross_cj_unions == 1


# ---------------------------------------------------------------------------
# Conservative output filter
# ---------------------------------------------------------------------------


def test_hop_with_too_many_outputs_dropped() -> None:
    slots = [
        _slot("T1", "M1", inputs=("seed:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("N1:0",), ch="T2:7"),
    ]
    hops = [
        NonCjHop(
            hop_txid="N1",
            n_outputs=5,
            consumed_maker_outpoints=("T1:7",),
            output_outpoints=("N1:0",),
        ),
    ]
    res = cluster_v72(slots, equal_outpoints_by_tx={}, non_cj_spenders=(), non_cj_hops=hops)
    assert not _same(res.labels, 0, 1)
    assert res.v72.n_dropped_by_outputs == 1


# ---------------------------------------------------------------------------
# Same-CJ rejection
# ---------------------------------------------------------------------------


def test_same_cj_round_trip_dropped() -> None:
    """If producer and consumer slots are in the same CJ, drop."""
    slots = [
        _slot("T1", "M1", inputs=("seed:0",), ch="T1:7"),
        # Consumer slot also in T1.
        _slot("T1", "M2", inputs=("N1:0",), ch="T1:8"),
    ]
    hops = [
        NonCjHop(
            hop_txid="N1",
            n_outputs=2,
            consumed_maker_outpoints=("T1:7",),
            output_outpoints=("N1:0",),
        ),
    ]
    res = cluster_v72(slots, equal_outpoints_by_tx={}, non_cj_spenders=(), non_cj_hops=hops)
    assert not _same(res.labels, 0, 1)
    assert res.v72.n_same_cj_pairs_dropped == 1


# ---------------------------------------------------------------------------
# Missing endpoints
# ---------------------------------------------------------------------------


def test_no_producer_dropped() -> None:
    slots = [
        _slot("T1", "M1", inputs=("seed:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("N1:0",), ch="T2:7"),
    ]
    hops = [
        NonCjHop(
            hop_txid="N1",
            n_outputs=2,
            consumed_maker_outpoints=("UNKNOWN:0",),
            output_outpoints=("N1:0",),
        ),
    ]
    res = cluster_v72(slots, equal_outpoints_by_tx={}, non_cj_spenders=(), non_cj_hops=hops)
    assert not _same(res.labels, 0, 1)
    assert res.v72.n_dropped_no_producer == 1


def test_no_consumer_dropped() -> None:
    slots = [
        _slot("T1", "M1", inputs=("seed:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("seed2:0",), ch="T2:7"),
    ]
    hops = [
        NonCjHop(
            hop_txid="N1",
            n_outputs=2,
            consumed_maker_outpoints=("T1:7",),
            output_outpoints=("N1:0",),
        ),
    ]
    res = cluster_v72(slots, equal_outpoints_by_tx={}, non_cj_spenders=(), non_cj_hops=hops)
    assert not _same(res.labels, 0, 1)
    assert res.v72.n_dropped_no_consumer == 1


# ---------------------------------------------------------------------------
# Composition with v7.1
# ---------------------------------------------------------------------------


def test_v71_and_v72_compose() -> None:
    """Same corpus exercises both v7.1 co-spend and v7.2 round-trip."""
    slots = [
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("seed2:0",), ch="T2:7"),
        _slot("T3", "M3", inputs=("N2:0",), ch="T3:7"),
    ]
    # v7.1: co-spend of T1:7 and T2:7 by N1.
    spenders = [
        NonCjSpender(spender_txid="N1", n_outputs=2, maker_outpoints=("T1:7", "T2:7")),
    ]
    # v7.2: round-trip from T2:7 -> N2 -> T3 input.
    # But T2:7 is consumed by N1 above, not N2; use a different change.
    # Adjust: give M2 a second slot in T2b, or use the equal_output of M2.
    # Simpler: have T1's slot produce a change consumed by N2 (a different non-CJ).
    # But T1:7 already used by N1. So let's use a new approach: add a separate slot.
    slots = [
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("seed2:0",), ch="T2:7"),
        _slot("T4", "M4", inputs=("seed4:0",), ch="T4:7"),
        _slot("T3", "M3", inputs=("N2:0",), ch="T3:7"),
    ]
    spenders = [
        NonCjSpender(spender_txid="N1", n_outputs=2, maker_outpoints=("T1:7", "T2:7")),
    ]
    hops = [
        NonCjHop(
            hop_txid="N2",
            n_outputs=2,
            consumed_maker_outpoints=("T4:7",),
            output_outpoints=("N2:0",),
        ),
    ]
    res = cluster_v72(
        slots,
        equal_outpoints_by_tx={},
        non_cj_spenders=spenders,
        non_cj_hops=hops,
    )
    # v7.1 merges slot 0 (T1) and slot 1 (T2).
    assert _same(res.labels, 0, 1)
    # v7.2 merges slot 2 (T4) and slot 3 (T3).
    assert _same(res.labels, 2, 3)
    # The two clusters are disjoint.
    assert not _same(res.labels, 0, 2)


# ---------------------------------------------------------------------------
# v6/v7 still fires
# ---------------------------------------------------------------------------


def test_v6_change_chain_still_fires() -> None:
    slots = [
        _slot("T1", "M1", inputs=("seed:0",), ch="T1:7"),
        _slot("T2", "M1", inputs=("T1:7",), ch="T2:7"),
    ]
    res = cluster_v72(slots, equal_outpoints_by_tx={}, non_cj_spenders=(), non_cj_hops=())
    assert _same(res.labels, 0, 1)


def test_v7_attribution_still_fires() -> None:
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), eq="T1:5", fee=100),
        _slot("T1", "M2", inputs=("in2:0",), eq="T1:6", fee=200),
        _slot("T2", "M3", inputs=("T1:5",), fee=100),
    ]
    res = cluster_v72(
        slots,
        equal_outpoints_by_tx={"T1": ["T1:5", "T1:6"]},
        non_cj_spenders=(),
        non_cj_hops=(),
    )
    assert _same(res.labels, 0, 2)
    assert not _same(res.labels, 1, 2)
