"""Tests for sybil attack simulation."""

from __future__ import annotations

from coinjoin_simulator.sybil import (
    BondedEntity,
    analyze_sybil_resistance_sweep,
    compute_sybil_probability_tree,
    find_required_sybil_weight,
    simulate_sybil_attack,
    weight_to_burned_btc,
    weight_to_locked_btc,
)


class TestComputeSybilProbabilityTree:
    def test_all_sybil_entities(self) -> None:
        """When all entities are sybil, probability should be 1.0."""
        sybils = [BondedEntity(entity_id=f"s_{i}", bond_value=1.0, is_sybil=True) for i in range(5)]
        prob = compute_sybil_probability_tree([], sybils, 3)
        assert abs(prob - 1.0) < 1e-10

    def test_no_sybil_entities(self) -> None:
        """When there are no sybil entities, probability should be 0."""
        honest = [BondedEntity(entity_id=f"h_{i}", bond_value=1.0) for i in range(5)]
        prob = compute_sybil_probability_tree(honest, [], 3)
        assert prob == 0.0

    def test_not_enough_sybils(self) -> None:
        """When fewer sybil entities than counterparties needed, probability is 0."""
        honest = [BondedEntity(entity_id="h_0", bond_value=1.0)]
        sybils = [BondedEntity(entity_id="s_0", bond_value=1.0, is_sybil=True)]
        prob = compute_sybil_probability_tree(honest, sybils, 3)
        assert prob == 0.0

    def test_not_enough_total(self) -> None:
        """When total entities < counterparties, probability is 0."""
        honest = [BondedEntity(entity_id="h_0", bond_value=1.0)]
        sybils = [BondedEntity(entity_id="s_0", bond_value=1.0, is_sybil=True)]
        prob = compute_sybil_probability_tree(honest, sybils, 5)
        assert prob == 0.0

    def test_equal_weight_two_of_two(self) -> None:
        """With 1 honest + 2 sybil, all weight=1, selecting 2: prob = 2/3 * 1/2 = 1/3."""
        honest = [BondedEntity(entity_id="h", bond_value=1.0)]
        sybils = [
            BondedEntity(entity_id="s_0", bond_value=1.0, is_sybil=True),
            BondedEntity(entity_id="s_1", bond_value=1.0, is_sybil=True),
        ]
        prob = compute_sybil_probability_tree(honest, sybils, 2)
        assert abs(prob - 1.0 / 3.0) < 1e-10

    def test_higher_sybil_weight_increases_prob(self) -> None:
        """Sybils with much higher weight should have higher success probability."""
        honest = [BondedEntity(entity_id="h", bond_value=1.0)]
        sybils_low = [
            BondedEntity(entity_id=f"s_{i}", bond_value=1.0, is_sybil=True) for i in range(3)
        ]
        sybils_high = [
            BondedEntity(entity_id=f"s_{i}", bond_value=100.0, is_sybil=True) for i in range(3)
        ]
        prob_low = compute_sybil_probability_tree(honest, sybils_low, 2)
        prob_high = compute_sybil_probability_tree(honest, sybils_high, 2)
        assert prob_high > prob_low

    def test_zero_weight_uniform(self) -> None:
        """With zero bond weights, falls back to uniform (hypergeometric)."""
        honest = [BondedEntity(entity_id=f"h_{i}", bond_value=0.0) for i in range(3)]
        sybils = [BondedEntity(entity_id=f"s_{i}", bond_value=0.0, is_sybil=True) for i in range(2)]
        # 5 total, 2 sybil, choose 2: hypergeometric P = C(2,2)*C(3,0)/C(5,2) = 1/10
        prob = compute_sybil_probability_tree(honest, sybils, 2)
        assert abs(prob - 1.0 / 10.0) < 1e-10

    def test_single_counterparty(self) -> None:
        """Selecting 1 counterparty: prob = sybil_weight / total_weight."""
        honest = [BondedEntity(entity_id="h", bond_value=3.0)]
        sybils = [BondedEntity(entity_id="s", bond_value=7.0, is_sybil=True)]
        prob = compute_sybil_probability_tree(honest, sybils, 1)
        assert abs(prob - 0.7) < 1e-10


class TestFindRequiredSybilWeight:
    def test_returns_positive(self) -> None:
        weight = find_required_sybil_weight(
            honest_total_weight=1.0,
            n_counterparties=3,
            target_success=0.5,
        )
        assert weight > 0

    def test_more_counterparties_needs_more_weight(self) -> None:
        """More counterparties should require more sybil weight for same success."""
        w3 = find_required_sybil_weight(
            honest_total_weight=1.0, n_counterparties=3, target_success=0.5
        )
        w6 = find_required_sybil_weight(
            honest_total_weight=1.0, n_counterparties=6, target_success=0.5
        )
        assert w6 > w3

    def test_higher_target_needs_more_weight(self) -> None:
        w50 = find_required_sybil_weight(
            honest_total_weight=1.0, n_counterparties=3, target_success=0.5
        )
        w90 = find_required_sybil_weight(
            honest_total_weight=1.0, n_counterparties=3, target_success=0.9
        )
        assert w90 > w50


