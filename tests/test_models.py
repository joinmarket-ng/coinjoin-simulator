"""Tests for core models."""

from __future__ import annotations

from coinjoin_simulator.models import (
    UTXO,
    CoinJoinTransaction,
    Offer,
    OfferType,
    Participant,
    Role,
    SimulationConfig,
)


class TestUTXO:
    def test_create_utxo(self) -> None:
        utxo = UTXO(value_sats=100_000, owner_id="alice")
        assert utxo.value_sats == 100_000
        assert utxo.owner_id == "alice"
        assert utxo.vout == 0
        assert not utxo.is_change
        assert not utxo.is_equal_output

    def test_outpoint(self) -> None:
        utxo = UTXO(txid="abc123", vout=1, value_sats=50_000, owner_id="bob")
        assert utxo.outpoint == "abc123:1"


class TestParticipant:
    def test_maker(self) -> None:
        p = Participant(role=Role.MAKER, entity_id="maker1")
        assert p.role == Role.MAKER
        assert p.cj_fee_sats == 0

    def test_taker(self) -> None:
        p = Participant(role=Role.TAKER, entity_id="taker1")
        assert p.role == Role.TAKER


class TestCoinJoinTransaction:
    def _make_tx(self, n_makers: int = 3) -> CoinJoinTransaction:
        participants = []
        for i in range(n_makers):
            p = Participant(
                role=Role.MAKER,
                entity_id=f"maker_{i}",
                utxos_in=[UTXO(value_sats=2_000_000, owner_id=f"maker_{i}")],
                equal_output=UTXO(
                    value_sats=1_000_000, owner_id=f"maker_{i}", is_equal_output=True
                ),
                change_output=UTXO(value_sats=1_000_500, owner_id=f"maker_{i}", is_change=True),
                cj_fee_sats=500,
            )
            participants.append(p)

        taker = Participant(
            role=Role.TAKER,
            entity_id="taker",
            utxos_in=[UTXO(value_sats=1_500_000, owner_id="taker")],
            equal_output=UTXO(value_sats=1_000_000, owner_id="taker", is_equal_output=True),
            change_output=UTXO(value_sats=495_000, owner_id="taker", is_change=True),
            cj_fee_sats=-3500,
        )
        participants.append(taker)

        return CoinJoinTransaction(
            cj_amount=1_000_000,
            participants=participants,
            total_mining_fee=2000,
        )

    def test_n_participants(self) -> None:
        tx = self._make_tx(3)
        assert tx.n_participants == 4

    def test_n_makers(self) -> None:
        tx = self._make_tx(3)
        assert tx.n_makers == 3

    def test_taker(self) -> None:
        tx = self._make_tx(3)
        taker = tx.taker
        assert taker is not None
        assert taker.role == Role.TAKER
        assert taker.entity_id == "taker"

    def test_equal_outputs(self) -> None:
        tx = self._make_tx(3)
        assert len(tx.equal_outputs) == 4

    def test_change_outputs(self) -> None:
        tx = self._make_tx(3)
        assert len(tx.change_outputs) == 4

    def test_all_inputs(self) -> None:
        tx = self._make_tx(3)
        assert len(tx.all_inputs) == 4


class TestSimulationConfig:
    def test_defaults(self) -> None:
        config = SimulationConfig()
        assert config.n_makers_total == 50
        assert config.n_makers_per_cj == 10
        assert config.dust_threshold == 27_300

    def test_custom(self) -> None:
        config = SimulationConfig(n_makers_total=100, n_sybil_entities=5)
        assert config.n_makers_total == 100
        assert config.n_sybil_entities == 5


class TestOffer:
    def test_relative_offer(self) -> None:
        offer = Offer(
            maker_id="m1",
            entity_id="e1",
            offer_type=OfferType.RELATIVE,
            cj_fee=0.001,
        )
        assert offer.offer_type == OfferType.RELATIVE

    def test_absolute_offer(self) -> None:
        offer = Offer(
            maker_id="m2",
            entity_id="e2",
            offer_type=OfferType.ABSOLUTE,
            cj_fee=500.0,
        )
        assert offer.offer_type == OfferType.ABSOLUTE
