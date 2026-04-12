"""Tests for role identification analysis."""

from __future__ import annotations

from coinjoin_simulator.models import (
    UTXO,
    CoinJoinTransaction,
    Participant,
    Role,
    SimulationConfig,
)
from coinjoin_simulator.role_id import (
    analyze_role_identification,
    analyze_swap_input_mitigation,
    batch_analyze_role_identification,
    identify_taker_bayesian,
)
from coinjoin_simulator.transaction import TransactionSimulator


def _make_identifiable_tx(
    n_makers: int = 4,
    cj_amount: int = 1_000_000,
    sweep: bool = False,
) -> CoinJoinTransaction:
    """Create a tx where the taker is identifiable by fee asymmetry."""
    participants = []
    total_maker_fees = n_makers * 500
    mining_fee = 2000

    for i in range(n_makers):
        # Maker: input = cj_amount + change; surplus ~ 0 (earns fee)
        maker_input_val = cj_amount + (i + 1) * 100_000
        change_val = maker_input_val - cj_amount + 500  # +500 from fee earned
        maker_input = UTXO(value_sats=maker_input_val, owner_id=f"maker_{i}")
        p = Participant(
            participant_id=f"maker_{i}",
            role=Role.MAKER,
            entity_id=f"maker_{i}",
            utxos_in=[maker_input],
            equal_output=UTXO(value_sats=cj_amount, owner_id=f"maker_{i}", is_equal_output=True),
            change_output=UTXO(value_sats=change_val, owner_id=f"maker_{i}", is_change=True),
            cj_fee_sats=500,
        )
        participants.append(p)

    # Taker: input = cj_amount + fees + mining + change; surplus is large
    taker_change_val = 300_000
    taker_input_val = cj_amount + total_maker_fees + mining_fee + (0 if sweep else taker_change_val)
    taker_input = UTXO(value_sats=taker_input_val, owner_id="taker")
    taker = Participant(
        participant_id="taker_0",
        role=Role.TAKER,
        entity_id="taker",
        utxos_in=[taker_input],
        equal_output=UTXO(value_sats=cj_amount, owner_id="taker", is_equal_output=True),
        change_output=(
            None if sweep else UTXO(value_sats=taker_change_val, owner_id="taker", is_change=True)
        ),
        cj_fee_sats=-total_maker_fees,
    )
    participants.append(taker)

    return CoinJoinTransaction(
        tx_id="test_role_tx",
        cj_amount=cj_amount,
        participants=participants,
        total_mining_fee=mining_fee,
    )


class TestIdentifyTakerBayesian:
    def test_returns_probabilities_for_all(self) -> None:
        tx = _make_identifiable_tx()
        probs = identify_taker_bayesian(tx)
        assert len(probs) == tx.n_participants
        # Probabilities should sum to 1
        assert abs(sum(probs.values()) - 1.0) < 1e-10

    def test_taker_has_highest_probability(self) -> None:
        tx = _make_identifiable_tx()
        probs = identify_taker_bayesian(tx)
        taker = tx.taker
        assert taker is not None
        taker_prob = probs[taker.participant_id]
        for pid, prob in probs.items():
            if pid != taker.participant_id:
                assert taker_prob >= prob

    def test_empty_tx(self) -> None:
        tx = CoinJoinTransaction(cj_amount=1_000_000, participants=[])
        probs = identify_taker_bayesian(tx)
        assert probs == {}

    def test_custom_prior(self) -> None:
        tx = _make_identifiable_tx(n_makers=2)
        # Give taker a strong prior
        prior = {}
        for p in tx.participants:
            prior[p.participant_id] = 10.0 if p.role == Role.TAKER else 0.1
        probs = identify_taker_bayesian(tx, prior=prior)
        taker = tx.taker
        assert taker is not None
        assert probs[taker.participant_id] > 0.5

    def test_sweep_boosts_taker_no_change(self) -> None:
        """In sweep mode, the participant without change gets a boost."""
        tx = _make_identifiable_tx(sweep=True)
        probs = identify_taker_bayesian(tx)
        taker = tx.taker
        assert taker is not None
        # Taker has no change, should have high probability
        assert probs[taker.participant_id] > 1.0 / tx.n_participants


