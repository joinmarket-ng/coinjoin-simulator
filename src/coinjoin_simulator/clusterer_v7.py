"""v7 maker clusterer: v6 plus fee-fingerprint equal-output attribution.

Background
----------
v6 (:mod:`coinjoin_simulator.clusterer_state_machine`) is a state
machine over maker slots that unions slots across CJs via two
falsifiable, protocol-grounded edges:

* **Change chain**: a slot's change output consumed as a maker input
  in a later CJ.
* **Equal-output chain**: a slot's equal-amount output consumed as a
  maker input in a later CJ.

On mainnet the ILP solver cannot identify which specific equal-amount
vout belongs to which maker (equal outputs are interchangeable in the
constraint system), so v6 stores ``equal_output=None`` per slot and the
equal-chain edge never fires. That leaves the recall gap visible in
the probe: 15 of 16 matched JoinMarket nicks are split across multiple
v6 clusters.

v7 closes part of that gap by exploiting the JoinMarket order-book
contract: a maker advertises a single ``cjfee`` (relative *or*
absolute), which is observable on-chain as the realised maker fee per
slot. When the equal output of producer CJ ``T`` is later consumed as
an input to a slot ``S'`` in CJ ``T'``, the producing slot ``S_i`` in
``T`` whose realised fee fingerprint matches ``S'`` is, with very
high probability, the same wallet (same maker, just advertising from
a different mixdepth in ``T'``). The v7 clusterer adds an
equal-output edge only when this fingerprint match is **univocal**:
exactly one ``S_i`` in ``T`` matches under either the absolute or the
relative interpretation, and the two interpretations do not point at
different slots.

The v7 clusterer is precision-preserving by construction: it inherits
v6's same-CJ must-not-link forbidance, and a fee-attribution edge is
only added when the match is unique under the fingerprint hypothesis.
Ambiguous matches and no-match cases produce no edge.

This module is independent of v6's source (v6 is frozen). It
reimplements the same union-find pipeline plus the v7 attribution
step.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coinjoin_simulator.clusterer_state_machine import (
    MakerSlot,
    _ConstrainedUnionFind,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Slot extensions for v7
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class MakerSlotV7:
    """A maker slot enriched with per-slot economic information.

    Compared to v6's :class:`MakerSlot`, v7 carries the realised fee
    in satoshis and the CJ's equal-amount value, which together give
    the maker's fingerprint::

        abs_fp = fee_sats
        rel_fp = round(fee_sats / equal_amt * 1_000_000)  # ppm

    A maker advertises a single ``cjfee`` (relative or absolute), so
    one of these two fingerprints is stable across that maker's
    slots in different CJs until the maker reconfigures.
    """

    txid: str
    owner_id: str
    inputs: tuple[str, ...]
    equal_output: str | None
    change_output: str | None
    equal_amt_sats: int
    fee_sats: int

    def abs_fp(self) -> int:
        return self.fee_sats

    def rel_fp_ppm(self) -> int | None:
        if self.equal_amt_sats <= 0:
            return None
        return round(self.fee_sats / self.equal_amt_sats * 1_000_000)

    def to_v6(self) -> MakerSlot:
        return MakerSlot(
            txid=self.txid,
            owner_id=self.owner_id,
            inputs=self.inputs,
            equal_output=self.equal_output,
            change_output=self.change_output,
        )


# ---------------------------------------------------------------------------
# Fee-fingerprint equal-output attribution
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class AttributionStats:
    """Diagnostic counters from :func:`attribute_equal_outputs`."""

    cross_cj_reuses: int = 0
    unique_either: int = 0
    unique_abs_only: int = 0
    unique_rel_only: int = 0
    unique_both_same_slot: int = 0
    unique_both_different_slot: int = 0
    ambiguous: int = 0
    no_match: int = 0


def attribute_equal_outputs(
    slots: Sequence[MakerSlotV7],
    equal_outpoints_by_tx: Mapping[str, Iterable[str]],
    *,
    strict: bool = False,
) -> tuple[dict[str, int], AttributionStats]:
    """Attribute reused equal outputs to specific producer slots by fee match.

    Parameters
    ----------
    slots:
        All maker slots (v7-enriched).
    equal_outpoints_by_tx:
        For each producing CJ ``T``, an iterable of canonical outpoint
        strings (``txid:vout``) that carry T's equal-amount value.
        Caller obtains these from the CJ's vout list.
    strict:
        If True, only emit an attribution edge when *both* the absolute
        and the relative fee fingerprint independently identify the
        same unique producer slot (corresponding to the
        ``unique_both_same_slot`` bucket). This rejects single-criterion
        matches whose target slot may have coincidentally collided with
        the consumer fingerprint under per-announcement jitter (see
        \u00a76.1 discussion of the scaled simulator). The default
        ``False`` reproduces the original v7 behaviour
        (``unique_either``).

    Returns
    -------
    edges:
        ``outpoint -> producer_slot_index`` for each outpoint that the
        attribution layer assigns univocally to one producer slot. The
        consumer side is found by the standard ``inputs`` index.
    stats:
        Counters useful for reporting/figures.
    """
    # Index slots by tx and by global index.
    slots_in_tx: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(slots):
        slots_in_tx[s.txid].append(i)
    # Build consumer lookup: for each input outpoint, the slot indices
    # consuming it. A non-CJ tx may also consume the outpoint; those
    # don't show up here.
    consumers_of: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(slots):
        for u in s.inputs:
            consumers_of[u].append(i)

    edges: dict[str, int] = {}
    cross_cj = 0
    n_abs = 0
    n_rel = 0
    n_both_same = 0
    n_both_diff = 0
    n_ambig = 0
    n_no_match = 0
    n_uniq_either = 0

    for ptxid, outpoints in equal_outpoints_by_tx.items():
        producer_slot_ids = slots_in_tx.get(ptxid, [])
        if not producer_slot_ids:
            continue
        # Precompute fingerprints for producer slots.
        prod_fps = []
        for sid in producer_slot_ids:
            s = slots[sid]
            prod_fps.append(
                (sid, s.abs_fp(), s.rel_fp_ppm()),
            )
        for outpoint in outpoints:
            consumer_ids = consumers_of.get(outpoint, [])
            if not consumer_ids:
                continue
            # Pick the consumer slot (could be more than one if the
            # outpoint appears in several CJs as input; in practice
            # only one consumes it). Use the first; the others are
            # follow-on slots of the same wallet.
            # Match each consumer separately.
            assigned = False
            for cid in consumer_ids:
                if slots[cid].txid == ptxid:
                    # Self-reuse, skip.
                    continue
                cross_cj += 1
                c = slots[cid]
                c_abs = c.abs_fp()
                c_rel = c.rel_fp_ppm()
                abs_hits = [sid for (sid, sa, _sr) in prod_fps if sa == c_abs]
                if c_rel is None:
                    rel_hits = []
                else:
                    rel_hits = [sid for (sid, _sa, sr) in prod_fps if sr == c_rel]
                abs_unique = len(abs_hits) == 1
                rel_unique = len(rel_hits) == 1
                chosen: int | None = None
                if abs_unique and rel_unique:
                    if abs_hits[0] == rel_hits[0]:
                        chosen = abs_hits[0]
                        n_both_same += 1
                        n_uniq_either += 1
                    else:
                        n_both_diff += 1
                elif abs_unique:
                    if not strict:
                        chosen = abs_hits[0]
                    n_abs += 1
                    n_uniq_either += 1
                elif rel_unique:
                    if not strict:
                        chosen = rel_hits[0]
                    n_rel += 1
                    n_uniq_either += 1
                elif not abs_hits and not rel_hits:
                    n_no_match += 1
                else:
                    n_ambig += 1
                if chosen is not None and not assigned:
                    edges[outpoint] = chosen
                    assigned = True

    return edges, AttributionStats(
        cross_cj_reuses=cross_cj,
        unique_either=n_uniq_either,
        unique_abs_only=n_abs,
        unique_rel_only=n_rel,
        unique_both_same_slot=n_both_same,
        unique_both_different_slot=n_both_diff,
        ambiguous=n_ambig,
        no_match=n_no_match,
    )


# ---------------------------------------------------------------------------
# v7 clusterer
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _IndexV7:
    slot_id_of: dict[tuple[str, str], int] = field(default_factory=dict)
    slot_by_id: list[MakerSlotV7] = field(default_factory=list)
    consumers_of_utxo: dict[str, list[int]] = field(
        default_factory=lambda: defaultdict(list),
    )
    slots_in_tx: dict[str, list[int]] = field(
        default_factory=lambda: defaultdict(list),
    )


def _build_index_v7(slots: Sequence[MakerSlotV7]) -> _IndexV7:
    idx = _IndexV7()
    for i, s in enumerate(slots):
        idx.slot_id_of[(s.txid, s.owner_id)] = i
        idx.slot_by_id.append(s)
        idx.slots_in_tx[s.txid].append(i)
        for u in s.inputs:
            idx.consumers_of_utxo[u].append(i)
    return idx


def cluster_v7(
    slots: Sequence[MakerSlotV7],
    equal_outpoints_by_tx: Mapping[str, Iterable[str]] | None = None,
    *,
    strict: bool = False,
) -> tuple[dict[int, int], AttributionStats]:
    """Run the v7 clusterer.

    The pipeline:

    1. Singletons.
    2. Same-CJ must-not-link.
    3. v6 chain edges: change-output reuse, equal-output reuse where
       ``MakerSlotV7.equal_output`` is set (i.e. simulator data).
    4. v7 equal-output attribution: for every reused equal-output
       outpoint provided via ``equal_outpoints_by_tx``, if a univocal
       producer slot can be identified by fee fingerprint, union it
       with the consumer slot(s).

    ``strict=True`` tightens the v7 attribution gate so that an edge is
    only added when both the absolute and the relative fingerprint
    point at the same unique producer slot. This blocks the
    single-criterion false unions that the scaled-simulator experiment
    (\u00a76.1) demonstrated under per-announcement fee jitter, at the
    cost of recall (the recovered chain set shrinks roughly to the
    ``unique_both_same_slot`` bucket).
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
    stats = AttributionStats()
    if equal_outpoints_by_tx:
        edges, stats = attribute_equal_outputs(
            slots, equal_outpoints_by_tx, strict=strict,
        )
        for outpoint, producer_slot_id in edges.items():
            for consumer_id in idx.consumers_of_utxo.get(outpoint, ()):
                if consumer_id == producer_slot_id:
                    continue
                uf.union(producer_slot_id, consumer_id)

    comps = uf.components()
    roots = sorted(comps.keys())
    root_to_cid = {r: c for c, r in enumerate(roots)}
    return {i: root_to_cid[uf.find(i)] for i in range(len(slots))}, stats
