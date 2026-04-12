"""Tests for sybil resistance comparison module."""

from __future__ import annotations

import math

import pytest

from coinjoin_simulator.sybil_comparison import (
    ComparisonResult,
    _simulate_joinstr_sybil,
    analyze_accountability,
    analyze_splitting,
    compute_cost_curves,
    jm_bond_value,
    jm_required_btc_for_weight,
    run_full_comparison,
    run_monte_carlo_comparison,
)


class TestJMBondValue:
    def test_positive_value(self) -> None:
        v = jm_bond_value(1.0, lock_months=6)
        assert v > 0

    def test_zero_btc(self) -> None:
        v = jm_bond_value(0.0, lock_months=6)
        assert v == 0.0

    def test_larger_btc_larger_value(self) -> None:
        v1 = jm_bond_value(1.0, lock_months=6)
        v2 = jm_bond_value(2.0, lock_months=6)
        assert v2 > v1

    def test_super_linear_scaling(self) -> None:
        """2x BTC should give >2x bond value due to exponent 1.3."""
        v1 = jm_bond_value(1.0, lock_months=6)
        v2 = jm_bond_value(2.0, lock_months=6)
        ratio = v2 / v1
        assert ratio > 2.0  # Super-linear
        expected_ratio = 2.0**1.3
        assert abs(ratio - expected_ratio) < 1e-6

    def test_longer_lock_more_value(self) -> None:
        v6 = jm_bond_value(1.0, lock_months=6)
        v12 = jm_bond_value(1.0, lock_months=12)
        assert v12 > v6

    def test_matches_formula(self) -> None:
        """Verify against the known formula."""
        btc = 1.5
        months = 8
        r = 0.015
        exp = 1.3
        t_years = months / 12.0
        time_factor = math.exp(r * t_years) - 1
        expected = (btc * time_factor) ** exp
        actual = jm_bond_value(btc, months, exp, r)
        assert abs(actual - expected) < 1e-10


class TestJMRequiredBtcForWeight:
    def test_round_trip(self) -> None:
        """Bond value -> required BTC -> bond value should be identity."""
        original_btc = 2.5
        weight = jm_bond_value(original_btc, lock_months=6)
        recovered_btc = jm_required_btc_for_weight(weight, lock_months=6)
        assert abs(recovered_btc - original_btc) < 1e-6

    def test_zero_weight(self) -> None:
        assert jm_required_btc_for_weight(0.0) == 0.0

    def test_positive(self) -> None:
        btc = jm_required_btc_for_weight(1.0, lock_months=6)
        assert btc > 0

    def test_more_weight_needs_more_btc(self) -> None:
        btc1 = jm_required_btc_for_weight(1.0, lock_months=6)
        btc10 = jm_required_btc_for_weight(10.0, lock_months=6)
        assert btc10 > btc1


class TestSimulateJoinstrSybil:
    def test_all_sybil(self) -> None:
        """When all participants are sybil, probability = 1."""
        prob = _simulate_joinstr_sybil(0, 10, 5)
        assert abs(prob - 1.0) < 1e-10

    def test_no_sybil(self) -> None:
        prob = _simulate_joinstr_sybil(10, 0, 5)
        assert prob == 0.0

    def test_not_enough_sybil(self) -> None:
        prob = _simulate_joinstr_sybil(10, 3, 5)
        assert prob == 0.0

    def test_not_enough_total(self) -> None:
        prob = _simulate_joinstr_sybil(2, 2, 5)
        assert prob == 0.0

    def test_hypergeometric_correctness(self) -> None:
        """1 honest + 2 sybil, choose 2: P = 2/3 * 1/2 = 1/3."""
        prob = _simulate_joinstr_sybil(1, 2, 2)
        assert abs(prob - 1.0 / 3.0) < 1e-10

    def test_more_sybils_higher_prob(self) -> None:
        p_low = _simulate_joinstr_sybil(50, 10, 4)
        p_high = _simulate_joinstr_sybil(50, 100, 4)
        assert p_high > p_low

    def test_more_counterparties_lower_prob(self) -> None:
        """More counterparties makes sybil attack harder."""
        p2 = _simulate_joinstr_sybil(50, 50, 2)
        p6 = _simulate_joinstr_sybil(50, 50, 6)
        assert p2 > p6


