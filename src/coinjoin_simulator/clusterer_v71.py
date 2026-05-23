"""v7.1 maker clusterer: v7 plus non-CJ CIOH edges (<=2-output spenders).

Background
----------
v7 (:mod:`coinjoin_simulator.clusterer_v7`) closes the equal-output
chain by fee-fingerprint attribution but still misses one class of
same-wallet evidence: maker change UTXOs that are later co-spent by a
non-JoinMarket transaction. Under common-input-ownership heuristic
(CIOH), all inputs of a non-CJ spend belong to the same wallet, so if
two such inputs trace back to maker slots in different CJs the two
slots are same-wallet.

CIOH is well known to be unreliable for batched-payment or
exchange-withdrawal transactions (many outputs), so v7.1 restricts to
"simple" spends with ``<= 2`` outputs (one payment + optional change).
That conservative filter keeps the precision contract intact: the
mainnet feasibility study finds zero same-CJ collisions among 56
qualifying consolidations covering 129 candidate cross-CJ edges.

Like v6 and v7, v7.1 inherits the same-CJ must-not-link constraint:
co-spending two maker change UTXOs that originate from the *same* CJ
is treated as evidence that the spender is not in fact a simple
single-wallet spend, and the edge is dropped (it would be a hard
precision violation).
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

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Non-CJ CIOH input
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class NonCjSpender:
    """A non-JoinMarket transaction that co-spends maker change UTXOs.

    Attributes
    ----------
    spender_txid:
        Hex txid of the spending transaction.
    n_outputs:
        Number of outputs of the spending transaction. Used to apply
        the conservative ``<= 2`` filter.
    maker_outpoints:
        Outpoints (``txid:vout``) of the maker change UTXOs that this
        spender consumes. Caller is responsible for ensuring each
        outpoint is in fact a maker change output of some slot in the
        v7.1 corpus.
    """

    spender_txid: str
    n_outputs: int
    maker_outpoints: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class V71Stats:
    """Diagnostics from v7.1 non-CJ CIOH edge addition."""

    n_candidate_spenders: int = 0
    n_qualifying_spenders: int = 0  # <=2 outputs and >=2 maker outpoints
    n_dropped_by_outputs: int = 0
    n_dropped_by_size: int = 0  # <2 maker outpoints
    n_candidate_pairs: int = 0
    n_same_cj_pairs_dropped: int = 0
    n_cross_cj_unions: int = 0


@dataclass(slots=True, frozen=True)
class ClusterV71Result:
    """Return type bundling v7 attribution stats and v7.1 CIOH stats."""

    labels: dict[int, int]
    attribution: AttributionStats
    v71: V71Stats


# ---------------------------------------------------------------------------
# v7.1 clusterer
# ---------------------------------------------------------------------------


def cluster_v71(
    slots: Sequence[MakerSlotV7],
    equal_outpoints_by_tx: Mapping[str, Iterable[str]] | None = None,
    non_cj_spenders: Iterable[NonCjSpender] | None = None,
    max_spender_outputs: int = 2,
) -> ClusterV71Result:
    """Run the v7.1 clusterer.

    Pipeline:

    1. Singletons.
    2. Same-CJ must-not-link.
    3. v6 chain edges: change-output reuse and equal-output reuse
       where ``MakerSlotV7.equal_output`` is set.
    4. v7 equal-output fee-fingerprint attribution edges.
    5. v7.1 non-CJ CIOH edges: for each ``NonCjSpender`` with
       ``n_outputs <= max_spender_outputs`` and at least two maker
       outpoints, union all consumer slots derived from those
       outpoints (skipping pairs from the same CJ, which would
       violate the precision contract).

    Parameters
    ----------
    slots:
        Maker slots (v7-enriched).
    equal_outpoints_by_tx:
        Same as ``cluster_v7``.
    non_cj_spenders:
        Stream of candidate non-CJ spenders. Caller computes this
        from a spend map plus a set of JM-CJ txids (the spender must
        not itself be a JM CJ; that case is already covered by the v6
        change-chain rule).
    max_spender_outputs:
        Conservative output-count cap. Default 2.

    Returns
    -------
    ClusterV71Result with labels, attribution stats and v7.1 stats.
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

    # v6-style chain edges (change + optionally equal when known).
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

    # v7 fee-fingerprint equal-output edges.
    attribution = AttributionStats()
    if equal_outpoints_by_tx:
        edges, attribution = attribute_equal_outputs(slots, equal_outpoints_by_tx)
        for outpoint, producer_slot_id in edges.items():
            for consumer_id in idx.consumers_of_utxo.get(outpoint, ()):
                if consumer_id == producer_slot_id:
                    continue
                uf.union(producer_slot_id, consumer_id)

    # v7.1 non-CJ CIOH edges.
    # Map: maker change_output outpoint -> producer slot id.
    change_outpoint_to_slot: dict[str, int] = {}
    for i, s in enumerate(idx.slot_by_id):
        if s.change_output is not None:
            change_outpoint_to_slot[s.change_output] = i

    n_candidate = 0
    n_qual = 0
    n_drop_out = 0
    n_drop_size = 0
    n_pairs = 0
    n_same_cj = 0
    n_unions = 0
    if non_cj_spenders is not None:
        for sp in non_cj_spenders:
            n_candidate += 1
            if sp.n_outputs > max_spender_outputs:
                n_drop_out += 1
                continue
            # Resolve maker outpoints to producer slots.
            slot_ids: list[int] = []
            for op in sp.maker_outpoints:
                sid = change_outpoint_to_slot.get(op)
                if sid is not None:
                    slot_ids.append(sid)
            if len(slot_ids) < 2:
                n_drop_size += 1
                continue
            n_qual += 1
            # Deduplicate (shouldn't happen since change_output is
            # unique per slot, but guard anyway).
            uniq = list(dict.fromkeys(slot_ids))
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    a, b = uniq[i], uniq[j]
                    n_pairs += 1
                    if idx.slot_by_id[a].txid == idx.slot_by_id[b].txid:
                        # Same-CJ pair -- skip (would be a hard
                        # precision violation by same-CJ forbid).
                        n_same_cj += 1
                        continue
                    if uf.union(a, b):
                        n_unions += 1

    comps = uf.components()
    roots = sorted(comps.keys())
    root_to_cid = {r: c for c, r in enumerate(roots)}
    labels = {i: root_to_cid[uf.find(i)] for i in range(len(slots))}

    v71 = V71Stats(
        n_candidate_spenders=n_candidate,
        n_qualifying_spenders=n_qual,
        n_dropped_by_outputs=n_drop_out,
        n_dropped_by_size=n_drop_size,
        n_candidate_pairs=n_pairs,
        n_same_cj_pairs_dropped=n_same_cj,
        n_cross_cj_unions=n_unions,
    )
    return ClusterV71Result(labels=labels, attribution=attribution, v71=v71)
