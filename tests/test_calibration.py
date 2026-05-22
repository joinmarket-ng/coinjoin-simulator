"""Tests for the calibration sweep harness."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from coinjoin_simulator.calibration import (
    AttackerTier,
    CalibrationCell,
    CalibrationGrid,
    CalibrationResult,
    CellSummary,
    read_jsonl,
    run_cell,
    smoke_grid,
    summarize,
    sweep,
    write_jsonl,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Single cell
# ---------------------------------------------------------------------------


def test_run_cell_returns_metrics_in_unit_interval() -> None:
    cell = CalibrationCell(
        n_makers=4,
        policy_diversity=2,
        fee_jitter_scale=1.0,
        n_payment_takers=2,
        attacker_tier=AttackerTier.ORACLE_HASH,
    )
    r = run_cell(cell, seed=123)
    assert isinstance(r, CalibrationResult)
    assert r.n_outputs > 0
    for v in (r.precision, r.recall, r.f1):
        assert 0.0 <= v <= 1.0
    assert -1.0 <= r.ari <= 1.0  # ARI lower bound is technically -0.5
    assert r.runtime_sec > 0.0


def test_run_cell_dispatches_each_attacker_tier() -> None:
    base = CalibrationCell(
        n_makers=4,
        policy_diversity=4,
        fee_jitter_scale=0.0,
        n_payment_takers=2,
        attacker_tier=AttackerTier.ORACLE_HASH,
    )
    for tier in AttackerTier:
        cell = CalibrationCell(
            n_makers=base.n_makers,
            policy_diversity=base.policy_diversity,
            fee_jitter_scale=base.fee_jitter_scale,
            n_payment_takers=base.n_payment_takers,
            attacker_tier=tier,
        )
        r = run_cell(cell, seed=7)
        assert r.cell.attacker_tier is tier


# ---------------------------------------------------------------------------
# Probing-full upper-bounds the other tiers
# ---------------------------------------------------------------------------


def test_probing_full_dominates_oracle_and_onchain() -> None:
    """The probing-full attacker has direct input-side labels, so its F1
    should not be beaten by any fingerprinting attacker on the same sim."""
    seed = 42
    base_args: dict[str, object] = {
        "n_makers": 6,
        "policy_diversity": 1,  # all makers identical -> oracle can't separate
        "fee_jitter_scale": 1.0,
        "n_payment_takers": 4,
    }
    rows = {
        tier: run_cell(CalibrationCell(attacker_tier=tier, **base_args), seed=seed)  # type: ignore[arg-type]
        for tier in AttackerTier
    }
    probe = rows[AttackerTier.PROBING_FULL]
    # On policy_diversity=1, fingerprinting attackers cannot tell makers
    # apart -> F1 << 1. Probing labels every output by counterparty -> F1 == 1.
    assert probe.f1 == pytest.approx(1.0)
    for tier, r in rows.items():
        if tier is AttackerTier.PROBING_FULL:
            continue
        assert probe.f1 >= r.f1 - 1e-9


# ---------------------------------------------------------------------------
# Grid invariants
# ---------------------------------------------------------------------------


def test_grid_skips_cells_with_diversity_above_n_makers() -> None:
    grid = CalibrationGrid(
        n_makers=(3,),
        policy_diversity=(1, 5),  # 5 > 3 must be skipped
        fee_jitter_scale=(0.0,),
        n_payment_takers=(1,),
        attacker_tiers=(AttackerTier.ORACLE_HASH,),
        seeds=(1,),
    )
    cells = list(grid.cells())
    assert len(cells) == 1
    assert cells[0].policy_diversity == 1


def test_smoke_grid_completes_quickly() -> None:
    grid = smoke_grid()
    rows = sweep(grid)
    # 48 cells * 2 seeds = 96 runs.
    assert len(rows) == grid.n_runs() == 96
    # Every row carries finite metrics.
    for r in rows:
        assert r.n_outputs >= 0
        assert 0.0 <= r.f1 <= 1.0


def test_sweep_progress_callback_invoked_for_each_run() -> None:
    grid = CalibrationGrid(
        n_makers=(3,),
        policy_diversity=(1,),
        fee_jitter_scale=(0.0,),
        n_payment_takers=(1,),
        attacker_tiers=(AttackerTier.ORACLE_HASH,),
        seeds=(1, 2, 3),
    )
    seen: list[tuple[int, int]] = []
    sweep(grid, on_progress=lambda done, total, _row: seen.append((done, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def test_summarize_aggregates_seeds_per_cell() -> None:
    grid = CalibrationGrid(
        n_makers=(4,),
        policy_diversity=(2,),
        fee_jitter_scale=(0.0,),
        n_payment_takers=(2,),
        attacker_tiers=(AttackerTier.ORACLE_HASH, AttackerTier.PROBING_FULL),
        seeds=(1, 2, 3),
    )
    rows = sweep(grid)
    summaries = summarize(rows)
    assert len(summaries) == 2  # two cells (one per tier)
    for s in summaries:
        assert isinstance(s, CellSummary)
        assert s.n_seeds == 3
        assert 0.0 <= s.f1_mean <= 1.0
        assert s.f1_std >= 0.0


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_jsonl_roundtrip(tmp_path: Path) -> None:
    grid = CalibrationGrid(
        n_makers=(4,),
        policy_diversity=(2,),
        fee_jitter_scale=(0.0,),
        n_payment_takers=(2,),
        attacker_tiers=(AttackerTier.ORACLE_HASH,),
        seeds=(1, 2),
    )
    rows = sweep(grid)
    out = tmp_path / "subdir" / "out.jsonl"
    write_jsonl(rows, out)
    assert out.exists()
    loaded = read_jsonl(out)
    assert len(loaded) == len(rows)
    for orig, restored in zip(rows, loaded, strict=True):
        assert restored.cell == orig.cell
        assert restored.seed == orig.seed
        assert restored.f1 == pytest.approx(orig.f1)


def test_to_jsonable_uses_string_for_attacker_tier() -> None:
    cell = CalibrationCell(
        n_makers=2,
        policy_diversity=1,
        fee_jitter_scale=0.0,
        n_payment_takers=1,
        attacker_tier=AttackerTier.ORACLE_DBSCAN,
    )
    r = run_cell(cell, seed=1)
    blob = json.dumps(r.to_jsonable())
    assert '"attacker_tier": "oracle_dbscan"' in blob
