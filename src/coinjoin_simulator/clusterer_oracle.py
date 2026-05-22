"""Oracle maker clusterer.

The "oracle" attacker has access to the simulator's full ground-truth
offer log: it sees, for every CJ tx, exactly which fee policy each
counterparty announced at fill time. This bounds from above what any
weaker attacker could achieve by fingerprinting fees alone, because no
on-chain or active-probing attacker has more information than the offer
log itself contains.

The module ships two clustering strategies:

- :func:`hash_bucket_cluster` -- map each fingerprint tuple
  ``(ordertype, log10_band(cjfee_r), log10_band(cjfee_a),
  log10_band(minsize), log10_band(fidelity_bond))``
  to a deterministic bucket id. Same fingerprint -> same cluster.
- :func:`dbscan_cluster` -- continuous-feature DBSCAN over
  ``(log10 cjfee_r, log10 cjfee_a, log10 minsize,
  log10(1 + fidelity_bond))``.

Both expose the same return shape: a mapping ``output_id -> cluster_id``
covering every MAKER_CJ + MAKER_CHANGE output emitted by the simulator.
``maxsize`` is intentionally excluded from fingerprints because the
yieldgenerator recomputes it from ``largest_mixdepth().balance`` each
time it announces (agents.py:160-165), so it drifts as the maker fills
joins; using it would destroy clustering recall.

Comparison against ground truth uses scikit-learn's adjusted Rand index
plus per-cluster precision / recall / F1 over the bipartite
maker <-> cluster contingency table.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.metrics import adjusted_rand_score

from coinjoin_simulator.world import OfferLogEntry, OutputRole

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coinjoin_simulator.world import SimResult


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ClusterAssignment:
    """Cluster labels for every maker-owned output, plus metrics."""

    labels: dict[str, int]  # output_id -> cluster id (-1 = noise/unclustered)
    ground_truth: dict[str, str]  # output_id -> true counterparty
    ari: float
    n_clusters: int
    n_outputs: int
    precision: float
    recall: float
    f1: float

    @property
    def n_noise(self) -> int:
        return sum(1 for label in self.labels.values() if label == -1)


@dataclass(slots=True)
class _MakerObservation:
    """Per-fill view the oracle has of one maker's contribution to a CJ tx."""

    output_id: str
    counterparty: str  # ground truth (oracle sees this)
    ordertype: str
    cjfee_r: float
    cjfee_a: float
    minsize: int
    fidelity_bond_value: float


# Default banding strides chosen so the yg-pe ±10% jitter on
# (cjfee_r, cjfee_a, minsize) is well inside one log10 band even at band
# edges. log10(1.1) ≈ 0.0414; a stride of 0.25 gives ~6 jitter widths of
# margin (4 bands per decade), which absorbs the jitter without flipping
# a value across band boundaries while preserving enough resolution to
# separate distinct policy centers separated by ≥ 0.5 dex.
DEFAULT_LOG_STRIDE = 0.25
DEFAULT_BOND_STRIDE = 0.5
# Treat any value <= 1 sat / 1.0 BTC^exp as "zero band"; all such offers
# share a single bucket so bondless free makers cluster together.
ZERO_LOG_BAND = -999


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_band(value: float, stride: float = DEFAULT_LOG_STRIDE) -> int:
    """Bucket a positive scalar by ``floor(log10(value) / stride)``.

    Non-positive / NaN values are mapped to a sentinel so they share
    one bucket regardless of magnitude.
    """
    if not np.isfinite(value) or value <= 0:
        return ZERO_LOG_BAND
    return int(math.floor(math.log10(value) / stride))


def _collect_observations(result: SimResult) -> list[_MakerObservation]:
    """Extract one observation per MAKER_CJ output, joined to its offer."""
    offer_by_key: dict[tuple[str, str], OfferLogEntry] = {
        (e.txid, e.counterparty): e for e in result.offer_log
    }
    obs: list[_MakerObservation] = []
    for tx in result.txs:
        for out in tx.outputs:
            if out.role != OutputRole.MAKER_CJ:
                continue
            entry = offer_by_key.get((tx.txid, out.owner))
            if entry is None:
                continue  # follow-up payout txs have no offer log
            offer = entry.offer
            ordertype = str(offer.get("ordertype", "sw0reloffer"))
            cjfee_raw = offer.get("cjfee", 0.0)
            cjfee_r, cjfee_a = _split_cjfee(ordertype, float(cjfee_raw))
            obs.append(
                _MakerObservation(
                    output_id=out.output_id,
                    counterparty=out.owner,
                    ordertype=ordertype,
                    cjfee_r=cjfee_r,
                    cjfee_a=cjfee_a,
                    minsize=int(offer.get("minsize", 0)),
                    fidelity_bond_value=float(offer.get("fidelity_bond_value", 0.0)),
                ),
            )
    return obs


