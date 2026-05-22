"""Probing-attacker maker clusterer.

Models a Chainalysis-style adversary that **actively probes** makers via
the PoDLE-bypass route described in the legacy reference implementation
(see ``support.py`` / ``yieldgenerator.py`` rate-limiting). The probing
flow is well-known:

1. The attacker dials a maker via IRC, sends a fill request with valid
   PoDLE commitments, receives the maker's ``!ioauth`` response which
   includes the UTXOs the maker is willing to spend, then aborts before
   signing. Each probe consumes PoDLE commitments (3 per UTXO upper
   bound) and is subject to nick blacklisting if the attacker hits the
   per-maker rate limit.

2. After enough probes the attacker has, per maker counterparty, a
   high-confidence input-side label set. When a CJ tx hits the chain,
   the attacker matches inputs against the probed UTXO sets to identify
   which counterparty contributed which inputs — *without* needing to
   solve subset-sum at the input side.

3. From the input-side label, recovering the maker's CJ output and
   change output reduces to subset-sum over a small participant
   contribution: the maker contributed ``input_sum`` sats and received
   ``cj_amount + change_value`` sats with ``change_value =
   input_sum - cj_amount + cjfee_paid``. In most cases this resolution
   is unique even without knowing ``cjfee_paid`` exactly, because the
   change values are almost always distinct across makers within a
   single tx.

This makes the probing attacker the *strongest* attacker we model: it
sees the input-side label directly, so the resulting partition is at
most degraded by

- **probe coverage** — only a fraction of makers are probed
  (rate-limited by PoDLE commitment cost),
- **subset-sum ambiguity** — within a single CJ tx, two makers with
  equal change values cannot be distinguished by subset-sum alone.

The clusterer ships two configurable knobs and otherwise mirrors the
:class:`coinjoin_simulator.clusterer_oracle.ClusterAssignment` shape so
calibration code can swap clusterers transparently.

Note: we re-use the ``ClusterAssignment`` type and metric helpers from
:mod:`coinjoin_simulator.clusterer_oracle` to keep the comparison
apples-to-apples.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coinjoin_simulator.clusterer_oracle import ClusterAssignment, _build_assignment
from coinjoin_simulator.world import OutputRole

if TYPE_CHECKING:
    import random

    from coinjoin_simulator.world import SimResult, Tx


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProbingConfig:
    """Tunables for the probing attacker.

    Defaults mirror a *moderately aggressive* adversary: probes 80% of
    counterparties and resolves 95% of subset-sum cases. These are the
    same defaults used by the legacy ``SurveillanceSimulator`` so the
    two are directly comparable.
    """

    probe_success_rate: float = 0.8
    """Fraction of distinct maker counterparties the attacker manages to
    probe at least once before they appear in a CJ tx. Modeling
    rate-limit failures + UTXO rotation (legacy ``support.py`` blacklist
    after ~3 probes per nick)."""

    subset_sum_resolution_rate: float = 0.95
    """Probability that, given a probed maker's input-side label, the
    attacker can unambiguously recover the maker's CJ + change outputs
    via subset-sum. Failures occur when two probed makers in the same
    CJ tx share identical change values (rare but non-zero)."""

    seed: int | None = None
    """RNG seed for deterministic probe sampling."""


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _maker_outputs_per_tx(tx: Tx) -> dict[str, list[tuple[str, OutputRole]]]:
    """Return ``{counterparty: [(output_id, role), ...]}`` for a tx.

    Mirrors the ground-truth labelling the simulator emits; a probing
    attacker resolves this same mapping at runtime via input-side
    matching + subset-sum, and we model the failure modes via the
    ``ProbingConfig`` knobs rather than re-implementing the subset-sum
    solver.
    """
    out: dict[str, list[tuple[str, OutputRole]]] = defaultdict(list)
    for o in tx.outputs:
        if o.role in {OutputRole.MAKER_CJ, OutputRole.MAKER_CHANGE}:
            out[o.owner].append((o.output_id, o.role))
    return out


def probing_cluster(
    result: SimResult,
    config: ProbingConfig | None = None,
) -> ClusterAssignment:
    """Run the probing-attacker clusterer over a simulator result.

    The attacker:

    1. Picks the subset of maker counterparties it manages to probe
       (Bernoulli sampling at ``probe_success_rate``).
    2. For every CJ tx, takes the ground-truth contribution map and
       drops contributions from un-probed makers, then drops a fraction
       ``1 - subset_sum_resolution_rate`` of the probed contributions
       to model subset-sum ambiguity.
    3. Labels every recovered output with its true counterparty (the
       cluster id). Outputs that the attacker fails to recover are
       labeled ``-1`` (noise) so the metric counts them as missed.

    Returns a :class:`ClusterAssignment` with the same shape and
    metrics as the oracle / on-chain clusterers.
    """
    cfg = config or ProbingConfig()
    rng = _Rng(cfg.seed)

    # 1. Pick probed counterparties from the universe seen at sim start
    #    (every maker is in ``maker_id_by_utxo`` at t=0). Sampling once
    #    up-front matches the "long-running probing campaign" model: a
    #    given counterparty is either successfully probed or not.
    all_makers = sorted({cp for cp in result.maker_id_by_utxo.values()})
    probed: set[str] = {cp for cp in all_makers if rng.random() < cfg.probe_success_rate}

    # 2. Walk txs and label outputs.
    labels: dict[str, int] = {}
    ground_truth: dict[str, str] = {}
    cp_to_cluster: dict[str, int] = {}
    for tx in result.txs:
        per_maker = _maker_outputs_per_tx(tx)
        for cp, outputs in per_maker.items():
            for output_id, _role in outputs:
                ground_truth[output_id] = cp
                if cp not in probed:
                    labels[output_id] = -1
                    continue
                if rng.random() >= cfg.subset_sum_resolution_rate:
                    labels[output_id] = -1
                    continue
                if cp not in cp_to_cluster:
                    cp_to_cluster[cp] = len(cp_to_cluster)
                labels[output_id] = cp_to_cluster[cp]

    return _build_assignment(labels, ground_truth)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProbingClustererReport:
    """Single-strategy report kept symmetrical with the oracle / on-chain reports."""

    probing: ClusterAssignment
    n_true_makers: int = field(default=0)
    n_probed_makers: int = field(default=0)


def run_probing_clusterer(
    result: SimResult,
    *,
    config: ProbingConfig | None = None,
) -> ProbingClustererReport:
    """Convenience wrapper returning the assignment plus probe coverage."""
    assignment = probing_cluster(result, config)
    n_true = len({cp for cp in assignment.ground_truth.values()})
    n_probed = len({c for c in assignment.labels.values() if c != -1})
    return ProbingClustererReport(
        probing=assignment,
        n_true_makers=n_true,
        n_probed_makers=n_probed,
    )


# ---------------------------------------------------------------------------
# Tiny deterministic RNG wrapper
# ---------------------------------------------------------------------------


class _Rng:
    """Thin wrapper over :mod:`random.Random` so a None seed still works."""

    __slots__ = ("_rng",)

    def __init__(self, seed: int | None) -> None:
        import random as _stdlib_random

        self._rng: random.Random = _stdlib_random.Random(seed)

    def random(self) -> float:
        return self._rng.random()


__all__ = [
    "ProbingClustererReport",
    "ProbingConfig",
    "probing_cluster",
    "run_probing_clusterer",
]
