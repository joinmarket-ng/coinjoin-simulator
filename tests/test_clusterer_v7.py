"""Unit tests for v7 maker clusterer.

Covers:

* MakerSlotV7 fingerprint computation.
* Univocal absolute fee match unions producer + consumer.
* Univocal relative fee (ppm) match unions producer + consumer.
* Ambiguous match adds no edge.
* Disagreeing absolute/relative interpretations add no edge.
* No-match adds no edge.
* Same-CJ must-not-link is respected: a v7 fee-attribution edge that
  would merge two slots already forbidden across a transitive chain
  is silently dropped (no precision loss).
* v6 chain edges still fire alongside v7 attribution.
"""

from __future__ import annotations

from coinjoin_simulator.clusterer_v7 import (
    AttributionStats,
    MakerSlotV7,
    attribute_equal_outputs,
    cluster_v7,
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


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def test_abs_fingerprint_is_fee_sats() -> None:
    s = _slot("t", "m0", fee=137)
    assert s.abs_fp() == 137


def test_rel_fingerprint_is_ppm_rounded() -> None:
    s = _slot("t", "m0", eq_amt=1_000_000, fee=20)  # 20 sats / 1e6 = 20 ppm
    assert s.rel_fp_ppm() == 20


def test_rel_fingerprint_none_when_no_equal_amt() -> None:
    s = _slot("t", "m0", eq_amt=0, fee=20)
    assert s.rel_fp_ppm() is None


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_attribution_unique_absolute_match() -> None:
    # T has 3 maker slots with distinct fees. S' from T' has fee = T's slot 1.
    # Equal output T:0 is consumed by S'.
    slots = [
        _slot("T", "T-m0", fee=100, eq_amt=10_000),
        _slot("T", "T-m1", fee=137, eq_amt=10_000),
        _slot("T", "T-m2", fee=200, eq_amt=10_000),
        # Different eq_amt so rel ppm differs; abs is the discriminator.
        _slot("Tp", "Tp-m0", inputs=("T:0",), fee=137, eq_amt=50_000),
    ]
    edges, stats = attribute_equal_outputs(slots, {"T": ["T:0"]})
    assert edges == {"T:0": 1}
    assert stats.unique_abs_only + stats.unique_both_same_slot == 1


def test_attribution_unique_relative_match() -> None:
    # T's slots have fees s.t. only one has rel ppm equal to S'.
    # S' rel = 20 ppm.
    slots = [
        _slot("T", "T-m0", fee=10, eq_amt=1_000_000),  # 10 ppm
        _slot("T", "T-m1", fee=20, eq_amt=1_000_000),  # 20 ppm
        _slot("T", "T-m2", fee=100, eq_amt=1_000_000),  # 100 ppm
        # 20 ppm of 2_000_000 = 40 sats; absolute won't collide.
        _slot("Tp", "Tp-m0", inputs=("T:5",), fee=40, eq_amt=2_000_000),
    ]
    edges, stats = attribute_equal_outputs(slots, {"T": ["T:5"]})
    assert edges == {"T:5": 1}
    assert stats.unique_rel_only + stats.unique_both_same_slot == 1


def test_attribution_ambiguous_dropped() -> None:
    # Two producer slots share the same abs and rel fingerprint -> ambig.
    slots = [
        _slot("T", "T-m0", fee=100, eq_amt=1_000_000),
        _slot("T", "T-m1", fee=100, eq_amt=1_000_000),
        _slot("Tp", "Tp-m0", inputs=("T:0",), fee=100, eq_amt=1_000_000),
    ]
    edges, stats = attribute_equal_outputs(slots, {"T": ["T:0"]})
    assert edges == {}
    assert stats.ambiguous == 1


def test_attribution_disagreeing_interpretations_dropped() -> None:
    # Slot 0: fee=100 abs, ppm=100. Slot 1: fee=20 abs, ppm=20.
    # S': fee=100 abs (matches slot 0), but ppm=20 (matches slot 1).
    # eq_amt of S' must yield: 100 / eq = 20 ppm -> eq = 5_000_000.
    slots = [
        _slot("T", "T-m0", fee=100, eq_amt=1_000_000),  # abs 100, rel 100ppm
        _slot("T", "T-m1", fee=20, eq_amt=1_000_000),  # abs 20,  rel 20ppm
        _slot("Tp", "Tp-m0", inputs=("T:0",), fee=100, eq_amt=5_000_000),
    ]
    edges, stats = attribute_equal_outputs(slots, {"T": ["T:0"]})
    assert edges == {}
    assert stats.unique_both_different_slot == 1


def test_attribution_no_match_dropped() -> None:
    slots = [
        _slot("T", "T-m0", fee=100, eq_amt=1_000_000),
        _slot("Tp", "Tp-m0", inputs=("T:0",), fee=999, eq_amt=5_000_000),
    ]
    edges, stats = attribute_equal_outputs(slots, {"T": ["T:0"]})
    assert edges == {}
    assert stats.no_match == 1


def test_attribution_self_reuse_skipped() -> None:
    # Self-reuse should never be counted: a slot's own equal output
    # consumed in the same tx is impossible by protocol but defensive.
    slots = [
        _slot("T", "T-m0", inputs=("T:0",), fee=100, eq_amt=1_000_000),
    ]
    edges, stats = attribute_equal_outputs(slots, {"T": ["T:0"]})
    assert edges == {}
    assert stats.cross_cj_reuses == 0


# ---------------------------------------------------------------------------
# cluster_v7
# ---------------------------------------------------------------------------


def test_cluster_v7_unions_attributed_producer_and_consumer() -> None:
    slots = [
        _slot("T", "T-m0", fee=100, eq_amt=10_000),
        _slot("T", "T-m1", fee=137, eq_amt=10_000),
        _slot("Tp", "Tp-m0", inputs=("T:5",), fee=137, eq_amt=50_000),
    ]
    labels, stats = cluster_v7(slots, {"T": ["T:5"]})
    # Producer slot 1 should share a cluster with consumer slot 2.
    assert labels[1] == labels[2]
    # Slot 0 is unaffected.
    assert labels[0] != labels[1]


def test_cluster_v7_respects_same_cj_must_not_link() -> None:
    # Construct a chain that would force two same-CJ slots together:
    # T has slots m0, m1. Tp consumes T's eq_out and matches m0 by abs.
    # Tpp consumes Tp's change as input and is forced into Tp's cluster.
    # We then add a separate path that would try to merge slot1 of T
    # with the consumer chain. The result must keep T's m0 and m1 in
    # separate clusters.
    slots = [
        _slot("T", "T-m0", fee=100, eq_amt=1_000_000),
        _slot("T", "T-m1", fee=200, eq_amt=1_000_000),
        # Tp consumer attributed to T-m0
        _slot(
            "Tp",
            "Tp-m0",
            inputs=("T:5",),
            fee=100,
            eq_amt=2_000_000,
            ch="Tp:9",
        ),
        # Tpp consumes Tp's change -> same cluster as Tp via v6 edge.
        # If we also (incorrectly) tried to attribute T-m1 to a Tpp
        # input, the must-not-link should block the merge.
        _slot(
            "Tpp",
            "Tpp-m0",
            inputs=("Tp:9", "T:6"),
            fee=200,
            eq_amt=1_000_000,
        ),
    ]
    labels, stats = cluster_v7(slots, {"T": ["T:5", "T:6"]})
    # T-m0 and T-m1 must NEVER share a cluster.
    assert labels[0] != labels[1]


def test_cluster_v7_v6_change_chain_still_fires() -> None:
    # Two slots in different CJs with a change-chain edge (named
    # change_output reused as input).
    slots = [
        _slot("T1", "T1-m0", ch="T1:5", fee=10, eq_amt=1_000_000),
        _slot("T2", "T2-m0", inputs=("T1:5",), fee=10, eq_amt=1_000_000),
    ]
    labels, _ = cluster_v7(slots, {})
    assert labels[0] == labels[1]


def test_cluster_v7_empty_input() -> None:
    labels, stats = cluster_v7([], {})
    assert labels == {}
    assert stats == AttributionStats()


# ---------------------------------------------------------------------------
# Adversarial precision stress
# ---------------------------------------------------------------------------


def test_cluster_v7_adversarial_collision_must_not_link() -> None:
    # Construct a chain where naive attribution would merge two
    # same-CJ slots through a long transitive path. The must-not-link
    # constraint, inherited from v6, must block it.
    #
    # T has m0 and m1 with distinct fees.
    # Tp consumes T:5 (an equal output of T) and matches m0 by abs.
    # Tp also has its own slot Tp-m1.
    # Tpp consumes T:6 (another equal output of T) and matches m1.
    # Tpp also has slot Tpp-mx whose fee matches Tp-m1 abs.
    # If we also let Tp-m1 and Tpp-mx be unioned via some other
    # transitive route, all four slots could end up in one cluster,
    # violating same-CJ for T.
    slots = [
        _slot("T", "T-m0", fee=100, eq_amt=1_000_000),
        _slot("T", "T-m1", fee=200, eq_amt=1_000_000),
        # Two consumers of T's equal outputs in Tp
        _slot("Tp", "Tp-m0", inputs=("T:5",), fee=100, eq_amt=2_000_000, ch="Tp:9"),
        _slot("Tp", "Tp-m1", inputs=("ext:0",), fee=300, eq_amt=2_000_000),
        # In Tpp, a slot Tpp-m0 consumes T:6 and matches T-m1 abs.
        # Another slot Tpp-mx consumes Tp:9 (change of Tp-m0).
        _slot("Tpp", "Tpp-m0", inputs=("T:6",), fee=200, eq_amt=2_000_000),
        _slot("Tpp", "Tpp-mx", inputs=("Tp:9",), fee=300, eq_amt=2_000_000),
    ]
    labels, stats = cluster_v7(slots, {"T": ["T:5", "T:6"]})
    # T-m0 and T-m1 must not collide.
    assert labels[0] != labels[1]
    # Tp's two slots must not collide.
    assert labels[2] != labels[3]
    # Tpp's two slots must not collide.
    assert labels[4] != labels[5]