class TestAnalyzeSplitting:
    def test_jm_splitting_is_penalized(self) -> None:
        result = analyze_splitting(total_btc=1.0)
        assert result.jm_splitting_penalty_2x > 0
        assert result.jm_splitting_penalty_5x > result.jm_splitting_penalty_2x
        assert result.jm_splitting_penalty_10x > result.jm_splitting_penalty_5x

    def test_jm_split_values_sum_less_than_single(self) -> None:
        result = analyze_splitting(total_btc=1.0)
        assert result.jm_split_2_total_value < result.jm_single_bond_value
        assert result.jm_split_5_total_value < result.jm_single_bond_value
        assert result.jm_split_10_total_value < result.jm_single_bond_value

    def test_joinstr_splitting_is_free(self) -> None:
        result = analyze_splitting(total_btc=1.0, joinstr_min_utxo_btc=0.01)
        assert result.joinstr_splitting_penalty == 0.0

    def test_joinstr_splitting_gives_more_slots(self) -> None:
        """Splitting is actually beneficial for joinstr attacker."""
        result = analyze_splitting(total_btc=1.0, joinstr_min_utxo_btc=0.01)
        assert result.joinstr_split_10_total_value > result.joinstr_single_proof_value

    def test_jm_penalty_increases_with_exponent(self) -> None:
        r_low = analyze_splitting(total_btc=1.0, exponent=1.1)
        r_high = analyze_splitting(total_btc=1.0, exponent=1.5)
        assert r_high.jm_splitting_penalty_2x > r_low.jm_splitting_penalty_2x

    def test_specific_penalty_values(self) -> None:
        """2-way split penalty should be 1 - 2*(1/2)^1.3 = 1 - 2^(1-1.3) = 1 - 2^(-0.3)."""
        result = analyze_splitting(total_btc=1.0, exponent=1.3)
        expected_penalty = 1 - 2 ** (1 - 1.3)
        assert abs(result.jm_splitting_penalty_2x - expected_penalty) < 1e-6


class TestAnalyzeAccountability:
    def test_jm_is_accountable(self) -> None:
        result = analyze_accountability()
        assert result.jm_bond_visible is True
        assert result.jm_can_blacklist is True

    def test_joinstr_is_not_accountable(self) -> None:
        result = analyze_accountability()
        assert result.joinstr_bond_visible is False
        assert result.joinstr_can_blacklist is False

    def test_jm_penalty_higher_than_joinstr(self) -> None:
        result = analyze_accountability()
        assert result.jm_effective_penalty_multiplier > result.joinstr_effective_penalty_multiplier

    def test_joinstr_penalty_is_one(self) -> None:
        result = analyze_accountability()
        assert result.joinstr_effective_penalty_multiplier == 1.0

    def test_custom_blacklist_params(self) -> None:
        result = analyze_accountability(
            jm_blacklist_probability=0.5,
            jm_blacklist_cost_fraction=1.0,
        )
        # 1 + 0.5 * 1.0 = 1.5
        assert abs(result.jm_effective_penalty_multiplier - 1.5) < 1e-10


class TestRunMonteCarloComparison:
    def test_returns_results(self) -> None:
        results = run_monte_carlo_comparison(
            attacker_budgets_btc=[1.0, 5.0],
            counterparty_counts=[2, 4],
            n_honest_makers_jm=20,
            n_honest_participants_joinstr=20,
            n_simulations=500,
            seed=42,
        )
        assert len(results) == 4  # 2 budgets * 2 cp counts

    def test_probabilities_in_range(self) -> None:
        results = run_monte_carlo_comparison(
            attacker_budgets_btc=[1.0],
            counterparty_counts=[2],
            n_honest_makers_jm=20,
            n_honest_participants_joinstr=20,
            n_simulations=500,
            seed=42,
        )
        for r in results:
            assert 0.0 <= r.jm_success_prob <= 1.0
            assert 0.0 <= r.joinstr_success_prob <= 1.0

    def test_higher_budget_higher_prob(self) -> None:
        results = run_monte_carlo_comparison(
            attacker_budgets_btc=[0.5, 50.0],
            counterparty_counts=[4],
            n_honest_makers_jm=20,
            n_honest_participants_joinstr=20,
            n_simulations=1000,
            seed=42,
        )
        low_budget = results[0]
        high_budget = results[1]
        assert high_budget.jm_success_prob >= low_budget.jm_success_prob
        assert high_budget.joinstr_success_prob >= low_budget.joinstr_success_prob

    def test_joinstr_more_sybil_bots_than_jm(self) -> None:
        """Joinstr attacker can split into more bots (no penalty)."""
        results = run_monte_carlo_comparison(
            attacker_budgets_btc=[10.0],
            counterparty_counts=[4],
            n_honest_makers_jm=20,
            n_honest_participants_joinstr=20,
            joinstr_min_utxo_btc=0.01,
            n_simulations=500,
            seed=42,
        )
        r = results[0]
        # 10 BTC / 0.01 BTC = 1000 bots for joinstr vs 4 for JM
        assert r.joinstr_n_sybil_bots == 1000
        assert r.jm_n_sybil_bots == 4

    def test_joinstr_higher_success_at_low_budget(self) -> None:
        """With a modest budget, joinstr should be easier to sybil (no bond penalty)."""
        results = run_monte_carlo_comparison(
            attacker_budgets_btc=[1.0],
            counterparty_counts=[4],
            n_honest_makers_jm=20,
            n_honest_participants_joinstr=20,
            joinstr_min_utxo_btc=0.01,
            n_simulations=1000,
            seed=42,
        )
        r = results[0]
        # Joinstr: 100 sybil bots out of 120 total, choosing 4
        # JM: 4 sybil bots with weak bonds among 24 total
        # Joinstr should have much higher success
        assert r.joinstr_success_prob > r.jm_success_prob


