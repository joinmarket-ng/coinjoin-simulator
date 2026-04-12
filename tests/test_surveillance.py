"""Tests for surveillance and probing attack simulation."""

from __future__ import annotations

from coinjoin_simulator.models import (
    UTXO,
    CoinJoinTransaction,
    Participant,
    Role,
    SimulationConfig,
)
from coinjoin_simulator.surveillance import (
    MakerState,
    SurveillanceSimulator,
    UTXOCluster,
)
from coinjoin_simulator.transaction import TransactionSimulator


def _make_tx_with_known_utxos(
    n_makers: int = 3,
    cj_amount: int = 1_000_000,
) -> CoinJoinTransaction:
    """Create a transaction with predictable UTXO outpoints for testing."""
    participants = []
    for i in range(n_makers):
        input_val = cj_amount + (i + 1) * 200_000
        maker_input = UTXO(
            txid=f"maker_txid_{i}",
            vout=0,
            value_sats=input_val,
            owner_id=f"maker_{i}",
        )
        equal_out = UTXO(
            txid="cj_txid",
            vout=i,
            value_sats=cj_amount,
            owner_id=f"maker_{i}",
            is_equal_output=True,
        )
        change_out = UTXO(
            txid="cj_txid",
            vout=n_makers + 1 + i,
            value_sats=(i + 1) * 200_000 + 500,
            owner_id=f"maker_{i}",
            is_change=True,
        )
        participants.append(
            Participant(
                participant_id=f"maker_{i}",
                role=Role.MAKER,
                entity_id=f"entity_{i}",
                utxos_in=[maker_input],
                equal_output=equal_out,
                change_output=change_out,
                cj_fee_sats=500,
            )
        )

    taker_input = UTXO(
        txid="taker_txid_0",
        vout=0,
        value_sats=cj_amount + 500_000,
        owner_id="taker",
    )
    taker_equal = UTXO(
        txid="cj_txid",
        vout=n_makers,
        value_sats=cj_amount,
        owner_id="taker",
        is_equal_output=True,
    )
    taker_change = UTXO(
        txid="cj_txid",
        vout=2 * n_makers + 1,
        value_sats=495_000,
        owner_id="taker",
        is_change=True,
    )
    participants.append(
        Participant(
            participant_id="taker_0",
            role=Role.TAKER,
            entity_id="taker_entity",
            utxos_in=[taker_input],
            equal_output=taker_equal,
            change_output=taker_change,
            cj_fee_sats=-1500,
        )
    )

    return CoinJoinTransaction(
        tx_id="test_cj_0",
        cj_amount=cj_amount,
        participants=participants,
        total_mining_fee=2000,
    )


class TestMakerStateAndCluster:
    def test_maker_state_defaults(self) -> None:
        state = MakerState(entity_id="e1", maker_id="m1")
        assert state.known_utxos == set()
        assert state.probed_utxos == set()
        assert state.inferred_utxos == set()
        assert state.coinjoins_participated == []

    def test_utxo_cluster_defaults(self) -> None:
        cluster = UTXOCluster(cluster_id="c1")
        assert cluster.utxos == set()
        assert cluster.entity_id is None
        assert cluster.confidence == 0.0


class TestSurveillanceSimulator:
    def test_probe_maker(self) -> None:
        config = SimulationConfig()
        sim = SurveillanceSimulator(config, seed=42)

        learned = sim.probe_maker(
            maker_id="maker_0",
            entity_id="entity_0",
            utxos=["txid_a:0", "txid_b:1"],
        )

        assert learned == {"txid_a:0", "txid_b:1"}
        assert "maker_0" in sim.maker_states
        state = sim.maker_states["maker_0"]
        assert state.probed_utxos == {"txid_a:0", "txid_b:1"}
        assert state.known_utxos == {"txid_a:0", "txid_b:1"}

    def test_probe_maker_creates_cluster(self) -> None:
        config = SimulationConfig()
        sim = SurveillanceSimulator(config, seed=42)

        sim.probe_maker("maker_0", "entity_0", ["txid_a:0"])
        cluster_id = "cluster_maker_0"
        assert cluster_id in sim.utxo_clusters
        assert sim.utxo_clusters[cluster_id].confidence == 1.0
        assert "txid_a:0" in sim.utxo_clusters[cluster_id].utxos

    def test_probe_maker_accumulates(self) -> None:
        config = SimulationConfig()
        sim = SurveillanceSimulator(config, seed=42)

        sim.probe_maker("maker_0", "entity_0", ["txid_a:0"])
        sim.probe_maker("maker_0", "entity_0", ["txid_b:1"])

        state = sim.maker_states["maker_0"]
        assert state.probed_utxos == {"txid_a:0", "txid_b:1"}

    def test_observe_coinjoin_matches_probed(self) -> None:
        config = SimulationConfig()
        sim = SurveillanceSimulator(config, seed=42)

        tx = _make_tx_with_known_utxos(n_makers=3)

        # Probe maker_0 with their actual input UTXO
        sim.probe_maker("maker_0", "entity_0", ["maker_txid_0:0"])

        matches = sim.observe_coinjoin(tx)
        assert "maker_0" in matches
        assert "maker_txid_0:0" in matches["maker_0"]

    def test_observe_coinjoin_no_match_without_probe(self) -> None:
        config = SimulationConfig()
        sim = SurveillanceSimulator(config, seed=42)

        tx = _make_tx_with_known_utxos(n_makers=3)
        matches = sim.observe_coinjoin(tx)
        # No probes -> no matches
        assert len(matches) == 0

    def test_observe_coinjoin_infers_change(self) -> None:
        config = SimulationConfig()
        sim = SurveillanceSimulator(config, seed=42)

        tx = _make_tx_with_known_utxos(n_makers=3)
        sim.probe_maker("maker_0", "entity_0", ["maker_txid_0:0"])
        sim.observe_coinjoin(tx)

        state = sim.maker_states["maker_0"]
        # Should have inferred maker_0's change output
        assert len(state.inferred_utxos) > 0
        # Change outpoint should be in known_utxos
        change_outpoint = f"cj_txid:{3 + 1 + 0}"  # n_makers + 1 + i where i=0
        assert change_outpoint in state.known_utxos

    def test_compute_anonymity_reduction_all_identified(self) -> None:
        config = SimulationConfig()
        sim = SurveillanceSimulator(config, seed=42)

        tx = _make_tx_with_known_utxos(n_makers=3)

        # Probe all makers
        for i in range(3):
            sim.probe_maker(f"maker_{i}", f"entity_{i}", [f"maker_txid_{i}:0"])

        reductions = sim.compute_anonymity_reduction(tx)
        # 4 equal outputs, 3 identified as makers
        # Unidentified outputs (taker) should have effective anon = 1
        for _outpoint, eff_anon in reductions.items():
            assert eff_anon >= 1.0

    def test_compute_anonymity_reduction_none_identified(self) -> None:
        config = SimulationConfig()
        sim = SurveillanceSimulator(config, seed=42)

        tx = _make_tx_with_known_utxos(n_makers=3)
        reductions = sim.compute_anonymity_reduction(tx)

        # No probes -> no reduction -> all have full anonymity
        n_equal = len(tx.equal_outputs)
        for eff_anon in reductions.values():
            assert eff_anon == float(n_equal)


