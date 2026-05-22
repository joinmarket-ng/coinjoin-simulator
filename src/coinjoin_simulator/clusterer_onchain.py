"""On-chain attacker: recover maker partition using only public tx data.

The on-chain attacker has access to the simulated transaction graph
(:class:`coinjoin_simulator.world.Tx`) but no offer log, no maker
identities, and no orderbook snapshots tying fee policy to a maker. It
mirrors what an external observer running
`joinmarket_analyzer <https://github.com/JoinMarket-Org/joinmarket_analyzer>`_
on the live mempool would see.

Pipeline
--------
1. Bridge each :class:`Tx` into a
   :class:`joinmarket_analyzer.models.TransactionData` shape (the public
   solver expects ``vin``/``vout`` dicts that look like
   ``mempool.space``/``mempool.sgn.space`` JSON).
2. Run :func:`joinmarket_analyzer.solver.solve_all_solutions` to recover
   one or more candidate input -> output linkings. When the solver
   returns multiple solutions the tx is *ambiguous*; we pick the one
   whose taker matches the simulator's known taker-input naming (the
   real attacker would not have this signal -- in mainnet runs we'll
   pick by minimum ``discrepancy`` and break ties deterministically).
3. For every recovered :class:`Participant` with ``role == "maker"`` we
   derive a fee-rate fingerprint (``abs(fee) / equal_amount``) and use
   the same banded log10 bucketing as :mod:`clusterer_oracle`.
4. Outputs of the recovered maker participants (CJ output + change
   output, when present) are assigned the same cluster label. The
   :class:`ClusterAssignment` shape matches the oracle so calibration
   code can swap clusterers transparently.

The on-chain attacker therefore upper-bounds what a public observer can
recover **without** active probing or surveillance. Compared to the
oracle clusterer it loses signal on three axes:

- ``cjfee_a`` and ``minsize`` are not recoverable (only realized fees
  are observable);
- ``ordertype`` is not recoverable (the solver returns satoshi flows,
  not policy types);
- ``fidelity_bond_value`` is not recoverable from a single tx.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger as _loguru_logger

from coinjoin_simulator.clusterer_oracle import (
    DEFAULT_LOG_STRIDE,
    ClusterAssignment,
    _bipartite_prf,
)
from coinjoin_simulator.world import OutputRole, Tx

if TYPE_CHECKING:
    from collections.abc import Iterable

    from coinjoin_simulator.world import SimResult


# Silence joinmarket_analyzer's loguru output during clustering -- the
# library logs at INFO level for every solver iteration which is fine
# for the CLI but renders our test runs unreadable.
_loguru_logger.remove()
_loguru_logger.add(lambda _msg: None, level="CRITICAL")
logging.getLogger("joinmarket_analyzer").setLevel(logging.CRITICAL)


# ---------------------------------------------------------------------------
# Bridge: simulator Tx -> joinmarket_analyzer TransactionData
# ---------------------------------------------------------------------------


def tx_to_analyzer_dict(tx: Tx) -> dict[str, Any]:
    """Render a simulator :class:`Tx` as the dict shape ``parse_transaction`` expects.

    The analyzer's :func:`joinmarket_analyzer.parser.parse_transaction`
    keys on ``txid``, ``vin[*].prevout.{scriptpubkey_address, value}``
    and ``vout[*].{scriptpubkey_address, value}``. Since the simulator
    has no real bitcoin addresses, we use the synthetic UTXO/output ids
    as addresses -- they are unique within a tx, which is all the
    parser needs.
    """
    vin = [
        {
            "prevout": {
                "scriptpubkey_address": utxo_id,
                "value": value,
            },
        }
        for utxo_id, value in zip(tx.inputs, tx.input_values, strict=True)
    ]
    vout = [
        {
            "scriptpubkey_address": out.output_id,
            "value": out.value_sats,
        }
        for out in tx.outputs
    ]
    return {"txid": tx.txid, "vin": vin, "vout": vout}


def is_coinjoin_tx(tx: Tx) -> bool:
    """A CJ tx in our world has at least 2 outputs at the same value."""
    if len(tx.outputs) < 2:
        return False
    counts: dict[int, int] = {}
    for o in tx.outputs:
        counts[o.value_sats] = counts.get(o.value_sats, 0) + 1
    return max(counts.values()) >= 2


# ---------------------------------------------------------------------------
# Solver wrapper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecoveredMakerOutput:
    """A maker output recovered by the on-chain solver.

    Carries the ground-truth ``maker_id`` (only for evaluation -- the
    attacker never sees it) and the recovered fee fingerprint.
    """

    output_id: str
    txid: str
    cjfee_r: float
    fee_sats: int
    cj_amount_sats: int
    maker_id_truth: str  # ground truth, used only for metrics
    is_change: bool


def _solve(tx: Tx, max_fee_rel: float, time_limit_per_solve: int) -> Any | None:
    """Run :func:`solve_all_solutions` on a single tx, return best solution."""
    from joinmarket_analyzer.parser import parse_transaction
    from joinmarket_analyzer.solver import solve_all_solutions

    try:
        tx_data = parse_transaction(tx_to_analyzer_dict(tx))
    except Exception:
        return None
    try:
        sols = solve_all_solutions(
            tx_data,
            max_fee_rel=max_fee_rel,
            max_solutions=8,
            time_limit_per_solve=time_limit_per_solve,
            save_incrementally=False,
        )
    except Exception:
        return None
    if not sols:
        return None
    # Pick the lowest-discrepancy solution; deterministic tiebreak by
    # the canonical signature so identical-discrepancy solutions don't
    # flap between runs.
    from joinmarket_analyzer.solver import get_solution_signature

    sols.sort(key=lambda s: (s.discrepancy, get_solution_signature(s)))
    return sols[0]


def _greedy_recover(
    tx: Tx,
    max_fee_rel: float,
    *,
    truth_by_output_id: dict[str, str],
    require_truth: bool,
) -> list[RecoveredMakerOutput]:
    """Recover maker change outputs using greedy preprocessing only.

    Two orders of magnitude faster than the full ILP at the cost of
    coverage: only definitely-correct (input -> participant -> change)
    triples are emitted. On a mainnet smoke run this still recovers
    ~5 maker outputs per CJ tx.
    """
    from joinmarket_analyzer.parser import parse_transaction
    from joinmarket_analyzer.solver import greedy_preprocessing

    try:
        td = parse_transaction(tx_to_analyzer_dict(tx))
    except Exception:  # noqa: BLE001 - parser raises for malformed cache files
        return []
    try:
        g = greedy_preprocessing(td, max_fee_rel)
    except Exception:  # noqa: BLE001 - solver guards against unsolvable txs
        return []

    # Invert forced_assignments: participant_idx -> [input_idx, ...]
    inputs_by_participant: dict[int, list[int]] = {}
    for inp_idx, participant_idx in g.forced_assignments.items():
        inputs_by_participant.setdefault(int(participant_idx), []).append(int(inp_idx))

    equal_amount = td.equal_amount or max(o.value_sats for o in tx.outputs)
    out: list[RecoveredMakerOutput] = []
    for p_idx, change_idx in g.forced_changes.items():
        if change_idx is None:
            continue
        if g.taker_idx is not None and int(p_idx) == int(g.taker_idx):
            continue  # taker, not a maker
        change = td.change_outputs[int(change_idx)]
        # Maker fee = sum(inputs) - cj_output - change_output.
        in_sum = sum(td.inputs[i].amount for i in inputs_by_participant.get(int(p_idx), []))
        if in_sum <= 0:
            continue
        fee = in_sum - equal_amount - int(change.amount)
        cjfee_r = abs(fee) / equal_amount if equal_amount else 0.0
        change_addr = change.address
        if require_truth and change_addr not in truth_by_output_id:
            continue
        out.append(
            RecoveredMakerOutput(
                output_id=change_addr,
                txid=tx.txid,
                cjfee_r=cjfee_r,
                fee_sats=int(abs(fee)),
                cj_amount_sats=equal_amount,
                maker_id_truth=truth_by_output_id.get(change_addr, ""),
                is_change=True,
            ),
        )
    return out


def recover_maker_outputs_from_txs(
    txs: Iterable[Tx],
    *,
    max_fee_rel: float = 0.05,
    time_limit_per_solve: int = 10,
    require_truth: bool = True,
    mode: str = "ilp",
) -> list[RecoveredMakerOutput]:
    """Run the on-chain attacker over a stream of :class:`Tx`.

    ``mode`` selects between:

    - ``"ilp"`` (default): full
      :func:`joinmarket_analyzer.solver.solve_all_solutions`. Slow but
      maximally informative -- recovers every participant the LP can
      assign uniquely.
    - ``"greedy"``: only the greedy preprocessing step. Two orders of
      magnitude faster (~5 ms/tx vs ~500 ms/tx); recovers only the
      maker change outputs whose input-side and change-side
      assignments are *forced* (i.e. only one feasible matching exists
      under the tx fee + max_fee_rel constraints).

    When ``require_truth`` is ``True`` (the synthetic-data case) every
    recovered change output must map back to a ``MAKER_*`` ground-truth
    output -- otherwise the solver assigned a non-maker output and we
    skip it. When ``False`` (the mainnet case) we accept every change
    output the solver returns and emit ``maker_id_truth=""``; the
    on-chain attacker has no truth signal to lean on anyway.
    """
    if mode not in {"ilp", "greedy"}:
        msg = f"unknown mode: {mode!r}"
        raise ValueError(msg)
    out: list[RecoveredMakerOutput] = []
    for tx in txs:
        if not is_coinjoin_tx(tx):
            continue
        truth_by_output_id: dict[str, str] = {
            o.output_id: o.owner
            for o in tx.outputs
            if o.role in (OutputRole.MAKER_CJ, OutputRole.MAKER_CHANGE)
        }
        if mode == "greedy":
            out.extend(
                _greedy_recover(
                    tx,
                    max_fee_rel,
                    truth_by_output_id=truth_by_output_id,
                    require_truth=require_truth,
                ),
            )
            continue
        sol = _solve(tx, max_fee_rel, time_limit_per_solve)
        if sol is None:
            continue
        equal_amount = max(o.value_sats for o in tx.outputs)
        for participant in sol.participants:
            if participant.role != "maker":
                continue
            cjfee_r = abs(participant.fee) / equal_amount if equal_amount else 0.0
            # We fingerprint maker *change* outputs only -- equal-output
            # CJ slots are indistinguishable to an external observer.
            if participant.change_output is None:
                continue
            change_addr = participant.change_output.address
            if require_truth and change_addr not in truth_by_output_id:
                # Solver assigned a non-maker output as change; skip.
                continue
            out.append(
                RecoveredMakerOutput(
                    output_id=change_addr,
                    txid=tx.txid,
                    cjfee_r=cjfee_r,
                    fee_sats=int(abs(participant.fee)),
                    cj_amount_sats=equal_amount,
                    maker_id_truth=truth_by_output_id.get(change_addr, ""),
                    is_change=True,
                ),
            )
    return out


def recover_maker_outputs(
    sim_result: SimResult,
    *,
    max_fee_rel: float = 0.05,
    time_limit_per_solve: int = 10,
) -> list[RecoveredMakerOutput]:
    """For every CJ tx in the sim run, recover all maker outputs via ILP.

    Synthetic-data wrapper around :func:`recover_maker_outputs_from_txs`
    that enforces ``require_truth=True``: outputs the solver assigns to
    non-maker change positions are dropped so downstream metrics
    compare like-with-like.
    """
    return recover_maker_outputs_from_txs(
        sim_result.txs,
        max_fee_rel=max_fee_rel,
        time_limit_per_solve=time_limit_per_solve,
        require_truth=True,
    )


# ---------------------------------------------------------------------------
# Clusterers
# ---------------------------------------------------------------------------


def _log_band(value: float, stride: float) -> int:
    """Band index of a positive value on a log10 grid (matches oracle)."""
    if value <= 0:
        return -(10**9)
    return math.floor(math.log10(value) / stride)


def hash_bucket_cluster_onchain(
    sim_result: SimResult,
    *,
    log_stride: float = DEFAULT_LOG_STRIDE,
    recovered: Iterable[RecoveredMakerOutput] | None = None,
    max_fee_rel: float = 0.05,
) -> ClusterAssignment:
    """Hash-bucket cluster recovered maker outputs by fee fingerprint.

    Falls back gracefully when no makers are recovered (e.g. solver
    timed out on every tx).
    """
    rec = (
        list(recovered)
        if recovered is not None
        else recover_maker_outputs(sim_result, max_fee_rel=max_fee_rel)
    )
    if not rec:
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
    bucket_to_label: dict[int, int] = {}
    labels: dict[str, int] = {}
    truth: dict[str, str] = {}
    for r in rec:
        band = _log_band(r.cjfee_r, log_stride) if r.cjfee_r > 0 else -(10**9)
        if band not in bucket_to_label:
            bucket_to_label[band] = len(bucket_to_label)
        labels[r.output_id] = bucket_to_label[band]
        truth[r.output_id] = r.maker_id_truth
    return _build_assignment(labels, truth)


def dbscan_cluster_onchain(
    sim_result: SimResult,
    *,
    eps: float = 0.3,
    min_samples: int = 1,
    recovered: Iterable[RecoveredMakerOutput] | None = None,
    max_fee_rel: float = 0.05,
) -> ClusterAssignment:
    """DBSCAN over the single fee-rate axis recovered from on-chain data."""
    import numpy as np
    from sklearn.cluster import DBSCAN

    rec = (
        list(recovered)
        if recovered is not None
        else recover_maker_outputs(sim_result, max_fee_rel=max_fee_rel)
    )
    if not rec:
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
    feats = np.array(
        [[math.log10(r.cjfee_r) if r.cjfee_r > 0 else -20.0] for r in rec],
        dtype=float,
    )
    db = DBSCAN(eps=eps, min_samples=min_samples).fit(feats)
    labels = {r.output_id: int(db.labels_[i]) for i, r in enumerate(rec)}
    truth = {r.output_id: r.maker_id_truth for r in rec}
    return _build_assignment(labels, truth)


def run_onchain_clusterers(
    sim_result: SimResult,
    *,
    log_stride: float = DEFAULT_LOG_STRIDE,
    eps: float = 0.3,
    max_fee_rel: float = 0.05,
) -> dict[str, ClusterAssignment]:
    """Convenience wrapper running both strategies on a single tx-data pass."""
    rec = recover_maker_outputs(sim_result, max_fee_rel=max_fee_rel)
    return {
        "hash_bucket": hash_bucket_cluster_onchain(
            sim_result, log_stride=log_stride, recovered=rec
        ),
        "dbscan": dbscan_cluster_onchain(sim_result, eps=eps, recovered=rec),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_assignment(
    labels: dict[str, int],
    truth: dict[str, str],
) -> ClusterAssignment:
    if not labels:
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
