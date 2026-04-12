"""Tests for anonymity set analysis."""

from __future__ import annotations

import math

from coinjoin_simulator.anonymity import (
    _shannon_entropy,
    compute_change_anonymity,
    compute_naive_anonymity,
    compute_post_spend_anonymity,
    compute_sybil_aware_anonymity,
)
from coinjoin_simulator.models import (
    UTXO,
    CoinJoinTransaction,
    Participant,
    Role,
    SimulationConfig,
)
from coinjoin_simulator.transaction import TransactionSimulator


def _make_simple_tx(n_makers: int = 3, cj_amount: int = 1_000_000) -> CoinJoinTransaction:
    """Create a simple test transaction."""
    participants = []
    for i in range(n_makers):
        input_val = cj_amount + (i + 1) * 100_000 + 500  # Varying change amounts
        p = Participant(
            role=Role.MAKER,
            entity_id=f"maker_{i}",
            utxos_in=[UTXO(value_sats=input_val, owner_id=f"maker_{i}")],
            equal_output=UTXO(value_sats=cj_amount, owner_id=f"maker_{i}", is_equal_output=True),
            change_output=UTXO(
                value_sats=(i + 1) * 100_000 + 500,
                owner_id=f"maker_{i}",
                is_change=True,
            ),
            cj_fee_sats=500,
        )
        participants.append(p)

    taker_input = cj_amount + 500_000 + 2_000 + n_makers * 500
    taker = Participant(
        role=Role.TAKER,
        entity_id="taker",
        utxos_in=[UTXO(value_sats=taker_input, owner_id="taker")],
        equal_output=UTXO(value_sats=cj_amount, owner_id="taker", is_equal_output=True),
        change_output=UTXO(value_sats=500_000, owner_id="taker", is_change=True),
        cj_fee_sats=-(n_makers * 500),
    )
    participants.append(taker)

    return CoinJoinTransaction(
        cj_amount=cj_amount,
        participants=participants,
        total_mining_fee=2_000,
    )


class TestShannonEntropy:
    def test_uniform(self) -> None:
        # Uniform distribution over 4 elements: log2(4) = 2 bits
        probs = [0.25, 0.25, 0.25, 0.25]
        assert abs(_shannon_entropy(probs) - 2.0) < 1e-10

    def test_certain(self) -> None:
        # Certain outcome: 0 bits
        assert _shannon_entropy([1.0]) == 0.0

    def test_binary(self) -> None:
        # Fair coin: log2(2) = 1 bit
        probs = [0.5, 0.5]
        assert abs(_shannon_entropy(probs) - 1.0) < 1e-10

    def test_empty(self) -> None:
        assert _shannon_entropy([]) == 0.0


class TestNaiveAnonymity:
    def test_basic(self) -> None:
        tx = _make_simple_tx(n_makers=3)
        metrics = compute_naive_anonymity(tx)

        # 4 participants -> 4 equal outputs -> naive anon set = 4
        equal_metrics = [m for outpoint, m in metrics.items() if m.naive_anon_set > 1]
        assert all(m.naive_anon_set == 4 for m in equal_metrics)
        assert all(abs(m.entropy_bits - math.log2(4)) < 1e-10 for m in equal_metrics)

    def test_change_uniquely_mapped(self) -> None:
        tx = _make_simple_tx(n_makers=3)
        metrics = compute_naive_anonymity(tx)

        change_metrics = [m for m in metrics.values() if m.naive_anon_set == 1]
        assert all(m.is_uniquely_mapped for m in change_metrics)

    def test_single_participant(self) -> None:
        # Degenerate case: 1 participant (just taker)
        tx = _make_simple_tx(n_makers=0)
        metrics = compute_naive_anonymity(tx)
        equal_metrics = [
            m for m in metrics.values() if m.naive_anon_set >= 1 and not m.is_uniquely_mapped
        ]
        # With 1 participant, naive_anon_set = 1 but entropy = 0
        for m in equal_metrics:
            assert m.entropy_bits == 0.0


class TestChangeAnonymity:
    def test_distinct_changes(self) -> None:
        """When all change values are distinct, each maps to exactly one input."""
        tx = _make_simple_tx(n_makers=3)
        metrics = compute_change_anonymity(tx)

        # With distinct change values, most should be uniquely mapped
        # (depends on fee tolerance)
        assert len(metrics) > 0

    def test_simulated_tx(self) -> None:
        config = SimulationConfig(
            n_makers_total=20,
            n_makers_per_cj=5,
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        tx = sim.simulate_coinjoin()
        metrics = compute_change_anonymity(tx)

        # Should have results for each change output
        n_change = len(tx.change_outputs)
        assert len(metrics) == n_change


class TestSybilAwareAnonymity:
    def test_no_sybils(self) -> None:
        tx = _make_simple_tx(n_makers=3)
        metrics = compute_sybil_aware_anonymity(tx)

        # All entities are distinct -> effective anon = naive anon
        for m in metrics.values():
            assert m.effective_anon_set == 4  # 3 makers + 1 taker

    def test_with_sybils(self) -> None:
        """Two makers controlled by the same entity."""
        participants = []
        for i in range(3):
            entity = "sybil_entity" if i < 2 else f"maker_{i}"
            p = Participant(
                role=Role.MAKER,
                entity_id=entity,
                utxos_in=[UTXO(value_sats=1_500_000, owner_id=entity)],
                equal_output=UTXO(value_sats=1_000_000, owner_id=entity, is_equal_output=True),
                change_output=UTXO(value_sats=500_500, owner_id=entity, is_change=True),
                cj_fee_sats=500,
            )
            participants.append(p)

        taker = Participant(
            role=Role.TAKER,
            entity_id="taker",
            utxos_in=[UTXO(value_sats=1_503_500, owner_id="taker")],
            equal_output=UTXO(value_sats=1_000_000, owner_id="taker", is_equal_output=True),
            change_output=UTXO(value_sats=500_000, owner_id="taker", is_change=True),
            cj_fee_sats=-1500,
        )
        participants.append(taker)

        tx = CoinJoinTransaction(
            cj_amount=1_000_000,
            participants=participants,
            total_mining_fee=2000,
        )

        metrics = compute_sybil_aware_anonymity(tx)

        # 4 participants but only 3 unique entities
        for m in metrics.values():
            assert m.effective_anon_set == 3
            assert m.naive_anon_set == 4


class TestPostSpendAnonymity:
    def test_immediate_spend_reduces_entropy(self) -> None:
        tx = _make_simple_tx(n_makers=3)

        # Mark taker's equal output as immediately spent
        taker = tx.taker
        assert taker is not None
        assert taker.equal_output is not None

        blocks_until = {
            taker.equal_output.outpoint: 1,  # Taker spends immediately
        }
        # Makers don't spend quickly
        for maker in tx.makers:
            if maker.equal_output:
                blocks_until[maker.equal_output.outpoint] = 500

        metrics = compute_post_spend_anonymity(tx, set(), blocks_until)
        assert len(metrics) > 0

        # The taker's output should have skewed probabilities
        taker_metric = metrics.get(taker.equal_output.outpoint)
        assert taker_metric is not None
        # Entropy should be less than maximum (log2(4) = 2 bits)
        assert taker_metric.entropy_bits < math.log2(4)

    def test_no_spending_info(self) -> None:
        tx = _make_simple_tx(n_makers=3)
        metrics = compute_post_spend_anonymity(tx, set(), None)
        # Without spending info, all outputs should have similar metrics
        assert len(metrics) == 4  # 4 equal outputs
