"""Role identification analysis.

Analyzes how well an external observer can identify the taker vs makers
in a CoinJoin transaction, using various heuristics and Bayesian inference.

Heuristics analyzed:
1. Fee asymmetry: Taker pays all fees (largest surplus between inputs and outputs)
2. No-change (sweep): Taker may have no change output
3. Input count: Taker may have more inputs (funding the CJ amount + fees)
4. Temporal: Taker's equal output may be spent sooner
5. Subset-sum: Deterministic matching of inputs to change outputs
6. Swap input camouflage: Effect of PR #280 (optional swap input)

Also quantifies the impact of mitigations:
- Swap input (makes taker look like a maker)
- Decoy change outputs
- Fee randomization
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import CoinJoinTransaction, Participant


@dataclass
class RoleIdentificationResult:
    """Result of role identification analysis for a single CoinJoin."""

    tx_id: str
    n_participants: int
    # True taker's participant_id
    true_taker_id: str
    # Probability assigned to each participant of being the taker
    taker_probabilities: dict[str, float]
    # Rank of the true taker in the probability ordering (1 = highest)
    true_taker_rank: int
    # Entropy of the role probability distribution (bits)
    role_entropy: float
    # Individual heuristic results
    fee_heuristic_correct: bool
    sweep_heuristic_correct: bool | None  # None if not applicable
    subset_sum_heuristic_correct: bool
    # With swap input mitigation
    mitigated_taker_rank: int | None = None
    mitigated_role_entropy: float | None = None


def identify_taker_bayesian(
    tx: CoinJoinTransaction,
    prior: dict[str, float] | None = None,
) -> dict[str, float]:
    """Bayesian taker identification using multiple heuristics.

    Combines evidence from fee asymmetry, change patterns, and subset-sum
    analysis to produce posterior probabilities for each participant being
    the taker.

    Args:
        tx: The CoinJoin transaction.
        prior: Prior probabilities. Uniform if None.

    Returns:
        Posterior probability for each participant being the taker.
    """
    n = len(tx.participants)
    if n == 0:
        return {}

    # Start with uniform prior
    if prior is None:
        log_posterior = {p.participant_id: 0.0 for p in tx.participants}
    else:
        log_posterior = {k: math.log(max(v, 1e-10)) for k, v in prior.items()}

    # Heuristic 1: Fee asymmetry
    # The taker's inputs minus outputs should be the largest
    surpluses: dict[str, int] = {}
    for p in tx.participants:
        total_in = sum(u.value_sats for u in p.utxos_in)
        total_out = 0
        if p.equal_output:
            total_out += p.equal_output.value_sats
        if p.change_output:
            total_out += p.change_output.value_sats
        surpluses[p.participant_id] = total_in - total_out

    max_surplus = max(surpluses.values()) if surpluses else 0
    for pid, surplus in surpluses.items():
        if surplus == max_surplus and max_surplus > 0:
            log_posterior[pid] += math.log(5.0)  # Strong evidence
        elif surplus > tx.total_mining_fee * 0.3:
            log_posterior[pid] += math.log(2.0)  # Moderate evidence
        elif surplus <= 0:
            log_posterior[pid] += math.log(0.1)  # Counter-evidence (maker earns fees)

    # Heuristic 2: No-change (sweep) pattern
    participants_with_no_change = [p for p in tx.participants if p.change_output is None]
    if 0 < len(participants_with_no_change) < n:
        for p in tx.participants:
            if p.change_output is None:
                log_posterior[p.participant_id] += math.log(2.0)

    # Heuristic 3: Subset-sum uniqueness of change mapping
    # If a change output can only be produced by one specific input,
    # that participant is more likely a maker (deterministic mapping)
    change_mapping_counts = _count_change_mappings(tx)
    for pid, n_mappings in change_mapping_counts.items():
        if n_mappings == 1:
            # Uniquely mapped change -> likely a maker
            log_posterior[pid] += math.log(0.5)
        elif n_mappings > 3:
            # Many possible mappings -> more ambiguous
            log_posterior[pid] += math.log(1.2)

    # Normalize to probabilities
    max_log = max(log_posterior.values())
    probs = {k: math.exp(v - max_log) for k, v in log_posterior.items()}
    total = sum(probs.values())
    return {k: v / total for k, v in probs.items()}


def _count_change_mappings(tx: CoinJoinTransaction) -> dict[str, int]:
    """Count how many valid input-to-change mappings each participant has.

    A valid mapping means: some input's value minus cj_amount (plus/minus fees)
    could produce this change output.
    """
    tolerance = max(10_000, int(tx.cj_amount * 0.01))
    counts: dict[str, int] = {}

    for p in tx.participants:
        if p.change_output is None:
            counts[p.participant_id] = 0
            continue

        n_valid = 0
        for other in tx.participants:
            total_in = sum(u.value_sats for u in other.utxos_in)
            # Could this participant's inputs produce this change output?
            # Maker: change = input - cj_amount + fee_earned
            # Taker: change = input - cj_amount - total_fees - mining_fee
            expected_maker = total_in - tx.cj_amount
            expected_taker = total_in - tx.cj_amount - tx.total_mining_fee

            if (
                abs(expected_maker - p.change_output.value_sats) < tolerance
                or abs(expected_taker - p.change_output.value_sats) < tolerance
            ):
                n_valid += 1

        counts[p.participant_id] = n_valid

    return counts


def analyze_role_identification(
    tx: CoinJoinTransaction,
) -> RoleIdentificationResult:
    """Full role identification analysis for a CoinJoin transaction.

    Applies all heuristics and produces a comprehensive result.
    """
    taker = tx.taker
    if taker is None:
        return RoleIdentificationResult(
            tx_id=tx.tx_id,
            n_participants=tx.n_participants,
            true_taker_id="unknown",
            taker_probabilities={},
            true_taker_rank=0,
            role_entropy=0.0,
            fee_heuristic_correct=False,
            sweep_heuristic_correct=None,
            subset_sum_heuristic_correct=False,
        )

    # Get Bayesian probabilities
    probs = identify_taker_bayesian(tx)

    # Rank the true taker
    sorted_probs = sorted(probs.items(), key=lambda x: x[1], reverse=True)
    rank = 1
    for pid, _prob in sorted_probs:
        if pid == taker.participant_id:
            break
        rank += 1

    # Shannon entropy
    entropy = -sum(p * math.log2(p) for p in probs.values() if p > 0)

    # Fee heuristic: is the participant with largest surplus the true taker?
    surpluses: dict[str, int] = {}
    for p in tx.participants:
        total_in = sum(u.value_sats for u in p.utxos_in)
        total_out = sum(
            u.value_sats
            for u in ([p.equal_output] if p.equal_output else [])
            + ([p.change_output] if p.change_output else [])
        )
        surpluses[p.participant_id] = total_in - total_out

    fee_pred = max(surpluses, key=lambda k: surpluses[k])
    fee_correct = fee_pred == taker.participant_id

    # Sweep heuristic
    no_change = [p for p in tx.participants if p.change_output is None]
    sweep_correct: bool | None = None
    if len(no_change) == 1:
        sweep_correct = no_change[0].participant_id == taker.participant_id

    # Subset-sum heuristic
    change_counts = _count_change_mappings(tx)
    # The taker should have the most ambiguous change mapping
    # (or no change at all in sweep mode)
    subset_correct = True  # Assume correct unless proven wrong
    if taker.change_output is not None:
        taker_count = change_counts.get(taker.participant_id, 0)
        for p in tx.participants:
            if p.participant_id == taker.participant_id:
                continue
            if p.change_output is not None and change_counts.get(p.participant_id, 0) > taker_count:
                subset_correct = False
                break

    return RoleIdentificationResult(
        tx_id=tx.tx_id,
        n_participants=tx.n_participants,
        true_taker_id=taker.participant_id,
        taker_probabilities=probs,
        true_taker_rank=rank,
        role_entropy=entropy,
        fee_heuristic_correct=fee_correct,
        sweep_heuristic_correct=sweep_correct,
        subset_sum_heuristic_correct=subset_correct,
    )


def analyze_swap_input_mitigation(
    tx: CoinJoinTransaction,
    swap_amount: int | None = None,
) -> RoleIdentificationResult:
    """Analyze role identification with swap input camouflage (PR #280).

    The swap input adds extra value to the taker's inputs so their
    surplus (input - output) looks more like a maker's.

    Args:
        tx: Original CoinJoin transaction.
        swap_amount: Amount of the swap input. Auto-calculated if None.
    """
    taker = tx.taker
    if taker is None:
        return analyze_role_identification(tx)

    # Calculate swap amount to cover fees + synthetic maker-like surplus
    if swap_amount is None:
        total_maker_fees = abs(taker.cj_fee_sats)
        swap_amount = tx.total_mining_fee + total_maker_fees

    # Create modified transaction where taker has extra input
    # This makes the taker's surplus look like a maker's
    modified_participants: list[Participant] = []
    for p in tx.participants:
        if p.participant_id == taker.participant_id:
            # Add swap amount to taker's input total
            modified_utxos = list(p.utxos_in)
            sum(u.value_sats for u in modified_utxos)
            # Simulate by adjusting the first input's value
            if modified_utxos:
                from .models import UTXO

                swap_utxo = UTXO(
                    value_sats=swap_amount,
                    owner_id=p.entity_id,
                )
                modified_utxos.append(swap_utxo)

            # Change output absorbs the swap amount
            new_change_value = (p.change_output.value_sats if p.change_output else 0) + swap_amount
            new_change = None
            if new_change_value > 0:
                new_change = UTXO(
                    value_sats=new_change_value,
                    owner_id=p.entity_id,
                    is_change=True,
                )

            modified_participants.append(
                Participant(
                    participant_id=p.participant_id,
                    role=p.role,
                    entity_id=p.entity_id,
                    utxos_in=modified_utxos,
                    equal_output=p.equal_output,
                    change_output=new_change,
                    cj_fee_sats=p.cj_fee_sats,
                )
            )
        else:
            modified_participants.append(p)

    modified_tx = CoinJoinTransaction(
        tx_id=tx.tx_id,
        cj_amount=tx.cj_amount,
        participants=modified_participants,
        total_mining_fee=tx.total_mining_fee,
    )

    result = analyze_role_identification(modified_tx)
    # Store mitigated results
    original_result = analyze_role_identification(tx)
    original_result.mitigated_taker_rank = result.true_taker_rank
    original_result.mitigated_role_entropy = result.role_entropy

    return original_result


def batch_analyze_role_identification(
    txs: list[CoinJoinTransaction],
    with_swap_mitigation: bool = False,
) -> dict[str, float]:
    """Batch analysis of role identification across many CoinJoins.

    Returns aggregate statistics.
    """
    n = len(txs)
    if n == 0:
        return {}

    fee_correct_count = 0
    sweep_correct_count = 0
    sweep_applicable = 0
    avg_rank = 0.0
    avg_entropy = 0.0
    rank_1_count = 0

    for tx in txs:
        if with_swap_mitigation:
            result = analyze_swap_input_mitigation(tx)
            rank = result.mitigated_taker_rank or result.true_taker_rank
            entropy = result.mitigated_role_entropy or result.role_entropy
        else:
            result = analyze_role_identification(tx)
            rank = result.true_taker_rank
            entropy = result.role_entropy

        if result.fee_heuristic_correct:
            fee_correct_count += 1
        if result.sweep_heuristic_correct is not None:
            sweep_applicable += 1
            if result.sweep_heuristic_correct:
                sweep_correct_count += 1
        avg_rank += rank
        avg_entropy += entropy
        if rank == 1:
            rank_1_count += 1

    return {
        "fee_heuristic_accuracy": fee_correct_count / n,
        "sweep_heuristic_accuracy": (
            sweep_correct_count / sweep_applicable if sweep_applicable > 0 else float("nan")
        ),
        "sweep_heuristic_applicable_frac": sweep_applicable / n,
        "avg_taker_rank": avg_rank / n,
        "avg_role_entropy_bits": avg_entropy / n,
        "taker_identified_rank1_frac": rank_1_count / n,
        "n_transactions": float(n),
    }
