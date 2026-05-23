"""Unit tests for v7.3 maker clusterer (fidelity-bond funding-tx CIOH)."""

from __future__ import annotations

from coinjoin_simulator.clusterer_v7 import MakerSlotV7
from coinjoin_simulator.clusterer_v71 import NonCjSpender
from coinjoin_simulator.clusterer_v72 import NonCjHop
from coinjoin_simulator.clusterer_v73 import FbFundingTx, cluster_v73


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


def test_fb_backward_via_change_merges_two_slots() -> None:
    """Two slots whose change outputs are co-inputs of the same FB-funding tx merge."""
    slots = [
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("seed2:0",), ch="T2:7"),
        _slot("T3", "M3", inputs=("seed3:0",), ch="T3:7"),
    ]
    fbs = [
        FbFundingTx(
            nick="NICK_A",
            fund_txid="F1",
            fb_vout=0,
            n_outputs=2,
            is_jm_cj=False,
            input_outpoints=("T1:7", "T2:7"),
            other_output_outpoints=("F1:1",),
        ),
    ]
    res = cluster_v73(slots, equal_outpoints_by_tx={}, fb_funding_txs=fbs)
    assert _same(res.labels, 0, 1)
    assert not _same(res.labels, 0, 2)
    assert res.v73.n_fb_used == 1
    assert res.v73.n_back_anchors_via_change == 2
    assert res.v73.n_cross_cluster_unions == 1


def test_fb_funding_that_is_jm_cj_is_skipped() -> None:
    slots = [
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("seed2:0",), ch="T2:7"),
    ]
    fbs = [
        FbFundingTx(
            nick="NICK_A",
            fund_txid="F1",
            fb_vout=0,
            n_outputs=2,
            is_jm_cj=True,
            input_outpoints=("T1:7", "T2:7"),
            other_output_outpoints=(),
        ),
    ]
    res = cluster_v73(slots, equal_outpoints_by_tx={}, fb_funding_txs=fbs)
    assert not _same(res.labels, 0, 1)
    assert res.v73.n_fb_skipped_jm_cj == 1
    assert res.v73.n_fb_used == 0


def test_fb_strict_forward_merges_via_sibling_output() -> None:
    """FB funding tx <=2 outputs: non-FB output consumed by a maker slot."""
    slots = [
        # Slot whose change is consumed by F as input (gives a backward anchor).
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        # Slot whose input is F's non-FB sibling output (forward anchor).
        _slot("T2", "M2", inputs=("F1:1",), ch="T2:7"),
    ]
    fbs = [
        FbFundingTx(
            nick="NICK_A",
            fund_txid="F1",
            fb_vout=0,
            n_outputs=2,
            is_jm_cj=False,
            input_outpoints=("T1:7",),
            other_output_outpoints=("F1:1",),
        ),
    ]
    res = cluster_v73(slots, equal_outpoints_by_tx={}, fb_funding_txs=fbs)
    assert _same(res.labels, 0, 1)
    assert res.v73.n_fwd_strict_anchors == 1
    assert res.v73.n_cross_cluster_unions == 1


def test_fb_forward_skipped_when_funding_has_more_than_two_outputs() -> None:
    slots = [
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("F1:1",), ch="T2:7"),
    ]
    fbs = [
        FbFundingTx(
            nick="NICK_A",
            fund_txid="F1",
            fb_vout=0,
            n_outputs=5,
            is_jm_cj=False,
            input_outpoints=(),  # No backward anchor either.
            other_output_outpoints=("F1:1", "F1:2"),
        ),
    ]
    res = cluster_v73(slots, equal_outpoints_by_tx={}, fb_funding_txs=fbs)
    assert not _same(res.labels, 0, 1)
    assert res.v73.n_fwd_strict_anchors == 0


def test_fb_same_cj_pair_dropped() -> None:
    """Two slots in the same CJ anchored by one nick must not merge."""
    slots = [
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        _slot("T1", "M2", inputs=("seed2:0",), ch="T1:8"),
    ]
    fbs = [
        FbFundingTx(
            nick="NICK_A",
            fund_txid="F1",
            fb_vout=0,
            n_outputs=2,
            is_jm_cj=False,
            input_outpoints=("T1:7", "T1:8"),
            other_output_outpoints=(),
        ),
    ]
    res = cluster_v73(slots, equal_outpoints_by_tx={}, fb_funding_txs=fbs)
    assert not _same(res.labels, 0, 1)
    assert res.v73.n_same_cj_pairs_dropped == 1