def _split_cjfee(ordertype: str, cjfee: float) -> tuple[float, float]:
    """Return ``(cjfee_r, cjfee_a)`` according to the offer ordertype.

    For absolute orders the policy is in sats; for relative orders the
    policy is a fraction. The unused dimension is set to 0.
    """
    if ordertype in {"sw0absoffer", "absoffer", "swabsoffer"}:
        return 0.0, cjfee
    return cjfee, 0.0


# ---------------------------------------------------------------------------
# Hash-bucket clusterer
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HashBucketConfig:
    log_stride: float = DEFAULT_LOG_STRIDE
    bond_stride: float = DEFAULT_BOND_STRIDE


def hash_bucket_cluster(
    result: SimResult,
    config: HashBucketConfig | None = None,
) -> ClusterAssignment:
    """Bucket every maker output by a banded fee fingerprint."""
    cfg = config or HashBucketConfig()
    observations = _collect_observations(result)
    if not observations:
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

    bucket_ids: dict[tuple[str, int, int, int, int], int] = {}
    labels: dict[str, int] = {}
    ground_truth: dict[str, str] = {}
    next_id = 0
    for o in observations:
        key = (
            o.ordertype,
            _log_band(o.cjfee_r, cfg.log_stride),
            _log_band(o.cjfee_a, cfg.log_stride),
            _log_band(o.minsize, cfg.log_stride),
            _log_band(o.fidelity_bond_value, cfg.bond_stride),
        )
        if key not in bucket_ids:
            bucket_ids[key] = next_id
            next_id += 1
        labels[o.output_id] = bucket_ids[key]
        ground_truth[o.output_id] = o.counterparty
    return _build_assignment(labels, ground_truth)


# ---------------------------------------------------------------------------
# DBSCAN clusterer
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DbscanConfig:
    eps: float = 0.15
    min_samples: int = 1  # we want every observation labeled
    feature_weights: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 2.0)
    """Weights applied to (log10 cjfee_r, log10 cjfee_a, log10 minsize,
    log10(1+bond)) before the L2 distance is taken. The default boosts
    the bond axis so makers with very different bonds never collide."""


def _features(
    observations: Sequence[_MakerObservation],
    weights: tuple[float, float, float, float],
) -> np.ndarray:
    rows = []
    wr, wa, wm, wb = weights
    for o in observations:
        rows.append(
            [
                wr * (math.log10(o.cjfee_r) if o.cjfee_r > 0 else -10.0),
                wa * (math.log10(o.cjfee_a) if o.cjfee_a > 0 else -10.0),
                wm * (math.log10(o.minsize) if o.minsize > 0 else 0.0),
                wb * math.log10(1.0 + o.fidelity_bond_value),
            ],
        )
    return np.asarray(rows, dtype=float)


def dbscan_cluster(
    result: SimResult,
    config: DbscanConfig | None = None,
) -> ClusterAssignment:
    """Cluster maker outputs in continuous fee-fingerprint space."""
    cfg = config or DbscanConfig()
    observations = _collect_observations(result)
    if not observations:
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
    features = _features(observations, cfg.feature_weights)
    db = DBSCAN(eps=cfg.eps, min_samples=cfg.min_samples).fit(features)
    raw_labels = db.labels_
    labels: dict[str, int] = {o.output_id: int(raw_labels[i]) for i, o in enumerate(observations)}
    ground_truth: dict[str, str] = {o.output_id: o.counterparty for o in observations}
    return _build_assignment(labels, ground_truth)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _build_assignment(
    labels: dict[str, int],
    ground_truth: dict[str, str],
) -> ClusterAssignment:
    if not labels:
        return ClusterAssignment(
            labels=labels,
            ground_truth=ground_truth,
            ari=1.0,
            n_clusters=0,
            n_outputs=0,
            precision=1.0,
            recall=1.0,
            f1=1.0,
        )
    output_ids = list(labels)
    pred = [labels[o] for o in output_ids]
    truth_strs = [ground_truth[o] for o in output_ids]
    cp_to_int: dict[str, int] = {}
    truth_int: list[int] = []
    for cp in truth_strs:
        if cp not in cp_to_int:
            cp_to_int[cp] = len(cp_to_int)
        truth_int.append(cp_to_int[cp])
    ari = float(adjusted_rand_score(truth_int, pred))
    p, r, f1 = _bipartite_prf(truth_int, pred)
    return ClusterAssignment(
        labels=labels,
        ground_truth=ground_truth,
        ari=ari,
        n_clusters=len({c for c in pred if c != -1}),
        n_outputs=len(output_ids),
        precision=p,
        recall=r,
        f1=f1,
    )


