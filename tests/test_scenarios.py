"""Tests for pre-built simulation scenarios."""

from __future__ import annotations

import pytest

from coinjoin_simulator.models import SimulationConfig
from coinjoin_simulator.scenarios import (
    ALL_SCENARIOS,
    scenario_active_surveillance,
    scenario_high_counterparties,
    scenario_low_counterparties,
    scenario_naive_baseline,
    scenario_small_orderbook,
    scenario_surveillance_plus_sybil,
    scenario_swap_input_camouflage,
    scenario_sweep_heavy,
    scenario_sybil_external_strong,
    scenario_sybil_external_weak,
    scenario_sybil_no_bonds,
    scenario_taker_immediate_spend,
)
from coinjoin_simulator.transaction import TransactionSimulator


class TestScenarioConfigs:
    """Test that each scenario returns a valid SimulationConfig."""

    @pytest.mark.parametrize(
        "scenario_fn",
        [
            scenario_naive_baseline,
            scenario_small_orderbook,
            scenario_sybil_external_weak,
            scenario_sybil_external_strong,
            scenario_sybil_no_bonds,
            scenario_active_surveillance,
            scenario_surveillance_plus_sybil,
            scenario_taker_immediate_spend,
            scenario_sweep_heavy,
            scenario_swap_input_camouflage,
            scenario_low_counterparties,
            scenario_high_counterparties,
        ],
    )
    def test_returns_simulation_config(self, scenario_fn: object) -> None:
        assert callable(scenario_fn)
        config = scenario_fn()
        assert isinstance(config, SimulationConfig)

    @pytest.mark.parametrize(
        "scenario_fn",
        [
            scenario_naive_baseline,
            scenario_small_orderbook,
            scenario_sybil_external_weak,
            scenario_sybil_external_strong,
            scenario_sybil_no_bonds,
            scenario_active_surveillance,
            scenario_surveillance_plus_sybil,
            scenario_taker_immediate_spend,
            scenario_sweep_heavy,
            scenario_swap_input_camouflage,
            scenario_low_counterparties,
            scenario_high_counterparties,
        ],
    )
    def test_config_has_valid_maker_counts(self, scenario_fn: object) -> None:
        assert callable(scenario_fn)
        config = scenario_fn()
        assert config.n_makers_total > 0
        assert config.n_makers_per_cj > 0
        assert config.n_makers_per_cj <= config.n_makers_total
        assert config.n_coinjoins > 0
        assert config.cj_amount > 0

    @pytest.mark.parametrize(
        "scenario_fn",
        [
            scenario_naive_baseline,
            scenario_small_orderbook,
            scenario_sybil_external_weak,
            scenario_sybil_external_strong,
            scenario_sybil_no_bonds,
            scenario_active_surveillance,
            scenario_surveillance_plus_sybil,
            scenario_taker_immediate_spend,
            scenario_sweep_heavy,
            scenario_swap_input_camouflage,
            scenario_low_counterparties,
            scenario_high_counterparties,
        ],
    )
    def test_config_deterministic(self, scenario_fn: object) -> None:
        """Each scenario should have a random_seed for reproducibility."""
        assert callable(scenario_fn)
        config = scenario_fn()
        assert config.random_seed is not None


class TestAllScenariosDict:
    def test_all_scenarios_count(self) -> None:
        assert len(ALL_SCENARIOS) == 12

    def test_all_scenarios_are_configs(self) -> None:
        for name, config in ALL_SCENARIOS.items():
            assert isinstance(config, SimulationConfig), f"Scenario {name} is not SimulationConfig"

    def test_all_scenarios_have_names(self) -> None:
        expected = {
            "naive_baseline",
            "small_orderbook",
            "sybil_external_weak",
            "sybil_external_strong",
            "sybil_no_bonds",
            "active_surveillance",
            "surveillance_plus_sybil",
            "taker_immediate_spend",
            "sweep_heavy",
            "swap_input_camouflage",
            "low_counterparties",
            "high_counterparties",
        }
        assert set(ALL_SCENARIOS.keys()) == expected


class TestScenarioProperties:
    def test_naive_no_sybils(self) -> None:
        config = scenario_naive_baseline()
        assert config.n_sybil_entities == 0
        assert config.use_fidelity_bonds is True

    def test_small_orderbook_fewer_makers(self) -> None:
        config = scenario_small_orderbook()
        baseline = scenario_naive_baseline()
        assert config.n_makers_total < baseline.n_makers_total
        assert config.n_makers_per_cj < baseline.n_makers_per_cj

    def test_sybil_weak_has_sybils(self) -> None:
        config = scenario_sybil_external_weak()
        assert config.n_sybil_entities > 0
        assert config.sybil_makers_per_entity > 0

    def test_sybil_strong_more_than_weak(self) -> None:
        weak = scenario_sybil_external_weak()
        strong = scenario_sybil_external_strong()
        total_sybil_weak = weak.n_sybil_entities * weak.sybil_makers_per_entity
        total_sybil_strong = strong.n_sybil_entities * strong.sybil_makers_per_entity
        assert total_sybil_strong > total_sybil_weak

    def test_no_bonds_scenario(self) -> None:
        config = scenario_sybil_no_bonds()
        assert config.use_fidelity_bonds is False
        assert config.selection_algorithm == "random"

    def test_surveillance_long_run(self) -> None:
        config = scenario_active_surveillance()
        baseline = scenario_naive_baseline()
        assert config.n_coinjoins >= baseline.n_coinjoins

    def test_sweep_heavy_high_probability(self) -> None:
        config = scenario_sweep_heavy()
        assert config.sweep_probability >= 0.5

    def test_immediate_spend_high_probability(self) -> None:
        config = scenario_taker_immediate_spend()
        assert config.immediate_spend_probability >= 0.5

    def test_low_counterparties(self) -> None:
        config = scenario_low_counterparties()
        assert config.n_makers_per_cj <= 5

    def test_high_counterparties(self) -> None:
        config = scenario_high_counterparties()
        assert config.n_makers_per_cj >= 15


class TestScenariosRunnable:
    """Test that scenarios can actually produce simulated transactions."""

    @pytest.mark.parametrize("name", list(ALL_SCENARIOS.keys()))
    def test_scenario_can_simulate(self, name: str) -> None:
        config = ALL_SCENARIOS[name]
        sim = TransactionSimulator(config)
        # Generate just 1 coinjoin to verify it works
        tx = sim.simulate_coinjoin()
        assert tx.n_participants > 1
        assert tx.cj_amount == config.cj_amount
        assert len(tx.equal_outputs) > 0
