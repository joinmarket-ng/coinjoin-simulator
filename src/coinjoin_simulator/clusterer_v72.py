"""v7.2 maker clusterer: v7.1 plus non-CJ round-trip CIOH edges.

Background
----------
v7.1 (:mod:`coinjoin_simulator.clusterer_v71`) merges two maker slots
when a simple non-CJ transaction co-spends two of their change UTXOs.
v7.2 closes another gap with the same conservative ``<= 2``-output
filter: the *round trip*.

When a maker change UTXO ``C`` (slot ``S``) is consumed by a non-CJ
transaction ``N`` with ``<= 2`` outputs, CIOH says every input of
``N`` belongs to ``S``'s wallet and every output is again that
wallet's (the wallet might keep the funds as change, send a small
payment to itself, or batch with one other internal payment). If one
of ``N``'s outputs is later consumed as a maker-slot input ``I'`` in
a different CJ slot ``S'``, then ``S`` and ``S'`` are same-wallet.

This is additive to v7.1: where v7.1 needs a single non-CJ tx that
co-spends *two* maker change UTXOs, v7.2 only needs one maker change
UTXO into a simple non-CJ tx whose output then feeds back into a
later CJ. Mainnet feasibility finds 155 such round-trip edges over
102 distinct hop transactions, with zero same-CJ collisions.

The same-CJ must-not-link constraint from v6 is inherited; a
round-trip whose endpoints are in the same CJ is dropped (it would
be a hard precision violation).
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

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# v7.2 hop input
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class NonCjHop:
    """A non-JoinMarket transaction acting as a round-trip hop.

    Attributes
    ----------
    hop_txid:
        Hex txid of the non-CJ transaction.
    n_outputs:
        Number of outputs of the hop. Used to apply the conservative
        ``<= 2`` filter.
    consumed_maker_outpoints:
        Maker change outputs (``txid:vout``) consumed as inputs by
        this hop. Each one ties the hop to a producer slot.
    output_outpoints:
        Outpoints (``txid:vout``) produced by this hop. The clusterer
        checks each one against the corpus's maker-slot input map to
        identify a consumer slot.
    """

    hop_txid: str
    n_outputs: int
    consumed_maker_outpoints: tuple[str, ...]
    output_outpoints: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class V72Stats:
    """Diagnostics from v7.2 round-trip edge addition."""

    n_candidate_hops: int = 0
    n_qualifying_hops: int = 0
    n_dropped_by_outputs: int = 0
    n_dropped_no_producer: int = 0
    n_dropped_no_consumer: int = 0
    n_candidate_pairs: int = 0
    n_same_cj_pairs_dropped: int = 0
    n_cross_cj_unions: int = 0


@dataclass(slots=True, frozen=True)
class ClusterV72Result:
    labels: dict[int, int]
    attribution: AttributionStats
    v71: V71Stats
    v72: V72Stats


# ---------------------------------------------------------------------------
# v7.2 clusterer
# ---------------------------------------------------------------------------


def cluster_v72(
    slots: Sequence[MakerSlotV7],
    equal_outpoints_by_tx: Mapping[str, Iterable[str]] | None = None,
    non_cj_spenders: Iterable[NonCjSpender] | None = None,
    non_cj_hops: Iterable[NonCjHop] | None = None,
    max_spender_outputs: int = 2,
    max_hop_outputs: int = 2,
) -> ClusterV72Result:
    """Run the v7.2 clusterer.

    Pipeline:

    1. Singletons.
    2. Same-CJ must-not-link.
    3. v6 chain edges: change-output and equal-output reuse.
    4. v7 equal-output fee-fingerprint attribution edges.
    5. v7.1 non-CJ CIOH edges (<= ``max_spender_outputs`` outputs).
    6. v7.2 non-CJ round-trip CIOH edges (<= ``max_hop_outputs`` outputs).
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

    # v7 attribution edges.
    attribution = AttributionStats()
    if equal_outpoints_by_tx:
        edges, attribution = attribute_equal_outputs(slots, equal_outpoints_by_tx)
        for outpoint, producer_slot_id in edges.items():
            for consumer_id in idx.consumers_of_utxo.get(outpoint, ()):
                if consumer_id == producer_slot_id:
                    continue
                uf.union(producer_slot_id, consumer_id)

    # v7.1 non-CJ CIOH co-spend edges.
    change_outpoint_to_slot: dict[str, int] = {}
    for i, s in enumerate(idx.slot_by_id):
        if s.change_output is not None:
            change_outpoint_to_slot[s.change_output] = i

    n_cand = 0
    n_qual = 0
    n_drop_out = 0
    n_drop_size = 0
    n_pairs = 0
    n_same_cj = 0
    n_unions = 0
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

    # v7.2 non-CJ round-trip edges.
    h_cand = 0
    h_qual = 0
    h_drop_out = 0
    h_drop_prod = 0
    h_drop_cons = 0
    h_pairs = 0
    h_same_cj = 0
    h_unions = 0
    if non_cj_hops is not None:
        for hop in non_cj_hops:
            h_cand += 1
            if hop.n_outputs > max_hop_outputs:
                h_drop_out += 1
                continue
            # Producer slots: any maker change outpoint consumed by hop.
            producer_ids: list[int] = []
            for op in hop.consumed_maker_outpoints:
                sid = change_outpoint_to_slot.get(op)
                if sid is not None:
                    producer_ids.append(sid)
            if not producer_ids:
                h_drop_prod += 1
                continue
            # Consumer slots: any hop output consumed as a maker-slot input.
            consumer_ids: list[int] = []
            for op in hop.output_outpoints:
                for cid in idx.consumers_of_utxo.get(op, ()):
                    consumer_ids.append(cid)
            if not consumer_ids:
                h_drop_cons += 1
                continue
            h_qual += 1
            # Union all producer slots with all consumer slots, dropping
            # same-CJ pairs.
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

    comps = uf.components()
    roots = sorted(comps.keys())
    root_to_cid = {r: c for c, r in enumerate(roots)}
    labels = {i: root_to_cid[uf.find(i)] for i in range(len(slots))}

    return ClusterV72Result(labels=labels, attribution=attribution, v71=v71, v72=v72)
