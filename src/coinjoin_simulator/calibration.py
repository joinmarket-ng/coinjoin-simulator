"""Calibration sweep for the maker-clustering attackers.

The three attacker tiers (oracle / on-chain / probing) all share the
same :class:`coinjoin_simulator.clusterer_oracle.ClusterAssignment`
interface, so we can drive them through one comparison harness.

What we sweep
-------------

The attacker's recovery is a function of the simulated population, not
the wallet internals. The dimensions that matter:

- **n_makers**: how many distinct counterparties make up the population.
  Determines the number of true clusters (upper bound).
- **policy_diversity**: how many *distinct policy centers* the makers
  draw from. ``policy_diversity == 1`` means all makers share a single
  fee policy and only the yg-pe jitter separates them; ``policy_diversity
  == n_makers`` means each maker is its own center.
- **fee_jitter_scale**: scales the ``cjfee_factor`` / ``txfee_factor`` /
  ``size_factor`` fields that drive the yg-privacyenhanced ±10%
  re-announce randomization. ``0.0`` freezes offers, ``1.0`` is stock,
  ``2.0`` doubles the spread.
- **n_payment_takers**: how many ``PaymentTaker`` flows run in parallel,
  which controls the total tx count emitted.
- **attacker_tier**: which clusterer to invoke (six variants total).
- **seeds**: per-cell repetitions, used for confidence intervals.

The output is a flat list of :class:`CalibrationResult` rows -- one per
``(cell, seed)`` -- which downstream code (notebooks, the publish-site
generator, the paper) can aggregate however it likes.

Privacy note: synthetic only. No mainnet UTXOs, addresses, or txids
appear here. The sweep operates entirely on simulator-emitted data.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from itertools import product
from typing import TYPE_CHECKING

from coinjoin_simulator.agents import (
    DEFAULT_MAX_MIXDEPTH,
    Maker,
    MakerFeePolicy,
    PaymentTaker,
    Utxo,
)
from coinjoin_simulator.clusterer_onchain import (
    dbscan_cluster_onchain,
    hash_bucket_cluster_onchain,
)
from coinjoin_simulator.clusterer_oracle import (
    ClusterAssignment,
    DbscanConfig,
    HashBucketConfig,
    dbscan_cluster,
    hash_bucket_cluster,
)
from coinjoin_simulator.clusterer_probing import ProbingConfig, probing_cluster
from coinjoin_simulator.world import SimResult, World, WorldConfig

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

from pathlib import Path

# ---------------------------------------------------------------------------
# Attacker enumeration
# ---------------------------------------------------------------------------


class AttackerTier(StrEnum):
    """Which clusterer + strategy to invoke per cell."""

    ORACLE_HASH = "oracle_hash"
    ORACLE_DBSCAN = "oracle_dbscan"
    ONCHAIN_HASH = "onchain_hash"
    ONCHAIN_DBSCAN = "onchain_dbscan"
    PROBING_FULL = "probing_full"  # full probe coverage, full subset-sum
    PROBING_REALISTIC = "probing_realistic"  # 80% probe, 95% resolution


_PROBING_REALISTIC = ProbingConfig(probe_success_rate=0.8, subset_sum_resolution_rate=0.95)


def _run_attacker(tier: AttackerTier, sim: SimResult, seed: int) -> ClusterAssignment:
    if tier is AttackerTier.ORACLE_HASH:
        return hash_bucket_cluster(sim, HashBucketConfig())
    if tier is AttackerTier.ORACLE_DBSCAN:
        return dbscan_cluster(sim, DbscanConfig())
    if tier is AttackerTier.ONCHAIN_HASH:
        return hash_bucket_cluster_onchain(sim)
    if tier is AttackerTier.ONCHAIN_DBSCAN:
        return dbscan_cluster_onchain(sim)
    if tier is AttackerTier.PROBING_FULL:
        return probing_cluster(
            sim,
            ProbingConfig(probe_success_rate=1.0, subset_sum_resolution_rate=1.0, seed=seed),
        )
    if tier is AttackerTier.PROBING_REALISTIC:
        return probing_cluster(
            sim,
            ProbingConfig(
                probe_success_rate=_PROBING_REALISTIC.probe_success_rate,
                subset_sum_resolution_rate=_PROBING_REALISTIC.subset_sum_resolution_rate,
                seed=seed,
            ),
        )
    raise ValueError(f"unknown attacker tier: {tier!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Population synthesis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PolicyTemplate:
    """One of K policy centers makers are drawn from."""

    cjfee_r: float
    cjfee_a_sats: int
    minsize_sats: int
    fidelity_bond_value: float
    ordertype: str = "sw0reloffer"


def _default_policy_templates(k: int, rng: random.Random) -> list[_PolicyTemplate]:
    """Generate K policy templates with distinct fee centers.

    Centers are chosen on a log-spaced grid spanning the live mainnet
    range (cjfee_r ~ 1e-6 .. 1e-3, bond value ~ 1e6 .. 1e10) so a
    well-tuned clusterer can separate them.
    """
    if k <= 0:
        raise ValueError("policy_diversity must be >= 1")
    out: list[_PolicyTemplate] = []
    for i in range(k):
        # Spread across 3 decades of cjfee_r, 4 of bond.
        frac = i / max(1, k - 1) if k > 1 else 0.5
        cjfee_r = 10 ** (-6.0 + 3.0 * frac) * (1.0 + 0.05 * (rng.random() - 0.5))
        bond = 10 ** (6.0 + 4.0 * frac) * (1.0 + 0.05 * (rng.random() - 0.5))
        out.append(
            _PolicyTemplate(
                cjfee_r=cjfee_r,
                cjfee_a_sats=200 + i * 50,
                minsize_sats=10_000 * (1 + i % 4),
                fidelity_bond_value=bond,
            ),
        )
    return out


def _build_maker(
    name: str,
    template: _PolicyTemplate,
    *,
    seed: int,
    fee_jitter_scale: float,
) -> Maker:
    base_factor = 0.1  # stock yg-pe ±10%
    factor = base_factor * fee_jitter_scale
    utxos: dict[int, list[Utxo]] = {
        m: [Utxo(utxo_id=f"u-{name}-m{m}", value_sats=100_000_000, mixdepth=m)]
        for m in range(DEFAULT_MAX_MIXDEPTH + 1)
    }
    return Maker(
        counterparty=name,
        policy=MakerFeePolicy(
            ordertype=template.ordertype,
            cjfee_r=template.cjfee_r,
            cjfee_a_sats=template.cjfee_a_sats,
            cjfee_factor=factor,
            txfee_factor=factor,
            size_factor=factor,
            txfee_contribution=100,
            minsize_sats=template.minsize_sats,
            fidelity_bond_value=template.fidelity_bond_value,
        ),
        utxos=utxos,
        rng=random.Random(seed),
    )


def _build_population(
    *,
    n_makers: int,
    policy_diversity: int,
    fee_jitter_scale: float,
    seed: int,
) -> list[Maker]:
    rng = random.Random(seed)
    k = min(policy_diversity, n_makers)
    templates = _default_policy_templates(k, rng)
    makers: list[Maker] = []
    for i in range(n_makers):
        makers.append(
            _build_maker(
                name=f"m{i:03d}",
                template=templates[i % k],
                seed=seed * 100_000 + i,
                fee_jitter_scale=fee_jitter_scale,
            ),
        )
    return makers


def _build_takers(
    *,
    n_payment_takers: int,
    makercount: int,
    seed: int,
) -> list[PaymentTaker]:
    rng = random.Random(seed)
    return [
        PaymentTaker.build(
            rng=random.Random(seed * 1_000 + i),
            recipient=f"bc1qrecipient{i:03d}",
            amount_sats=int(1_500_000 + rng.random() * 1_000_000),
            src_mixdepth=0,
            makercount=makercount,
        )
        for i in range(n_payment_takers)
    ]


# ---------------------------------------------------------------------------
# Cell + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalibrationCell:
    """One coordinate in the calibration grid."""

    n_makers: int
    policy_diversity: int
    fee_jitter_scale: float
    n_payment_takers: int
    attacker_tier: AttackerTier

    @property
    def makercount(self) -> int:
        """JoinMarket ``makercount`` parameter; capped at ``n_makers - 1``."""
        return min(3, max(1, self.n_makers - 1))


@dataclass(slots=True)
class CalibrationResult:
    """Metrics for one ``(cell, seed)`` run."""

    cell: CalibrationCell
    seed: int
    n_outputs: int
    n_clusters: int
    n_noise: int
    precision: float
    recall: float
    f1: float
    ari: float
    runtime_sec: float

    def to_jsonable(self) -> dict[str, object]:
        d = asdict(self)
        d["cell"] = {**asdict(self.cell), "attacker_tier": self.cell.attacker_tier.value}
        return d


# ---------------------------------------------------------------------------
# Single-cell run
# ---------------------------------------------------------------------------


def run_cell(cell: CalibrationCell, seed: int) -> CalibrationResult:
    """Build the world for ``cell``, run it under ``seed``, return metrics."""
    t0 = time.perf_counter()
    makers = _build_population(
        n_makers=cell.n_makers,
        policy_diversity=cell.policy_diversity,
        fee_jitter_scale=cell.fee_jitter_scale,
        seed=seed,
    )
    takers = _build_takers(
        n_payment_takers=cell.n_payment_takers,
        makercount=cell.makercount,
        seed=seed,
    )
    sim = World.from_components(
        config=WorldConfig(seed=seed),
        makers=makers,
        takers=takers,
    ).run()
    assignment = _run_attacker(cell.attacker_tier, sim, seed=seed)
    runtime = time.perf_counter() - t0
    return CalibrationResult(
        cell=cell,
        seed=seed,
        n_outputs=assignment.n_outputs,
        n_clusters=assignment.n_clusters,
        n_noise=assignment.n_noise,
        precision=assignment.precision,
        recall=assignment.recall,
        f1=assignment.f1,
        ari=assignment.ari,
        runtime_sec=runtime,
    )


# ---------------------------------------------------------------------------
# Grid sweep
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CalibrationGrid:
    """Cartesian-product specification for a sweep."""

    n_makers: Sequence[int] = field(default_factory=lambda: (5, 10, 20))
    policy_diversity: Sequence[int] = field(default_factory=lambda: (1, 3, 8))
    fee_jitter_scale: Sequence[float] = field(default_factory=lambda: (0.0, 1.0, 2.0))
    n_payment_takers: Sequence[int] = field(default_factory=lambda: (5, 20))
    attacker_tiers: Sequence[AttackerTier] = field(default_factory=lambda: tuple(AttackerTier))
    seeds: Sequence[int] = field(default_factory=lambda: (101, 202, 303))

    def cells(self) -> Iterable[CalibrationCell]:
        for n_m, k, jit, n_t, tier in product(
            self.n_makers,
            self.policy_diversity,
            self.fee_jitter_scale,
            self.n_payment_takers,
            self.attacker_tiers,
        ):
            if k > n_m:
                continue
            yield CalibrationCell(
                n_makers=n_m,
                policy_diversity=k,
                fee_jitter_scale=jit,
                n_payment_takers=n_t,
                attacker_tier=tier,
            )

    def n_runs(self) -> int:
        return sum(1 for _ in self.cells()) * len(self.seeds)


def sweep(
    grid: CalibrationGrid,
    *,
    on_progress: object | None = None,
) -> list[CalibrationResult]:
    """Run every ``(cell, seed)`` combo, returning a flat list of results."""
    rows: list[CalibrationResult] = []
    cells = list(grid.cells())
    total = len(cells) * len(grid.seeds)
    done = 0
    for cell in cells:
        for seed in grid.seeds:
            row = run_cell(cell, seed)
            rows.append(row)
            done += 1
            if callable(on_progress):
                on_progress(done, total, row)
    return rows


# ---------------------------------------------------------------------------
# Aggregation + persistence
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CellSummary:
    """Across-seed summary of a single cell."""

    cell: CalibrationCell
    n_seeds: int
    f1_mean: float
    f1_std: float
    precision_mean: float
    recall_mean: float
    ari_mean: float
    runtime_mean: float


def summarize(results: Sequence[CalibrationResult]) -> list[CellSummary]:
    """Group results by cell and compute mean/std summaries."""
    groups: dict[CalibrationCell, list[CalibrationResult]] = {}
    for r in results:
        groups.setdefault(r.cell, []).append(r)
    summaries: list[CellSummary] = []
    for cell, rows in groups.items():
        f1s = [r.f1 for r in rows]
        n = len(rows)
        f1_mean = sum(f1s) / n
        f1_var = sum((x - f1_mean) ** 2 for x in f1s) / n
        summaries.append(
            CellSummary(
                cell=cell,
                n_seeds=n,
                f1_mean=f1_mean,
                f1_std=f1_var**0.5,
                precision_mean=sum(r.precision for r in rows) / n,
                recall_mean=sum(r.recall for r in rows) / n,
                ari_mean=sum(r.ari for r in rows) / n,
                runtime_mean=sum(r.runtime_sec for r in rows) / n,
            ),
        )
    return summaries


def write_jsonl(rows: Sequence[CalibrationResult], path: Path | str) -> None:
    """Persist results as one JSON object per line for downstream tooling."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r.to_jsonable()))
            fh.write("\n")


