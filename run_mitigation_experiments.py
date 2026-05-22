#!/usr/bin/env python3
"""Run mitigation experiments for the network simulator.

Generates a comprehensive grid of experiments varying:
- makers_per_cj (8, 10)
- mitigations (baseline, slot_size_1, slot_size_3, initiation_500,
  initiation_1000, combined_light, combined_full)
- n_mixdepths (3, 5, 8)
- evil_taker_fraction (0.0 to 0.8)

Outputs JSON results to mitigation_experiments.json.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

from coinjoin_simulator.network import (
    DEFAULT_ORDERBOOK_URL,
    BondedMakerProfile,
    NetworkSimulationConfig,
    RealisticNetworkSimulator,
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
    return fetch_orderbook_snapshot(url=DEFAULT_ORDERBOOK_URL)


def _run_experiment(
    label: str,
    config: NetworkSimulationConfig,
    profiles: list[BondedMakerProfile],
    seed_offset: int,
) -> dict[str, object]:
    """Run one experiment and return the result dict with a label."""
    final_config = replace(
        config,
        random_seed=(config.random_seed or 0) + seed_offset,
    )
    sim = RealisticNetworkSimulator(config=final_config, maker_profiles=profiles)
    result = sim.run()
    d: dict[str, object] = result.to_dict()
    d["label"] = label
    d["n_makers_per_coinjoin"] = config.n_makers_per_coinjoin
    return d


def main() -> None:
    snapshot = _load_snapshot()
    profiles = extract_bonded_maker_profiles(snapshot)
    print(f"  {len(profiles)} bonded maker profiles loaded")

    evil_fractions = [0.0, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8]
    makers_per_cj_values = [8, 10]
    n_mixdepths_values = [3, 5, 8]
    n_rounds = 1000
    n_makers = 100

    # Mitigation configurations
    mitigation_configs: list[tuple[str, dict[str, object]]] = [
        ("baseline", {}),
        ("slot_size_1", {"offer_slot_size": 1}),
        ("slot_size_3", {"offer_slot_size": 3}),
        ("initiation_500", {"initiation_fee_sats": 500}),
        ("initiation_1000", {"initiation_fee_sats": 1000}),
        (
            "combined_light",
            {
                "offer_slot_size": 3,
                "initiation_fee_sats": 500,
            },
        ),
        (
            "combined_full",
            {
                "offer_slot_size": 1,
                "initiation_fee_sats": 1000,
            },
        ),
    ]

    all_results: list[dict[str, object]] = []
    total_experiments = (
        len(evil_fractions) * len(makers_per_cj_values) * len(mitigation_configs)
        + len(evil_fractions) * len(n_mixdepths_values) * 2  # mixdepth experiments
    )
    print(f"\nRunning {total_experiments} experiments...")
    start = time.time()
    idx = 0

    # Phase 1: mitigation experiments (with default 5 mixdepths)
    for mpc in makers_per_cj_values:
        for mit_label, mit_kwargs in mitigation_configs:
            for evil_frac in evil_fractions:
                label = f"mpc{mpc}_{mit_label}_evil{evil_frac:.1f}"
                config = NetworkSimulationConfig(
                    n_makers=n_makers,
                    n_rounds=n_rounds,
                    n_makers_per_coinjoin=mpc,
                    evil_taker_fraction=evil_frac,
                    probes_per_evil_taker=5,
                    random_seed=42,
                    **mit_kwargs,  # type: ignore[arg-type]
                )
                result = _run_experiment(label, config, profiles, seed_offset=idx)
                all_results.append(result)
                idx += 1

                if idx % 10 == 0:
                    elapsed = time.time() - start
                    rate = idx / elapsed if elapsed > 0 else 0
                    remaining = (total_experiments - idx) / rate if rate > 0 else 0
                    print(
                        f"  [{idx}/{total_experiments}] "
                        f"{elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining"
                    )

    # Phase 2: mixdepth sensitivity experiments (baseline + combined_full only)
    mixdepth_configs = [
        ("baseline", {}),
        (
            "combined_full",
            {
                "offer_slot_size": 1,
                "initiation_fee_sats": 1000,
            },
        ),
    ]
    for n_depths in n_mixdepths_values:
        for mit_label, mit_kwargs in mixdepth_configs:
            for evil_frac in evil_fractions:
                label = f"depths{n_depths}_{mit_label}_evil{evil_frac:.1f}_mpc8"
                config = NetworkSimulationConfig(
                    n_makers=n_makers,
                    n_rounds=n_rounds,
                    n_makers_per_coinjoin=8,
                    evil_taker_fraction=evil_frac,
                    probes_per_evil_taker=5,
                    n_mixdepths=n_depths,
                    random_seed=42,
                    **mit_kwargs,  # type: ignore[arg-type]
                )
                result = _run_experiment(label, config, profiles, seed_offset=idx)
                all_results.append(result)
                idx += 1

                if idx % 10 == 0:
                    elapsed = time.time() - start
                    rate = idx / elapsed if elapsed > 0 else 0
                    remaining = (total_experiments - idx) / rate if rate > 0 else 0
                    print(
                        f"  [{idx}/{total_experiments}] "
                        f"{elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining"
                    )

    elapsed = time.time() - start
    print(f"\nDone: {len(all_results)} experiments in {elapsed:.1f}s")

    output_path = "mitigation_experiments.json"
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"Results saved to {output_path}")


if __name__ == "__main__":
    main()
