"""v7.3 maker clusterer: v7.2 plus fidelity-bond funding-tx CIOH edges.

Background
----------
A JoinMarket maker advertises a fidelity bond (FB): a timelocked P2WSH
output owned by the maker. The public orderbook snapshot maps each
maker nick to its FB UTXO ``(txid, vout)``. The transaction ``F`` that
created the FB UTXO is itself non-CJ (FBs are funded from a regular
wallet send): every input of ``F`` is same-wallet as the FB owner by
common-input ownership heuristic (CIOH), and so are any of ``F``'s
other outputs (change) when ``F`` has ``<= 2`` outputs.

v7.3 turns this into two new same-wallet edges over the maker slots
that v7.2 already clusters:

* **Backward (FB-funding inputs):** for each FB ``(N, U_N)`` with
  funding tx ``F`` available, if any input outpoint of ``F`` equals
  the *change output* of a maker slot ``S``, then ``S``'s wallet
  equals ``N``'s wallet. Two slots ``S1, S2`` anchored to the same
  nick become same-wallet.
* **Strict forward (FB-funding sibling output):** if ``F`` has
  ``<= max_funding_outputs`` outputs, each non-FB output is change of
  ``N``'s wallet; if any such output is consumed as a maker-slot
  input ``I'`` in slot ``S'``, then ``S'`` is same-wallet as ``N``.

Safety guards
-------------
* Funding txs that are themselves JoinMarket CoinJoins are excluded:
  CIOH is unsound on a CJ.
* The same-CJ must-not-link constraint from v6 is inherited; v7.3
  edges that would merge two slots of the same CJ are dropped.
* If two distinct FB nicks anchor the same v7.2 cluster, they are
  reported as a conflict and *neither* anchor produces a merge. This
  is the analogue of v7's ``conflict`` resolution.

The clusterer is implemented as a post-pass over a v7.2 run: it
inherits the v7.2 labels and only adds FB-driven unions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from coinjoin_simulator.clusterer_state_machine import _ConstrainedUnionFind
from coinjoin_simulator.clusterer_v7 import (
    AttributionStats,
    MakerSlotV7,
    _build_index_v7,
    attribute_equal_outputs,
)
from coinjoin_simulator.clusterer_v71 import NonCjSpender, V71Stats
from coinjoin_simulator.clusterer_v72 import NonCjHop, V72Stats

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


@dataclass(slots=True, frozen=True)
class FbFundingTx:
    """A fidelity-bond funding transaction, public from orderbook.

    Attributes
    ----------
    nick:
        Maker nick that owns the fidelity bond.
    fund_txid:
        Txid of the funding transaction ``F``.
    fb_vout:
        Vout index of the FB UTXO within ``F``.
    n_outputs:
        Total number of outputs of ``F``. Used to gate the strict
        forward rule.
    is_jm_cj:
        Whether ``F`` itself is a known JoinMarket CoinJoin. If true,
        the funding tx is skipped (CIOH unsound on a CJ).
    input_outpoints:
        Tuple of ``F``'s input outpoints ``(txid:vout)``. Used by the
        backward rule.
    other_output_outpoints:
        Tuple of ``F``'s non-FB output outpoints ``(txid:vout)``. Used
        by the strict forward rule when ``n_outputs <= max``.
    """

    nick: str
    fund_txid: str
    fb_vout: int
    n_outputs: int
    is_jm_cj: bool
    input_outpoints: tuple[str, ...]
    other_output_outpoints: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class V73Stats:
    """Diagnostics from v7.3 fidelity-bond edge addition."""

    n_fb_funding_txs: int = 0
    n_fb_skipped_jm_cj: int = 0
    n_fb_used: int = 0
    n_back_anchors_via_change: int = 0
    n_back_anchors_via_slot_input: int = 0
    n_fwd_strict_anchors: int = 0
    n_nicks_with_anchors: int = 0
    n_nicks_with_multi_cluster: int = 0
    n_cluster_nick_conflicts: int = 0
    n_anchors_dropped_by_conflict: int = 0
    n_candidate_pairs: int = 0
    n_same_cj_pairs_dropped: int = 0
    n_cross_cluster_unions: int = 0


@dataclass(slots=True, frozen=True)
class ClusterV73Result:
    labels: dict[int, int]
    attribution: AttributionStats
    # Per-outpoint v7 fee-fingerprint attributions: maps a producer CJ's
    # equal-output outpoint to the producer slot id that the within-CJ
    # fingerprint uniquely identifies. This is the ONLY channel through
    # which the on-chain analyst can bind a specific equal output to a
    # specific maker; the change-chain and v7.1/v7.2/v7.3 edges link
    # *slots* across CJs but cannot label equal outputs because the ILP
    # of section 5 cannot tell two equal outputs of the same CJ apart.
    # Anonymity-set reducers therefore certify an equal output only
    # when it appears as a key in ``attribution_edges``.
    attribution_edges: dict[str, int]
    v71: V71Stats
    v72: V72Stats
    v73: V73Stats


def cluster_v73(  # noqa: PLR0912, PLR0915
    slots: Sequence[MakerSlotV7],
    equal_outpoints_by_tx: Mapping[str, Iterable[str]] | None = None,
    non_cj_spenders: Iterable[NonCjSpender] | None = None,
    non_cj_hops: Iterable[NonCjHop] | None = None,
    fb_funding_txs: Iterable[FbFundingTx] | None = None,
    max_spender_outputs: int = 2,
    max_hop_outputs: int = 2,
    max_funding_outputs: int = 2,
) -> ClusterV73Result:
    """Run the v7.3 clusterer.

    Pipeline:

    1. Same as v7.2 through step 6.
    2. v7.3 fidelity-bond funding-tx edges (backward + strict forward),
       with funding-tx-is-CJ exclusion and nick-conflict guard.
    """
    idx = _build_index_v7(slots)
    uf = _ConstrainedUnionFind()
    for i in range(len(slots)):
        uf.make(i)

    # Same-CJ must-not-link.
    for tx_slots in idx.slots_in_tx.values():
        for i in range(len(tx_slots)):
            for j in range(i + 1, len(tx_slots)):
                uf.forbid(tx_slots[i], tx_slots[j])

    # v6 chain edges.
    producer_of_named: dict[str, int] = {}
    for i, s in enumerate(idx.slot_by_id):
        if s.change_output is not None:
            producer_of_named[s.change_output] = i
        if s.equal_output is not None:
            producer_of_named[s.equal_output] = i
    for utxo_id, producer_id in producer_of_named.items():
        for consumer_id in idx.consumers_of_utxo.get(utxo_id, ()):
            if consumer_id == producer_id:
                continue
            uf.union(producer_id, consumer_id)

    # v7 attribution.
    attribution = AttributionStats()
    attribution_edges: dict[str, int] = {}
    if equal_outpoints_by_tx:
        attribution_edges, attribution = attribute_equal_outputs(
            slots, equal_outpoints_by_tx,
        )
        for outpoint, producer_slot_id in attribution_edges.items():
            for consumer_id in idx.consumers_of_utxo.get(outpoint, ()):
                if consumer_id == producer_slot_id:
                    continue
                uf.union(producer_slot_id, consumer_id)

    # v7.1 non-CJ CIOH co-spend.
    change_outpoint_to_slot: dict[str, int] = {}
    for i, s in enumerate(idx.slot_by_id):
        if s.change_output is not None:
            change_outpoint_to_slot[s.change_output] = i

    n_cand = n_qual = n_drop_out = n_drop_size = 0
    n_pairs = n_same_cj = n_unions = 0
    if non_cj_spenders is not None:
        for sp in non_cj_spenders:
            n_cand += 1
            if sp.n_outputs > max_spender_outputs:
                n_drop_out += 1
                continue
            slot_ids: list[int] = []
            for op in sp.maker_outpoints:
                sid = change_outpoint_to_slot.get(op)
                if sid is not None:
                    slot_ids.append(sid)
            if len(slot_ids) < 2:
                n_drop_size += 1
                continue
            n_qual += 1
            uniq = list(dict.fromkeys(slot_ids))
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    a, b = uniq[i], uniq[j]
                    n_pairs += 1
                    if idx.slot_by_id[a].txid == idx.slot_by_id[b].txid:
                        n_same_cj += 1
                        continue
                    if uf.union(a, b):
                        n_unions += 1
    v71 = V71Stats(
        n_candidate_spenders=n_cand,
        n_qualifying_spenders=n_qual,
        n_dropped_by_outputs=n_drop_out,
        n_dropped_by_size=n_drop_size,
        n_candidate_pairs=n_pairs,
        n_same_cj_pairs_dropped=n_same_cj,
        n_cross_cj_unions=n_unions,
    )

    # v7.2 round-trip.
    h_cand = h_qual = h_drop_out = h_drop_prod = h_drop_cons = 0
    h_pairs = h_same_cj = h_unions = 0
    if non_cj_hops is not None:
        for hop in non_cj_hops:
            h_cand += 1
            if hop.n_outputs > max_hop_outputs:
                h_drop_out += 1
                continue
            producer_ids: list[int] = []
            for op in hop.consumed_maker_outpoints:
                sid = change_outpoint_to_slot.get(op)
                if sid is not None:
                    producer_ids.append(sid)
            if not producer_ids:
                h_drop_prod += 1
                continue
            consumer_ids: list[int] = []
            for op in hop.output_outpoints:
                for cid in idx.consumers_of_utxo.get(op, ()):
                    consumer_ids.append(cid)
            if not consumer_ids:
                h_drop_cons += 1
                continue
            h_qual += 1
            uniq_p = list(dict.fromkeys(producer_ids))
            uniq_c = list(dict.fromkeys(consumer_ids))
            for a in uniq_p:
                for b in uniq_c:
                    if a == b:
                        continue
                    h_pairs += 1
                    if idx.slot_by_id[a].txid == idx.slot_by_id[b].txid:
                        h_same_cj += 1
                        continue
                    if uf.union(a, b):
                        h_unions += 1
    v72 = V72Stats(
        n_candidate_hops=h_cand,
        n_qualifying_hops=h_qual,
        n_dropped_by_outputs=h_drop_out,
        n_dropped_no_producer=h_drop_prod,
        n_dropped_no_consumer=h_drop_cons,
        n_candidate_pairs=h_pairs,
        n_same_cj_pairs_dropped=h_same_cj,
        n_cross_cj_unions=h_unions,
    )

    # v7.3 FB-funding edges.
    f_total = 0
    f_skip_cj = 0
    f_used = 0
    n_back_ch = 0
    n_back_si = 0
    n_fwd_s = 0
    # Per-nick anchored slot ids (before conflict resolution).
    nick_to_slots: dict[str, set[int]] = {}
    if fb_funding_txs is not None:
        for fb in fb_funding_txs:
            f_total += 1
            if fb.is_jm_cj:
                f_skip_cj += 1
                continue
            f_used += 1
            anchored: set[int] = nick_to_slots.setdefault(fb.nick, set())
            # Backward via change.
            for op in fb.input_outpoints:
                sid = change_outpoint_to_slot.get(op)
                if sid is not None:
                    anchored.add(sid)
                    n_back_ch += 1
                # Backward via slot-input (FB-funding input IS a maker-slot input).
                for cid in idx.consumers_of_utxo.get(op, ()):
                    anchored.add(cid)
                    n_back_si += 1
            # Strict forward.
            if fb.n_outputs <= max_funding_outputs:
                for op in fb.other_output_outpoints:
                    for cid in idx.consumers_of_utxo.get(op, ()):
                        anchored.add(cid)
                        n_fwd_s += 1

    # Compute current cluster id (representative) for each slot, so we can
    # detect cluster<-nicks conflicts.
    def repr_of(sid: int) -> int:
        return uf.find(sid)

    cluster_to_nicks: dict[int, set[str]] = {}
    nick_clusters_pre: dict[str, set[int]] = {}
    for nick, sids in nick_to_slots.items():
        cls = {repr_of(s) for s in sids}
        nick_clusters_pre[nick] = cls
        for c in cls:
            cluster_to_nicks.setdefault(c, set()).add(nick)

    conflicting_clusters = {c for c, ns in cluster_to_nicks.items() if len(ns) >= 2}
    n_cluster_conflicts = len(conflicting_clusters)
    dropped_conflict = 0

    n_nicks_anchored = sum(1 for cls in nick_clusters_pre.values() if cls)
    n_nicks_multi = sum(1 for cls in nick_clusters_pre.values() if len(cls) >= 2)

    n_pairs73 = 0
    n_same_cj73 = 0
    n_unions73 = 0
    for nick, sids in nick_to_slots.items():
        cls = nick_clusters_pre[nick]
        if not cls:
            continue
        # Drop entire nick anchor if any of its clusters is in a conflict
        # (two distinct FB nicks anchored to the same cluster: precision-
        # safe to abstain).
        if cls & conflicting_clusters:
            dropped_conflict += len(sids)
            continue
        if len(sids) < 2:
            continue
        uniq = sorted(sids)
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = uniq[i], uniq[j]
                n_pairs73 += 1
                if idx.slot_by_id[a].txid == idx.slot_by_id[b].txid:
                    n_same_cj73 += 1
                    continue
                if uf.union(a, b):
                    n_unions73 += 1

    v73 = V73Stats(
        n_fb_funding_txs=f_total,
        n_fb_skipped_jm_cj=f_skip_cj,
        n_fb_used=f_used,
        n_back_anchors_via_change=n_back_ch,
        n_back_anchors_via_slot_input=n_back_si,
        n_fwd_strict_anchors=n_fwd_s,
        n_nicks_with_anchors=n_nicks_anchored,
        n_nicks_with_multi_cluster=n_nicks_multi,
        n_cluster_nick_conflicts=n_cluster_conflicts,
        n_anchors_dropped_by_conflict=dropped_conflict,
        n_candidate_pairs=n_pairs73,
        n_same_cj_pairs_dropped=n_same_cj73,
        n_cross_cluster_unions=n_unions73,
    )

    comps = uf.components()
    roots = sorted(comps.keys())
    root_to_cid = {r: c for c, r in enumerate(roots)}
    labels = {i: root_to_cid[uf.find(i)] for i in range(len(slots))}

    return ClusterV73Result(
        labels=labels,
        attribution=attribution,
        attribution_edges=attribution_edges,
        v71=v71,
        v72=v72,
        v73=v73,
    )
