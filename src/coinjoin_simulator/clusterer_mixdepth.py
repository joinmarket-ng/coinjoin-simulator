"""Per-(maker, mixdepth) Bayesian clusterer.

The :mod:`clusterer_onchain` clusterer groups maker change outputs by
a single fee-band on the full corpus. That collapses everything an
operator does into one bucket and loses two structural signals
JoinMarket gives us for free:

1. **The mixdepth cycle.** A maker funds a CoinJoin slot from
   mixdepth ``m``, emits an equal-output to mixdepth ``m + 1 (mod 5)``
   and a change output back to mixdepth ``m``. Across rounds an
   operator therefore appears in five interleaved "books" (one per
   mixdepth), each with its own change/equal-output cadence.
2. **The orderbook fidelity-bond prior.** Each public offer carries a
   ``fidelity_bond_value``: a soft per-identity prior because bonds
   are expensive to forge and tend to be reused across rounds at a
   stable fee band.

This module builds (a) per-``(fee_band, mixdepth)`` sub-clusters of
maker change outputs, (b) merges them within a mixdepth via Bayesian
agglomerative scoring that combines a Gaussian log-fee likelihood
with a fidelity-bond multiplicative prior, and (c) stitches the five
per-mixdepth books into per-maker identities via the cycle
``m -> m+1 -> m+2 -> m+3 -> m+4 -> m``.

The interface mirrors :class:`coinjoin_simulator.clusterer_oracle.ClusterAssignment`
so calibration code can swap the new clusterer in transparently.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coinjoin_simulator.agents import DEFAULT_MAX_MIXDEPTH
from coinjoin_simulator.clusterer_oracle import (
    DEFAULT_LOG_STRIDE,
    ClusterAssignment,
    _bipartite_prf,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


# Number of mixdepths (5 by default: 0..4 with cyclic spend).
N_MIXDEPTHS = DEFAULT_MAX_MIXDEPTH + 1


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MakerChangeObservation:
    """Single maker-change observation, attributed by an ILP solver.

    Mirrors the ``RecoveredMakerOutput`` from :mod:`clusterer_onchain`
    but adds the *mixdepth* signal. ``mixdepth`` is the mixdepth that
    funded the maker's CJ slot (= the mixdepth the change lands in,
    by JM convention).

    ``equal_output_id`` and ``equal_output_value`` carry the matching
    equal-output that the same participant emitted at mixdepth
    ``(mixdepth + 1) % N_MIXDEPTHS``: this is the bridge used to wire
    cross-CJ cycles. They are optional because for txs where the
    solver does not return a participant equal-output we still want
    the change-only signal.
    """

    output_id: str
    txid: str
    mixdepth: int
    cjfee_r: float
    fidelity_bond_value: float = 0.0
    # Ground-truth label (only used to score in tests / synthetic
    # runs; never read by the clustering math itself).
    maker_id_truth: str = ""
    # The matching equal-output of the same participant slot.
    equal_output_id: str | None = None
    equal_output_value: int = 0


@dataclass(slots=True)
class CrossCjLink:
    """Hard cross-CJ link recovered from the corpus spend map.

    An equal output ``E`` at mixdepth ``m+1`` produced by tx ``T`` was
    later spent as a participant input in tx ``S``, and ``S``'s
    decomposition attributed that input to a maker slot whose change
    lands at mixdepth ``m+1``. The change observation of ``S`` is
    therefore from the same identity as some change observation of
    ``T`` (at mixdepth ``m``). This is a *strong* link: we encode it
    as a must-link constraint between the two ``MakerChangeObservation``
    output ids.
    """

    src_output_id: str  # change observation in T (mixdepth m)
    dst_output_id: str  # change observation in S (mixdepth m+1)


# ---------------------------------------------------------------------------
# Clusterer config and result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MixdepthClustererConfig:
    """Hyperparameters for the Bayesian per-mixdepth clusterer.

    ``log_stride`` matches the hash-bucket clusterer; it sets the
    initial banding width *and* the standard deviation of the
    Gaussian log-fee likelihood.

    ``bond_log_prior_weight`` controls how strongly the fidelity-bond
    similarity pulls clusters together (0 disables the prior).

    ``min_log_likelihood_delta`` is the agglomerative threshold: a
    merge is accepted iff its log-posterior gain exceeds this value.
    """

    log_stride: float = DEFAULT_LOG_STRIDE
    bond_log_prior_weight: float = 1.0
    # Negative threshold: accept merges whose squared-distance penalty
    # stays within ~1.5 standard deviations. At ``log_stride = 0.25``
    # and zero bond term, the boundary is roughly ``|d| ~= sqrt(-2 *
    # sigma^2 * threshold) = sqrt(2 * 0.0625 * 0.5) = 0.25 dex``.
    min_log_likelihood_delta: float = -0.5


@dataclass(slots=True)
class _SubCluster:
    """Per-mixdepth working state for agglomerative merging."""

    cluster_id: int
    mixdepth: int
    members: list[str] = field(default_factory=list)
    log_fee_sum: float = 0.0
    log_fee_sq_sum: float = 0.0
    bond_sum: float = 0.0
    bond_count: int = 0

    @property
    def n(self) -> int:
        return len(self.members)

    @property
    def log_fee_mean(self) -> float:
        return self.log_fee_sum / self.n if self.n else 0.0

    @property
    def log_fee_var(self) -> float:
        if self.n < 2:
            return 0.0
        m = self.log_fee_mean
        return max(0.0, self.log_fee_sq_sum / self.n - m * m)

    @property
    def bond_mean(self) -> float:
        return self.bond_sum / self.bond_count if self.bond_count else 0.0


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------


def _log10_safe(x: float) -> float:
    return math.log10(x) if x > 0 else -20.0


def _initial_band_clusters(
    obs: list[MakerChangeObservation],
    stride: float,
) -> dict[tuple[int, int], _SubCluster]:
    """Seed one ``_SubCluster`` per ``(mixdepth, log-fee band)`` pair.

    Equivalent to the hash-bucket clusterer of :mod:`clusterer_onchain`
    *within* each mixdepth. This is the starting partition the
    agglomerative pass refines.
    """
    by_key: dict[tuple[int, int], _SubCluster] = {}
    next_id = 0
    for o in obs:
        lf = _log10_safe(o.cjfee_r)
        band = math.floor(lf / stride) if o.cjfee_r > 0 else -(10**9)
        key = (o.mixdepth, band)
        sc = by_key.get(key)
        if sc is None:
            sc = _SubCluster(cluster_id=next_id, mixdepth=o.mixdepth)
            by_key[key] = sc
            next_id += 1
        sc.members.append(o.output_id)
        sc.log_fee_sum += lf
        sc.log_fee_sq_sum += lf * lf
        if o.fidelity_bond_value > 0:
            sc.bond_sum += o.fidelity_bond_value
            sc.bond_count += 1
    return by_key


def _log_merge_gain(
    a: _SubCluster,
    b: _SubCluster,
    cfg: MixdepthClustererConfig,
) -> float:
    """Bayesian log-posterior gain of merging two sub-clusters.

    Combines:

    * a Gaussian log-likelihood on ``log10(cjfee_r)`` with shared
      std-dev ``log_stride``, comparing the mean-of-merged vs the
      pair of separate means;
    * a fidelity-bond similarity prior: bonds within ~one order of
      magnitude on log10 favour merging.

    The merge is accepted upstream when this gain exceeds
    ``min_log_likelihood_delta``. Returning ``-inf`` vetoes the merge
    irrevocably (used for incompatible mixdepth pairs).
    """
    if a.mixdepth != b.mixdepth:
        return -math.inf
    sigma = max(cfg.log_stride, 0.05)
    # Squared distance between cluster means (penalises far-apart bands).
    dm = a.log_fee_mean - b.log_fee_mean
    gauss_pen = -(dm * dm) / (2 * sigma * sigma)
    # Bond prior: clusters with similar bond values are more likely
    # to come from the same identity. When either side has no bonded
    # observations we fall back to 0 (neutral).
    if a.bond_count and b.bond_count and cfg.bond_log_prior_weight > 0:
        bd = _log10_safe(a.bond_mean) - _log10_safe(b.bond_mean)
        bond_pen = -cfg.bond_log_prior_weight * (bd * bd) / 2.0
    else:
        bond_pen = 0.0
    return gauss_pen + bond_pen


def _apply_links(
    subclusters: dict[tuple[int, int], _SubCluster],
    obs_by_id: Mapping[str, MakerChangeObservation],
    links: Iterable[CrossCjLink],
) -> tuple[dict[int, set[str]], dict[str, int]]:
    """Fold hard cross-CJ links into per-output union-find groups.

    Returns ``(groups, parent)`` where ``parent[output_id]`` is the
    output_id's group representative and ``groups[rep] = {members}``.
    A group spans multiple mixdepths because cross-CJ links connect
    mixdepth ``m`` with mixdepth ``m+1``. Within a mixdepth, the
    grouping is collapsed against the sub-cluster ids later.
    """
    # Initialise: every observation is its own group.
    parent: dict[str, str] = {}
    for sc in subclusters.values():
        for oid in sc.members:
            parent[oid] = oid

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for link in links:
        if link.src_output_id in parent and link.dst_output_id in parent:
            # Sanity: only union if mixdepths are consecutive.
            src_md = obs_by_id[link.src_output_id].mixdepth
            dst_md = obs_by_id[link.dst_output_id].mixdepth
            if (src_md + 1) % N_MIXDEPTHS == dst_md:
                union(link.src_output_id, link.dst_output_id)

    groups: dict[str, set[str]] = defaultdict(set)
    for oid in parent:
        groups[find(oid)].add(oid)
    # Re-key by an integer id for stable downstream handling.
    int_groups = {i: members for i, members in enumerate(groups.values())}
    rep_by_id: dict[str, int] = {}
    for gid, members in int_groups.items():
        for oid in members:
            rep_by_id[oid] = gid
    return int_groups, rep_by_id


def _agglomerate_within_mixdepth(
    subclusters: dict[tuple[int, int], _SubCluster],
    cfg: MixdepthClustererConfig,
) -> dict[int, _SubCluster]:
    """Greedy agglomerative merging within each mixdepth.

    Returns the surviving sub-clusters indexed by their (stable)
    ``cluster_id``.
    """
    by_md: dict[int, list[_SubCluster]] = defaultdict(list)
    for sc in subclusters.values():
        by_md[sc.mixdepth].append(sc)

    surviving: dict[int, _SubCluster] = {}
    for md, scs in by_md.items():
        active = list(scs)
        # Greedy nearest-neighbour merging by descending log-gain.
        while True:
            best = None
            best_gain = cfg.min_log_likelihood_delta
            for i in range(len(active)):
                for j in range(i + 1, len(active)):
                    g = _log_merge_gain(active[i], active[j], cfg)
                    if g > best_gain:
                        best_gain = g
                        best = (i, j)
            if best is None:
                break
            i, j = best
            a, b = active[i], active[j]
            merged = _SubCluster(
                cluster_id=min(a.cluster_id, b.cluster_id),
                mixdepth=md,
                members=a.members + b.members,
                log_fee_sum=a.log_fee_sum + b.log_fee_sum,
                log_fee_sq_sum=a.log_fee_sq_sum + b.log_fee_sq_sum,
                bond_sum=a.bond_sum + b.bond_sum,
                bond_count=a.bond_count + b.bond_count,
            )
            active = [c for k, c in enumerate(active) if k not in {i, j}]
            active.append(merged)
        for sc in active:
            surviving[sc.cluster_id] = sc
    return surviving


def _stitch_cycle(
    surviving: dict[int, _SubCluster],
    obs_by_id: Mapping[str, MakerChangeObservation],
    rep_by_id: Mapping[str, int],
) -> dict[int, int]:
    """Stitch per-mixdepth sub-clusters into per-maker identities.

    Two sub-clusters at consecutive mixdepths are stitched (and
    become part of the same identity cluster) when a cross-CJ link
    group bridges them, i.e. there is a ``rep_by_id`` group that
    contains at least one member of each.

    Returns ``cluster_id -> identity_id`` (small dense ints).
    """
    # Build: link-group -> set of per-mixdepth cluster ids it touches.
    group_to_clusters: dict[int, set[int]] = defaultdict(set)
    cluster_of_output: dict[str, int] = {}
    for cid, sc in surviving.items():
        for oid in sc.members:
            cluster_of_output[oid] = cid
    for oid, gid in rep_by_id.items():
        maybe_cid = cluster_of_output.get(oid)
        if maybe_cid is not None:
            group_to_clusters[gid].add(maybe_cid)
    # Union sub-clusters that share a link group.
    parent: dict[int, int] = {cid: cid for cid in surviving}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for cluster_set in group_to_clusters.values():
        if len(cluster_set) < 2:
            continue
        it = iter(cluster_set)
        base = next(it)
        for other in it:
            union(base, other)

    # Compact roots into dense identity ids.
    root_to_id: dict[int, int] = {}
    cluster_to_identity: dict[int, int] = {}
    for cid in surviving:
        r = find(cid)
        if r not in root_to_id:
            root_to_id[r] = len(root_to_id)
        cluster_to_identity[cid] = root_to_id[r]
    return cluster_to_identity


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def cluster_maker_changes_by_mixdepth(
    observations: Iterable[MakerChangeObservation],
    *,
    links: Iterable[CrossCjLink] = (),
    config: MixdepthClustererConfig | None = None,
) -> ClusterAssignment:
    """Cluster maker change outputs by ``(maker_identity)``.

    Steps:

    1. Seed sub-clusters by ``(mixdepth, log10(cjfee_r) band)``.
    2. Agglomeratively merge sub-clusters *within* a mixdepth using a
       Gaussian log-fee likelihood and a fidelity-bond prior.
    3. Apply cross-CJ hard links via union-find to wire mixdepth
       ``m`` to mixdepth ``m+1``.
    4. Stitch per-mixdepth sub-clusters into per-maker identities
       via the cycle traversed by the links.

    The output labels are the identity ids; the ground-truth column
    is populated from ``MakerChangeObservation.maker_id_truth`` so
    standard ARI / bipartite PRF metrics can be computed.
    """
    cfg = config or MixdepthClustererConfig()
    obs = list(observations)
    if not obs:
        return ClusterAssignment(
            labels={},
            ground_truth={},
            ari=1.0,
            n_clusters=0,
            n_outputs=0,
            precision=1.0,
            recall=1.0,
            f1=1.0,
        )
    obs_by_id = {o.output_id: o for o in obs}

    subclusters = _initial_band_clusters(obs, cfg.log_stride)
    surviving = _agglomerate_within_mixdepth(subclusters, cfg)

    # Apply hard cross-CJ links by re-projecting them through the
    # post-agglomeration cluster membership.
    _, rep_by_id = _apply_links(subclusters, obs_by_id, links)
    cluster_to_identity = _stitch_cycle(surviving, obs_by_id, rep_by_id)

    labels: dict[str, int] = {}
    truth: dict[str, str] = {}
    for cid, sc in surviving.items():
        ident = cluster_to_identity[cid]
        for oid in sc.members:
            labels[oid] = ident
            truth[oid] = obs_by_id[oid].maker_id_truth

    output_ids = sorted(labels.keys())
    pred = [labels[o] for o in output_ids]
    truth_list = [truth[o] for o in output_ids]
    truth_to_int = {tid: i for i, tid in enumerate({t for t in truth_list})}
    truth_int = [truth_to_int[t] for t in truth_list]
    from sklearn.metrics import adjusted_rand_score

    ari = float(adjusted_rand_score(truth_int, pred))
    precision, recall, f1 = _bipartite_prf(truth_int, pred)
    return ClusterAssignment(
        labels=labels,
        ground_truth=truth,
        ari=ari,
        n_clusters=len(set(pred)),
        n_outputs=len(output_ids),
        precision=precision,
        recall=recall,
        f1=f1,
    )
