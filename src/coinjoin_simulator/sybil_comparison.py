"""Comparative sybil resistance analysis: JoinMarket fidelity bonds vs Joinstr aut-ct.

Models the economic cost of sybil attacks under each protocol's anti-sybil
mechanism and quantifies their relative strengths.

JoinMarket fidelity bonds:
  - Public UTXO commitment (locktime + value visible on-chain)
  - Bond value = (V * time_factor)^1.3  (super-linear in value)
  - Accountability: misbehaving bonds can be blacklisted
  - Splitting penalty: V^1.3 > 2*(V/2)^1.3  (attacker pays more when splitting)

Joinstr aut-ct (curve trees):
  - Anonymous UTXO ownership proof via zero-knowledge proof
  - Linear in value: prove ownership of >= threshold sats
  - No accountability: verifier cannot identify which UTXO was used
  - No splitting penalty: 10 UTXOs of 0.1 BTC = same total cost as 1 of 1 BTC
  - Key image prevents reuse of same UTXO in same round, but attacker can
    use different UTXOs across rounds
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from pydantic import BaseModel, Field

from .sybil import (
    simulate_sybil_attack,
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SybilCostPoint(BaseModel):
    """Cost of achieving a given sybil success probability."""

    n_counterparties: int
    success_probability: float
    # JoinMarket costs
    jm_required_btc_locked: float = 0.0  # BTC locked for 6 months
    jm_required_btc_burned: float = 0.0  # Equivalent burned BTC
    jm_opportunity_cost_usd: float = 0.0  # At given BTC price + risk-free rate
    # Joinstr costs
    joinstr_required_btc_held: float = 0.0  # BTC that must exist in UTXOs
    joinstr_n_utxos_needed: int = 0  # Number of distinct UTXOs
    joinstr_opportunity_cost_usd: float = 0.0  # Minimal: just hold, no lock


class ComparisonResult(BaseModel):
    """Full comparison result between JM and Joinstr sybil resistance."""

    btc_price_usd: float
    risk_free_rate: float  # Annual
    jm_bond_exponent: float
    jm_interest_rate: float
    jm_lock_months: int
    joinstr_min_utxo_sats: int
    joinstr_min_age_blocks: int

    cost_curves: list[SybilCostPoint] = Field(default_factory=list)
    splitting_analysis: SplittingAnalysis | None = None
    accountability_analysis: AccountabilityAnalysis | None = None
    monte_carlo_comparison: list[MonteCarloComparison] = Field(default_factory=list)


class SplittingAnalysis(BaseModel):
    """Analysis of UTXO splitting economics under each scheme."""

    # JoinMarket: splitting is penalized by super-linear exponent
    jm_single_bond_value: float  # Bond value of single V BTC
    jm_split_2_total_value: float  # Sum of bond values for 2 * V/2
    jm_split_5_total_value: float  # Sum of bond values for 5 * V/5
    jm_split_10_total_value: float  # Sum of bond values for 10 * V/10
    jm_splitting_penalty_2x: float  # Fraction lost: 1 - split/single
    jm_splitting_penalty_5x: float
    jm_splitting_penalty_10x: float

    # Joinstr: splitting is free (linear)
    joinstr_single_proof_value: float
    joinstr_split_2_total_value: float
    joinstr_split_10_total_value: float
    joinstr_splitting_penalty: float  # Should be ~0


class AccountabilityAnalysis(BaseModel):
    """Analysis of accountability differences."""

    # JoinMarket: public bonds can be blacklisted
    jm_bond_visible: bool = True
    jm_can_blacklist: bool = True
    jm_cost_of_blacklisting: str = ""  # Narrative

    # Joinstr: anonymous proofs
    joinstr_bond_visible: bool = False
    joinstr_can_blacklist: bool = False
    joinstr_cost_of_misbehavior: str = ""  # Narrative

    # Quantified impact
    jm_effective_penalty_multiplier: float = 1.0  # Extra deterrence from accountability
    joinstr_effective_penalty_multiplier: float = 1.0


class MonteCarloComparison(BaseModel):
    """Monte Carlo sybil success comparison at a specific budget."""

    attacker_budget_btc: float
    n_counterparties: int
    jm_success_prob: float
    joinstr_success_prob: float
    jm_n_sybil_bots: int
    joinstr_n_sybil_bots: int


# ---------------------------------------------------------------------------
# JoinMarket bond value calculations (matching bond_calc.py)
# ---------------------------------------------------------------------------

JM_DEFAULT_EXPONENT = 1.3
JM_DEFAULT_INTEREST_RATE = 0.015


def jm_bond_value(
    utxo_value_btc: float,
    lock_months: int = 6,
    exponent: float = JM_DEFAULT_EXPONENT,
    interest_rate: float = JM_DEFAULT_INTEREST_RATE,
) -> float:
    """Compute JoinMarket fidelity bond value.

    bond_value = (V * (exp(r*T) - 1))^exponent

    Args:
        utxo_value_btc: UTXO value in BTC.
        lock_months: Lock period in months.
        exponent: Super-linear exponent (default 1.3).
        interest_rate: Annual interest rate.

    Returns:
        Bond value (dimensionless weight).
    """
    t_years = lock_months / 12.0
    time_factor = math.exp(interest_rate * t_years) - 1
    return float((utxo_value_btc * time_factor) ** exponent)


def jm_required_btc_for_weight(
    target_weight: float,
    lock_months: int = 6,
    exponent: float = JM_DEFAULT_EXPONENT,
    interest_rate: float = JM_DEFAULT_INTEREST_RATE,
) -> float:
    """Inverse: how much BTC must be locked to achieve a given bond weight.

    V = target_weight^(1/exponent) / (exp(r*T) - 1)
    """
    if target_weight <= 0:
        return 0.0
    t_years = lock_months / 12.0
    time_factor = math.exp(interest_rate * t_years) - 1
    if time_factor <= 0:
        return float("inf")
    v_times_factor = target_weight ** (1.0 / exponent)
    return float(v_times_factor / time_factor)


# ---------------------------------------------------------------------------
# Joinstr aut-ct model
# ---------------------------------------------------------------------------


@dataclass
class JoinstrPool:
    """A Joinstr CoinJoin pool configuration."""

    min_utxo_sats: int = 1_000_000  # 0.01 BTC default
    min_age_blocks: int = 6  # ~1 hour
    n_participants: int = 5  # Equal-output participants per round


def joinstr_sybil_weight(
    utxo_value_btc: float,
    min_utxo_btc: float = 0.01,
) -> float:
    """Joinstr "bond weight" -- linear in value above threshold.

    In aut-ct, the proof just shows ownership of a UTXO >= min_utxo.
    All qualifying UTXOs have equal selection probability (no weighting
    by value). So each qualifying UTXO contributes weight = 1.

    The "cost" is having that BTC available, but there is no lock
    and no super-linear penalty.

    Returns:
        1.0 if utxo_value_btc >= min_utxo_btc, else 0.0.
    """
    return 1.0 if utxo_value_btc >= min_utxo_btc else 0.0


# ---------------------------------------------------------------------------
# Splitting analysis
# ---------------------------------------------------------------------------


def analyze_splitting(
    total_btc: float = 1.0,
    lock_months: int = 6,
    exponent: float = JM_DEFAULT_EXPONENT,
    interest_rate: float = JM_DEFAULT_INTEREST_RATE,
    joinstr_min_utxo_btc: float = 0.01,
) -> SplittingAnalysis:
    """Compare the economics of UTXO splitting under each scheme.

    For JoinMarket, splitting V into N pieces means each piece has bond value
    (V/N * time_factor)^exponent. The total is N * (V/N * tf)^exp.
    Since exponent > 1, this is strictly less than (V * tf)^exp.

    For Joinstr, splitting has no penalty because each qualifying UTXO
    contributes equal weight (1.0). In fact, splitting is *beneficial*
    for the attacker because they get more "slots" in the pool.
    """
    single_jm = jm_bond_value(total_btc, lock_months, exponent, interest_rate)

    split_2_jm = 2 * jm_bond_value(total_btc / 2, lock_months, exponent, interest_rate)
    split_5_jm = 5 * jm_bond_value(total_btc / 5, lock_months, exponent, interest_rate)
    split_10_jm = 10 * jm_bond_value(total_btc / 10, lock_months, exponent, interest_rate)

    penalty_2 = 1 - split_2_jm / single_jm if single_jm > 0 else 0
    penalty_5 = 1 - split_5_jm / single_jm if single_jm > 0 else 0
    penalty_10 = 1 - split_10_jm / single_jm if single_jm > 0 else 0

    # Joinstr: each UTXO above threshold counts as 1.
    # Single 1 BTC = 1 slot. Split into 10 x 0.1 BTC = 10 slots.
    # Splitting is actually *rewarded* (more sybil identities).
    n_possible_splits = int(total_btc / joinstr_min_utxo_btc) if joinstr_min_utxo_btc > 0 else 1
    joinstr_single = 1.0
    joinstr_split_2 = min(2, n_possible_splits) * 1.0
    joinstr_split_10 = min(10, n_possible_splits) * 1.0

    return SplittingAnalysis(
        jm_single_bond_value=single_jm,
        jm_split_2_total_value=split_2_jm,
        jm_split_5_total_value=split_5_jm,
        jm_split_10_total_value=split_10_jm,
        jm_splitting_penalty_2x=penalty_2,
        jm_splitting_penalty_5x=penalty_5,
        jm_splitting_penalty_10x=penalty_10,
        joinstr_single_proof_value=joinstr_single,
        joinstr_split_2_total_value=joinstr_split_2,
        joinstr_split_10_total_value=joinstr_split_10,
        joinstr_splitting_penalty=0.0,  # Splitting is free or beneficial
    )


# ---------------------------------------------------------------------------
# Accountability analysis
# ---------------------------------------------------------------------------


def analyze_accountability(
    jm_blacklist_probability: float = 0.3,
    jm_blacklist_cost_fraction: float = 0.5,
) -> AccountabilityAnalysis:
    """Analyze the accountability difference between the two schemes.

    JoinMarket fidelity bonds are public: the UTXO, its value, and locktime
    are visible to everyone. If a maker misbehaves (e.g., refuses to sign,
    participates in known sybil attack), other participants can blacklist
    that bond UTXO. The maker would need to create a new bond with a new
    UTXO, losing the accumulated time value of the old one.

    Joinstr aut-ct proofs are anonymous: the verifier only knows that the
    prover owns *some* UTXO meeting the threshold, but not which one.
    Key images prevent double-use in the same round, but a misbehaving
    participant cannot be banned from future rounds because their identity
    is unknown.

    The effective penalty multiplier captures how much more costly
    misbehavior is when you can be identified and punished.
    """
    # JM: cost of misbehavior includes potential blacklisting
    # If blacklisted with probability p, you lose fraction f of your bond value
    # Expected additional cost = p * f * bond_value
    jm_penalty = 1.0 + jm_blacklist_probability * jm_blacklist_cost_fraction

    # Joinstr: no accountability, no additional cost beyond the base cost
    joinstr_penalty = 1.0

    return AccountabilityAnalysis(
        jm_bond_visible=True,
        jm_can_blacklist=True,
        jm_cost_of_blacklisting=(
            f"If blacklisted (est. {jm_blacklist_probability:.0%} probability for detected "
            f"misbehavior), the maker loses the time value accumulated on their bond UTXO "
            f"(est. {jm_blacklist_cost_fraction:.0%} of total bond value). They must create a "
            f"new time-locked UTXO and wait for it to mature, costing months of lost revenue."
        ),
        joinstr_bond_visible=False,
        joinstr_can_blacklist=False,
        joinstr_cost_of_misbehavior=(
            "No accountability mechanism exists. A misbehaving participant cannot be identified "
            "or banned from future rounds. The key image prevents reuse of the same UTXO in the "
            "same round, but the attacker can use a different UTXO next round. The only defense "
            "is raising the minimum UTXO threshold, which also excludes legitimate small users."
        ),
        jm_effective_penalty_multiplier=jm_penalty,
        joinstr_effective_penalty_multiplier=joinstr_penalty,
    )


# ---------------------------------------------------------------------------
# Monte Carlo sybil comparison
# ---------------------------------------------------------------------------


def _simulate_joinstr_sybil(
    n_honest_participants: int,
    n_sybil_utxos: int,
    n_counterparties: int,
    n_simulations: int = 10_000,
    seed: int | None = None,
) -> float:
    """Simulate joinstr sybil attack success rate.

    In joinstr, all qualifying UTXOs have equal weight (no bond weighting).
    Selection is uniform among participants who present valid proofs.
    Key images prevent the same UTXO from being used twice in one round,
    but the attacker can use different UTXOs.

    The attacker controls n_sybil_utxos distinct qualifying UTXOs,
    each appearing as an independent participant.

    Returns:
        Probability that all n_counterparties are controlled by attacker.
    """
    n_total = n_honest_participants + n_sybil_utxos

    if n_total < n_counterparties:
        return 0.0
    if n_sybil_utxos < n_counterparties:
        return 0.0

    # Uniform selection without replacement -> hypergeometric
    # P(all k chosen are sybil) = C(s,k) * C(h,0) / C(n,k)
    # = product_{i=0}^{k-1} (s-i) / (n-i)
    prob = 1.0
    for i in range(n_counterparties):
        prob *= (n_sybil_utxos - i) / (n_total - i)

    return prob


def run_monte_carlo_comparison(
    attacker_budgets_btc: list[float] | None = None,
    counterparty_counts: list[int] | None = None,
    n_honest_makers_jm: int = 50,
    n_honest_participants_joinstr: int = 50,
    jm_lock_months: int = 6,
    jm_exponent: float = JM_DEFAULT_EXPONENT,
    jm_interest_rate: float = JM_DEFAULT_INTEREST_RATE,
    joinstr_min_utxo_btc: float = 0.01,
    n_simulations: int = 5_000,
    seed: int = 42,
) -> list[MonteCarloComparison]:
    """Compare sybil success rates for the same attacker budget.

    For each budget level, compute:
    - JM: attacker locks budget BTC for lock_months, creating bond weight,
      distributed across sybil bots. Success rate via weighted MC.
    - Joinstr: attacker splits budget into max possible qualifying UTXOs
      (budget / min_utxo). Success rate via hypergeometric.

    Args:
        attacker_budgets_btc: BTC amounts the attacker is willing to commit.
        counterparty_counts: Number of counterparties to test.
        n_honest_makers_jm: Honest JM makers.
        n_honest_participants_joinstr: Honest Joinstr participants.
        jm_lock_months: Lock period for JM bonds.
        jm_exponent: JM bond exponent.
        jm_interest_rate: JM interest rate.
        joinstr_min_utxo_btc: Minimum UTXO for Joinstr proof.
        n_simulations: MC iterations for JM simulation.
        seed: Random seed.

    Returns:
        List of MonteCarloComparison results.
    """
    if attacker_budgets_btc is None:
        attacker_budgets_btc = [0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 50.0, 100.0]

    if counterparty_counts is None:
        counterparty_counts = [2, 4, 6, 8, 10]

    rng = np.random.default_rng(seed)

    # Generate honest JM maker bond values (realistic distribution)
    # Median ~0.1 BTC locked for 6 months, with heavy tail
    honest_bond_weights: list[float] = []
    for _ in range(n_honest_makers_jm):
        btc_locked = float(rng.lognormal(mean=-2.3, sigma=1.5))  # ~0.1 BTC median
        btc_locked = min(btc_locked, 100.0)  # Cap at 100 BTC
        weight = jm_bond_value(btc_locked, jm_lock_months, jm_exponent, jm_interest_rate)
        honest_bond_weights.append(weight)

    results: list[MonteCarloComparison] = []

    for budget in attacker_budgets_btc:
        for n_cp in counterparty_counts:
            # --- JoinMarket ---
            # Attacker locks budget BTC, creates sybil bots
            # Optimal strategy: concentrate into fewer larger bonds (due to super-linear)
            # but need at least n_cp bots. Use n_cp bots with equal weight.
            jm_n_bots = n_cp
            per_bot_btc = budget / jm_n_bots
            per_bot_weight = jm_bond_value(
                per_bot_btc, jm_lock_months, jm_exponent, jm_interest_rate
            )
            total_sybil_weight = per_bot_weight * jm_n_bots

            jm_result = simulate_sybil_attack(
                n_honest_makers=n_honest_makers_jm,
                honest_bond_values=honest_bond_weights,
                n_sybil_bots=jm_n_bots,
                sybil_total_weight=total_sybil_weight,
                n_counterparties=n_cp,
                n_simulations=n_simulations,
                seed=seed,
            )

            # --- Joinstr ---
            # Attacker splits budget into max qualifying UTXOs
            joinstr_n_bots = int(budget / joinstr_min_utxo_btc)
            joinstr_n_bots = max(joinstr_n_bots, 0)

            joinstr_prob = _simulate_joinstr_sybil(
                n_honest_participants=n_honest_participants_joinstr,
                n_sybil_utxos=joinstr_n_bots,
                n_counterparties=n_cp,
            )

            results.append(
                MonteCarloComparison(
                    attacker_budget_btc=budget,
                    n_counterparties=n_cp,
                    jm_success_prob=jm_result.success_probability,
                    joinstr_success_prob=joinstr_prob,
                    jm_n_sybil_bots=jm_n_bots,
                    joinstr_n_sybil_bots=joinstr_n_bots,
                )
            )

    return results


# ---------------------------------------------------------------------------
# Cost curve computation
# ---------------------------------------------------------------------------


def compute_cost_curves(
    counterparty_range: range | None = None,
    target_success: float = 0.95,
    btc_price_usd: float = 100_000.0,
    risk_free_rate: float = 0.05,  # 5% annual
    jm_lock_months: int = 6,
    jm_exponent: float = JM_DEFAULT_EXPONENT,
    jm_interest_rate: float = JM_DEFAULT_INTEREST_RATE,
    joinstr_min_utxo_btc: float = 0.01,
    n_honest_makers_jm: int = 50,
    n_honest_participants_joinstr: int = 50,
    seed: int = 42,
) -> list[SybilCostPoint]:
    """Compute the cost to achieve target sybil success under each scheme.

    For each counterparty count, finds the minimum BTC the attacker needs.

    JoinMarket: binary search for the locked BTC amount whose bond weight
    gives target_success probability (weighted sampling).

    Joinstr: solve analytically for the number of sybil UTXOs needed
    (hypergeometric), then multiply by min_utxo to get BTC cost.
    """
    if counterparty_range is None:
        counterparty_range = range(2, 16)

    rng = np.random.default_rng(seed)

    # Generate honest JM maker weights
    honest_bond_weights: list[float] = []
    for _ in range(n_honest_makers_jm):
        btc = min(float(rng.lognormal(mean=-2.3, sigma=1.5)), 100.0)
        honest_bond_weights.append(
            jm_bond_value(btc, jm_lock_months, jm_exponent, jm_interest_rate)
        )
    sum(honest_bond_weights)

    results: list[SybilCostPoint] = []

    for n_cp in counterparty_range:
        # --- JoinMarket: binary search for required BTC ---
        lo_btc, hi_btc = 0.0, 10_000.0
        for _ in range(40):  # Binary search iterations
            mid_btc = (lo_btc + hi_btc) / 2
            per_bot_btc = mid_btc / n_cp
            per_bot_weight = jm_bond_value(
                per_bot_btc, jm_lock_months, jm_exponent, jm_interest_rate
            )
            total_sybil_weight = per_bot_weight * n_cp

            # Use Monte Carlo simulation (probability tree is O(n!) with many makers)
            r = simulate_sybil_attack(
                n_honest_makers=n_honest_makers_jm,
                honest_bond_values=honest_bond_weights,
                n_sybil_bots=n_cp,
                sybil_total_weight=total_sybil_weight,
                n_counterparties=n_cp,
                n_simulations=2000,
                seed=seed,
            )
            prob = r.success_probability

            if prob < target_success:
                lo_btc = mid_btc
            else:
                hi_btc = mid_btc

        jm_required_btc = hi_btc
        jm_opp_cost = jm_required_btc * btc_price_usd * risk_free_rate * (jm_lock_months / 12)

        # --- Joinstr: solve for required sybil UTXOs ---
        # Need P(all k from n_sybil out of n_total) >= target_success
        # P = product_{i=0}^{k-1} (s-i)/(n_honest+s-i) >= target
        # Binary search on s (number of sybil UTXOs)
        joinstr_lo, joinstr_hi = n_cp, 100_000
        for _ in range(40):
            mid_s = (joinstr_lo + joinstr_hi) // 2
            prob_j = _simulate_joinstr_sybil(
                n_honest_participants=n_honest_participants_joinstr,
                n_sybil_utxos=mid_s,
                n_counterparties=n_cp,
            )
            if prob_j < target_success:
                joinstr_lo = mid_s + 1
            else:
                joinstr_hi = mid_s
        joinstr_n_utxos = joinstr_hi
        joinstr_btc = joinstr_n_utxos * joinstr_min_utxo_btc
        # Joinstr: no lock required, so opportunity cost is just the
        # risk of having BTC in UTXOs (essentially zero beyond base holding)
        joinstr_opp_cost = 0.0  # No lock, no opportunity cost

        results.append(
            SybilCostPoint(
                n_counterparties=n_cp,
                success_probability=target_success,
                jm_required_btc_locked=jm_required_btc,
                jm_required_btc_burned=jm_required_btc * 0.0,  # Not burned, locked
                jm_opportunity_cost_usd=jm_opp_cost,
                joinstr_required_btc_held=joinstr_btc,
                joinstr_n_utxos_needed=joinstr_n_utxos,
                joinstr_opportunity_cost_usd=joinstr_opp_cost,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Full comparison
# ---------------------------------------------------------------------------


def run_full_comparison(
    btc_price_usd: float = 100_000.0,
    risk_free_rate: float = 0.05,
    jm_lock_months: int = 6,
    jm_exponent: float = JM_DEFAULT_EXPONENT,
    jm_interest_rate: float = JM_DEFAULT_INTEREST_RATE,
    joinstr_min_utxo_sats: int = 1_000_000,
    joinstr_min_age_blocks: int = 6,
    n_honest_makers_jm: int = 50,
    n_honest_participants_joinstr: int = 50,
    seed: int = 42,
) -> ComparisonResult:
    """Run the full comparative analysis.

    Returns a ComparisonResult containing:
    - Cost curves (BTC required for 95% sybil success per counterparty count)
    - Splitting analysis (penalty for splitting UTXOs under each scheme)
    - Accountability analysis (deterrence from public vs anonymous bonds)
    - Monte Carlo comparison (head-to-head at various budgets)
    """
    joinstr_min_utxo_btc = joinstr_min_utxo_sats / 1e8

    # 1. Cost curves
    cost_curves = compute_cost_curves(
        counterparty_range=range(2, 16),
        target_success=0.95,
        btc_price_usd=btc_price_usd,
        risk_free_rate=risk_free_rate,
        jm_lock_months=jm_lock_months,
        jm_exponent=jm_exponent,
        jm_interest_rate=jm_interest_rate,
        joinstr_min_utxo_btc=joinstr_min_utxo_btc,
        n_honest_makers_jm=n_honest_makers_jm,
        n_honest_participants_joinstr=n_honest_participants_joinstr,
        seed=seed,
    )

    # 2. Splitting analysis
    splitting = analyze_splitting(
        total_btc=1.0,
        lock_months=jm_lock_months,
        exponent=jm_exponent,
        interest_rate=jm_interest_rate,
        joinstr_min_utxo_btc=joinstr_min_utxo_btc,
    )

    # 3. Accountability analysis
    accountability = analyze_accountability()

    # 4. Monte Carlo head-to-head
    mc_comparison = run_monte_carlo_comparison(
        attacker_budgets_btc=[0.5, 1.0, 2.0, 5.0, 10.0, 50.0],
        counterparty_counts=[2, 4, 6, 8, 10],
        n_honest_makers_jm=n_honest_makers_jm,
        n_honest_participants_joinstr=n_honest_participants_joinstr,
        jm_lock_months=jm_lock_months,
        jm_exponent=jm_exponent,
        jm_interest_rate=jm_interest_rate,
        joinstr_min_utxo_btc=joinstr_min_utxo_btc,
        n_simulations=5_000,
        seed=seed,
    )

    return ComparisonResult(
        btc_price_usd=btc_price_usd,
        risk_free_rate=risk_free_rate,
        jm_bond_exponent=jm_exponent,
        jm_interest_rate=jm_interest_rate,
        jm_lock_months=jm_lock_months,
        joinstr_min_utxo_sats=joinstr_min_utxo_sats,
        joinstr_min_age_blocks=joinstr_min_age_blocks,
        cost_curves=cost_curves,
        splitting_analysis=splitting,
        accountability_analysis=accountability,
        monte_carlo_comparison=mc_comparison,
    )