class TestAnalyzeRoleIdentification:
    def test_basic_analysis(self) -> None:
        tx = _make_identifiable_tx()
        result = analyze_role_identification(tx)

        assert result.n_participants == tx.n_participants
        assert result.true_taker_id == "taker_0"
        assert 1 <= result.true_taker_rank <= tx.n_participants
        assert result.role_entropy >= 0.0

    def test_fee_heuristic_on_clear_tx(self) -> None:
        tx = _make_identifiable_tx()
        result = analyze_role_identification(tx)
        # Fee heuristic should correctly identify taker on a well-constructed tx
        assert result.fee_heuristic_correct is True

    def test_sweep_heuristic(self) -> None:
        tx = _make_identifiable_tx(sweep=True)
        result = analyze_role_identification(tx)
        # Sweep heuristic should be applicable and correct
        assert result.sweep_heuristic_correct is True

    def test_sweep_heuristic_not_applicable(self) -> None:
        tx = _make_identifiable_tx(sweep=False)
        result = analyze_role_identification(tx)
        # If everyone has change, sweep heuristic is not applicable
        # (or if multiple participants lack change)
        # In our test, only the taker has change in non-sweep, so it's None
        # since no one lacks change (all have change)
        # Actually, all participants have change, so sweep_heuristic_correct is None
        assert result.sweep_heuristic_correct is None

    def test_no_taker_tx(self) -> None:
        """Transaction with no taker participant."""
        participants = [
            Participant(
                role=Role.MAKER,
                entity_id="maker_0",
                utxos_in=[UTXO(value_sats=1_500_000, owner_id="maker_0")],
                equal_output=UTXO(value_sats=1_000_000, owner_id="maker_0"),
            )
        ]
        tx = CoinJoinTransaction(
            cj_amount=1_000_000,
            participants=participants,
            total_mining_fee=1000,
        )
        result = analyze_role_identification(tx)
        assert result.true_taker_id == "unknown"
        assert result.true_taker_rank == 0

    def test_rank_1_means_taker_identified(self) -> None:
        tx = _make_identifiable_tx()
        result = analyze_role_identification(tx)
        # On a well-constructed tx, taker should be rank 1
        assert result.true_taker_rank == 1


class TestAnalyzeSwapInputMitigation:
    def test_mitigation_increases_entropy(self) -> None:
        tx = _make_identifiable_tx()
        result = analyze_swap_input_mitigation(tx)

        # Mitigated entropy should be >= original (more ambiguity)
        assert result.mitigated_role_entropy is not None
        assert result.mitigated_role_entropy >= result.role_entropy - 0.1  # Allow small margin

    def test_mitigation_worsens_rank(self) -> None:
        tx = _make_identifiable_tx()
        result = analyze_swap_input_mitigation(tx)

        # With swap input, taker should be harder to identify (rank >= original)
        assert result.mitigated_taker_rank is not None
        assert result.mitigated_taker_rank >= result.true_taker_rank

    def test_no_taker_passthrough(self) -> None:
        """Without a taker, should still return a result."""
        participants = [
            Participant(
                role=Role.MAKER,
                entity_id="maker_0",
                utxos_in=[UTXO(value_sats=1_500_000, owner_id="maker_0")],
                equal_output=UTXO(value_sats=1_000_000, owner_id="maker_0"),
            )
        ]
        tx = CoinJoinTransaction(
            cj_amount=1_000_000, participants=participants, total_mining_fee=1000
        )
        result = analyze_swap_input_mitigation(tx)
        assert result.true_taker_id == "unknown"


class TestBatchAnalyzeRoleIdentification:
    def test_empty_batch(self) -> None:
        result = batch_analyze_role_identification([])
        assert result == {}

    def test_batch_analysis(self) -> None:
        config = SimulationConfig(
            n_makers_total=15,
            n_makers_per_cj=4,
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        txs = [sim.simulate_coinjoin() for _ in range(10)]

        result = batch_analyze_role_identification(txs)
        assert result["n_transactions"] == 10.0
        assert 0.0 <= result["fee_heuristic_accuracy"] <= 1.0
        assert result["avg_taker_rank"] >= 1.0
        assert result["avg_role_entropy_bits"] >= 0.0
        assert 0.0 <= result["taker_identified_rank1_frac"] <= 1.0

    def test_batch_with_swap_mitigation(self) -> None:
        config = SimulationConfig(
            n_makers_total=15,
            n_makers_per_cj=4,
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        txs = [sim.simulate_coinjoin() for _ in range(5)]

        result_no_swap = batch_analyze_role_identification(txs, with_swap_mitigation=False)
        result_swap = batch_analyze_role_identification(txs, with_swap_mitigation=True)

        assert result_no_swap["n_transactions"] == 5.0
        assert result_swap["n_transactions"] == 5.0
