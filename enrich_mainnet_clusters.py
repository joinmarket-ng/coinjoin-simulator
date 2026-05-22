"""Enrich the mainnet cluster dumps with candidate maker eq-outputs.

The driver in ``run_mainnet_sweep.py`` writes one JSON per cluster
under ``tmp/mainnet_clusters/`` containing the *change outputs* the
on-chain solver attributed to a single maker fee policy. For
ground-truth validation against a live maker's wallet, the operator
also wants the *equal-amount* outputs the same maker created.

In the S_eff=1 CJ subset (where exactly one participant was not
already pre-clustered by the chain-only attack), the lone unmatched
participant's eq-output is almost certainly the maker's. ``maker_utxos.json``
ships those candidate eq-outputs as a flat list of 25k entries.

This script joins the two: every cluster's tx set is intersected with
the S=1 CJ set, and the matching eq-outputs are appended to the
cluster as ``equal_amount_utxos`` so a human can pull a single JSON
and compare the union of (change + eq) UTXOs against a live maker.

Usage::

    python enrich_mainnet_clusters.py

Reads ``tmp/maker_utxos.json`` and ``tmp/mainnet_clusters/cluster_*.json``;
writes the merged versions back in place. Idempotent.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

CLUSTERS_DIR = Path("tmp/mainnet_clusters")
MAKER_UTXOS = Path("tmp/maker_utxos.json")


def main() -> int:
    if not MAKER_UTXOS.is_file():
        sys.stderr.write(f"missing: {MAKER_UTXOS}\n")
        return 2
    if not CLUSTERS_DIR.is_dir():
        sys.stderr.write(f"missing: {CLUSTERS_DIR}\n")
        return 2

    candidates: list[dict[str, Any]] = json.loads(MAKER_UTXOS.read_text())[
        "candidate_maker_eq_outputs"
    ]
    by_txid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_txid[c["txid"]].append(c)
    print(
        f"loaded {len(candidates)} candidate eq-outputs across "
        f"{len(by_txid)} S=1 CJ txs",
        flush=True,
    )

    cluster_files = sorted(CLUSTERS_DIR.glob("cluster_*.json"))
    if not cluster_files:
        sys.stderr.write(f"no cluster files in {CLUSTERS_DIR}\n")
        return 1

    n_eq_total = 0
    enriched = 0
    for path in cluster_files:
        d = json.loads(path.read_text())
        cluster_txids = {u["txid"] for u in d["utxos"]}
        eq_utxos: list[dict[str, Any]] = []
        for txid in cluster_txids:
            for c in by_txid.get(txid, ()):
                eq_utxos.append(
                    {
                        "txid": c["txid"],
                        "vout": c["vout"],
                        "address": c["address"],
                        "value_sats": int(c["value"]),
                        "block_height": int(c.get("block_height", 0)),
                    },
                )
        eq_utxos.sort(key=lambda u: (u["txid"], u["vout"]))
        # Drop any duplicate (txid, vout) pairs in case of rare overlap.
        seen: set[tuple[str, int]] = set()
        deduped: list[dict[str, Any]] = []
        for u in eq_utxos:
            key = (u["txid"], u["vout"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(u)
        d["equal_amount_utxos"] = deduped
        d["n_equal_amount_utxos"] = len(deduped)
        d["n_distinct_eq_addresses"] = len({u["address"] for u in deduped})
        d["total_eq_value_sats"] = sum(u["value_sats"] for u in deduped)
        # Combined wallet-shaped view for the validator.
        change_view = [
            {
                "txid": u["txid"],
                "vout": u["vout"],
                "address": u["address"],
                "value_sats": u["value_sats"],
                "kind": "change",
            }
            for u in d["utxos"]
        ]
        eq_view = [
            {
                "txid": u["txid"],
                "vout": u["vout"],
                "address": u["address"],
                "value_sats": u["value_sats"],
                "kind": "equal_amount",
            }
            for u in deduped
        ]
        all_utxos = sorted(
            change_view + eq_view,
            key=lambda u: (u["txid"], u["vout"]),
        )
        d["all_utxos"] = all_utxos
        d["n_all_utxos"] = len(all_utxos)
        d["n_distinct_all_addresses"] = len({u["address"] for u in all_utxos})
        path.write_text(json.dumps(d, indent=2))
        n_eq_total += len(deduped)
        if deduped:
            enriched += 1

    print(
        f"enriched {enriched}/{len(cluster_files)} clusters; "
        f"added {n_eq_total} equal-amount UTXOs",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