def read_jsonl(path: Path | str) -> list[CalibrationResult]:
    """Inverse of :func:`write_jsonl`."""
    path = Path(path)
    rows: list[CalibrationResult] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            cell_d = d.pop("cell")
            cell = CalibrationCell(
                n_makers=cell_d["n_makers"],
                policy_diversity=cell_d["policy_diversity"],
                fee_jitter_scale=cell_d["fee_jitter_scale"],
                n_payment_takers=cell_d["n_payment_takers"],
                attacker_tier=AttackerTier(cell_d["attacker_tier"]),
            )
            rows.append(CalibrationResult(cell=cell, **d))
    return rows


# ---------------------------------------------------------------------------
# Smoke profile
# ---------------------------------------------------------------------------


def smoke_grid() -> CalibrationGrid:
    """A tiny grid suitable for CI / dev iteration (~30 cells, seconds)."""
    return CalibrationGrid(
        n_makers=(5, 10),
        policy_diversity=(1, 3),
        fee_jitter_scale=(0.0, 1.0),
        n_payment_takers=(3,),
        attacker_tiers=tuple(AttackerTier),
        seeds=(101, 202),
    )


__all__ = [
    "AttackerTier",
    "CalibrationCell",
    "CalibrationGrid",
    "CalibrationResult",
    "CellSummary",
    "read_jsonl",
    "run_cell",
    "smoke_grid",
    "summarize",
    "sweep",
    "write_jsonl",
]
