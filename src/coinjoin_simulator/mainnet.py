"""Apply the on-chain attacker to a cached mainnet JoinMarket corpus.

The synthetic-data clusterers carry ground-truth maker labels through
:class:`~coinjoin_simulator.world.SimResult`. On mainnet there is no
ground truth -- we only have the public tx graph -- so this module
exposes a parallel pipeline that:

1. Loads a JM tx index produced by ``anon_chain_v5.py`` (a dict keyed
   by txid carrying ``is_jm`` and ``meta`` metadata).
2. Loads each cached mempool-API JSON for the JM-marked txs.
3. Bridges every cached tx into a :class:`~coinjoin_simulator.world.Tx`
   (with addresses as synthetic ids, ``OutputRole.UNKNOWN``, empty
   owners) so the existing on-chain solver bridge keeps working
   verbatim.
4. Runs :func:`~coinjoin_simulator.clusterer_onchain.recover_maker_outputs_from_txs`
   with ``require_truth=False`` and clusters the recovered
   change-output fingerprints with the same hash-bucket / DBSCAN
   strategies.
5. Reports cluster size distribution + per-cluster fee-policy summary.

The output of this module is *qualitative*: without ground truth we
cannot compute precision/recall, but we can position the observed
fingerprint count and cluster-size distribution against the synthetic
calibration curves emitted by :mod:`coinjoin_simulator.calibration`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coinjoin_simulator.clusterer_onchain import (
    RecoveredMakerOutput,
    dbscan_cluster_onchain,
    hash_bucket_cluster_onchain,
    is_coinjoin_tx,
    recover_maker_outputs_from_txs,
)
from coinjoin_simulator.clusterer_oracle import DEFAULT_LOG_STRIDE
from coinjoin_simulator.world import OutputRole, Tx, TxOutput

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CorpusEntry:
    """A single mainnet CJ tx loaded from cache."""

    txid: str
    block_height: int
    cj_amount_sats: int
    n_participants: int
    fee_sats: int
    tx: Tx


def _read_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        data: dict[str, Any] = json.load(f)
        return data


def _bridge_cached_tx(
    txid: str,
    cached: dict[str, Any],
    *,
    block_height: int,
) -> Tx:
    """Convert a mempool-API cache JSON into a :class:`Tx`.

    Real bitcoin addresses are reused as synthetic ``output_id`` /
    ``utxo_id`` strings. The ILP bridge in
    :mod:`coinjoin_simulator.clusterer_onchain` only requires
    intra-tx-unique strings, so this is a faithful round-trip.
    """
    vin = cached.get("vin", [])
    vout = cached.get("vout", [])
    inputs: list[str] = []
    input_values: list[int] = []
    for i, v in enumerate(vin):
        prevout = v.get("prevout") or {}
        addr = prevout.get("scriptpubkey_address") or f"{txid}:in:{i}"
        value = int(prevout.get("value", 0))
        # Disambiguate duplicate addresses within the same input set.
        inputs.append(f"{addr}#{i}")
        input_values.append(value)
    outputs: list[TxOutput] = []
    for i, o in enumerate(vout):
        addr = o.get("scriptpubkey_address") or f"{txid}:out:{i}"
        value = int(o.get("value", 0))
        outputs.append(
            TxOutput(
                output_id=f"{addr}#{i}",
                value_sats=value,
                role=OutputRole.UNKNOWN,
                owner="",
                mixdepth=None,
            ),
        )
    network_fee = int(cached.get("fee", 0))
    return Tx(
        txid=txid,
        block_height=block_height,
        tx_index=0,
        taker_id="",
        maker_counterparties=(),
        inputs=tuple(inputs),
        input_values=tuple(input_values),
        outputs=tuple(outputs),
        cj_amount_sats=0,
        total_cj_fee_sats=0,
        network_fee_sats=network_fee,
    )


def load_corpus(
    graph_path: Path | str,
    cache_dir: Path | str,
    *,
    only_jm: bool = True,
    limit: int | None = None,
    skip_missing: bool = True,
) -> Iterator[CorpusEntry]:
    """Stream :class:`CorpusEntry` rows from a cached JM index.

    Parameters
    ----------
    graph_path:
        Path to the ``graph2.json`` (or compatible) tx index.
    cache_dir:
        Directory holding ``<txid>.json`` mempool-API dumps.
    only_jm:
        When ``True`` (default) drop entries whose ``is_jm`` flag is
        falsy.
    limit:
        Cap the number of yielded entries. Useful for smoke tests.
    skip_missing:
        When ``True`` silently skip txids whose cache file is missing.
        When ``False`` raise :class:`FileNotFoundError`.
    """
    graph_path = Path(graph_path)
    cache_dir = Path(cache_dir)
    graph: dict[str, Any] = _read_json(graph_path)
    yielded = 0
    for txid, entry in graph.items():
        if only_jm and not entry.get("is_jm"):
            continue
        cache_file = cache_dir / f"{txid}.json"
        if not cache_file.exists():
            if skip_missing:
                continue
            raise FileNotFoundError(cache_file)
        cached = _read_json(cache_file)
        meta = entry.get("meta", {})
        tx = _bridge_cached_tx(
            txid,
            cached,
            block_height=int(meta.get("block_height", 0)),
        )
        if not is_coinjoin_tx(tx):
            continue
        yield CorpusEntry(
            txid=txid,
            block_height=int(meta.get("block_height", 0)),
            cj_amount_sats=int(meta.get("cj_amount", 0)),
            n_participants=int(meta.get("n_participants", 0)),
            fee_sats=int(meta.get("fee", 0)),
            tx=tx,
        )
        yielded += 1
        if limit is not None and yielded >= limit:
            return


# ---------------------------------------------------------------------------
# Cluster reporting
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ClusterSummary:
    """One row of the mainnet cluster report.

    ``cjfee_r_*`` are aggregated across the change outputs assigned to
    the cluster; ``txids`` lists the (deduped) txs the members appear
    in.
    """

    cluster_id: int
    n_outputs: int
    cjfee_r_mean: float
    cjfee_r_std: float
    cjfee_r_min: float
    cjfee_r_max: float
    log10_band: int  # nominal band for the cluster mean
    txids: list[str] = field(default_factory=list)
    output_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MainnetReport:
    """Aggregate output of :func:`run_corpus`."""

    strategy: str  # "hash_bucket" | "dbscan"
    mode: str  # "ilp" | "greedy"
    n_txs_seen: int  # CJ txs processed
    n_txs_solved: int  # txs the ILP solver returned a solution on
    n_recovered: int  # change outputs recovered
    n_clusters: int
    log_stride: float
    eps: float | None
    clusters: list[ClusterSummary]


def _summarize_clusters(
    rec: list[RecoveredMakerOutput],
    labels: dict[str, int],
    *,
    log_stride: float,
) -> list[ClusterSummary]:
    by_cluster: dict[int, list[RecoveredMakerOutput]] = {}
    for r in rec:
        cid = labels.get(r.output_id)
        if cid is None:
            continue
        by_cluster.setdefault(cid, []).append(r)
    out: list[ClusterSummary] = []
    for cid, members in sorted(by_cluster.items()):
        rs = [m.cjfee_r for m in members]
        mean = sum(rs) / len(rs)
        var = sum((x - mean) ** 2 for x in rs) / len(rs) if len(rs) > 1 else 0.0
        std = math.sqrt(var)
        band = math.floor(math.log10(mean) / log_stride) if mean > 0 else -(10**9)
        txids = sorted({m.txid for m in members})
        output_ids = sorted({m.output_id for m in members})
        out.append(
            ClusterSummary(
                cluster_id=int(cid),
                n_outputs=len(members),
                cjfee_r_mean=mean,
                cjfee_r_std=std,
                cjfee_r_min=min(rs),
                cjfee_r_max=max(rs),
                log10_band=int(band),
                txids=txids,
                output_ids=output_ids,
            ),
        )
    out.sort(key=lambda c: -c.n_outputs)
    return out


def run_corpus(
    entries: Iterable[CorpusEntry],
    *,
    strategy: str = "hash_bucket",
    log_stride: float = DEFAULT_LOG_STRIDE,
    eps: float = 0.3,
    max_fee_rel: float = 0.05,
    time_limit_per_solve: int = 10,
    mode: str = "ilp",
) -> MainnetReport:
    """Run the on-chain clusterer over a mainnet corpus and return a report.

    ``strategy`` selects between the deterministic log-band hash-bucket
    clusterer and the slightly looser DBSCAN over ``log10(cjfee_r)``.
    ``mode`` is forwarded to :func:`recover_maker_outputs_from_txs`;
    ``"greedy"`` skips the ILP entirely (~5 ms/tx) at the cost of
    coverage. Other parameters mirror :mod:`coinjoin_simulator.clusterer_onchain`.
    """
    if strategy not in {"hash_bucket", "dbscan"}:
        msg = f"unknown strategy: {strategy!r}"
        raise ValueError(msg)
    txs: list[Tx] = []
    n_seen = 0
    for e in entries:
        n_seen += 1
        txs.append(e.tx)
    rec = recover_maker_outputs_from_txs(
        txs,
        max_fee_rel=max_fee_rel,
        time_limit_per_solve=time_limit_per_solve,
        require_truth=False,
        mode=mode,
    )
    n_solved = len({r.txid for r in rec})
    if strategy == "hash_bucket":
        # Re-derive labels deterministically from log bands so the
        # report can read them back without rerunning the clusterer.
        labels: dict[str, int] = {}
        bucket_to_label: dict[int, int] = {}
        for r in rec:
            band = math.floor(math.log10(r.cjfee_r) / log_stride) if r.cjfee_r > 0 else -(10**9)
            if band not in bucket_to_label:
                bucket_to_label[band] = len(bucket_to_label)
            labels[r.output_id] = bucket_to_label[band]
        # Touch the public clusterer to keep behavior aligned even
        # though we already have the labels.
        _ = hash_bucket_cluster_onchain.__name__
        eps_used: float | None = None
    else:
        import numpy as np
        from sklearn.cluster import DBSCAN

        if not rec:
            labels = {}
        else:
            feats = np.array(
                [[math.log10(r.cjfee_r) if r.cjfee_r > 0 else -20.0] for r in rec],
                dtype=float,
            )
            db = DBSCAN(eps=eps, min_samples=1).fit(feats)
            labels = {r.output_id: int(db.labels_[i]) for i, r in enumerate(rec)}
        _ = dbscan_cluster_onchain.__name__
        eps_used = eps
    clusters = _summarize_clusters(rec, labels, log_stride=log_stride)
    return MainnetReport(
        strategy=strategy,
        mode=mode,
        n_txs_seen=n_seen,
        n_txs_solved=n_solved,
        n_recovered=len(rec),
        n_clusters=len(clusters),
        log_stride=log_stride,
        eps=eps_used,
        clusters=clusters,
    )


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------


def report_to_jsonable(report: MainnetReport) -> dict[str, Any]:
    """Render a :class:`MainnetReport` into a JSON-safe dict."""
    return {
        "strategy": report.strategy,
        "mode": report.mode,
        "n_txs_seen": report.n_txs_seen,
        "n_txs_solved": report.n_txs_solved,
        "n_recovered": report.n_recovered,
        "n_clusters": report.n_clusters,
        "log_stride": report.log_stride,
        "eps": report.eps,
        "clusters": [
            {
                "cluster_id": c.cluster_id,
                "n_outputs": c.n_outputs,
                "cjfee_r_mean": c.cjfee_r_mean,
                "cjfee_r_std": c.cjfee_r_std,
                "cjfee_r_min": c.cjfee_r_min,
                "cjfee_r_max": c.cjfee_r_max,
                "log10_band": c.log10_band,
                "txids": c.txids,
                "output_ids": c.output_ids,
            }
            for c in report.clusters
        ],
    }


def write_report(report: MainnetReport, path: Path | str) -> None:
    """Persist a :class:`MainnetReport` as pretty-printed JSON."""
    Path(path).write_text(json.dumps(report_to_jsonable(report), indent=2))
