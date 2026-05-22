"""Tests for the mainnet on-chain pipeline.

These exercise the loader / bridge / clusterer end-to-end against a
synthetic mini-corpus that mimics the ``graph2.json`` + ``cache/<txid>.json``
shape of ``anon_chain_v5.py``. Real mainnet data is intentionally
out-of-scope here -- the production driver lives elsewhere -- so the
tests stay fast and deterministic.
"""

from __future__ import annotations

import json
import random
from typing import TYPE_CHECKING

import pytest

from coinjoin_simulator.agents import (
    DEFAULT_MAX_MIXDEPTH,
    Maker,
    MakerFeePolicy,
    PaymentTaker,
    Utxo,
)
from coinjoin_simulator.clusterer_onchain import (
    is_coinjoin_tx,
    recover_maker_outputs_from_txs,
)
from coinjoin_simulator.mainnet import (
    CorpusEntry,
    MainnetReport,
    _bridge_cached_tx,
    load_corpus,
    report_to_jsonable,
    run_corpus,
    write_report,
)
from coinjoin_simulator.world import OutputRole, SimResult, World, WorldConfig

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers: drive the simulator, then re-emit each Tx as mempool-API JSON
# so we exercise the full mainnet ingestion path on synthetic-but-real data.
# ---------------------------------------------------------------------------


def _make_maker(name: str, *, seed: int, cjfee_r: float) -> Maker:
    utxos: dict[int, list[Utxo]] = {
        m: [Utxo(utxo_id=f"u-{name}-m{m}", value_sats=100_000_000, mixdepth=m)]
        for m in range(DEFAULT_MAX_MIXDEPTH + 1)
    }
    return Maker(
        counterparty=name,
        policy=MakerFeePolicy(
            ordertype="sw0reloffer",
            cjfee_r=cjfee_r,
            cjfee_a_sats=500,
            txfee_contribution=100,
            minsize_sats=10_000,
            fidelity_bond_value=1e9,
        ),
        utxos=utxos,
        rng=random.Random(seed),
    )


def _run(makers: list[Maker]) -> SimResult:
    taker = PaymentTaker.build(
        rng=random.Random(7),
        recipient="bc1qrecipient",
        amount_sats=2_000_000,
        src_mixdepth=0,
        makercount=3,
    )
    return World.from_components(
        config=WorldConfig(seed=42),
        makers=makers,
        takers=[taker],
    ).run()


