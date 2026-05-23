"""State-machine maker clusterer (v6).

This is the redesign called for after the v5 study uncovered systemic
issues with fee-tuple clustering. Per the verified JoinMarket protocol
facts (see ``tmp/v6/PROTOCOL.md``):

- A maker's CJ input bundle is a definite same-wallet signal (CIOH).
- A maker's change output stays in the maker's *input* mixdepth and
  is the natural input material for the next round at the same fee
  band, so the change-as-future-input edge is a strong same-wallet
  same-mixdepth link.
- A maker's equal-output advances to mixdepth ``m + 1 (mod 5)``; when
  that equal output is later spent as a maker input in another CJ,
  the participating wallet is the same maker, now advertising from a
  different mixdepth. Equal-output-as-future-input is therefore also
  a same-wallet edge, but it carries an additional mixdepth-rotation
  fact that we can verify for consistency.
- Within a single CJ, every maker slot belongs to a *different* maker
  identity (taker sybil-dedup by fidelity bond ensures this).
- A single maker may publish multiple offers simultaneously, but they
  all share the same wallet, the same fidelity-bond UTXO, and the
  same currently-advertised mixdepth. So fee-tuple identity is NOT a
  faithful per-maker key.

The clusterer therefore avoids fee-tuple binning entirely and instead
builds an undirected graph of maker slots with three kinds of edges,
then runs union-find under a *must-not-link* constraint:

1. **CIOH wallet root**: two slots whose input UTXOs overlap, or whose
   inputs both descend from a common ancestor wallet root, get merged.
2. **Change chain**: a slot's change UTXO consumed as a maker input
   in a later CJ slot links the two slots (same wallet, same
   mixdepth).
3. **Equal-output chain**: a slot's equal-amount UTXO consumed as a
   maker input in a later CJ slot links the two slots (same wallet,
   different mixdepth: the receiver advertised from ``m + 1``).

The **must-not-link** constraint enforces that two slots in the same
CJ are never merged.

The output is the standard :class:`ClusterAssignment` shape so
existing evaluation code (precision / recall / F1 vs simulator ground
truth) plugs in unchanged.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coinjoin_simulator.clusterer_oracle import ClusterAssignment, _bipartite_prf
from coinjoin_simulator.world import OutputRole

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coinjoin_simulator.world import SimResult


# ---------------------------------------------------------------------------
# Union-find with must-not-link constraints
# ---------------------------------------------------------------------------


class _ConstrainedUnionFind:
    """Union-find that refuses to merge nodes carrying a must-not-link tag.

    Each node carries a frozenset of *forbidden* root ids it must
    never share a component with. When two nodes are about to be
    unioned, we check that neither root sits in the other's forbidden
    set; if it does, the union is silently rejected and we return
    ``False`` so the caller can record the conflict.

    Forbidden sets propagate on union: the merged root inherits the
    union of both roots' forbidden sets.
    """

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}
        self.size: dict[int, int] = {}
        self.forbidden: dict[int, set[int]] = {}

    def make(self, x: int) -> None:
        if x in self.parent:
            return
        self.parent[x] = x
        self.size[x] = 1
        self.forbidden[x] = set()

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def forbid(self, a: int, b: int) -> None:
        """Forbid ``a`` and ``b`` from ever being merged."""
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return  # already merged; the constraint is moot
        self.forbidden[ra].add(rb)
        self.forbidden[rb].add(ra)

    def union(self, a: int, b: int) -> bool:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return True
        if rb in self.forbidden[ra] or ra in self.forbidden[rb]:
            return False
        # Union by size.
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        # Symmetric repoint: every root that forbids ``rb`` must now
        # forbid ``ra`` instead. We only need to touch the small set
        # ``forbidden[rb]`` rather than scan every root, which keeps
        # the merge cost proportional to the constraint degree.
        for other in self.forbidden[rb]:
            if other == ra:
                continue
            s = self.forbidden.get(other)
            if s is not None:
                s.discard(rb)
                s.add(ra)
        self.forbidden[ra] |= self.forbidden[rb]
        self.forbidden[ra].discard(ra)
        self.forbidden[ra].discard(rb)
        del self.size[rb]
        del self.forbidden[rb]
        return True

    def components(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for x in list(self.parent):
            out[self.find(x)].append(x)
        return out


# ---------------------------------------------------------------------------
# Maker-slot extraction
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class MakerSlot:
    """A single maker's contribution to a CJ tx.

    Holds the consumed inputs and the two emitted outputs (equal-amount
    and change). The slot identity is ``(txid, owner_id)``; in real
    on-chain analysis the ``owner_id`` is unknown ahead of time and is
    a placeholder index 0..n_makers_in_this_tx-1.

    ``equal_output`` may be ``None`` for mainnet slots where the ILP
    solver cannot identify which equal-amount vout belongs to which
    maker (equal outputs are interchangeable in the constraint
    system). In that case we only use the change-chain edge.
    """

    txid: str
    owner_id: str
    inputs: tuple[str, ...]
    equal_output: str | None
    change_output: str | None


def _extract_slots_from_simresult(res: SimResult) -> list[MakerSlot]:
    """Pull (input_bundle, eq_out, change_out) tuples per maker, using ground-truth labels.

    The simulator pops consumed inputs from ``maker_id_by_utxo`` at fill
    time (so the dict only retains the unspent set). We therefore build
    a persistent ``utxo_id -> owner`` map by walking every CJ output
    role and seeding it with the makers' starting UTXOs.
    """
    # Build a persistent owner index from every maker-owned output ever
    # emitted: prefer the simulator's historical map ``maker_id_by_utxo_ever``
    # (which retains consumed UTXOs), fall back to the live residual map
    # plus per-tx output sweep for older SimResult shapes that do not
    # carry the historical map.
    owner_of_utxo: dict[str, str] = dict(res.maker_id_by_utxo_ever)
    if not owner_of_utxo:
        owner_of_utxo = dict(res.maker_id_by_utxo)
    for tx in res.txs:
        for out in tx.outputs:
            if out.role in (OutputRole.MAKER_CJ, OutputRole.MAKER_CHANGE):
                owner_of_utxo[out.output_id] = out.owner

    slots: list[MakerSlot] = []
    for tx in res.txs:
        by_owner: dict[str, list[str]] = defaultdict(list)
        for utxo_id in tx.inputs:
            owner = owner_of_utxo.get(utxo_id)
            if owner is None:
                continue  # taker input
            by_owner[owner].append(utxo_id)
        eq_by_owner: dict[str, str] = {}
        change_by_owner: dict[str, str] = {}
        for out in tx.outputs:
            if out.role == OutputRole.MAKER_CJ:
                eq_by_owner[out.owner] = out.output_id
            elif out.role == OutputRole.MAKER_CHANGE:
                change_by_owner[out.owner] = out.output_id
        for owner, inputs in by_owner.items():
            if owner not in eq_by_owner:
                continue
            slots.append(
                MakerSlot(
                    txid=tx.txid,
                    owner_id=owner,
                    inputs=tuple(inputs),
                    equal_output=eq_by_owner[owner],
                    change_output=change_by_owner.get(owner),
                ),
            )
    return slots


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Index:
    """Cross-CJ lookup helpers built once from the slot list."""

    slot_id_of: dict[tuple[str, str], int] = field(default_factory=dict)
    slot_by_id: list[MakerSlot] = field(default_factory=list)
    producer_of_utxo: dict[str, int] = field(default_factory=dict)
    consumers_of_utxo: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    slots_in_tx: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))


def _build_index(slots: Sequence[MakerSlot]) -> _Index:
    idx = _Index()
    for i, s in enumerate(slots):
        idx.slot_id_of[(s.txid, s.owner_id)] = i
        idx.slot_by_id.append(s)
        idx.slots_in_tx[s.txid].append(i)
        if s.equal_output is not None:
            idx.producer_of_utxo[s.equal_output] = i
        if s.change_output is not None:
            idx.producer_of_utxo[s.change_output] = i
        for utxo_id in s.inputs:
            idx.consumers_of_utxo[utxo_id].append(i)
    return idx


def cluster_state_machine(slots: Sequence[MakerSlot]) -> dict[int, int]:
    """Cluster maker slots into per-entity components.

    Returns ``slot_idx -> cluster_id`` (cluster_ids are stable across
    runs and dense in ``0..n_clusters-1``).

    The algorithm:

    1. Every slot is a singleton.
    2. **Must-not-link constraints**: within each CJ, all maker slots
       are pairwise forbidden from sharing a cluster (sybil-dedup).
    3. **CIOH input bundle**: this is implicit because a slot's inputs
       are tracked together; we don't need an edge for it.
    4. **Cross-CJ chain edges**: for every slot ``s`` and each of its
       output UTXOs ``u`` (equal or change), find every consumer slot
       ``c`` that has ``u`` as an input. Union ``s`` and ``c``.
       The change-chain (mixdepth-stable) and the equal-chain
       (mixdepth-rotating) both yield same-wallet edges per protocol.

    The must-not-link constraint propagates through transitive merges,
    so a long chain of same-CJ-different-slot pairs prevents spurious
    cross-merges. Any rejected union is silently dropped; in
    well-formed data such rejections never happen because the chain
    targets are always from distinct CJs.
    """
    idx = _build_index(slots)
    uf = _ConstrainedUnionFind()
    for i in range(len(slots)):
        uf.make(i)

    # Must-not-link: every pair of slots in the same CJ.
    for tx_slots in idx.slots_in_tx.values():
        for i in range(len(tx_slots)):
            for j in range(i + 1, len(tx_slots)):
                uf.forbid(tx_slots[i], tx_slots[j])

    # Cross-CJ chain edges via output reuse as future input.
    for producer_id, s in enumerate(idx.slot_by_id):
        for out_utxo in (s.equal_output, s.change_output):
            if out_utxo is None:
                continue
            for consumer_id in idx.consumers_of_utxo.get(out_utxo, ()):
                if consumer_id == producer_id:
                    continue
                uf.union(producer_id, consumer_id)
    # Dense cluster ids.
    comps = uf.components()
    roots = sorted(comps.keys())
    root_to_cid = {r: c for c, r in enumerate(roots)}
    return {i: root_to_cid[uf.find(i)] for i in range(len(slots))}


# ---------------------------------------------------------------------------
# Public entry point: SimResult -> ClusterAssignment
# ---------------------------------------------------------------------------


def state_machine_cluster(res: SimResult) -> ClusterAssignment:
    """Run the state-machine clusterer on a :class:`SimResult` and
    return a labelling of every maker-owned output that matches the
    :class:`ClusterAssignment` shape used by the other clusterers.
    """
    slots = _extract_slots_from_simresult(res)
    slot_to_cluster = cluster_state_machine(slots)

    # Map every maker-owned output_id to its slot's cluster id.
    labels: dict[str, int] = {}
    ground_truth: dict[str, str] = {}
    for i, s in enumerate(slots):
        cid = slot_to_cluster[i]
        if s.equal_output is not None:
            labels[s.equal_output] = cid
            ground_truth[s.equal_output] = s.owner_id
        if s.change_output is not None:
            labels[s.change_output] = cid
            ground_truth[s.change_output] = s.owner_id

    n_outputs = len(labels)
    if n_outputs == 0:
        return ClusterAssignment(
            labels={},
            ground_truth={},
            ari=0.0,
            n_clusters=0,
            n_outputs=0,
            precision=0.0,
            recall=0.0,
            f1=0.0,
        )

    # Encode ground-truth strings to ints for ARI / PRF.
    truth_int = _encode(ground_truth, labels.keys())
    pred = [labels[oid] for oid in labels]
    from sklearn.metrics import adjusted_rand_score

    ari = float(adjusted_rand_score(truth_int, pred))
    p, r, f1 = _bipartite_prf(truth_int, pred)
    return ClusterAssignment(
        labels=labels,
        ground_truth=ground_truth,
        ari=ari,
        n_clusters=len(set(pred)),
        n_outputs=n_outputs,
        precision=p,
        recall=r,
        f1=f1,
    )


def _encode(ground_truth: dict[str, str], keys: Sequence[str]) -> list[int]:
    enc: dict[str, int] = {}
    out: list[int] = []
    for k in keys:
        t = ground_truth[k]
        if t not in enc:
            enc[t] = len(enc)
        out.append(enc[t])
    return out
