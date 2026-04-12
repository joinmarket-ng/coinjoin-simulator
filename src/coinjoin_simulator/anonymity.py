"""Anonymity set analysis for CoinJoin transactions.

Computes anonymity metrics for equal outputs, change outputs, and different
roles under various assumptions:

1. Naive: All makers honest, anon set = number of equal outputs
2. Change analysis: Using ILP-style subset sum to map inputs to change outputs
3. Post-spend behavior: Equal outputs spent immediately are more likely taker
4. Sybil-aware: Reduced anonymity when some makers are controlled by same entity
5. Role identification: Taker distinguishability via fee asymmetry
"""

from __future__ import annotations

import math
from itertools import permutations

from .models import (
    AnonymityMetrics,
    CoinJoinTransaction,
    Participant,
    Role,
)


def compute_naive_anonymity(tx: CoinJoinTransaction) -> dict[str, AnonymityMetrics]:
    """Compute naive anonymity sets.

    Assumption: All participants are honest distinct entities.
    Equal outputs have anonymity set = number of equal outputs.
    Change outputs have anonymity set = 0 (uniquely linkable if mapping is unique).
    """
    n_equal = len(tx.equal_outputs)
    results: dict[str, AnonymityMetrics] = {}

    for p in tx.participants:
        if p.equal_output is not None:
            # Uniform distribution over all equal output owners
            probs = {pp.entity_id: 1.0 / n_equal for pp in tx.participants}
            entropy = math.log2(n_equal) if n_equal > 1 else 0.0
            results[p.equal_output.outpoint] = AnonymityMetrics(
                naive_anon_set=n_equal,
                effective_anon_set=float(n_equal),
                entropy_bits=entropy,
                owner_probabilities=probs,
                is_uniquely_mapped=False,
                n_valid_mappings=math.factorial(n_equal),
            )

        if p.change_output is not None:
            # Naive: change is uniquely mapped if there's exactly one valid mapping
            results[p.change_output.outpoint] = AnonymityMetrics(
                naive_anon_set=1,
                effective_anon_set=1.0,
                entropy_bits=0.0,
                owner_probabilities={p.entity_id: 1.0},
                is_uniquely_mapped=True,
                n_valid_mappings=1,
            )

    return results


def compute_change_anonymity(tx: CoinJoinTransaction) -> dict[str, AnonymityMetrics]:
    """Compute change output anonymity using subset-sum analysis.

    For each change output, find how many valid input-to-change mappings exist
    given the fee constraints. If multiple valid mappings exist, the change
    output has anonymity > 0.

    This implements a simplified version of the joinmarket_analyzer ILP approach.
    """
    participants = tx.participants
    n = len(participants)
    results: dict[str, AnonymityMetrics] = {}

    # Build input/output value vectors
    input_totals: list[int] = []
    change_values: list[int | None] = []
    equal_values: list[int] = []

    for p in participants:
        total_in = sum(u.value_sats for u in p.utxos_in)
        input_totals.append(total_in)
        change_values.append(p.change_output.value_sats if p.change_output else None)
        equal_values.append(p.equal_output.value_sats if p.equal_output else 0)

    # For each permutation of change output assignments, check validity
    # A valid assignment means: for each participant i assigned change j,
    # input_i - cj_amount - fee_i = change_j (within tolerance)
    #
    # This is combinatorially expensive for large n, so we use a greedy approach
    # for n > 8 and exact enumeration for small n.

    [i for i, c in enumerate(change_values) if c is not None]
    [i for i, c in enumerate(change_values) if c is None]

    if n <= 10:
        valid_mappings = _enumerate_valid_mappings(
            input_totals, change_values, tx.cj_amount, participants, tx.total_mining_fee
        )
    else:
        valid_mappings = _greedy_valid_mappings(
            input_totals, change_values, tx.cj_amount, participants, tx.total_mining_fee
        )

    n_mappings = len(valid_mappings)

    # Compute per-change-output anonymity
    for idx, p in enumerate(participants):
        if p.change_output is None:
            continue

        # Count how many different participants could own this change output
        possible_owners: dict[str, int] = {}
        for mapping in valid_mappings:
            # mapping[idx] = which participant index owns change output idx
            owner_idx = mapping.get(idx)
            if owner_idx is not None:
                entity = participants[owner_idx].entity_id
                possible_owners[entity] = possible_owners.get(entity, 0) + 1

        if n_mappings > 0:
            probs = {e: count / n_mappings for e, count in possible_owners.items()}
        else:
            probs = {p.entity_id: 1.0}

        entropy = _shannon_entropy(list(probs.values()))
        eff_anon = 2**entropy if entropy > 0 else 1.0

        results[p.change_output.outpoint] = AnonymityMetrics(
            naive_anon_set=1,
            effective_anon_set=eff_anon,
            entropy_bits=entropy,
            owner_probabilities=probs,
            is_uniquely_mapped=(len(possible_owners) <= 1),
            n_valid_mappings=n_mappings,
        )

    return results