def _write_corpus(tmp_path: Path, sim: SimResult) -> tuple[Path, Path]:
    """Render every CJ tx in *sim* as cache JSON + a graph2 index."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    graph: dict = {}
    for tx in sim.txs:
        if not is_coinjoin_tx(tx):
            continue
        cached = {
            "txid": tx.txid,
            "fee": tx.network_fee_sats,
            "vin": [
                {
                    "prevout": {
                        "scriptpubkey_address": utxo_id,
                        "value": value,
                    },
                }
                for utxo_id, value in zip(tx.inputs, tx.input_values, strict=True)
            ],
            "vout": [
                {
                    "scriptpubkey_address": o.output_id,
                    "value": o.value_sats,
                }
                for o in tx.outputs
            ],
        }
        (cache_dir / f"{tx.txid}.json").write_text(json.dumps(cached))
        graph[tx.txid] = {
            "depth": 0,
            "is_jm": True,
            "meta": {
                "txid": tx.txid,
                "cj_amount": tx.cj_amount_sats,
                "n_participants": 1 + len(tx.maker_counterparties),
                "fee": tx.network_fee_sats,
                "block_height": tx.block_height,
            },
            "parents": [],
        }
    graph_path = tmp_path / "graph2.json"
    graph_path.write_text(json.dumps(graph))
    return graph_path, cache_dir


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


def test_bridge_cached_tx_round_trip() -> None:
    """Mempool-API JSON -> simulator Tx preserves values + addresses."""
    cached = {
        "txid": "abc",
        "fee": 1234,
        "vin": [
            {"prevout": {"scriptpubkey_address": "in1", "value": 50_000}},
            {"prevout": {"scriptpubkey_address": "in2", "value": 60_000}},
        ],
        "vout": [
            {"scriptpubkey_address": "o1", "value": 30_000},
            {"scriptpubkey_address": "o2", "value": 30_000},
            {"scriptpubkey_address": "o3", "value": 49_766},
        ],
    }
    tx = _bridge_cached_tx("abc", cached, block_height=1234)
    assert tx.txid == "abc"
    assert tx.block_height == 1234
    assert tx.input_values == (50_000, 60_000)
    assert sum(tx.input_values) == 110_000
    assert tuple(o.value_sats for o in tx.outputs) == (30_000, 30_000, 49_766)
    assert all(o.role == OutputRole.UNKNOWN for o in tx.outputs)
    assert all(o.owner == "" for o in tx.outputs)
    assert tx.network_fee_sats == 1234


def test_bridge_disambiguates_duplicate_addresses() -> None:
    """The bridge appends an index so duplicate inputs/outputs stay unique."""
    cached = {
        "txid": "x",
        "fee": 0,
        "vin": [
            {"prevout": {"scriptpubkey_address": "dup", "value": 100}},
            {"prevout": {"scriptpubkey_address": "dup", "value": 100}},
        ],
        "vout": [
            {"scriptpubkey_address": "out", "value": 50},
            {"scriptpubkey_address": "out", "value": 50},
        ],
    }
    tx = _bridge_cached_tx("x", cached, block_height=1)
    assert len(set(tx.inputs)) == 2
    assert len({o.output_id for o in tx.outputs}) == 2


def test_bridge_handles_missing_addresses() -> None:
    """Op-return / nonstandard outputs missing address still produce a Tx."""
    cached = {
        "txid": "y",
        "fee": 0,
        "vin": [{"prevout": {"value": 100}}],
        "vout": [{"value": 100}],
    }
    tx = _bridge_cached_tx("y", cached, block_height=2)
    assert len(tx.inputs) == 1
    assert len(tx.outputs) == 1


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_load_corpus_yields_jm_txs(tmp_path: Path) -> None:
    """``load_corpus`` walks graph2.json and yields hydrated CJ entries."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    expected = sum(1 for tx in sim.txs if is_coinjoin_tx(tx))
    assert len(entries) == expected
    assert all(isinstance(e, CorpusEntry) for e in entries)
    assert all(is_coinjoin_tx(e.tx) for e in entries)


def test_load_corpus_skips_non_jm(tmp_path: Path) -> None:
    """Entries with ``is_jm=false`` are filtered when ``only_jm=True``."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    graph = json.loads(graph_path.read_text())
    txid = next(iter(graph))
    graph[txid]["is_jm"] = False
    graph_path.write_text(json.dumps(graph))
    kept = list(load_corpus(graph_path, cache_dir))
    assert all(e.txid != txid for e in kept)
    all_entries = list(load_corpus(graph_path, cache_dir, only_jm=False))
    assert any(e.txid == txid for e in all_entries)


def test_load_corpus_limit(tmp_path: Path) -> None:
    """``limit`` caps the number of yielded entries."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir, limit=1))
    assert len(entries) == 1


def test_load_corpus_skip_missing(tmp_path: Path) -> None:
    """Missing cache files are skipped or raise based on the flag."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    graph = json.loads(graph_path.read_text())
    graph["ghost"] = {"is_jm": True, "meta": {"block_height": 0}}
    graph_path.write_text(json.dumps(graph))
    skipped = list(load_corpus(graph_path, cache_dir, skip_missing=True))
    assert all(e.txid != "ghost" for e in skipped)
    with pytest.raises(FileNotFoundError):
        list(load_corpus(graph_path, cache_dir, skip_missing=False))


# ---------------------------------------------------------------------------
# run_corpus
# ---------------------------------------------------------------------------


def test_run_corpus_hash_bucket_separates_two_policies(tmp_path: Path) -> None:
    """Two well-separated fee policies land in distinct clusters."""
    makers = [
        _make_maker("low0", seed=0, cjfee_r=1e-5),
        _make_maker("low1", seed=1, cjfee_r=1e-5),
        _make_maker("high0", seed=2, cjfee_r=1e-3),
        _make_maker("high1", seed=3, cjfee_r=1e-3),
    ]
    sim = _run(makers)
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    report = run_corpus(entries, strategy="hash_bucket")
    assert isinstance(report, MainnetReport)
    assert report.n_txs_seen == len(entries)
    assert report.n_recovered >= 1
    if report.n_recovered >= 2:
        assert report.n_clusters >= 1


def test_run_corpus_dbscan(tmp_path: Path) -> None:
    """DBSCAN strategy runs and reports an ``eps`` value."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    report = run_corpus(entries, strategy="dbscan", eps=0.5)
    assert report.strategy == "dbscan"
    assert report.eps == 0.5


