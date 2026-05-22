#!/usr/bin/env python3
"""Sustained attack daily cost study.

Models a realistic attacker scenario where:
- Honest CoinJoins happen at a fixed rate (100/day).
- The attacker probes ALL makers simultaneously per probe round.
- Each probe pays only the initiation fee (no CJ completion).
- Probes and honest CJs are interleaved within each day.

Experiments:
1. Baseline vs recommended policy at different probe intensities.
2. Daily cost at different fee levels (0, 250, 500, 1000 sats).
3. Attack + recovery: 14 days attack, 30 days recovery.
4. Cost-effectiveness table in sats/BTC only.
5. High-pressure regime where probes/day > honest CJs/day.
"""

from __future__ import annotations

import json
from typing import Any

from coinjoin_simulator.network import (
    DEFAULT_ORDERBOOK_URL,
    NetworkSimulationConfig,
    RealisticNetworkSimulator,
    SustainedAttackConfig,
    extract_bonded_maker_profiles,
    fetch_orderbook_snapshot,
    load_orderbook_snapshot,
)


ORDERBOOK_CACHE_PATH = "data/orderbook_live_snapshot.json"


def _load_snapshot() -> dict[str, object]:
    import os

    if os.path.exists(ORDERBOOK_CACHE_PATH):
        print(f"Loading cached orderbook from {ORDERBOOK_CACHE_PATH}...")
        return load_orderbook_snapshot(ORDERBOOK_CACHE_PATH)
    print("Fetching live orderbook...")
    return fetch_orderbook_snapshot(DEFAULT_ORDERBOOK_URL)


def _baseline_config(n_makers: int, fee_sats: int, seed: int) -> NetworkSimulationConfig:
    return NetworkSimulationConfig(
        n_makers=n_makers,
        n_rounds=1,  # Not used by run_sustained_attack
        n_makers_per_coinjoin=8,
        n_mixdepths=5,
        initiation_fee_sats=fee_sats,
        wallet_init_mode="seeded_depth0",
        random_seed=seed,
    )


def _recommended_config(n_makers: int, fee_sats: int, seed: int) -> NetworkSimulationConfig:
    return NetworkSimulationConfig.recommended_policy_defaults(
        n_makers=n_makers,
        n_rounds=1,
        initiation_fee_sats=fee_sats,
        random_seed=seed,
    )


def _run_one(
    sim_config: NetworkSimulationConfig,
    attack_config: SustainedAttackConfig,
    profiles: list[Any],
    policy_label: str,
) -> dict[str, object]:
    sim = RealisticNetworkSimulator(config=sim_config, maker_profiles=profiles)
    result = sim.run_sustained_attack(attack_config)
    d = result.to_dict()
    d["policy_label"] = policy_label
    return d


