"""Run the on-chain maker-clustering attack over the cached mainnet corpus.

Usage::

    python run_mainnet_sweep.py [--limit N] [--mode greedy|ilp] \
        [--strategy hash_bucket|dbscan]

Reads ``tmp/jm/graph2.json`` + ``tmp/jm/cache/`` (produced by
``anon_chain_v5.py``), recovers maker change outputs via the on-chain
clusterer, groups them into clusters by fee-rate fingerprint, and writes:

* ``data/mainnet_report.json`` — aggregate :class:`MainnetReport`.
* ``tmp/mainnet_clusters/<cluster_id>.json`` — per-cluster UTXO dump
  (txid, vout, address, value, fee fingerprint) so a human can spot-check
  whether a cluster's outputs really do look like a single maker.

Greedy mode is the default because the ILP at ~0.5 s/tx makes a full
11 k-tx sweep take ~90 minutes, while greedy alone runs in seconds.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from coinjoin_simulator.clusterer_onchain import (
    RecoveredMakerOutput,
    recover_maker_outputs_from_txs,
)
from coinjoin_simulator.clusterer_oracle import DEFAULT_LOG_STRIDE
from coinjoin_simulator.mainnet import (
    CorpusEntry,
    MainnetReport,
    load_corpus,
    report_to_jsonable,
    run_corpus,
)

DEFAULT_GRAPH = Path("tmp/jm/graph2.json")
DEFAULT_CACHE = Path("tmp/jm/cache")
DEFAULT_REPORT = Path("data/mainnet_report.json")
DEFAULT_CLUSTERS_DIR = Path("tmp/mainnet_clusters")


def _read_cache(cache_dir: Path, txid: str) -> dict[str, Any]:
    with (cache_dir / f"{txid}.json").open() as f:
        d: dict[str, Any] = json.load(f)
        return d


def _hash_bucket_label(cjfee_r: float, log_stride: float) -> int:
    """Replicate the hash-bucket label used in :func:`run_corpus`."""
    if cjfee_r <= 0:
        return -(10**9)
    return math.floor(math.log10(cjfee_r) / log_stride)


def _assign_labels(
    rec: list[RecoveredMakerOutput],
    *,
    strategy: str,
    log_stride: float,
    eps: float,
) -> dict[str, int]:
    """Re-derive cluster labels for the recovered outputs.

    Mirrors :func:`coinjoin_simulator.mainnet.run_corpus` so callers can
    map outputs back to the same cluster ids that appear in the report.
    """
    if strategy == "hash_bucket":
        bucket_to_label: dict[int, int] = {}
        labels: dict[str, int] = {}
        for r in rec:
            band = _hash_bucket_label(r.cjfee_r, log_stride)
            if band not in bucket_to_label:
                bucket_to_label[band] = len(bucket_to_label)
            labels[r.output_id] = bucket_to_label[band]
        return labels
    if strategy == "dbscan":
        if not rec:
            return {}
        import numpy as np
        from sklearn.cluster import DBSCAN

        feats = np.array(
            [[math.log10(r.cjfee_r) if r.cjfee_r > 0 else -20.0] for r in rec],
            dtype=float,
        )
        db = DBSCAN(eps=eps, min_samples=1).fit(feats)
        return {r.output_id: int(db.labels_[i]) for i, r in enumerate(rec)}
    msg = f"unknown strategy: {strategy!r}"
    raise ValueError(msg)


def _utxo_detail(
    rec: RecoveredMakerOutput,
    cache_dir: Path,
) -> dict[str, Any]:
    """Resolve a recovered output back to its on-chain (txid, vout, ...)."""
    cached = _read_cache(cache_dir, rec.txid)
    # output_id is "{address}#{i}" (or fallback "{txid}:out:{i}#{i}")
    raw, _, idx_str = rec.output_id.rpartition("#")
    try:
        vout_idx = int(idx_str)
    except ValueError:
        vout_idx = -1
    address = raw if not raw.startswith(f"{rec.txid}:out:") else ""
    value = 0
    if 0 <= vout_idx < len(cached.get("vout", [])):
        o = cached["vout"][vout_idx]
        value = int(o.get("value", 0))
        if not address:
            address = o.get("scriptpubkey_address") or ""
    return {
        "txid": rec.txid,
        "vout": vout_idx,
        "address": address,
        "value_sats": value,
        "cjfee_r": rec.cjfee_r,
        "fee_sats": rec.fee_sats,
        "cj_amount_sats": rec.cj_amount_sats,
    }


def _dump_clusters(
    rec: list[RecoveredMakerOutput],
    labels: dict[str, int],
    *,
    report: MainnetReport,
    cache_dir: Path,
    out_dir: Path,
) -> None:
    """Write one JSON per cluster with full UTXO detail for spot checking."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Wipe any prior dump so stale clusters from earlier runs don't linger.
    for stale in out_dir.glob("cluster_*.json"):
        stale.unlink()
    by_cluster: dict[int, list[RecoveredMakerOutput]] = defaultdict(list)
    for r in rec:
        cid = labels.get(r.output_id)
        if cid is None:
            continue
        by_cluster[cid].append(r)
    cluster_meta = {c.cluster_id: c for c in report.clusters}
    for cid, members in by_cluster.items():
        meta = cluster_meta.get(cid)
        utxos = [_utxo_detail(m, cache_dir) for m in members]
        utxos.sort(key=lambda u: (u["txid"], u["vout"]))
        payload = {
            "cluster_id": cid,
            "n_outputs": len(members),
            "cjfee_r_mean": meta.cjfee_r_mean if meta else 0.0,
            "cjfee_r_std": meta.cjfee_r_std if meta else 0.0,
            "cjfee_r_min": meta.cjfee_r_min if meta else 0.0,
            "cjfee_r_max": meta.cjfee_r_max if meta else 0.0,
            "log10_band": meta.log10_band if meta else 0,
            "n_distinct_addresses": len({u["address"] for u in utxos if u["address"]}),
            "n_distinct_txs": len({u["txid"] for u in utxos}),
            "total_value_sats": sum(u["value_sats"] for u in utxos),
            "utxos": utxos,
        }
        path = out_dir / f"cluster_{cid:06d}.json"
        path.write_text(json.dumps(payload, indent=2))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    p.add_argument("--clusters-dir", type=Path, default=DEFAULT_CLUSTERS_DIR)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--mode", choices=("greedy", "ilp"), default="greedy")
    p.add_argument("--strategy", choices=("hash_bucket", "dbscan"), default="hash_bucket")
    p.add_argument("--log-stride", type=float, default=DEFAULT_LOG_STRIDE)
    p.add_argument("--eps", type=float, default=0.3)
    p.add_argument("--max-fee-rel", type=float, default=0.05)
    p.add_argument(
        "--time-limit-per-solve",
        type=int,
        default=10,
        help="Per-tx ILP time limit in seconds (ignored in greedy mode).",
    )
    p.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="How many top clusters to print to stdout.",
    )
    p.add_argument(
        "--no-cluster-dump",
        action="store_true",
        help="Skip per-cluster UTXO JSON dumps (faster).",
    )
    args = p.parse_args()

    if not args.graph.is_file():
        sys.stderr.write(f"graph file not found: {args.graph}\n")
        return 2
    if not args.cache.is_dir():
        sys.stderr.write(f"cache dir not found: {args.cache}\n")
        return 2

    print(f"loading corpus from {args.graph} ...", flush=True)
    t0 = time.monotonic()
    entries: list[CorpusEntry] = list(load_corpus(args.graph, args.cache, limit=args.limit))
    print(f"  loaded {len(entries)} CJ txs in {time.monotonic() - t0:.1f}s", flush=True)
    if not entries:
        sys.stderr.write("no entries loaded; aborting\n")
        return 1

    print(f"running on-chain recovery ({args.mode}, {args.strategy}) ...", flush=True)
    t1 = time.monotonic()
    report = run_corpus(
        entries,
        strategy=args.strategy,
        log_stride=args.log_stride,
        eps=args.eps,
        max_fee_rel=args.max_fee_rel,
        time_limit_per_solve=args.time_limit_per_solve,
        mode=args.mode,
    )
    elapsed = time.monotonic() - t1
    print(
        f"  recovered {report.n_recovered} outputs from "
        f"{report.n_txs_solved}/{report.n_txs_seen} txs into "
        f"{report.n_clusters} clusters in {elapsed:.1f}s "
        f"({elapsed / max(1, report.n_txs_seen) * 1000:.1f} ms/tx)",
        flush=True,
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report_to_jsonable(report), indent=2))
    print(f"wrote report to {args.report}", flush=True)

    if not args.no_cluster_dump:
        # Re-run recovery (cheap; cached at ILP layer) to get the raw
        # RecoveredMakerOutput list. ``run_corpus`` does not expose it.
        rec = recover_maker_outputs_from_txs(
            [e.tx for e in entries],
            max_fee_rel=args.max_fee_rel,
            time_limit_per_solve=args.time_limit_per_solve,
            require_truth=False,
            mode=args.mode,
        )
        labels = _assign_labels(
            rec,
            strategy=args.strategy,
            log_stride=args.log_stride,
            eps=args.eps,
        )
        _dump_clusters(
            rec,
            labels,
            report=report,
            cache_dir=args.cache,
            out_dir=args.clusters_dir,
        )
        print(
            f"wrote per-cluster UTXO dumps to {args.clusters_dir}/cluster_*.json",
            flush=True,
        )

    print()
    print(f"top {args.top_n} clusters by output count:")
    print(f"{'cid':>5}  {'#out':>5}  {'#tx':>5}  {'cjfee_r mean':>14}  {'std':>10}  band")
    for c in report.clusters[: args.top_n]:
        print(
            f"{c.cluster_id:>5}  {c.n_outputs:>5}  {len(c.txids):>5}  "
            f"{c.cjfee_r_mean:>14.3e}  {c.cjfee_r_std:>10.3e}  {c.log10_band}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