class TestComputeCostCurves:
    def test_returns_results(self) -> None:
        results = compute_cost_curves(
            counterparty_range=range(2, 5),
            n_honest_makers_jm=10,
            n_honest_participants_joinstr=10,
            seed=42,
        )
        assert len(results) == 3

    def test_cost_increases_with_counterparties(self) -> None:
        results = compute_cost_curves(
            counterparty_range=range(2, 5),
            n_honest_makers_jm=10,
            n_honest_participants_joinstr=10,
            seed=42,
        )
        jm_costs = [r.jm_required_btc_locked for r in results]
        joinstr_costs = [r.joinstr_required_btc_held for r in results]
        # Both should increase with counterparties
        assert jm_costs[-1] > jm_costs[0]
        assert joinstr_costs[-1] > joinstr_costs[0]

    def test_joinstr_cheaper_than_jm(self) -> None:
        """Joinstr should generally require less BTC than JM for same success."""
        results = compute_cost_curves(
            counterparty_range=range(2, 4),
            n_honest_makers_jm=10,
            n_honest_participants_joinstr=10,
            seed=42,
        )
        for r in results:
            # Joinstr just needs to hold UTXOs, JM needs to lock with time value
            # The absolute BTC comparison depends on parameters, but the
            # opportunity cost should be lower for joinstr
            assert r.joinstr_opportunity_cost_usd <= r.jm_opportunity_cost_usd

    def test_joinstr_utxo_count(self) -> None:
        results = compute_cost_curves(
            counterparty_range=range(2, 4),
            n_honest_makers_jm=10,
            n_honest_participants_joinstr=10,
            joinstr_min_utxo_btc=0.01,
            seed=42,
        )
        for r in results:
            assert r.joinstr_n_utxos_needed >= r.n_counterparties


@pytest.fixture(scope="module")
def small_comparison() -> ComparisonResult:
    """Run a small comparison once for all tests using this fixture."""
    return run_full_comparison(
        n_honest_makers_jm=5,
        n_honest_participants_joinstr=5,
        seed=42,
    )


class TestRunFullComparison:
    """Integration tests -- use small parameters for speed."""

    def test_returns_complete_result(self, small_comparison: ComparisonResult) -> None:
        assert isinstance(small_comparison, ComparisonResult)
        assert len(small_comparison.cost_curves) > 0
        assert small_comparison.splitting_analysis is not None
        assert small_comparison.accountability_analysis is not None
        assert len(small_comparison.monte_carlo_comparison) > 0

    def test_splitting_results_valid(self, small_comparison: ComparisonResult) -> None:
        s = small_comparison.splitting_analysis
        assert s is not None
        assert s.jm_splitting_penalty_2x > 0
        assert s.joinstr_splitting_penalty == 0.0

    def test_accountability_results_valid(self, small_comparison: ComparisonResult) -> None:
        a = small_comparison.accountability_analysis
        assert a is not None
        assert a.jm_effective_penalty_multiplier > a.joinstr_effective_penalty_multiplier