def main() -> None:
    snapshot = _load_snapshot()
    profiles = extract_bonded_maker_profiles(snapshot)
    print(f"  {len(profiles)} bonded makers found")

    n_makers = 100
    honest_cjs_per_day = 100
    n_seeds_intensity = 5
    n_seeds_fee = 3

    # Per-policy fee defaults: baseline is truly unmitigated (fee=0); recommended
    # applies the cost lever as one of its layers (fee=500).
    policy_fee_defaults: dict[str, int] = {"baseline": 0, "recommended": 500}

    # -----------------------------------------------------------------------
    # Experiment 1: Probe intensity sweep (baseline vs recommended)
    # 14 days attack, varying probes/day. Baseline runs at fee=0,
    # recommended at fee=500. Multiple seeds give CI bands in the report.
    # -----------------------------------------------------------------------
    print("\nExperiment 1: Probe intensity sweep...")
    intensity_results: list[dict[str, object]] = []
    probe_rates = [1, 2, 5, 10, 20, 50]

    for probes_per_day in probe_rates:
        for label, builder in (
            ("baseline", _baseline_config),
            ("recommended", _recommended_config),
        ):
            fee_for_policy = policy_fee_defaults[label]
            for seed_idx in range(n_seeds_intensity):
                cfg = builder(n_makers, fee_for_policy, seed=1000 + seed_idx * 23)
                attack_cfg = SustainedAttackConfig(
                    n_days=30,
                    honest_cjs_per_day=honest_cjs_per_day,
                    probes_per_day=probes_per_day,
                    attack_start_day=0,
                    attack_end_day=14,
                )
                row = _run_one(cfg, attack_cfg, profiles, label)
                row["experiment"] = "intensity_sweep"
                row["seed_index"] = seed_idx
                intensity_results.append(row)
                snap_last_attack = [s for s in row["daily_snapshots"] if s["phase"] == "attack"]
                deanon = (
                    snap_last_attack[-1]["taker_deanonymized_fraction"]
                    if snap_last_attack
                    else 0
                )
                cost = row["total_probe_cost_sats"]
                print(
                    f"  {label:12s} probes/day={probes_per_day:3d} seed={seed_idx} "
                    f"deanon={deanon:.3f}  cost={cost} sats"
                )

    # -----------------------------------------------------------------------
    # Experiment 2: Fee level impact on daily cost
    # Fixed probe intensity (10/day), sweep fee levels
    # -----------------------------------------------------------------------
    print("\nExperiment 2: Fee level impact...")
    fee_results: list[dict[str, object]] = []
    fee_levels = [0, 100, 250, 500, 1000, 2000]
    probes_per_day = 10

    for fee in fee_levels:
        for label, builder in (
            ("baseline", _baseline_config),
            ("recommended", _recommended_config),
        ):
            for seed_idx in range(n_seeds_fee):
                cfg = builder(n_makers, fee, seed=2000 + seed_idx * 31)
                attack_cfg = SustainedAttackConfig(
                    n_days=30,
                    honest_cjs_per_day=honest_cjs_per_day,
                    probes_per_day=probes_per_day,
                    attack_start_day=0,
                    attack_end_day=14,
                )
                row = _run_one(cfg, attack_cfg, profiles, label)
                row["experiment"] = "fee_sweep"
                row["seed_index"] = seed_idx
                fee_results.append(row)
                daily_cost_btc = row["attack_daily_cost_btc"]
                total_btc = row["total_probe_cost_btc"]
                print(
                    f"  {label:12s} fee={fee:5d} seed={seed_idx} "
                    f"daily={daily_cost_btc:.6f} BTC  "
                    f"total={total_btc:.6f} BTC"
                )

    # -----------------------------------------------------------------------
    # Experiment 3: Attack + recovery timeline (detailed daily view)
    # 14-day attack at different intensities, then 30-day recovery
    # -----------------------------------------------------------------------
    print("\nExperiment 3: Attack + recovery timelines...")
    recovery_results: list[dict[str, object]] = []
    recovery_probes = [5, 20]

    for probes_per_day in recovery_probes:
        for label, builder in (
            ("baseline", _baseline_config),
            ("recommended", _recommended_config),
        ):
            fee_for_policy = policy_fee_defaults[label]
            cfg = builder(n_makers, fee_for_policy, seed=3000)
            attack_cfg = SustainedAttackConfig(
                n_days=44,
                honest_cjs_per_day=honest_cjs_per_day,
                probes_per_day=probes_per_day,
                attack_start_day=0,
                attack_end_day=14,
            )
            row = _run_one(cfg, attack_cfg, profiles, label)
            row["experiment"] = "recovery_timeline"
            recovery_results.append(row)
            rec_deanon = row.get("recovery_day_deanon_le_5pct")
            rec_known = row.get("recovery_day_known_live_le_10pct")
            print(
                f"  {label:12s} probes/day={probes_per_day:3d}  "
                f"recovery_deanon_day={rec_deanon}  "
                f"recovery_known_live_day={rec_known}"
            )

    # -----------------------------------------------------------------------
    # Experiment 4: Cost-effectiveness table
    # How much the attacker spends per day at different intensities.
    # -----------------------------------------------------------------------
    print("\nExperiment 4: Cost-effectiveness summary...")
    cost_effectiveness: list[dict[str, object]] = []
    for probes_per_day in [1, 5, 10, 20, 50]:
        for fee in [250, 500, 1000]:
            daily_cost_sats = probes_per_day * n_makers * fee
            daily_cost_btc = daily_cost_sats / 100_000_000
            cost_effectiveness.append(
                {
                    "probes_per_day": probes_per_day,
                    "initiation_fee_sats": fee,
                    "n_makers": n_makers,
                    "daily_cost_sats": daily_cost_sats,
                    "daily_cost_btc": daily_cost_btc,
                    "14day_cost_btc": daily_cost_btc * 14,
                    "30day_cost_btc": daily_cost_btc * 30,
                }
            )

    # -----------------------------------------------------------------------
    # Experiment 5: Maker count sensitivity
    # Same probe intensity, different maker counts
    # -----------------------------------------------------------------------
    print("\nExperiment 5: Maker count sensitivity...")
    maker_count_results: list[dict[str, object]] = []
    maker_counts = [50, 100, 200]

    for n_mk in maker_counts:
        for label, builder in (
            ("baseline", _baseline_config),
            ("recommended", _recommended_config),
        ):
            fee_for_policy = policy_fee_defaults[label]
            cfg = builder(n_mk, fee_for_policy, seed=5000)
            attack_cfg = SustainedAttackConfig(
                n_days=30,
                honest_cjs_per_day=honest_cjs_per_day,
                probes_per_day=10,
                attack_start_day=0,
                attack_end_day=14,
            )
            row = _run_one(cfg, attack_cfg, profiles, label)
            row["experiment"] = "maker_count_sensitivity"
            maker_count_results.append(row)
            print(
                f"  {label:12s} n_makers={n_mk:4d}  "
                f"deanon={row['attack_taker_deanonymized_fraction']:.3f}  "
                f"daily_cost={row['attack_daily_cost_btc']:.6f} BTC"
            )

    # -----------------------------------------------------------------------
    # Experiment 6: High probe pressure (> honest CJs/day)
    # Privacy impact and attack cost when probing outpaces honest joins.
    # -----------------------------------------------------------------------
    print("\nExperiment 6: High probe pressure (probes/day > honest CJs/day)...")
    high_pressure_results: list[dict[str, object]] = []
    high_probe_rates = [120, 150, 200, 300, 500]

    for probes_per_day in high_probe_rates:
        for label, builder in (
            ("baseline", _baseline_config),
            ("recommended", _recommended_config),
        ):
            fee_for_policy = policy_fee_defaults[label]
            cfg = builder(n_makers, fee_for_policy, seed=6000)
            attack_cfg = SustainedAttackConfig(
                n_days=30,
                honest_cjs_per_day=honest_cjs_per_day,
                probes_per_day=probes_per_day,
                attack_start_day=0,
                attack_end_day=14,
            )
            row = _run_one(cfg, attack_cfg, profiles, label)
            row["experiment"] = "high_probe_pressure"
            row["probe_to_honest_ratio"] = probes_per_day / honest_cjs_per_day
            high_pressure_results.append(row)

            print(
                f"  {label:12s} probes/day={probes_per_day:4d}  "
                f"ratio={row['probe_to_honest_ratio']:.2f}  "
                f"deanon={row['attack_taker_deanonymized_fraction']:.3f}  "
                f"daily_cost={row['attack_daily_cost_btc']:.6f} BTC"
            )

    # -----------------------------------------------------------------------
    # Save all results
    # -----------------------------------------------------------------------
    payload = {
        "orderbook_url": DEFAULT_ORDERBOOK_URL,
        "n_bonded_profiles": len(profiles),
        "honest_cjs_per_day": honest_cjs_per_day,
        "intensity_sweep": intensity_results,
        "fee_sweep": fee_results,
        "recovery_timelines": recovery_results,
        "cost_effectiveness_table": cost_effectiveness,
        "maker_count_sensitivity": maker_count_results,
        "high_probe_pressure": high_pressure_results,
    }

    with open("daily_cost_study_results.json", "w") as f:
        json.dump(payload, f, indent=2, default=str)

    print("\nWrote daily_cost_study_results.json")


if __name__ == "__main__":
    main()