def _bipartite_prf(truth: Sequence[int], pred: Sequence[int]) -> tuple[float, float, float]:
    """Pair-counting precision / recall / F1.

    A pair (i, j) of outputs is a "positive" if both clusterings agree
    they belong together. Standard pair-counting external clustering
    metric (Manning, IR, sec 16.3); robust to label permutation.

    Computed in O(N + K) via a contingency table instead of the naive
    O(N^2) double loop. The pair-counting identities are:

        TP                   = sum_{i,j} C(n_{ij}, 2)
        TP + FP              = sum_j     C(a_j,   2)   (pairs same in pred)
        TP + FN              = sum_i     C(b_i,   2)   (pairs same in truth)

    where ``n_{ij}`` is the contingency cell count, ``a_j`` the column
    sum (pred cluster sizes) and ``b_i`` the row sum (truth cluster
    sizes). A predicted label of ``-1`` denotes "noise" and never
    contributes to a positive pair (mirrors the previous semantics).
    """
    n = len(truth)
    if n < 2:
        return 1.0, 1.0, 1.0

    # Contingency cells (truth, pred), excluding noise (-1) on the pred side.
    cell_counts: dict[tuple[int, int], int] = defaultdict(int)
    pred_counts: dict[int, int] = defaultdict(int)
    for t, p in zip(truth, pred, strict=False):
        if p == -1:
            continue
        cell_counts[(t, p)] += 1
        pred_counts[p] += 1

    # Truth-row sums are over *all* points (noise included on the truth side):
    # FN counts pairs that are same in truth but split or noised in pred.
    truth_counts: dict[int, int] = defaultdict(int)
    for t in truth:
        truth_counts[t] += 1

    def _c2(x: int) -> int:
        return x * (x - 1) // 2 if x >= 2 else 0

    tp = sum(_c2(c) for c in cell_counts.values())
    tp_plus_fp = sum(_c2(c) for c in pred_counts.values())
    tp_plus_fn = sum(_c2(c) for c in truth_counts.values())
    fp = tp_plus_fp - tp
    fn = tp_plus_fn - tp

    if tp == 0 and fp == 0 and fn == 0:
        # No positive pairs in either partition (e.g. every output is
        # singleton in both truth and prediction); pair-counting is
        # degenerate -- the partitions trivially agree.
        return 1.0, 1.0, 1.0
    if tp == 0:
        return 0.0, 0.0, 0.0
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OracleClustererReport:
    """Side-by-side report comparing both oracle clustering strategies."""

    hash_bucket: ClusterAssignment
    dbscan: ClusterAssignment
    n_true_makers: int = field(default=0)


def run_oracle_clusterers(
    result: SimResult,
    *,
    hash_bucket: HashBucketConfig | None = None,
    dbscan: DbscanConfig | None = None,
) -> OracleClustererReport:
    """Run both oracle clusterers and bundle their assignments side by side."""
    hb = hash_bucket_cluster(result, hash_bucket)
    db = dbscan_cluster(result, dbscan)
    n_true = len({cp for cp in hb.ground_truth.values()})
    return OracleClustererReport(hash_bucket=hb, dbscan=db, n_true_makers=n_true)


# Re-exported so callers don't need to dig into world.py for the input types.
__all__ = [
    "ClusterAssignment",
    "DbscanConfig",
    "HashBucketConfig",
    "OracleClustererReport",
    "dbscan_cluster",
    "hash_bucket_cluster",
    "run_oracle_clusterers",
]