def _enumerate_valid_mappings(
    input_totals: list[int],
    change_values: list[int | None],
    cj_amount: int,
    participants: list[Participant],
    mining_fee: int,
) -> list[dict[int, int]]:
    """Enumerate all valid input-to-change-output mappings.

    A mapping assigns each change output to a participant such that:
    input_total - cj_amount - fee = change_value (approximately)
    """
    n = len(participants)
    change_indices = [i for i, c in enumerate(change_values) if c is not None]
    [i for i, c in enumerate(change_values) if c is None]

    if not change_indices:
        return [{}]

    valid: list[dict[int, int]] = []

    # Fee tolerance: fees can vary, so we allow some tolerance
    max_fee_tolerance = max(10_000, int(cj_amount * 0.01))  # 1% or 10k sats

    # Try all permutations of participant-to-change assignments
    all_participant_indices = list(range(n))

    for perm in permutations(all_participant_indices, len(change_indices)):
        mapping: dict[int, int] = {}
        valid_perm = True

        for change_idx, participant_idx in zip(change_indices, perm, strict=False):
            # Check: can participant_idx produce change output at change_idx?
            p_fee = participants[participant_idx].cj_fee_sats
            expected_change = input_totals[participant_idx] - cj_amount + p_fee
            if participants[participant_idx].role == Role.TAKER:
                expected_change = (
                    input_totals[participant_idx]
                    - cj_amount
                    - abs(participants[participant_idx].cj_fee_sats)
                    - mining_fee
                )

            actual_change = change_values[change_idx]
            if actual_change is None:
                valid_perm = False
                break

            if abs(expected_change - actual_change) > max_fee_tolerance:
                valid_perm = False
                break

            mapping[change_idx] = participant_idx

        if valid_perm:
            valid.append(mapping)

    return valid


def _greedy_valid_mappings(
    input_totals: list[int],
    change_values: list[int | None],
    cj_amount: int,
    participants: list[Participant],
    mining_fee: int,
) -> list[dict[int, int]]:
    """Greedy heuristic for finding valid mappings (for large transactions).

    Uses deterministic single-match elimination, then counts remaining ambiguity.
    """
    n = len(participants)
    change_indices = [i for i, c in enumerate(change_values) if c is not None]

    if not change_indices:
        return [{}]

    max_fee_tolerance = max(10_000, int(cj_amount * 0.01))

    # Build compatibility matrix
    compatible: dict[int, list[int]] = {ci: [] for ci in change_indices}
    for ci in change_indices:
        for pi in range(n):
            expected_change = input_totals[pi] - cj_amount + participants[pi].cj_fee_sats
            if participants[pi].role == Role.TAKER:
                expected_change = (
                    input_totals[pi] - cj_amount - abs(participants[pi].cj_fee_sats) - mining_fee
                )

            actual = change_values[ci]
            if actual is not None and abs(expected_change - actual) <= max_fee_tolerance:
                compatible[ci].append(pi)

    # Greedy: iteratively assign uniquely-determined change outputs
    mapping: dict[int, int] = {}
    assigned_participants: set[int] = set()
    changed = True
    while changed:
        changed = False
        for ci in change_indices:
            if ci in mapping:
                continue
            candidates = [p for p in compatible[ci] if p not in assigned_participants]
            if len(candidates) == 1:
                mapping[ci] = candidates[0]
                assigned_participants.add(candidates[0])
                changed = True

    # Count remaining ambiguity
    unassigned = [ci for ci in change_indices if ci not in mapping]
    if not unassigned:
        return [mapping]

    # For remaining ambiguous outputs, estimate number of valid mappings
    # (exact enumeration would be factorial)
    n_ambiguous = len(unassigned)
    # Return the single greedy mapping plus estimate
    return [mapping] * max(1, math.factorial(min(n_ambiguous, 5)))


def compute_role_anonymity(
    tx: CoinJoinTransaction,
    fee_knowledge: bool = True,
) -> dict[str, float]:
    """Compute role identification probabilities.

    Returns probability that each participant is the taker, based on:
    - Fee asymmetry (taker pays mining fee + maker fees)
    - Change output patterns (taker may have no change in sweep mode)
    - Input/output value analysis

    Args:
        tx: The CoinJoin transaction to analyze.
        fee_knowledge: Whether the observer knows typical fee ranges.

    Returns:
        Mapping from participant_id to probability of being the taker.
    """
    n = len(tx.participants)
    if n == 0:
        return {}

    # Start with uniform prior
    taker_probs = {p.participant_id: 1.0 / n for p in tx.participants}

    if fee_knowledge:
        # Bayesian update based on fee asymmetry
        # The taker's input minus (equal output + change) should be larger
        # than any maker's because taker pays mining fee + all maker fees
        for p in tx.participants:
            total_in = sum(u.value_sats for u in p.utxos_in)
            total_out = 0
            if p.equal_output:
                total_out += p.equal_output.value_sats
            if p.change_output:
                total_out += p.change_output.value_sats
            surplus = total_in - total_out

            # Taker typically has largest surplus (pays all fees)
            # Makers have small or zero surplus (they earn fees)
            if surplus > tx.total_mining_fee * 0.5:
                taker_probs[p.participant_id] *= 3.0
            elif surplus < 0:
                # Maker earning fees
                taker_probs[p.participant_id] *= 0.1

    # No-change heuristic: sweep mode taker has no change
    n_no_change = sum(1 for p in tx.participants if p.change_output is None)
    if n_no_change < n:  # Not everyone lacks change
        for p in tx.participants:
            if p.change_output is None:
                # No change output is slightly more likely taker (sweep mode)
                taker_probs[p.participant_id] *= 1.5

    # Normalize
    total = sum(taker_probs.values())
    if total > 0:
        taker_probs = {k: v / total for k, v in taker_probs.items()}

    return taker_probs


