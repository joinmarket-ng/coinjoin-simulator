"""CoinJoin transaction simulator.

Generates realistic CoinJoin transactions based on the JoinMarket protocol,
including maker selection, fee calculation, and UTXO management.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import numpy as np

from .models import (
    UTXO,
    CoinJoinTransaction,
    Offer,
    Participant,
    Role,
    SimulationConfig,
)

if TYPE_CHECKING:
    from numpy.random import Generator


# Transaction size estimation (from joinmarket-ng constants)
INPUT_VBYTES = 68  # P2WPKH input
OUTPUT_VBYTES = 31  # P2WPKH output
TX_OVERHEAD_VBYTES = 11


def estimate_tx_fee(n_inputs: int, n_outputs: int, fee_per_vbyte: int) -> int:
    """Estimate mining fee for a transaction."""
    vsize = TX_OVERHEAD_VBYTES + n_inputs * INPUT_VBYTES + n_outputs * OUTPUT_VBYTES
    return vsize * fee_per_vbyte


def calculate_cj_fee(amount: int, offer: Offer) -> int:
    """Calculate CoinJoin fee for a given amount and offer."""
    from .models import OfferType

    if offer.offer_type == OfferType.RELATIVE:
        return max(1, int(amount * offer.cj_fee))
    return int(offer.cj_fee)


class OrderBook:
    """Simulated orderbook of maker offers."""

    def __init__(self, offers: list[Offer]) -> None:
        self.offers = list(offers)

    def filter_offers(
        self,
        cj_amount: int,
        max_cj_fee_abs: int = 500,
        max_cj_fee_rel: float = 0.001,
    ) -> list[Offer]:
        """Filter offers suitable for the given amount."""
        valid: list[Offer] = []
        for offer in self.offers:
            if not (offer.min_size <= cj_amount <= offer.max_size):
                continue
            fee = calculate_cj_fee(cj_amount, offer)
            if fee > max_cj_fee_abs and fee > int(cj_amount * max_cj_fee_rel):
                continue
            valid.append(offer)
        return valid

    def select_makers_fidelity_bond(
        self,
        offers: list[Offer],
        n_makers: int,
        rng: Generator,
        bondless_allowance: float = 0.125,
    ) -> list[Offer]:
        """Select makers weighted by fidelity bond value.

        Implements the same algorithm as joinmarket-ng:
        ~87.5% bond-weighted selection + ~12.5% random selection.
        """
        if len(offers) <= n_makers:
            return list(offers)

        bonded = [o for o in offers if o.fidelity_bond_value > 0]
        bondless = [o for o in offers if o.fidelity_bond_value <= 0]

        n_bondless = max(0, min(len(bondless), int(n_makers * bondless_allowance)))
        n_bonded = n_makers - n_bondless

        selected: list[Offer] = []

        # Select bonded makers by fidelity bond weight
        if bonded and n_bonded > 0:
            available = list(bonded)
            for _ in range(min(n_bonded, len(available))):
                weights = np.array([o.fidelity_bond_value for o in available])
                weights = weights / weights.sum()
                idx = int(rng.choice(len(available), p=weights))
                selected.append(available.pop(idx))

        # Select bondless makers randomly
        if bondless and n_bondless > 0:
            indices = rng.choice(len(bondless), size=min(n_bondless, len(bondless)), replace=False)
            selected.extend(bondless[int(i)] for i in indices)

        return selected

    def select_makers_random(
        self,
        offers: list[Offer],
        n_makers: int,
        rng: Generator,
    ) -> list[Offer]:
        """Select makers uniformly at random."""
        if len(offers) <= n_makers:
            return list(offers)
        indices = rng.choice(len(offers), size=n_makers, replace=False)
        return [offers[int(i)] for i in indices]

    def select_makers_cheapest(
        self,
        offers: list[Offer],
        n_makers: int,
        cj_amount: int,
    ) -> list[Offer]:
        """Select the cheapest makers."""
        sorted_offers = sorted(offers, key=lambda o: calculate_cj_fee(cj_amount, o))
        return sorted_offers[:n_makers]


class TransactionSimulator:
    """Simulates CoinJoin transactions."""

    def __init__(self, config: SimulationConfig) -> None:
        self.config = config
        self.rng: Generator = np.random.default_rng(config.random_seed)
        self.orderbook: OrderBook | None = None
        self._utxo_counter = 0

    def _make_utxo(self, value: int, owner_id: str, **kwargs: object) -> UTXO:
        """Create a UTXO with a unique ID."""
        self._utxo_counter += 1
        return UTXO(
            txid=uuid.uuid4().hex,
            vout=0,
            value_sats=value,
            owner_id=owner_id,
            **kwargs,  # type: ignore[arg-type]
        )

    def generate_orderbook(self) -> OrderBook:
        """Generate a realistic orderbook with honest and sybil makers."""
        offers: list[Offer] = []

        # Generate honest maker offers
        n_honest = self.config.n_makers_total - (
            self.config.n_sybil_entities * self.config.sybil_makers_per_entity
        )

        for i in range(n_honest):
            # Realistic distribution of maker balances (log-normal)
            balance = int(self.rng.lognormal(mean=16, sigma=2))  # ~9M sats median
            balance = max(self.config.dust_threshold * 2, balance)

            # Fidelity bond values follow a power law
            if self.config.use_fidelity_bonds and self.rng.random() > 0.3:
                bond_value = float(self.rng.lognormal(mean=-8, sigma=3))
            else:
                bond_value = 0.0

            fee_rel = max(0.00001, self.rng.lognormal(mean=-7, sigma=1))

            offers.append(
                Offer(
                    maker_id=f"honest_maker_{i}",
                    entity_id=f"honest_entity_{i}",
                    min_size=self.config.dust_threshold,
                    max_size=_round_to_power_of_2(balance),
                    cj_fee=fee_rel,
                    fidelity_bond_value=bond_value,
                )
            )

        # Generate sybil maker offers
        for s in range(self.config.n_sybil_entities):
            entity_id = f"sybil_entity_{s}"
            for m in range(self.config.sybil_makers_per_entity):
                balance = int(self.rng.lognormal(mean=17, sigma=1))
                # Sybil makers may have lower bond values (split across bots)
                if self.config.use_fidelity_bonds:
                    bond_value = float(self.rng.lognormal(mean=-10, sigma=2))
                else:
                    bond_value = 0.0

                offers.append(
                    Offer(
                        maker_id=f"sybil_maker_{s}_{m}",
                        entity_id=entity_id,
                        min_size=self.config.dust_threshold,
                        max_size=_round_to_power_of_2(balance),
                        cj_fee=max(0.00001, self.rng.lognormal(mean=-8, sigma=0.5)),
                        fidelity_bond_value=bond_value,
                    )
                )

        self.orderbook = OrderBook(offers)
        return self.orderbook

    def simulate_coinjoin(
        self,
        taker_entity_id: str = "taker",
        taker_balance: int | None = None,
        cj_amount: int | None = None,
        selected_offers: list[Offer] | None = None,
    ) -> CoinJoinTransaction:
        """Simulate a single CoinJoin transaction.

        Args:
            taker_entity_id: Entity ID of the taker.
            taker_balance: Taker's input balance. Random if None.
            cj_amount: CoinJoin amount. Uses config default if None.
            selected_offers: Pre-selected offers. Selects from orderbook if None.

        Returns:
            A simulated CoinJoin transaction.
        """
        if cj_amount is None:
            cj_amount = self.config.cj_amount

        is_sweep = self.rng.random() < self.config.sweep_probability

        if selected_offers is None:
            if self.orderbook is None:
                self.generate_orderbook()
            assert self.orderbook is not None
            valid = self.orderbook.filter_offers(cj_amount)
            if len(valid) < self.config.n_makers_per_cj:
                valid = self.orderbook.offers[: self.config.n_makers_per_cj]

            if self.config.selection_algorithm == "fidelity_bond_weighted":
                selected_offers = self.orderbook.select_makers_fidelity_bond(
                    valid,
                    self.config.n_makers_per_cj,
                    self.rng,
                    self.config.bondless_makers_allowance,
                )
            elif self.config.selection_algorithm == "random":
                selected_offers = self.orderbook.select_makers_random(
                    valid, self.config.n_makers_per_cj, self.rng
                )
            elif self.config.selection_algorithm == "cheapest":
                selected_offers = self.orderbook.select_makers_cheapest(
                    valid, self.config.n_makers_per_cj, cj_amount
                )
            else:
                selected_offers = self.orderbook.select_makers_random(
                    valid, self.config.n_makers_per_cj, self.rng
                )

        n_participants = len(selected_offers) + 1
        n_inputs_est = n_participants  # 1 input per participant (simplified)
        n_outputs_est = n_participants * 2  # equal + change per participant
        mining_fee = estimate_tx_fee(n_inputs_est, n_outputs_est, self.config.tx_fee_per_vbyte)

        # Calculate total maker fees
        total_maker_fees = sum(calculate_cj_fee(cj_amount, o) for o in selected_offers)

        # Create maker participants
        participants: list[Participant] = []
        tx_id = uuid.uuid4().hex

        for offer in selected_offers:
            fee = calculate_cj_fee(cj_amount, offer)
            # Maker input: cj_amount + some extra for change (minus fee earned)
            maker_input_value = cj_amount + int(self.rng.integers(50_000, 5_000_000)) - fee
            maker_input_value = max(cj_amount + self.config.dust_threshold, maker_input_value)

            maker_input = self._make_utxo(maker_input_value, offer.entity_id)
            change_value = maker_input_value - cj_amount + fee
            equal_out = self._make_utxo(
                cj_amount,
                offer.entity_id,
                is_equal_output=True,
                source_coinjoin_id=tx_id,
            )

            change_out = None
            if change_value >= self.config.dust_threshold:
                change_out = self._make_utxo(
                    change_value,
                    offer.entity_id,
                    is_change=True,
                    source_coinjoin_id=tx_id,
                )

            participants.append(
                Participant(
                    role=Role.MAKER,
                    entity_id=offer.entity_id,
                    utxos_in=[maker_input],
                    equal_output=equal_out,
                    change_output=change_out,
                    cj_fee_sats=fee,
                    fidelity_bond_value=offer.fidelity_bond_value,
                )
            )

        # Create taker participant
        if taker_balance is None:
            if is_sweep:
                taker_balance = cj_amount + total_maker_fees + mining_fee
            else:
                taker_balance = (
                    cj_amount
                    + total_maker_fees
                    + mining_fee
                    + int(self.rng.integers(100_000, 10_000_000))
                )

        taker_input = self._make_utxo(taker_balance, taker_entity_id)
        taker_equal = self._make_utxo(
            cj_amount,
            taker_entity_id,
            is_equal_output=True,
            source_coinjoin_id=tx_id,
        )

        taker_change_value = taker_balance - cj_amount - total_maker_fees - mining_fee
        taker_change = None
        if taker_change_value >= self.config.dust_threshold and not is_sweep:
            taker_change = self._make_utxo(
                taker_change_value,
                taker_entity_id,
                is_change=True,
                source_coinjoin_id=tx_id,
            )

        participants.append(
            Participant(
                role=Role.TAKER,
                entity_id=taker_entity_id,
                utxos_in=[taker_input],
                equal_output=taker_equal,
                change_output=taker_change,
                cj_fee_sats=-total_maker_fees,
                fidelity_bond_value=0.0,
            )
        )

        # Shuffle participants (mimics random output ordering)
        indices = self.rng.permutation(len(participants)).tolist()
        participants = [participants[i] for i in indices]

        return CoinJoinTransaction(
            tx_id=tx_id,
            cj_amount=cj_amount,
            participants=participants,
            total_mining_fee=mining_fee,
        )

    def simulate_chain(
        self,
        n_coinjoins: int | None = None,
        taker_entity_id: str = "taker",
    ) -> list[CoinJoinTransaction]:
        """Simulate a chain of CoinJoin transactions."""
        if n_coinjoins is None:
            n_coinjoins = self.config.n_coinjoins

        if self.orderbook is None:
            self.generate_orderbook()

        return [self.simulate_coinjoin(taker_entity_id=taker_entity_id) for _ in range(n_coinjoins)]


def _round_to_power_of_2(value: int) -> int:
    """Round down to the nearest power of 2 (privacy feature from joinmarket-ng)."""
    if value <= 0:
        return 0
    return 1 << (value.bit_length() - 1)
