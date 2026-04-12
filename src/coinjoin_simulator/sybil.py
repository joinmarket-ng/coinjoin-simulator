"""Sybil attack simulation.

Implements probability tree diagram analysis for computing sybil attack
success rates, following the mathematical framework from Chris Belcher's
fidelity bond design document.

Key questions answered:
- What is the probability a sybil attacker controls all makers in a CoinJoin?
- How does the number of counterparties affect sybil resistance?
- What is the cost (in locked/burned BTC) to achieve a target success rate?
- How do fidelity bonds change the economics?
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .models import SybilAttackResult


@dataclass
class BondedEntity:
    """An entity with a fidelity bond value."""

    entity_id: str
    bond_value: float
    is_sybil: bool = False


def compute_sybil_probability_tree(
    honest_entities: list[BondedEntity],
    sybil_entities: list[BondedEntity],
    n_counterparties: int,
) -> float:
    """Compute sybil attack success probability using probability tree diagrams.

    A sybil attack succeeds when ALL chosen counterparties are sybil entities.

    This uses recursive probability tree analysis (sampling without replacement)
    as described in Belcher's fidelity bond math document.

    Args:
        honest_entities: List of honest makers with bond values.
        sybil_entities: List of sybil makers (all controlled by attacker).
        n_counterparties: Number of makers the taker selects.

    Returns:
        Probability that all selected counterparties are sybil entities.
    """
    all_entities = list(honest_entities) + list(sybil_entities)
    sybil_ids = {e.entity_id for e in sybil_entities}

    if len(all_entities) < n_counterparties:
        return 0.0

    if len(sybil_entities) < n_counterparties:
        return 0.0

    # Use recursive probability tree
    return _prob_tree_recursive(
        available=all_entities,
        sybil_ids=sybil_ids,
        remaining_choices=n_counterparties,
    )


def _prob_tree_recursive(
    available: list[BondedEntity],
    sybil_ids: set[str],
    remaining_choices: int,
) -> float:
    """Recursively compute probability that all remaining choices are sybil.

    At each step, we select one entity with probability proportional to
    its bond value, then recurse with the remaining entities.
    """
    if remaining_choices == 0:
        return 1.0

    if len(available) < remaining_choices:
        return 0.0

    total_weight = sum(e.bond_value for e in available)
    if total_weight <= 0:
        # Uniform selection if no bonds
        n_sybil = sum(1 for e in available if e.entity_id in sybil_ids)
        if n_sybil < remaining_choices:
            return 0.0
        # Hypergeometric probability
        prob = 1.0
        for i in range(remaining_choices):
            prob *= (n_sybil - i) / (len(available) - i)
        return prob

    # Sum over all possible first choices
    success_prob = 0.0
    for i, entity in enumerate(available):
        choice_prob = entity.bond_value / total_weight
        if entity.entity_id in sybil_ids:
            # This choice is sybil, continue with remaining
            remaining = available[:i] + available[i + 1 :]
            sub_prob = _prob_tree_recursive(remaining, sybil_ids, remaining_choices - 1)
            success_prob += choice_prob * sub_prob
        # If choice is honest, attack fails (prob contribution = 0)

    return success_prob


def find_required_sybil_weight(
    honest_total_weight: float,
    n_counterparties: int,
    target_success: float = 0.95,
    n_sybil_bots: int | None = None,
    max_iterations: int = 100,
) -> float:
    """Find the total sybil bond weight needed to achieve target success rate.

    Uses binary search to find the minimum sybil weight.

    Args:
        honest_total_weight: Total fidelity bond weight of honest makers.
        n_counterparties: Number of makers taker selects.
        target_success: Desired attack success probability.
        n_sybil_bots: Number of sybil bots. Defaults to n_counterparties.
        max_iterations: Maximum binary search iterations.

    Returns:
        Required total sybil bond weight.
    """
    if n_sybil_bots is None:
        n_sybil_bots = n_counterparties

    # Create honest entity with the total weight
    honest = [BondedEntity(entity_id="honest", bond_value=honest_total_weight)]

    lo, hi = 0.0, honest_total_weight * 1000
    for _ in range(max_iterations):
        mid = (lo + hi) / 2
        per_bot_weight = mid / n_sybil_bots
        sybil = [
            BondedEntity(entity_id=f"sybil_{i}", bond_value=per_bot_weight, is_sybil=True)
            for i in range(n_sybil_bots)
        ]
        prob = compute_sybil_probability_tree(honest, sybil, n_counterparties)
        if prob < target_success:
            lo = mid
        else:
            hi = mid

    return (lo + hi) / 2


def weight_to_burned_btc(weight: float, exponent: float = 1.3) -> float:
    """Convert fidelity bond weight to equivalent burned BTC.

    bond_value = V^exponent, so V = bond_value^(1/exponent)
    """
    if weight <= 0:
        return 0.0
    return float(weight ** (1.0 / exponent))


def weight_to_locked_btc(
    weight: float,
    lock_months: int = 6,
    interest_rate: float = 0.015,
    exponent: float = 1.3,
) -> float:
    """Convert fidelity bond weight to equivalent locked BTC.

    bond_value = (V * (exp(r*T) - 1))^exponent
    So V = bond_value^(1/exponent) / (exp(r*T) - 1)
    """
    if weight <= 0:
        return 0.0
    t_years = lock_months / 12.0
    time_factor = math.exp(interest_rate * t_years) - 1
    if time_factor <= 0:
        return float("inf")
    v_times_factor = float(weight ** (1.0 / exponent))
    return v_times_factor / time_factor


def simulate_sybil_attack(
    n_honest_makers: int = 50,
    honest_bond_values: list[float] | None = None,
    n_sybil_bots: int = 10,
    sybil_total_weight: float | None = None,
    n_counterparties: int = 10,
    n_simulations: int = 10_000,
    seed: int | None = None,
) -> SybilAttackResult:
    """Monte Carlo simulation of sybil attacks.

    Simulates many CoinJoin maker selections and counts how often
    the sybil attacker controls all chosen makers.

    Args:
        n_honest_makers: Number of honest makers in the orderbook.
        honest_bond_values: Bond values for honest makers. Generated if None.
        n_sybil_bots: Number of sybil maker bots.
        sybil_total_weight: Total sybil bond weight. Auto-computed if None.
        n_counterparties: Number of makers taker selects.
        n_simulations: Number of simulation rounds.
        seed: Random seed.

    Returns:
        SybilAttackResult with success probability and cost metrics.
    """
    rng = np.random.default_rng(seed)

    if honest_bond_values is None:
        # Generate realistic bond value distribution
        honest_bond_values = [
            float(rng.lognormal(mean=-8, sigma=3)) for _ in range(n_honest_makers)
        ]

    honest_total = sum(honest_bond_values)

    if sybil_total_weight is None:
        # Default: sybil has same total weight as honest makers
        sybil_total_weight = honest_total

    sybil_per_bot = sybil_total_weight / n_sybil_bots

    # Build weight arrays
    all_weights = np.array(honest_bond_values + [sybil_per_bot] * n_sybil_bots)
    n_total = len(all_weights)
    sybil_start = n_honest_makers

    successes = 0
    for _ in range(n_simulations):
        # Weighted sampling without replacement
        remaining_weights = all_weights.copy()
        remaining_indices = list(range(n_total))
        chosen_sybil = True

        for _ in range(min(n_counterparties, n_total)):
            total_w = remaining_weights.sum()
            if total_w <= 0:
                # Uniform selection
                idx_in_remaining = int(rng.integers(len(remaining_indices)))
            else:
                probs = remaining_weights / total_w
                idx_in_remaining = int(rng.choice(len(remaining_indices), p=probs))

            original_idx = remaining_indices[idx_in_remaining]
            if original_idx < sybil_start:
                # Honest maker chosen -> attack fails
                chosen_sybil = False
                break

            # Remove chosen entity
            remaining_weights = np.delete(remaining_weights, idx_in_remaining)
            remaining_indices.pop(idx_in_remaining)

        if chosen_sybil:
            successes += 1

    success_prob = successes / n_simulations

    return SybilAttackResult(
        n_counterparties=n_counterparties,
        sybil_entity_bond_value=sybil_total_weight,
        honest_total_bond_value=honest_total,
        success_probability=success_prob,
        required_locked_btc_6mo=weight_to_locked_btc(sybil_total_weight),
        required_burned_btc=weight_to_burned_btc(sybil_total_weight),
    )


def analyze_sybil_resistance_sweep(
    honest_bond_values: list[float] | None = None,
    n_honest_makers: int = 50,
    counterparty_range: range | None = None,
    n_simulations: int = 5_000,
    seed: int | None = None,
) -> list[SybilAttackResult]:
    """Sweep over different counterparty counts to analyze sybil resistance.

    Returns results for each counterparty count showing how resistance
    improves with more counterparties.
    """
    if counterparty_range is None:
        counterparty_range = range(2, 16)

    rng = np.random.default_rng(seed)

    if honest_bond_values is None:
        honest_bond_values = [
            float(rng.lognormal(mean=-8, sigma=3)) for _ in range(n_honest_makers)
        ]

    honest_total = sum(honest_bond_values)

    results: list[SybilAttackResult] = []
    for n_cp in counterparty_range:
        # Find required sybil weight for 95% success
        required_weight = find_required_sybil_weight(
            honest_total_weight=honest_total,
            n_counterparties=n_cp,
            target_success=0.95,
            n_sybil_bots=n_cp,
        )

        # Also simulate with the top-N makers being sybils
        sorted_bonds = sorted(honest_bond_values, reverse=True)
        if len(sorted_bonds) >= n_cp:
            sum(sorted_bonds[:n_cp])
            remaining_weight = sum(sorted_bonds[n_cp:])

            # Compute success probability for "enemies within" scenario
            honest_for_tree = [BondedEntity(entity_id="honest_pool", bond_value=remaining_weight)]
            sybil_for_tree = [
                BondedEntity(
                    entity_id=f"sybil_{i}",
                    bond_value=sorted_bonds[i],
                    is_sybil=True,
                )
                for i in range(n_cp)
            ]
            enemies_within_prob = compute_sybil_probability_tree(
                honest_for_tree, sybil_for_tree, n_cp
            )
        else:
            enemies_within_prob = 0.0

        results.append(
            SybilAttackResult(
                n_counterparties=n_cp,
                sybil_entity_bond_value=required_weight,
                honest_total_bond_value=honest_total,
                success_probability=0.95,
                entity_success_rates={"enemies_within": enemies_within_prob},
                required_locked_btc_6mo=weight_to_locked_btc(required_weight),
                required_burned_btc=weight_to_burned_btc(required_weight),
            )
        )

    return results
