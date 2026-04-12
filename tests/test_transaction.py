"""Tests for the transaction simulator."""

from __future__ import annotations

from typing import Literal

from coinjoin_simulator.models import Role, SimulationConfig
from coinjoin_simulator.transaction import (
    TransactionSimulator,
    _round_to_power_of_2,
    estimate_tx_fee,
)


class TestFeeCalculation:
    def test_estimate_tx_fee(self) -> None:
        # 4 inputs, 8 outputs, 10 sat/vB
        fee = estimate_tx_fee(4, 8, 10)
        # 11 + 4*68 + 8*31 = 11 + 272 + 248 = 531 -> 531*10 = 5310
        assert fee == 5310

    def test_estimate_tx_fee_single(self) -> None:
        fee = estimate_tx_fee(1, 1, 1)
        # 11 + 68 + 31 = 110
        assert fee == 110


class TestRoundToPower:
    def test_powers(self) -> None:
        assert _round_to_power_of_2(1) == 1
        assert _round_to_power_of_2(2) == 2
        assert _round_to_power_of_2(3) == 2
        assert _round_to_power_of_2(4) == 4
        assert _round_to_power_of_2(1_000_000) == 524288  # 2^19
        assert _round_to_power_of_2(0) == 0


class TestTransactionSimulator:
    def test_generate_orderbook(self) -> None:
        config = SimulationConfig(
            n_makers_total=20,
            n_sybil_entities=0,
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        ob = sim.generate_orderbook()
        assert len(ob.offers) == 20

    def test_generate_orderbook_with_sybils(self) -> None:
        config = SimulationConfig(
            n_makers_total=20,
            n_sybil_entities=2,
            sybil_makers_per_entity=3,
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        ob = sim.generate_orderbook()
        # 20 - 6 honest + 6 sybil = 20
        assert len(ob.offers) == 20

        sybil_entities = {o.entity_id for o in ob.offers if o.entity_id.startswith("sybil")}
        assert len(sybil_entities) == 2

    def test_simulate_coinjoin(self) -> None:
        config = SimulationConfig(
            n_makers_total=20,
            n_makers_per_cj=5,
            cj_amount=500_000,
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        tx = sim.simulate_coinjoin()

        assert tx.n_participants == 6  # 5 makers + 1 taker
        assert tx.cj_amount == 500_000
        assert tx.taker is not None
        assert tx.taker.role == Role.TAKER
        assert tx.n_makers == 5

        # All equal outputs should have the same value
        for eq in tx.equal_outputs:
            assert eq.value_sats == 500_000

    def test_simulate_coinjoin_sweep(self) -> None:
        config = SimulationConfig(
            n_makers_total=20,
            n_makers_per_cj=5,
            cj_amount=500_000,
            sweep_probability=1.0,  # Force sweep
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        tx = sim.simulate_coinjoin()

        taker = tx.taker
        assert taker is not None
        # In sweep mode, taker has no change output
        assert taker.change_output is None

    def test_simulate_chain(self) -> None:
        config = SimulationConfig(
            n_makers_total=20,
            n_makers_per_cj=5,
            n_coinjoins=10,
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        txs = sim.simulate_chain()

        assert len(txs) == 10
        for tx in txs:
            assert tx.n_participants == 6

    def test_different_selection_algorithms(self) -> None:
        algo: Literal["fidelity_bond_weighted", "random", "cheapest"]
        for algo in ("fidelity_bond_weighted", "random", "cheapest"):
            config = SimulationConfig(
                n_makers_total=20,
                n_makers_per_cj=5,
                selection_algorithm=algo,
                random_seed=42,
            )
            sim = TransactionSimulator(config)
            tx = sim.simulate_coinjoin()
            assert tx.n_participants == 6

    def test_mining_fee_positive(self) -> None:
        config = SimulationConfig(
            n_makers_total=20,
            n_makers_per_cj=5,
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        tx = sim.simulate_coinjoin()
        assert tx.total_mining_fee > 0

    def test_inputs_exceed_outputs(self) -> None:
        """Total inputs should exceed total outputs (difference = mining fee)."""
        config = SimulationConfig(
            n_makers_total=20,
            n_makers_per_cj=5,
            random_seed=42,
        )
        sim = TransactionSimulator(config)
        tx = sim.simulate_coinjoin()

        total_in = sum(u.value_sats for u in tx.all_inputs)
        total_out = sum(u.value_sats for u in tx.all_outputs)
        assert total_in > total_out