def test_fb_cluster_nick_conflict_drops_all_anchors_for_conflicting_nicks() -> None:
    """If two FB nicks anchor the same v7.2 cluster, neither merges."""
    slots = [
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("seed2:0",), ch="T2:7"),
        _slot("T3", "M3", inputs=("seed3:0",), ch="T3:7"),
    ]
    # NICK_A anchors slot 0 and slot 1 (would merge).
    # NICK_B also anchors slot 0 (single, but conflict with NICK_A on cluster of slot 0).
    fbs = [
        FbFundingTx(
            nick="NICK_A",
            fund_txid="FA",
            fb_vout=0,
            n_outputs=2,
            is_jm_cj=False,
            input_outpoints=("T1:7", "T2:7"),
            other_output_outpoints=(),
        ),
        FbFundingTx(
            nick="NICK_B",
            fund_txid="FB",
            fb_vout=0,
            n_outputs=2,
            is_jm_cj=False,
            input_outpoints=("T1:7", "T3:7"),
            other_output_outpoints=(),
        ),
    ]
    res = cluster_v73(slots, equal_outpoints_by_tx={}, fb_funding_txs=fbs)
    # All three slots stay separate: cluster of slot 0 is conflict-touched,
    # and both nicks have at least one cluster in the conflict set.
    assert not _same(res.labels, 0, 1)
    assert not _same(res.labels, 0, 2)
    assert not _same(res.labels, 1, 2)
    assert res.v73.n_cluster_nick_conflicts >= 1


def test_fb_single_anchor_does_not_change_labels() -> None:
    slots = [
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("seed2:0",), ch="T2:7"),
    ]
    fbs = [
        FbFundingTx(
            nick="NICK_A",
            fund_txid="F1",
            fb_vout=0,
            n_outputs=2,
            is_jm_cj=False,
            input_outpoints=("T1:7",),
            other_output_outpoints=(),
        ),
    ]
    res = cluster_v73(slots, equal_outpoints_by_tx={}, fb_funding_txs=fbs)
    assert not _same(res.labels, 0, 1)
    assert res.v73.n_nicks_with_anchors == 1
    assert res.v73.n_nicks_with_multi_cluster == 0


def test_v72_pipeline_still_fires_under_v73() -> None:
    """v7.2 round-trip continues to merge under the v7.3 clusterer."""
    slots = [
        _slot("T1", "M1", inputs=("seed:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("N1:0",), ch="T2:7"),
    ]
    hops = [
        NonCjHop(
            hop_txid="N1",
            n_outputs=2,
            consumed_maker_outpoints=("T1:7",),
            output_outpoints=("N1:0", "N1:1"),
        ),
    ]
    res = cluster_v73(slots, equal_outpoints_by_tx={}, non_cj_hops=hops)
    assert _same(res.labels, 0, 1)


def test_v71_pipeline_still_fires_under_v73() -> None:
    slots = [
        _slot("T1", "M1", inputs=("seed1:0",), ch="T1:7"),
        _slot("T2", "M2", inputs=("seed2:0",), ch="T2:7"),
    ]
    spenders = [
        NonCjSpender(spender_txid="N1", n_outputs=2, maker_outpoints=("T1:7", "T2:7")),
    ]
    res = cluster_v73(slots, equal_outpoints_by_tx={}, non_cj_spenders=spenders)
    assert _same(res.labels, 0, 1)


def test_v7_attribution_still_fires_under_v73() -> None:
    slots = [
        _slot("T1", "M1", inputs=("in1:0",), eq="T1:5", fee=100),
        _slot("T1", "M2", inputs=("in2:0",), eq="T1:6", fee=200),
        _slot("T2", "M3", inputs=("T1:5",), fee=100),
    ]
    res = cluster_v73(
        slots,
        equal_outpoints_by_tx={"T1": ["T1:5", "T1:6"]},
    )
    assert _same(res.labels, 0, 2)
    assert not _same(res.labels, 1, 2)