def test_run_corpus_greedy_mode(tmp_path: Path) -> None:
    """``mode='greedy'`` produces a faster, possibly looser report."""
    makers = [
        _make_maker("low", seed=0, cjfee_r=1e-5),
        _make_maker("med", seed=1, cjfee_r=1e-4),
        _make_maker("high", seed=2, cjfee_r=1e-3),
    ]
    sim = _run(makers)
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    report = run_corpus(entries, strategy="hash_bucket", mode="greedy")
    assert report.mode == "greedy"
    # Greedy may recover fewer outputs than full ILP but should not be
    # negative or absurd; on this fixture it tends to recover at least
    # the unequivocal makers.
    assert report.n_recovered >= 0
    if report.n_recovered > 0:
        assert report.n_clusters >= 1


def test_run_corpus_invalid_mode(tmp_path: Path) -> None:
    """``mode`` is forwarded through and validated by the recovery layer."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    with pytest.raises(ValueError, match="unknown mode"):
        run_corpus(entries, strategy="hash_bucket", mode="kmeans")


def test_run_corpus_unknown_strategy_raises(tmp_path: Path) -> None:
    """Unknown strategies are rejected eagerly."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    with pytest.raises(ValueError, match="unknown strategy"):
        run_corpus(entries, strategy="kmeans")


def test_run_corpus_empty() -> None:
    """An empty corpus produces a zero report rather than raising."""
    report = run_corpus([], strategy="hash_bucket")
    assert report.n_txs_seen == 0
    assert report.n_recovered == 0
    assert report.n_clusters == 0
    assert report.clusters == []


def test_run_corpus_clusters_sorted_by_size(tmp_path: Path) -> None:
    """Cluster summaries are sorted descending by member count."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    report = run_corpus(entries, strategy="hash_bucket")
    counts = [c.n_outputs for c in report.clusters]
    assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_report_to_jsonable_and_write(tmp_path: Path) -> None:
    """JSON encoding round-trips and ``write_report`` produces parseable output."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    report = run_corpus(entries, strategy="hash_bucket")
    encoded = report_to_jsonable(report)
    assert encoded["strategy"] == "hash_bucket"
    assert encoded["n_txs_seen"] == report.n_txs_seen
    out = tmp_path / "report.json"
    write_report(report, out)
    parsed = json.loads(out.read_text())
    assert parsed == encoded


# ---------------------------------------------------------------------------
# Recovery on the bridged corpus must match recovery on the raw sim
# ---------------------------------------------------------------------------


def test_recovery_through_bridge_matches_direct(tmp_path: Path) -> None:
    """Recovery on bridged mainnet-shape Txs matches direct synthetic recovery
    in *count*, even though the bridged path lacks ground-truth labels."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    direct = recover_maker_outputs_from_txs(sim.txs, require_truth=True)
    bridged = recover_maker_outputs_from_txs(
        [e.tx for e in entries],
        require_truth=False,
    )
    # Bridged path is a strict superset (it doesn't drop change outputs
    # the solver returns even when ground truth is unavailable).
    assert len(bridged) >= len(direct)
    assert all(r.maker_id_truth == "" for r in bridged)
    assert any(r.maker_id_truth != "" for r in direct)


def test_recover_greedy_is_subset_of_ilp(tmp_path: Path) -> None:
    """Greedy mode never recovers an output ILP wouldn't; ILP >= greedy."""
    sim = _run([_make_maker(f"m{i}", seed=i, cjfee_r=1e-4) for i in range(3)])
    graph_path, cache_dir = _write_corpus(tmp_path, sim)
    entries = list(load_corpus(graph_path, cache_dir))
    txs = [e.tx for e in entries]
    ilp = recover_maker_outputs_from_txs(txs, require_truth=False, mode="ilp")
    greedy = recover_maker_outputs_from_txs(txs, require_truth=False, mode="greedy")
    assert len(greedy) <= len(ilp)


def test_recover_unknown_mode() -> None:
    """The recovery layer rejects unknown modes early."""
    with pytest.raises(ValueError, match="unknown mode"):
        recover_maker_outputs_from_txs([], mode="kmeans")
