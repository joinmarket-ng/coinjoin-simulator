#!/usr/bin/env python3
"""Long-run policy study for probing attacks and recovery behavior."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

import numpy as np

from coinjoin_simulator.network import (
    DEFAULT_ORDERBOOK_URL,
    NetworkSimulationConfig,
    RealisticNetworkSimulator,
    extract_bonded_maker_profiles,
    fetch_orderbook_snapshot,
)


def _initial_network_stats(sim: RealisticNetworkSimulator) -> dict[str, float | int]:
    total_live_utxos = 0
    total_funds_sats = 0
    for maker in sim.makers:
        for depth in maker.mixdepths:
            total_live_utxos += len(depth)
            total_funds_sats += sum(u.value_sats for u in depth)
    return {
        "initial_total_live_utxos": total_live_utxos,
        "initial_total_funds_sats": total_funds_sats,
        "initial_avg_utxos_per_maker": total_live_utxos / len(sim.makers),
        "initial_avg_funds_per_maker_sats": total_funds_sats / len(sim.makers),
    }


def _known_live_utxo_fraction(sim: RealisticNetworkSimulator) -> float:
    total_live = 0
    known_live = 0
    for maker in sim.makers:
        live_ids: set[str] = set()
        for depth in maker.mixdepths:
            for utxo in depth:
                live_ids.add(utxo.utxo_id)
        total_live += len(live_ids)
        known_live += len(sim.known_utxos_by_maker[maker.maker_id] & live_ids)
    return (known_live / total_live) if total_live > 0 else 0.0


def _run_single(
    config: NetworkSimulationConfig,
    profiles: list,
    seed_offset: int,
) -> dict[str, object]:
    cfg = replace(config, random_seed=(config.random_seed or 0) + seed_offset)
    sim = RealisticNetworkSimulator(cfg, profiles)
    init_stats = _initial_network_stats(sim)
    result = sim.run().to_dict()
    result.update(init_stats)
    result["honest_volume_btc"] = float(result["total_honest_volume_sats"]) / 100_000_000
    return result


def _run_recovery_timeline(
    config: NetworkSimulationConfig,
    profiles: list,
    attack_rounds: int,
    recovery_rounds: int,
    attack_evil_fraction: float,
    sample_every: int = 50,
    recent_window: int = 200,
) -> dict[str, object]:
    sim = RealisticNetworkSimulator(config, profiles)

    taker_anon_history: list[int] = []
    attack_anon_history: list[int] = []
    recovery_anon_history: list[int] = []
    timeline: list[dict[str, float | int | str]] = []
    total_rounds = attack_rounds + recovery_rounds
    attack_probe_actions = 0
    attack_probed_utxos = 0
    attack_end_known_live: float | None = None

    for round_idx in range(total_rounds):
        phase = "attack" if round_idx < attack_rounds else "recovery"
        evil_fraction = attack_evil_fraction if phase == "attack" else 0.0

        if sim.rng.random() < evil_fraction:
            targets, probed = sim._run_evil_round()
            if phase == "attack":
                attack_probe_actions += targets
                attack_probed_utxos += probed
        else:
            rec = sim.simulate_single_honest_coinjoin(round_index=round_idx)
            if rec is not None:
                taker_anon_history.append(rec.taker_anon_set)
                if phase == "attack":
                    attack_anon_history.append(rec.taker_anon_set)
                else:
                    recovery_anon_history.append(rec.taker_anon_set)

        if round_idx == attack_rounds - 1:
            attack_end_known_live = _known_live_utxo_fraction(sim)

        if (round_idx + 1) % sample_every == 0:
            recent = taker_anon_history[-recent_window:]
            if recent:
                recent_deanon = float(np.mean(np.asarray(recent) <= 1))
                recent_mean_anon = float(np.mean(recent))
            else:
                recent_deanon = 0.0
                recent_mean_anon = 0.0

            timeline.append(
                {
                    "round": round_idx + 1,
                    "phase": phase,
                    "known_live_utxo_fraction": _known_live_utxo_fraction(sim),
                    "recent_taker_deanon_fraction": recent_deanon,
                    "recent_mean_taker_anon": recent_mean_anon,
                    "cumulative_probing_cost_sats": sim._total_probing_cost_sats,
                }
            )

    # Recovery milestones from attack->recovery boundary onward
    known_live_recovery_round: int | None = None
    deanon_recovery_round: int | None = None
    for item in timeline:
        if int(item["round"]) <= attack_rounds:
            continue
        if known_live_recovery_round is None and float(item["known_live_utxo_fraction"]) <= 0.10:
            known_live_recovery_round = int(item["round"])
        if deanon_recovery_round is None and float(item["recent_taker_deanon_fraction"]) <= 0.05:
            deanon_recovery_round = int(item["round"])

    attack_deanon_fraction = (
        float(np.mean(np.asarray(attack_anon_history) <= 1)) if attack_anon_history else None
    )
    attack_mean_anon = float(np.mean(attack_anon_history)) if attack_anon_history else None
    first_120 = recovery_anon_history[:120]
    post120_deanon = float(np.mean(np.asarray(first_120) <= 1)) if first_120 else None
    post120_mean_anon = float(np.mean(first_120)) if first_120 else None

    return {
        "attack_rounds": attack_rounds,
        "recovery_rounds": recovery_rounds,
        "attack_evil_fraction": attack_evil_fraction,
        "sample_every": sample_every,
        "timeline": timeline,
        "recovery_round_known_live_le_10pct": known_live_recovery_round,
        "recovery_round_recent_deanon_le_5pct": deanon_recovery_round,
        "attack_probe_actions": attack_probe_actions,
        "attack_probed_utxos": attack_probed_utxos,
        "upfront_probe_actions": sim._preprobe_actions,
        "upfront_probed_utxos": sim._preprobe_utxos,
        "attack_end_known_live_utxo_fraction": attack_end_known_live,
        "attack_honest_cj_count": len(attack_anon_history),
        "attack_deanon_fraction": attack_deanon_fraction,
        "attack_mean_anon": attack_mean_anon,
        "post_attack_first120_honest_deanon": post120_deanon,
        "post_attack_first120_honest_mean_anon": post120_mean_anon,
        "upfront_fee_cost_sats": sim._preprobe_actions * config.initiation_fee_sats,
        "attack_fee_cost_sats": attack_probe_actions * config.initiation_fee_sats,
        "total_fee_cost_sats": sim._total_probing_cost_sats,
    }


def _baseline_policy(rounds: int, fee_sats: int, seed: int) -> NetworkSimulationConfig:
    return NetworkSimulationConfig(
        n_makers=100,
        n_rounds=rounds,
        n_makers_per_coinjoin=8,
        n_mixdepths=5,
        pre_probe_all_makers=True,
        wallet_init_mode="seeded_depth0",
        merge_algorithm="default",
        disclosed_input_policy="ignore",
        max_utxos_per_offer=None,
        sticky_disclosed_utxos=False,
        flagged_utxo_isolation=False,
        initiation_fee_sats=fee_sats,
        random_seed=seed,
    )


def _recommended_policy(rounds: int, fee_sats: int, seed: int) -> NetworkSimulationConfig:
    return NetworkSimulationConfig.recommended_policy_defaults(
        n_makers=100,
        n_rounds=rounds,
        pre_probe_all_makers=True,
        initiation_fee_sats=fee_sats,
        random_seed=seed,
    )


def main() -> None:
    snapshot = fetch_orderbook_snapshot(DEFAULT_ORDERBOOK_URL)
    profiles = extract_bonded_maker_profiles(snapshot)

    sustained_rounds = 5000
    sustained_evil = [0.1, 0.2, 0.4, 0.6]
    fee_levels = [0, 500]

    sustained_rows: list[dict[str, object]] = []
    seed_offset = 0
    for policy_name, policy_builder in (
        ("baseline", _baseline_policy),
        ("recommended", _recommended_policy),
    ):
        for fee in fee_levels:
            for evil in sustained_evil:
                cfg = policy_builder(sustained_rounds, fee, 100)
                cfg = replace(cfg, evil_taker_fraction=evil)
                row = _run_single(cfg, profiles, seed_offset)
                row["policy_name"] = policy_name
                row["scenario"] = "sustained_attack"
                sustained_rows.append(row)
                seed_offset += 1

    # Impact threshold: first evil fraction where sustained deanon >= 30%
    threshold_rows: list[dict[str, object]] = []
    for policy_name in ("baseline", "recommended"):
        for fee in fee_levels:
            subset = [
                r
                for r in sustained_rows
                if r["policy_name"] == policy_name and int(r["initiation_fee_sats"]) == fee
            ]
            subset = sorted(subset, key=lambda x: float(x["evil_taker_fraction"]))
            crossing = next(
                (r for r in subset if float(r["taker_deanonymized_fraction"]) >= 0.30),
                None,
            )
            threshold_rows.append(
                {
                    "policy_name": policy_name,
                    "initiation_fee_sats": fee,
                    "deanon_threshold": 0.30,
                    "crossing_evil_fraction": (
                        None if crossing is None else float(crossing["evil_taker_fraction"])
                    ),
                    "crossing_total_probing_cost_sats": (
                        None if crossing is None else int(crossing["total_probing_cost_sats"])
                    ),
                    "crossing_cost_to_volume_ratio": (
                        None
                        if crossing is None
                        else float(crossing["probing_cost_to_volume_ratio"])
                    ),
                }
            )

    # Sensitivity to initial UTXO count and total funds
    sensitivity_rows: list[dict[str, object]] = []
    sensitivity_specs = [
        {
            "name": "few_utxos_low_funds",
            "wallet_init_mode": "seeded_depth0",
            "seed_depth0_min_initial_utxos": 1,
            "seed_depth0_max_initial_utxos": 2,
            "total_balance_ratio_mean": 3.6,
            "total_balance_ratio_cap": 3.9,
        },
        {
            "name": "few_utxos_high_funds",
            "wallet_init_mode": "seeded_depth0",
            "seed_depth0_min_initial_utxos": 1,
            "seed_depth0_max_initial_utxos": 2,
            "total_balance_ratio_mean": 4.8,
            "total_balance_ratio_cap": 5.2,
        },
        {
            "name": "many_utxos_low_funds",
            "wallet_init_mode": "distributed",
            "total_balance_ratio_mean": 3.6,
            "total_balance_ratio_cap": 3.9,
        },
        {
            "name": "many_utxos_high_funds",
            "wallet_init_mode": "distributed",
            "total_balance_ratio_mean": 4.8,
            "total_balance_ratio_cap": 5.2,
        },
    ]
    for spec in sensitivity_specs:
        for fee in fee_levels:
            cfg_kwargs = {k: v for k, v in spec.items() if k != "name"}
            cfg = NetworkSimulationConfig.recommended_policy_defaults(
                n_makers=100,
                n_rounds=3000,
                pre_probe_all_makers=True,
                initiation_fee_sats=fee,
                evil_taker_fraction=0.4,
                probes_per_evil_taker=5,
                random_seed=600,
                **cfg_kwargs,
            )
            row = _run_single(cfg, profiles, seed_offset)
            row["scenario"] = "utxo_funds_sensitivity"
            row["sensitivity_name"] = spec["name"]
            sensitivity_rows.append(row)
            seed_offset += 1

    # Recovery study after attack pulse
    recovery_runs: list[dict[str, object]] = []
    for policy_name, policy_builder in (
        ("baseline", _baseline_policy),
        ("recommended", _recommended_policy),
    ):
        for fee in fee_levels:
            for attack_evil in (0.4, 0.6):
                cfg = policy_builder(rounds=1, fee_sats=fee, seed=700)
                cfg = replace(cfg, evil_taker_fraction=attack_evil)
                timeline = _run_recovery_timeline(
                    config=cfg,
                    profiles=profiles,
                    attack_rounds=1200,
                    recovery_rounds=3200,
                    attack_evil_fraction=attack_evil,
                    sample_every=50,
                )
                timeline["policy_name"] = policy_name
                timeline["initiation_fee_sats"] = fee
                recovery_runs.append(timeline)

    # Extreme sustained attack pressure (evil close to 1.0)
    extreme_rows: list[dict[str, object]] = []
    for policy_name, policy_builder in (
        ("baseline", _baseline_policy),
        ("recommended", _recommended_policy),
    ):
        for fee in fee_levels:
            for attack_evil in (0.9, 1.0):
                cfg = policy_builder(rounds=1, fee_sats=fee, seed=800)
                cfg = replace(cfg, evil_taker_fraction=attack_evil)
                item = _run_recovery_timeline(
                    config=cfg,
                    profiles=profiles,
                    attack_rounds=1800,
                    recovery_rounds=4200,
                    attack_evil_fraction=attack_evil,
                    sample_every=25,
                )
                item["policy_name"] = policy_name
                item["initiation_fee_sats"] = fee
                extreme_rows.append(item)

    payload = {
        "orderbook_url": DEFAULT_ORDERBOOK_URL,
        "n_bonded_profiles": len(profiles),
        "recommended_defaults": asdict(NetworkSimulationConfig.recommended_policy_defaults()),
        "sustained_attack_results": sustained_rows,
        "substantial_impact_thresholds": threshold_rows,
        "utxo_funds_sensitivity_results": sensitivity_rows,
        "recovery_timelines": recovery_runs,
        "extreme_attack_results": extreme_rows,
    }

    with open("longrun_policy_results.json", "w") as f:
        json.dump(payload, f, indent=2, default=str)

    print("Wrote longrun_policy_results.json")


if __name__ == "__main__":
    main()