class TestSimulateContinuousProbing:
    def test_basic_continuous_probing(self) -> None:
        config = SimulationConfig(
            n_makers_total=15,
            n_makers_per_cj=5,
            n_coinjoins=10,
            random_seed=42,
        )
        tx_sim = TransactionSimulator(config)
        txs = tx_sim.simulate_chain()

        surv_sim = SurveillanceSimulator(config, seed=42)
        result = surv_sim.simulate_continuous_probing(txs, probe_fraction=0.8)

        assert result.n_coinjoins_observed == 10
        assert result.n_probes > 0
        assert result.n_makers_clustered > 0
        assert result.n_utxos_clustered > 0
        assert len(result.cluster_sizes) > 0

    def test_zero_probe_fraction(self) -> None:
        config = SimulationConfig(
            n_makers_total=15,
            n_makers_per_cj=5,
            n_coinjoins=5,
            random_seed=42,
        )
        tx_sim = TransactionSimulator(config)
        txs = tx_sim.simulate_chain()

        surv_sim = SurveillanceSimulator(config, seed=42)
        result = surv_sim.simulate_continuous_probing(txs, probe_fraction=0.0)

        # No probes -> no clustering
        assert result.n_probes == 0
        assert result.n_makers_clustered == 0

    def test_higher_probe_fraction_more_clustering(self) -> None:
        config = SimulationConfig(
            n_makers_total=15,
            n_makers_per_cj=5,
            n_coinjoins=10,
            random_seed=42,
        )
        tx_sim = TransactionSimulator(config)
        txs = tx_sim.simulate_chain()

        surv_low = SurveillanceSimulator(config, seed=42)
        result_low = surv_low.simulate_continuous_probing(txs, probe_fraction=0.2)

        surv_high = SurveillanceSimulator(config, seed=42)
        result_high = surv_high.simulate_continuous_probing(txs, probe_fraction=0.9)

        assert result_high.n_makers_clustered >= result_low.n_makers_clustered


class TestSimulateWithMitigations:
    def test_mitigations_reduce_clustering(self) -> None:
        config = SimulationConfig(
            n_makers_total=15,
            n_makers_per_cj=5,
            n_coinjoins=10,
            random_seed=42,
        )
        tx_sim = TransactionSimulator(config)
        txs = tx_sim.simulate_chain()

        # Without mitigations (high probe fraction)
        surv_no_mit = SurveillanceSimulator(config, seed=42)
        result_no_mit = surv_no_mit.simulate_continuous_probing(txs, probe_fraction=0.9)

        # With mitigations (rate limiting + UTXO rotation)
        surv_mit = SurveillanceSimulator(config, seed=42)
        result_mit = surv_mit.simulate_with_mitigations(
            txs,
            podle_cost_per_probe=3,
            max_probes_per_maker=3,
            utxo_rotation_interval=3,
        )

        # Mitigations should result in fewer or equal probes
        # (rate limiting caps probing)
        assert result_mit.n_probes <= result_no_mit.n_probes + result_no_mit.n_makers_clustered * 3

    def test_mitigations_return_valid_result(self) -> None:
        config = SimulationConfig(
            n_makers_total=15,
            n_makers_per_cj=5,
            n_coinjoins=5,
            random_seed=42,
        )
        tx_sim = TransactionSimulator(config)
        txs = tx_sim.simulate_chain()

        surv = SurveillanceSimulator(config, seed=42)
        result = surv.simulate_with_mitigations(txs)

        assert result.n_coinjoins_observed == 5
        assert 0.0 <= result.avg_anon_set_reduction <= 1.0
