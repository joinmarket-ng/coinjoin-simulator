"""CLI entry point for running CoinJoin privacy simulations.

Usage:
    coinjoin-sim run [--scenario NAME] [--output DIR]
    coinjoin-sim list-scenarios
    coinjoin-sim benchmark [--output DIR]
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .anonymity import (
    compute_change_anonymity,
    compute_naive_anonymity,
    compute_post_spend_anonymity,
    compute_sybil_aware_anonymity,
)
from .network import (
    DEFAULT_ORDERBOOK_URL,
    NetworkSimulationConfig,
    run_live_network_sweep,
)
from .publish_site import generate_publish_site
from .report import generate_report
from .role_id import (
    batch_analyze_role_identification,
)
from .scenarios import ALL_SCENARIOS
from .surveillance import SurveillanceSimulator
from .sybil import analyze_sybil_resistance_sweep
from .transaction import TransactionSimulator

if TYPE_CHECKING:
    from .models import SimulationConfig


def _parse_float_list(raw: str) -> list[float]:
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("expected a comma-separated list of floats")

    parsed: list[float] = []
    for value in values:
        try:
            parsed.append(float(value))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid float value: {value}") from exc
    return parsed


def run_scenario(name: str, config: SimulationConfig, verbose: bool = True) -> dict[str, object]:
    """Run a complete analysis for a given scenario.

    Returns a dictionary of all results suitable for JSON serialization.
    """
    if verbose:
        print(f"\n{'=' * 60}")
        print(f"  Scenario: {name}")
        print(f"{'=' * 60}")
        print(f"  Makers: {config.n_makers_total} total, {config.n_makers_per_cj} per CJ")
        print(f"  Sybil: {config.n_sybil_entities} entities x {config.sybil_makers_per_entity}")
        print(f"  Bonds: {config.use_fidelity_bonds}, Algorithm: {config.selection_algorithm}")
        print(f"  CoinJoins: {config.n_coinjoins}")

    start = time.time()
    sim = TransactionSimulator(config)
    txs = sim.simulate_chain()
    sim_time = time.time() - start

    if verbose:
        print(f"  Simulation: {sim_time:.2f}s for {len(txs)} transactions")

    # 1. Naive anonymity
    naive_anon_sets: list[int] = []
    for tx in txs:
        metrics = compute_naive_anonymity(tx)
        for m in metrics.values():
            naive_anon_sets.append(m.naive_anon_set)

    # 2. Change output anonymity
    change_entropies: list[float] = []
    unique_changes: int = 0
    total_changes: int = 0
    for tx in txs:
        metrics = compute_change_anonymity(tx)
        for m in metrics.values():
            change_entropies.append(m.entropy_bits)
            total_changes += 1
            if m.is_uniquely_mapped:
                unique_changes += 1

    # 3. Sybil-aware anonymity
    sybil_anon_sets: list[float] = []
    if config.n_sybil_entities > 0:
        for tx in txs:
            metrics = compute_sybil_aware_anonymity(tx)
            for m in metrics.values():
                sybil_anon_sets.append(m.effective_anon_set)

    # 4. Role identification
    role_results = batch_analyze_role_identification(txs)
    role_results_swap = batch_analyze_role_identification(txs, with_swap_mitigation=True)

    # 5. Post-spend analysis
    post_spend_entropies: list[float] = []
    for tx in txs:
        # Simulate: taker spends immediately with configured probability
        import numpy as np

        rng = np.random.default_rng(config.random_seed)
        blocks_until: dict[str, int] = {}
        for p in tx.participants:
            if p.equal_output:
                if p.role.value == "taker" and rng.random() < config.immediate_spend_probability:
                    blocks_until[p.equal_output.outpoint] = 1
                else:
                    blocks_until[p.equal_output.outpoint] = int(rng.integers(10, 1000))

        metrics = compute_post_spend_anonymity(tx, set(), blocks_until)
        for m in metrics.values():
            post_spend_entropies.append(m.entropy_bits)

    elapsed = time.time() - start

    results: dict[str, object] = {
        "scenario": name,
        "config": config.model_dump(),
        "timing_seconds": elapsed,
        "n_transactions": len(txs),
        "naive_anonymity": {
            "mean": float(sum(naive_anon_sets) / len(naive_anon_sets)) if naive_anon_sets else 0,
            "min": min(naive_anon_sets) if naive_anon_sets else 0,
            "max": max(naive_anon_sets) if naive_anon_sets else 0,
        },
        "change_anonymity": {
            "mean_entropy_bits": (
                sum(change_entropies) / len(change_entropies) if change_entropies else 0
            ),
            "uniquely_mapped_fraction": unique_changes / total_changes if total_changes > 0 else 0,
            "total_change_outputs": total_changes,
        },
        "sybil_anonymity": {
            "mean_effective_anon_set": (
                sum(sybil_anon_sets) / len(sybil_anon_sets) if sybil_anon_sets else None
            ),
        },
        "role_identification": role_results,
        "role_identification_with_swap": role_results_swap,
        "post_spend_anonymity": {
            "mean_entropy_bits": (
                sum(post_spend_entropies) / len(post_spend_entropies) if post_spend_entropies else 0
            ),
        },
    }

    if verbose:
        _print_results(results)

    return results


def run_sybil_analysis(verbose: bool = True) -> list[dict[str, object]]:
    """Run dedicated sybil resistance analysis."""
    if verbose:
        print("\n" + "=" * 60)
        print("  Sybil Resistance Analysis")
        print("=" * 60)

    results = analyze_sybil_resistance_sweep(
        n_honest_makers=50,
        counterparty_range=range(2, 16),
        n_simulations=5_000,
        seed=42,
    )

    output: list[dict[str, object]] = []
    for r in results:
        entry: dict[str, object] = {
            "n_counterparties": r.n_counterparties,
            "required_burned_btc": r.required_burned_btc,
            "required_locked_btc_6mo": r.required_locked_btc_6mo,
            "enemies_within_success_prob": r.entity_success_rates.get("enemies_within", 0),
        }
        output.append(entry)

        if verbose:
            print(
                f"  {r.n_counterparties:2d} counterparties: "
                f"burned={r.required_burned_btc:.4f} BTC, "
                f"locked(6mo)={r.required_locked_btc_6mo:.1f} BTC, "
                f"enemies_within={r.entity_success_rates.get('enemies_within', 0):.1%}"
            )

    return output


def run_surveillance_analysis(verbose: bool = True) -> dict[str, object]:
    """Run surveillance/probing attack analysis."""
    if verbose:
        print("\n" + "=" * 60)
        print("  Surveillance Attack Analysis")
        print("=" * 60)

    config = ALL_SCENARIOS["active_surveillance"]
    sim = TransactionSimulator(config)
    txs = sim.simulate_chain()

    # Without mitigations
    surv_no_mit = SurveillanceSimulator(config, seed=42)
    result_no_mit = surv_no_mit.simulate_continuous_probing(txs, probe_fraction=0.8)

    # With mitigations
    surv_mit = SurveillanceSimulator(config, seed=42)
    result_mit = surv_mit.simulate_with_mitigations(
        txs,
        podle_cost_per_probe=3,
        max_probes_per_maker=10,
        utxo_rotation_interval=5,
    )

    results: dict[str, object] = {
        "no_mitigation": {
            "n_probes": result_no_mit.n_probes,
            "makers_clustered": result_no_mit.n_makers_clustered,
            "utxos_clustered": result_no_mit.n_utxos_clustered,
            "coinjoins_with_reduction": result_no_mit.coinjoins_deanonymized,
            "avg_anon_reduction": result_no_mit.avg_anon_set_reduction,
            "top_cluster_sizes": result_no_mit.cluster_sizes[:10],
        },
        "with_mitigation": {
            "n_probes": result_mit.n_probes,
            "makers_clustered": result_mit.n_makers_clustered,
            "utxos_clustered": result_mit.n_utxos_clustered,
            "coinjoins_with_reduction": result_mit.coinjoins_deanonymized,
            "avg_anon_reduction": result_mit.mitigated_anon_set_reduction,
            "top_cluster_sizes": result_mit.cluster_sizes[:10],
        },
    }

    if verbose:
        print("\n  Without mitigations:")
        print(f"    Probes: {result_no_mit.n_probes}")
        print(f"    Makers clustered: {result_no_mit.n_makers_clustered}")
        print(f"    UTXOs clustered: {result_no_mit.n_utxos_clustered}")
        print(f"    CJs with reduced anon: {result_no_mit.coinjoins_deanonymized}")
        print(f"    Avg anon reduction: {result_no_mit.avg_anon_set_reduction:.1%}")
        print("\n  With mitigations (PoDLE + rate limit + rotation):")
        print(f"    Probes: {result_mit.n_probes}")
        print(f"    Makers clustered: {result_mit.n_makers_clustered}")
        print(f"    UTXOs clustered: {result_mit.n_utxos_clustered}")
        print(f"    CJs with reduced anon: {result_mit.coinjoins_deanonymized}")
        print(f"    Avg anon reduction: {result_mit.mitigated_anon_set_reduction:.1%}")

    return results


def run_network_analysis(
    config: NetworkSimulationConfig,
    evil_fractions: list[float],
    orderbook_url: str = DEFAULT_ORDERBOOK_URL,
    verbose: bool = True,
) -> dict[str, object]:
    """Run realistic network-level simulation sweep."""
    if verbose:
        print("\n" + "=" * 60)
        print("  Realistic Network Simulation")
        print("=" * 60)
        print(f"  Makers: {config.n_makers}, makers/CJ: {config.n_makers_per_coinjoin}")
        print(
            "  CJ amount distribution: "
            f"mean={config.mean_cj_amount_btc:.4f} BTC, std={config.std_cj_amount_btc:.4f} BTC"
        )
        print(
            "  Wallet balance ratio: "
            f"mean={config.total_balance_ratio_mean:.2f}x, "
            f"cap={config.total_balance_ratio_cap:.2f}x"
        )
        print(f"  Evil taker sweep: {', '.join(f'{x:.2f}' for x in evil_fractions)}")

    start = time.time()
    results, n_profiles = run_live_network_sweep(
        base_config=config,
        evil_taker_fractions=evil_fractions,
        orderbook_url=orderbook_url,
    )
    elapsed = time.time() - start

    output: dict[str, object] = {
        "orderbook_url": orderbook_url,
        "n_bonded_maker_profiles": n_profiles,
        "config": asdict(config),
        "timing_seconds": elapsed,
        "results": [result.to_dict() for result in results],
    }

    if verbose:
        print(f"  Bonded maker profiles: {n_profiles}")
        print(f"  Completed in {elapsed:.2f}s")
        print("\n  Sweep results:")
        for result in results:
            print(
                "    "
                f"evil={result.evil_taker_fraction:.2f}, "
                f"success={result.n_successful_coinjoins}, "
                f"clustered={result.maker_clustered_fraction:.1%}, "
                f"known_live_utxos={result.known_live_utxo_fraction:.1%}, "
                f"mean_taker_anon={result.mean_taker_anon_set:.2f}, "
                f"deanon={result.taker_deanonymized_fraction:.1%}"
            )

    return output


def _print_results(results: dict[str, object]) -> None:
    """Print results in a readable format."""
    naive = results.get("naive_anonymity", {})
    change = results.get("change_anonymity", {})
    role = results.get("role_identification", {})
    role_swap = results.get("role_identification_with_swap", {})
    sybil = results.get("sybil_anonymity", {})
    post = results.get("post_spend_anonymity", {})

    print(f"\n  Results ({results.get('timing_seconds', 0):.2f}s):")
    if isinstance(naive, dict):
        print(
            f"    Naive anon set: mean={naive.get('mean', 0):.1f}, "
            f"min={naive.get('min', 0)}, max={naive.get('max', 0)}"
        )
    if isinstance(change, dict):
        print(
            f"    Change outputs: entropy={change.get('mean_entropy_bits', 0):.2f} bits, "
            f"uniquely_mapped={change.get('uniquely_mapped_fraction', 0):.1%}"
        )
    if isinstance(sybil, dict) and sybil.get("mean_effective_anon_set") is not None:
        print(f"    Sybil-aware anon set: {sybil.get('mean_effective_anon_set', 0):.1f}")
    if isinstance(role, dict):
        print(
            f"    Taker identification: rank1={role.get('taker_identified_rank1_frac', 0):.1%}, "
            f"fee_heuristic={role.get('fee_heuristic_accuracy', 0):.1%}, "
            f"entropy={role.get('avg_role_entropy_bits', 0):.2f} bits"
        )
    if isinstance(role_swap, dict):
        print(
            f"    With swap input: rank1={role_swap.get('taker_identified_rank1_frac', 0):.1%}, "
            f"entropy={role_swap.get('avg_role_entropy_bits', 0):.2f} bits"
        )
    if isinstance(post, dict):
        print(f"    Post-spend entropy: {post.get('mean_entropy_bits', 0):.2f} bits")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="CoinJoin Privacy Simulation Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run a specific scenario")
    run_parser.add_argument(
        "--scenario",
        "-s",
        choices=list(ALL_SCENARIOS.keys()),
        default="naive_baseline",
        help="Scenario to run",
    )
    run_parser.add_argument("--output", "-o", type=Path, help="Output directory for results")

    # List command
    subparsers.add_parser("list", help="List available scenarios")

    # Benchmark command
    bench_parser = subparsers.add_parser("benchmark", help="Run all scenarios")
    bench_parser.add_argument("--output", "-o", type=Path, help="Output directory")

    # Sybil analysis command
    subparsers.add_parser("sybil", help="Run sybil resistance analysis")

    # Surveillance analysis command
    subparsers.add_parser("surveillance", help="Run surveillance attack analysis")

    # Report command
    report_parser = subparsers.add_parser("report", help="Generate full HTML report")
    report_parser.add_argument("--output", "-o", type=Path, help="Output HTML file path")

    # Curated publish report command
    publish_parser = subparsers.add_parser(
        "publish-site",
        help="Generate curated publish site and compact JSON",
    )
    publish_parser.add_argument(
        "--mitigation",
        type=Path,
        default=Path("mitigation_experiments.json"),
        help="Path to mitigation experiment JSON",
    )
    publish_parser.add_argument(
        "--longrun",
        type=Path,
        default=Path("longrun_policy_results.json"),
        help="Path to long-run policy JSON",
    )
    publish_parser.add_argument(
        "--daily",
        type=Path,
        default=Path("daily_cost_study_results.json"),
        help="Path to daily cost study JSON",
    )
    publish_parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/index.html"),
        help="Path to generated publish HTML",
    )
    publish_parser.add_argument(
        "--data-output",
        type=Path,
        default=Path("docs/publish_summary.json"),
        help="Path to generated compact JSON",
    )

    # Realistic network simulation command
    network_parser = subparsers.add_parser(
        "network",
        help="Run realistic network-level maker/probing simulation",
    )
    network_parser.add_argument("--makers", type=int, default=100, help="Number of makers")
    network_parser.add_argument("--rounds", type=int, default=1000, help="Simulation rounds")
    network_parser.add_argument(
        "--makers-per-cj",
        type=int,
        default=5,
        help="Makers per successful CoinJoin",
    )
    network_parser.add_argument(
        "--evil-fractions",
        type=_parse_float_list,
        default=_parse_float_list("0.0,0.1,0.2,0.4,0.6,0.8"),
        help="Comma-separated evil taker fractions",
    )
    network_parser.add_argument(
        "--probes-per-evil",
        type=int,
        default=5,
        help="Maker probes attempted per evil taker round",
    )
    network_parser.add_argument(
        "--mean-cj-btc",
        type=float,
        default=0.02,
        help="Mean CoinJoin amount in BTC",
    )
    network_parser.add_argument(
        "--std-cj-btc",
        type=float,
        default=0.006,
        help="CoinJoin amount stddev in BTC",
    )
    network_parser.add_argument(
        "--ratio-mean",
        type=float,
        default=4.8,
        help="Mean wallet balance to max-offer ratio",
    )
    network_parser.add_argument(
        "--ratio-std",
        type=float,
        default=0.15,
        help="Stddev of wallet balance ratio",
    )
    network_parser.add_argument(
        "--ratio-cap",
        type=float,
        default=4.95,
        help="Upper cap of wallet balance ratio",
    )
    network_parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed",
    )
    network_parser.add_argument(
        "--orderbook-url",
        type=str,
        default=DEFAULT_ORDERBOOK_URL,
        help="Orderbook JSON URL",
    )
    network_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write results JSON to file",
    )
    network_parser.add_argument(
        "--n-mixdepths",
        type=int,
        default=5,
        help="Number of mixdepths per wallet (default: 5)",
    )
    network_parser.add_argument(
        "--max-utxos-per-offer",
        type=int,
        default=None,
        help="Cap UTXOs revealed per probe (mitigation, default: unlimited)",
    )
    network_parser.add_argument(
        "--sticky-disclosed",
        action="store_true",
        default=False,
        help="Enable sticky disclosed UTXOs mitigation",
    )
    network_parser.add_argument(
        "--flagged-isolation",
        action="store_true",
        default=False,
        help="Enable flagged UTXO isolation mitigation",
    )
    network_parser.add_argument(
        "--initiation-fee",
        type=int,
        default=0,
        help="Initiation fee in sats per protocol start (mitigation, default: 0)",
    )

    args = parser.parse_args()

    if args.command == "list":
        print("Available scenarios:")
        for name, config in ALL_SCENARIOS.items():
            print(
                f"  {name:30s} makers={config.n_makers_per_cj}, "
                f"sybil={config.n_sybil_entities}x{config.sybil_makers_per_entity}, "
                f"bonds={config.use_fidelity_bonds}"
            )
        return

    if args.command == "run":
        config = ALL_SCENARIOS[args.scenario]
        results = run_scenario(args.scenario, config)
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            with open(args.output / f"{args.scenario}.json", "w") as f:
                json.dump(results, f, indent=2, default=str)
            print(f"\n  Results saved to {args.output / f'{args.scenario}.json'}")
        return

    if args.command == "benchmark":
        all_results: dict[str, object] = {}
        for name, config in ALL_SCENARIOS.items():
            all_results[name] = run_scenario(name, config)

        # Also run sybil and surveillance analyses
        all_results["sybil_resistance"] = run_sybil_analysis()
        all_results["surveillance"] = run_surveillance_analysis()

        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            with open(args.output / "benchmark_results.json", "w") as f:
                json.dump(all_results, f, indent=2, default=str)
            print(f"\n  All results saved to {args.output / 'benchmark_results.json'}")
        return

    if args.command == "sybil":
        run_sybil_analysis()
        return

    if args.command == "surveillance":
        run_surveillance_analysis()
        return

    if args.command == "report":
        out = args.output if args.output else None
        path = generate_report(out)
        print(f"Report generated: {path}")
        return

    if args.command == "publish-site":
        html_path, data_path = generate_publish_site(
            mitigation_path=args.mitigation,
            longrun_path=args.longrun,
            daily_path=args.daily,
            output_path=args.output,
            data_output_path=args.data_output,
        )
        print(f"Publish page generated: {html_path}")
        print(f"Curated data generated: {data_path}")
        return

    if args.command == "network":
        network_config = NetworkSimulationConfig(
            n_makers=args.makers,
            n_rounds=args.rounds,
            n_makers_per_coinjoin=args.makers_per_cj,
            probes_per_evil_taker=args.probes_per_evil,
            mean_cj_amount_btc=args.mean_cj_btc,
            std_cj_amount_btc=args.std_cj_btc,
            total_balance_ratio_mean=args.ratio_mean,
            total_balance_ratio_std=args.ratio_std,
            total_balance_ratio_cap=args.ratio_cap,
            random_seed=args.seed,
            n_mixdepths=args.n_mixdepths,
            max_utxos_per_offer=args.max_utxos_per_offer,
            sticky_disclosed_utxos=args.sticky_disclosed,
            flagged_utxo_isolation=args.flagged_isolation,
            initiation_fee_sats=args.initiation_fee,
        )

        results = run_network_analysis(
            config=network_config,
            evil_fractions=args.evil_fractions,
            orderbook_url=args.orderbook_url,
        )

        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2, default=str)
            print(f"\n  Results saved to {args.output}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
