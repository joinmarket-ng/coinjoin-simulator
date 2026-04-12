"""Pre-built simulation scenarios for benchmarking CoinJoin protocols.

Each scenario represents a specific threat model or assumption set.
Scenarios can be composed and compared to evaluate different protocol
configurations and their impact on privacy.
"""

from __future__ import annotations

from .models import SimulationConfig


def scenario_naive_baseline() -> SimulationConfig:
    """Naive baseline: all makers are honest, no sybil, no surveillance.

    This is the best-case scenario where anonymity set equals the number
    of equal outputs. Useful as an upper bound.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=10,
        n_coinjoins=100,
        cj_amount=1_000_000,
        n_sybil_entities=0,
        use_fidelity_bonds=True,
        selection_algorithm="fidelity_bond_weighted",
        sweep_probability=0.1,
        immediate_spend_probability=0.0,
        random_seed=42,
    )


def scenario_small_orderbook() -> SimulationConfig:
    """Small orderbook: few makers, lower anonymity.

    Represents early-stage JoinMarket or low-liquidity periods.
    """
    return SimulationConfig(
        n_makers_total=15,
        n_makers_per_cj=5,
        n_coinjoins=100,
        cj_amount=500_000,
        n_sybil_entities=0,
        use_fidelity_bonds=True,
        selection_algorithm="fidelity_bond_weighted",
        random_seed=42,
    )


def scenario_sybil_external_weak() -> SimulationConfig:
    """Weak external sybil: 1 entity runs 3 makers (10% of orderbook).

    The attacker has a small presence but cannot dominate selection
    due to fidelity bond weighting.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=10,
        n_coinjoins=100,
        cj_amount=1_000_000,
        n_sybil_entities=1,
        sybil_makers_per_entity=3,
        use_fidelity_bonds=True,
        selection_algorithm="fidelity_bond_weighted",
        random_seed=42,
    )


def scenario_sybil_external_strong() -> SimulationConfig:
    """Strong external sybil: 3 entities each run 5 makers (30% of orderbook).

    A well-funded attacker with significant orderbook presence.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=10,
        n_coinjoins=100,
        cj_amount=1_000_000,
        n_sybil_entities=3,
        sybil_makers_per_entity=5,
        use_fidelity_bonds=True,
        selection_algorithm="fidelity_bond_weighted",
        random_seed=42,
    )


def scenario_sybil_no_bonds() -> SimulationConfig:
    """Sybil attack without fidelity bonds.

    Demonstrates how much worse things are without bonds.
    Selection is random, making sybil attacks much cheaper.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=10,
        n_coinjoins=100,
        cj_amount=1_000_000,
        n_sybil_entities=2,
        sybil_makers_per_entity=5,
        use_fidelity_bonds=False,
        selection_algorithm="random",
        random_seed=42,
    )


def scenario_active_surveillance() -> SimulationConfig:
    """Active surveillance: Chainalysis-style continuous monitoring.

    The adversary probes the orderbook constantly and monitors
    all blockchain transactions. No sybil makers, just passive
    observation + active probing.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=10,
        n_coinjoins=200,
        cj_amount=1_000_000,
        n_sybil_entities=0,
        use_fidelity_bonds=True,
        selection_algorithm="fidelity_bond_weighted",
        random_seed=42,
    )


def scenario_surveillance_plus_sybil() -> SimulationConfig:
    """Combined threat: surveillance + sybil attack.

    The worst case: an adversary both probes the orderbook and
    runs sybil makers. Represents a well-funded state actor or
    chain analysis company.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=10,
        n_coinjoins=200,
        cj_amount=1_000_000,
        n_sybil_entities=2,
        sybil_makers_per_entity=3,
        use_fidelity_bonds=True,
        selection_algorithm="fidelity_bond_weighted",
        random_seed=42,
    )


def scenario_taker_immediate_spend() -> SimulationConfig:
    """Taker spends equal output immediately.

    Models a taker who uses the CoinJoin output right away,
    revealing behavioral information about their identity.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=10,
        n_coinjoins=100,
        cj_amount=1_000_000,
        n_sybil_entities=0,
        use_fidelity_bonds=True,
        sweep_probability=0.0,
        immediate_spend_probability=0.8,
        random_seed=42,
    )


def scenario_sweep_heavy() -> SimulationConfig:
    """Many sweep (no-change) transactions.

    When the taker uses sweep mode (no change output), the anonymity
    of change outputs changes, and the taker may be identifiable
    as the participant without change.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=10,
        n_coinjoins=100,
        cj_amount=1_000_000,
        n_sybil_entities=0,
        use_fidelity_bonds=True,
        sweep_probability=0.5,
        random_seed=42,
    )


def scenario_swap_input_camouflage() -> SimulationConfig:
    """Swap input camouflage (PR #280 mitigation).

    Tests the effectiveness of adding a swap input to make the
    taker's fee pattern indistinguishable from a maker's.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=10,
        n_coinjoins=100,
        cj_amount=1_000_000,
        n_sybil_entities=0,
        use_fidelity_bonds=True,
        selection_algorithm="fidelity_bond_weighted",
        random_seed=42,
    )


def scenario_low_counterparties() -> SimulationConfig:
    """Low counterparty count (2-3 makers).

    Minimum viable CoinJoin with reduced anonymity.
    Tests lower bounds of privacy.
    """
    return SimulationConfig(
        n_makers_total=50,
        n_makers_per_cj=3,
        n_coinjoins=100,
        cj_amount=1_000_000,
        n_sybil_entities=0,
        use_fidelity_bonds=True,
        random_seed=42,
    )


def scenario_high_counterparties() -> SimulationConfig:
    """High counterparty count (20+ makers).

    Maximum privacy configuration. Tests upper bounds and
    the diminishing returns of adding more counterparties.
    """
    return SimulationConfig(
        n_makers_total=100,
        n_makers_per_cj=20,
        n_coinjoins=50,
        cj_amount=1_000_000,
        n_sybil_entities=0,
        use_fidelity_bonds=True,
        random_seed=42,
    )


ALL_SCENARIOS: dict[str, SimulationConfig] = {
    "naive_baseline": scenario_naive_baseline(),
    "small_orderbook": scenario_small_orderbook(),
    "sybil_external_weak": scenario_sybil_external_weak(),
    "sybil_external_strong": scenario_sybil_external_strong(),
    "sybil_no_bonds": scenario_sybil_no_bonds(),
    "active_surveillance": scenario_active_surveillance(),
    "surveillance_plus_sybil": scenario_surveillance_plus_sybil(),
    "taker_immediate_spend": scenario_taker_immediate_spend(),
    "sweep_heavy": scenario_sweep_heavy(),
    "swap_input_camouflage": scenario_swap_input_camouflage(),
    "low_counterparties": scenario_low_counterparties(),
    "high_counterparties": scenario_high_counterparties(),
}