class TestWeightConversions:
    def test_burned_btc_positive(self) -> None:
        btc = weight_to_burned_btc(1.0)
        assert btc > 0

    def test_burned_btc_zero(self) -> None:
        assert weight_to_burned_btc(0.0) == 0.0

    def test_burned_btc_negative(self) -> None:
        assert weight_to_burned_btc(-1.0) == 0.0

    def test_burned_btc_inverse(self) -> None:
        """V^exponent = weight => V = weight^(1/exponent)."""
        exponent = 1.3
        weight = 10.0
        btc = weight_to_burned_btc(weight, exponent)
        # Verify: btc^exponent should equal weight
        assert abs(btc**exponent - weight) < 1e-6

    def test_locked_btc_positive(self) -> None:
        btc = weight_to_locked_btc(1.0, lock_months=6)
        assert btc > 0

    def test_locked_btc_zero(self) -> None:
        assert weight_to_locked_btc(0.0) == 0.0

    def test_locked_btc_longer_lock_cheaper(self) -> None:
        """Longer lock time should require less BTC for the same weight."""
        btc_6mo = weight_to_locked_btc(1.0, lock_months=6)
        btc_12mo = weight_to_locked_btc(1.0, lock_months=12)
        assert btc_12mo < btc_6mo

    def test_locked_vs_burned(self) -> None:
        """Locked should require more BTC than burned (time value discount)."""
        weight = 5.0
        burned = weight_to_burned_btc(weight)
        locked_6mo = weight_to_locked_btc(weight, lock_months=6)
        assert locked_6mo > burned


class TestSimulateSybilAttack:
    def test_basic_simulation(self) -> None:
        result = simulate_sybil_attack(
            n_honest_makers=20,
            n_sybil_bots=5,
            n_counterparties=5,
            n_simulations=1_000,
            seed=42,
        )
        assert 0.0 <= result.success_probability <= 1.0
        assert result.n_counterparties == 5
        assert result.required_locked_btc_6mo > 0
        assert result.required_burned_btc > 0

    def test_no_sybils_zero_probability(self) -> None:
        """With zero sybil weight, success probability should be 0."""
        result = simulate_sybil_attack(
            n_honest_makers=20,
            n_sybil_bots=5,
            sybil_total_weight=0.0,
            n_counterparties=5,
            n_simulations=100,
            seed=42,
        )
        assert result.success_probability == 0.0

    def test_high_sybil_weight_high_prob(self) -> None:
        """With overwhelmingly high sybil weight, success should be near 1."""
        result = simulate_sybil_attack(
            n_honest_makers=5,
            honest_bond_values=[0.001] * 5,
            n_sybil_bots=10,
            sybil_total_weight=1000.0,
            n_counterparties=3,
            n_simulations=1_000,
            seed=42,
        )
        assert result.success_probability > 0.9

    def test_more_counterparties_lower_prob(self) -> None:
        """More counterparties should make sybil attack harder."""
        r3 = simulate_sybil_attack(
            n_honest_makers=20,
            n_sybil_bots=5,
            n_counterparties=3,
            n_simulations=2_000,
            seed=42,
        )
        r8 = simulate_sybil_attack(
            n_honest_makers=20,
            n_sybil_bots=5,
            n_counterparties=8,
            n_simulations=2_000,
            seed=42,
        )
        assert r3.success_probability >= r8.success_probability


class TestAnalyzeSybilResistanceSweep:
    def test_returns_results(self) -> None:
        results = analyze_sybil_resistance_sweep(
            n_honest_makers=20,
            counterparty_range=range(2, 6),
            n_simulations=100,
            seed=42,
        )
        assert len(results) == 4  # range(2,6) = 4 values
        for r in results:
            assert r.required_locked_btc_6mo >= 0
            assert r.required_burned_btc >= 0

    def test_increasing_counterparties_increases_cost(self) -> None:
        results = analyze_sybil_resistance_sweep(
            n_honest_makers=20,
            counterparty_range=range(2, 6),
            n_simulations=100,
            seed=42,
        )
        # Required weight should generally increase with counterparties
        weights = [r.sybil_entity_bond_value for r in results]
        # Not strictly monotonic due to binary search precision, but overall trend
        assert weights[-1] > weights[0]

    def test_enemies_within_probability(self) -> None:
        results = analyze_sybil_resistance_sweep(
            n_honest_makers=20,
            counterparty_range=range(2, 5),
            n_simulations=100,
            seed=42,
        )
        for r in results:
            prob = r.entity_success_rates.get("enemies_within", 0.0)
            assert 0.0 <= prob <= 1.0
