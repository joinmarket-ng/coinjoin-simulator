"""Core data models for CoinJoin simulation.

Pydantic models representing UTXOs, participants, transactions, and offers
in the JoinMarket CoinJoin protocol.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Role(StrEnum):
    """Participant role in a CoinJoin."""

    TAKER = "taker"
    MAKER = "maker"


class OfferType(StrEnum):
    """Type of maker offer."""

    RELATIVE = "relative"
    ABSOLUTE = "absolute"


class UTXO(BaseModel):
    """An unspent transaction output."""

    txid: str = Field(default_factory=lambda: uuid.uuid4().hex)
    vout: int = 0
    value_sats: int
    owner_id: str
    confirmations: int = 100
    is_change: bool = False
    is_equal_output: bool = False
    source_coinjoin_id: str | None = None

    @property
    def outpoint(self) -> str:
        return f"{self.txid}:{self.vout}"


class Participant(BaseModel):
    """A participant in a CoinJoin transaction."""

    participant_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    role: Role
    entity_id: str  # The real entity controlling this participant (for sybil analysis)
    utxos_in: list[UTXO] = Field(default_factory=list)
    equal_output: UTXO | None = None
    change_output: UTXO | None = None
    cj_fee_sats: int = 0  # Positive = earned (maker), negative = paid (taker)
    fidelity_bond_value: float = 0.0  # In BTC^exponent units


class Offer(BaseModel):
    """A maker's offer in the orderbook."""

    maker_id: str
    entity_id: str  # Real controlling entity
    offer_type: OfferType = OfferType.RELATIVE
    min_size: int = 27_300
    max_size: int = 10_000_000
    cj_fee: float = 0.001  # Relative fee (0.1%) or absolute sats
    txfee_contribution: int = 0  # Mining fee contribution
    fidelity_bond_value: float = 0.0


class CoinJoinTransaction(BaseModel):
    """A simulated CoinJoin transaction."""

    tx_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    cj_amount: int  # The equal output amount in sats
    participants: list[Participant] = Field(default_factory=list)
    total_mining_fee: int = 0
    block_height: int = 0

    @property
    def n_participants(self) -> int:
        return len(self.participants)

    @property
    def n_makers(self) -> int:
        return sum(1 for p in self.participants if p.role == Role.MAKER)

    @property
    def taker(self) -> Participant | None:
        for p in self.participants:
            if p.role == Role.TAKER:
                return p
        return None

    @property
    def makers(self) -> list[Participant]:
        return [p for p in self.participants if p.role == Role.MAKER]

    @property
    def all_inputs(self) -> list[UTXO]:
        result: list[UTXO] = []
        for p in self.participants:
            result.extend(p.utxos_in)
        return result

    @property
    def equal_outputs(self) -> list[UTXO]:
        return [p.equal_output for p in self.participants if p.equal_output is not None]

    @property
    def change_outputs(self) -> list[UTXO]:
        return [p.change_output for p in self.participants if p.change_output is not None]

    @property
    def all_outputs(self) -> list[UTXO]:
        return self.equal_outputs + self.change_outputs


class SimulationConfig(BaseModel):
    """Configuration for a simulation run."""

    n_makers_total: int = 50
    n_makers_per_cj: int = 10
    n_coinjoins: int = 100
    cj_amount: int = 1_000_000  # 0.01 BTC in sats
    dust_threshold: int = 27_300
    maker_fee_relative: float = 0.001
    maker_fee_absolute: int = 500
    fee_type: OfferType = OfferType.RELATIVE
    tx_fee_per_vbyte: int = 10
    # Sybil parameters
    n_sybil_entities: int = 0
    sybil_makers_per_entity: int = 1
    # Fidelity bond parameters
    use_fidelity_bonds: bool = True
    bond_value_exponent: float = 1.3
    bondless_makers_allowance: float = 0.125
    interest_rate: float = 0.015
    # Maker selection algorithm
    selection_algorithm: Literal["fidelity_bond_weighted", "random", "cheapest", "weighted_fee"] = (
        "fidelity_bond_weighted"
    )
    # Sweep mode probability
    sweep_probability: float = 0.1
    # Post-CJ spending
    immediate_spend_probability: float = 0.05  # Prob of spending equal output next block
    random_seed: int | None = None


class AnonymityMetrics(BaseModel):
    """Anonymity metrics for a single output or participant."""

    # Naive anonymity set (number of equal outputs)
    naive_anon_set: int = 0
    # Effective anonymity set after analysis
    effective_anon_set: float = 0.0
    # Shannon entropy (bits)
    entropy_bits: float = 0.0
    # Probability distribution over possible owners
    owner_probabilities: dict[str, float] = Field(default_factory=dict)
    # Whether this output can be uniquely mapped to an input
    is_uniquely_mapped: bool = False
    # Number of valid input-output mappings
    n_valid_mappings: int = 0


class SybilAttackResult(BaseModel):
    """Results of a sybil attack simulation."""

    n_counterparties: int
    sybil_entity_bond_value: float
    honest_total_bond_value: float
    success_probability: float
    # Per-entity breakdown
    entity_success_rates: dict[str, float] = Field(default_factory=dict)
    # Cost metrics
    required_locked_btc_6mo: float = 0.0
    required_burned_btc: float = 0.0


class SurveillanceResult(BaseModel):
    """Results of a surveillance/probing attack simulation."""

    n_probes: int
    n_coinjoins_observed: int
    # Maker UTXO clustering
    n_makers_clustered: int = 0
    n_utxos_clustered: int = 0
    cluster_sizes: list[int] = Field(default_factory=list)
    # Information gained about past CoinJoins
    coinjoins_deanonymized: int = 0
    avg_anon_set_reduction: float = 0.0
    # With mitigations
    mitigated_utxos_clustered: int = 0
    mitigated_anon_set_reduction: float = 0.0