def compute_sybil_aware_anonymity(
    tx: CoinJoinTransaction,
    entity_map: dict[str, str] | None = None,
) -> dict[str, AnonymityMetrics]:
    """Compute anonymity sets aware of sybil participants.

    When a sybil entity controls multiple makers, the effective anonymity
    set is reduced because those makers are actually the same entity.

    Args:
        tx: The CoinJoin transaction.
        entity_map: Mapping from participant_id to entity_id. If None,
                    uses the entity_id from participants directly.
    """
    # Count unique entities among equal output owners
    entities: set[str] = set()
    for p in tx.participants:
        if entity_map:
            entities.add(entity_map.get(p.participant_id, p.entity_id))
        else:
            entities.add(p.entity_id)

    n_unique_entities = len(entities)
    n_equal = len(tx.equal_outputs)

    results: dict[str, AnonymityMetrics] = {}

    for p in tx.participants:
        if p.equal_output is None:
            continue

        entity_map.get(p.participant_id, p.entity_id) if entity_map else p.entity_id

        # Count how many equal outputs each entity controls
        entity_counts: dict[str, int] = {}
        for pp in tx.participants:
            if pp.equal_output is None:
                continue
            e = entity_map.get(pp.participant_id, pp.entity_id) if entity_map else pp.entity_id
            entity_counts[e] = entity_counts.get(e, 0) + 1

        # Probability distribution: each entity's probability is proportional
        # to the number of equal outputs they control
        probs = {e: count / n_equal for e, count in entity_counts.items()}
        entropy = _shannon_entropy(list(probs.values()))

        results[p.equal_output.outpoint] = AnonymityMetrics(
            naive_anon_set=n_equal,
            effective_anon_set=float(n_unique_entities),
            entropy_bits=entropy,
            owner_probabilities=probs,
            is_uniquely_mapped=False,
            n_valid_mappings=n_equal,  # Simplified
        )

    return results


def compute_post_spend_anonymity(
    tx: CoinJoinTransaction,
    spent_outputs: set[str],
    blocks_until_spend: dict[str, int] | None = None,
) -> dict[str, AnonymityMetrics]:
    """Compute anonymity after observing spending behavior.

    If an equal output is spent quickly after the CoinJoin, it is more likely
    to belong to the taker (who initiated the CoinJoin for a specific purpose).

    Args:
        tx: The CoinJoin transaction.
        spent_outputs: Set of outpoints that have been spent.
        blocks_until_spend: Outpoint -> number of blocks until spent.
    """
    n_equal = len(tx.equal_outputs)
    results: dict[str, AnonymityMetrics] = {}

    if blocks_until_spend is None:
        blocks_until_spend = {}

    # Prior: uniform
    for p in tx.participants:
        if p.equal_output is None:
            continue

        outpoint = p.equal_output.outpoint
        blocks_until_spend.get(outpoint, float("inf"))

        # Bayesian prior: uniform across all equal outputs
        probs: dict[str, float] = {}
        for pp in tx.participants:
            if pp.equal_output is None:
                continue
            pp_outpoint = pp.equal_output.outpoint
            pp_blocks = blocks_until_spend.get(pp_outpoint, float("inf"))

            # Likelihood of being taker given spending time
            if pp_blocks == float("inf"):
                # Not spent: slight evidence of being a maker (holding)
                likelihood = 0.8
            elif pp_blocks <= 1:
                # Spent immediately: strong evidence of being taker
                likelihood = 3.0
            elif pp_blocks <= 6:
                # Spent within an hour: moderate evidence
                likelihood = 2.0
            elif pp_blocks <= 144:
                # Spent within a day: slight evidence
                likelihood = 1.2
            else:
                likelihood = 1.0

            probs[pp.entity_id] = probs.get(pp.entity_id, 0.0) + likelihood

        # Normalize
        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}

        entropy = _shannon_entropy(list(probs.values()))

        results[outpoint] = AnonymityMetrics(
            naive_anon_set=n_equal,
            effective_anon_set=2**entropy if entropy > 0 else 1.0,
            entropy_bits=entropy,
            owner_probabilities=probs,
            is_uniquely_mapped=False,
            n_valid_mappings=n_equal,
        )

    return results


def _shannon_entropy(probabilities: list[float]) -> float:
    """Compute Shannon entropy in bits."""
    entropy = 0.0
    for p in probabilities:
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy
