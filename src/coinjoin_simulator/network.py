"""Realistic network-level JoinMarket simulator.

This module focuses on maker-wallet dynamics that matter for probing attacks:

- Makers have 5 mixdepths.
- The advertised max offer size is the largest mixdepth balance.
- Probing at max size reveals UTXOs from that mixdepth only.
- In honest CoinJoins, equal outputs move to the next mixdepth while change stays
  in the source mixdepth.

The goal is to quantify how a mix of honest takers (successful CoinJoins) and
evil takers (probing-only) affects maker clustering and taker privacy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.request import urlopen

import numpy as np

DEFAULT_ORDERBOOK_URL = "https://joinmarket-ng.sgn.space/orderbook.json"
SATS_PER_BTC = 100_000_000

WalletInitMode = Literal["distributed", "seeded_depth0"]


def _coerce_int(value: object, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _coerce_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


@dataclass(frozen=True)
class BondedMakerProfile:
    """Maker-level profile sampled from the public orderbook."""

    counterparty: str
    max_size_sats: int
    fidelity_bond_value: float
    fee_type: Literal["relative", "absolute"]
    fee_value: float


@dataclass(frozen=True)
class WalletUTXO:
    """Minimal UTXO model used by the network simulator."""

    utxo_id: str
    value_sats: int
    mixdepth: int
    created_round: int


@dataclass
class SimulatedMaker:
    """Maker wallet state with configurable mixdepths."""

    maker_id: str
    fidelity_bond_value: float
    fee_type: Literal["relative", "absolute"]
    fee_value: float
    offer_cap_sats: int
    mixdepths: list[list[WalletUTXO]]

    def mixdepth_balance(self, mixdepth: int) -> int:
        return sum(u.value_sats for u in self.mixdepths[mixdepth])

    def total_balance(self) -> int:
        return sum(self.mixdepth_balance(idx) for idx in range(len(self.mixdepths)))

    def largest_mixdepth(self) -> int:
        balances = [self.mixdepth_balance(idx) for idx in range(len(self.mixdepths))]
        return int(np.argmax(np.asarray(balances)))

    def advertised_max_size(self) -> int:
        return min(self.mixdepth_balance(self.largest_mixdepth()), self.offer_cap_sats)


@dataclass(frozen=True)
class NetworkSimulationConfig:
    """Configuration for realistic network simulation sweeps."""

    n_makers: int = 100
    n_rounds: int = 1_000
    n_makers_per_coinjoin: int = 5
    evil_taker_fraction: float = 0.2
    probes_per_evil_taker: int = 5
    mean_cj_amount_btc: float = 0.02
    std_cj_amount_btc: float = 0.006
    total_balance_ratio_mean: float = 4.8
    total_balance_ratio_std: float = 0.15
    total_balance_ratio_cap: float = 4.95
    dust_threshold_sats: int = 27_300
    min_cj_amount_sats: int = 100_000
    random_seed: int | None = 42

    # Configurable wallet structure
    n_mixdepths: int = 5

    # Mitigation: timed sticky offer slot. When offer_slot_size is set, each maker
    # advertises a random subset of size N from its active mixdepth and uses ONLY
    # those UTXOs to fill orders. The slot is sticky for a randomized lifetime
    # drawn uniformly from [slot_ttl_min_rounds, slot_ttl_max_rounds] -- it is NOT
    # rebuilt on probe (otherwise re-probing in a tight window would defeat it).
    # The slot rotates when (a) its TTL expires or (b) one of its UTXOs is spent
    # in a successful CoinJoin.
    offer_slot_size: int | None = None
    slot_ttl_min_rounds: int = 4
    slot_ttl_max_rounds: int = 20

    # Mitigation: taker pays this fee (sats) to start the protocol with each maker
    initiation_fee_sats: int = 0

    # Optional setup: pre-probe every maker before normal rounds start.
    pre_probe_all_makers: bool = False

    # Wallet initialization controls
    wallet_init_mode: WalletInitMode = "distributed"
    seed_depth0_min_initial_utxos: int = 1
    seed_depth0_max_initial_utxos: int = 3

    def __post_init__(self) -> None:
        if self.n_makers <= 0:
            raise ValueError("n_makers must be positive")
        if self.n_rounds <= 0:
            raise ValueError("n_rounds must be positive")
        if self.n_makers_per_coinjoin <= 0:
            raise ValueError("n_makers_per_coinjoin must be positive")
        if not (0.0 <= self.evil_taker_fraction <= 1.0):
            raise ValueError("evil_taker_fraction must be in [0, 1]")
        if self.probes_per_evil_taker <= 0:
            raise ValueError("probes_per_evil_taker must be positive")
        if self.mean_cj_amount_btc <= 0:
            raise ValueError("mean_cj_amount_btc must be positive")
        if self.std_cj_amount_btc < 0:
            raise ValueError("std_cj_amount_btc must be non-negative")
        if self.total_balance_ratio_mean <= 1:
            raise ValueError("total_balance_ratio_mean must be > 1")
        if self.total_balance_ratio_cap <= 1:
            raise ValueError("total_balance_ratio_cap must be > 1")
        if self.dust_threshold_sats <= 0:
            raise ValueError("dust_threshold_sats must be positive")
        if self.n_mixdepths < 2:
            raise ValueError("n_mixdepths must be >= 2")
        if self.offer_slot_size is not None and self.offer_slot_size <= 0:
            raise ValueError("offer_slot_size must be positive or None")
        if self.slot_ttl_min_rounds <= 0:
            raise ValueError("slot_ttl_min_rounds must be positive")
        if self.slot_ttl_max_rounds < self.slot_ttl_min_rounds:
            raise ValueError("slot_ttl_max_rounds must be >= slot_ttl_min_rounds")
        if self.initiation_fee_sats < 0:
            raise ValueError("initiation_fee_sats must be non-negative")
        if self.seed_depth0_min_initial_utxos <= 0:
            raise ValueError("seed_depth0_min_initial_utxos must be positive")
        if self.seed_depth0_max_initial_utxos < self.seed_depth0_min_initial_utxos:
            raise ValueError(
                "seed_depth0_max_initial_utxos must be >= seed_depth0_min_initial_utxos"
            )
        if self.wallet_init_mode not in ("distributed", "seeded_depth0"):
            raise ValueError("invalid wallet_init_mode")

    @classmethod
    def recommended_policy_defaults(cls, **overrides: Any) -> NetworkSimulationConfig:
        """Create a config with recommended maker-policy defaults.

        These defaults target a pragmatic privacy/profit balance:
        - timed sticky slot of 3 random UTXOs from the active mixdepth, rotating
          on a 4-20 round randomized lifetime or when a slot UTXO is spent
        - realistic seeded depth-0 wallet start
        - modest initiation fee to make probing measurable in cost terms
        """
        base: dict[str, Any] = {
            "n_makers_per_coinjoin": 8,
            "n_mixdepths": 5,
            "offer_slot_size": 3,
            "slot_ttl_min_rounds": 4,
            "slot_ttl_max_rounds": 20,
            "wallet_init_mode": "seeded_depth0",
            "seed_depth0_min_initial_utxos": 1,
            "seed_depth0_max_initial_utxos": 3,
            "initiation_fee_sats": 500,
        }
        base.update(overrides)
        return cls(**base)


@dataclass(frozen=True)
class MakerParticipation:
    """Maker-side trace for one successful CoinJoin."""

    maker_id: str
    source_mixdepth: int
    next_mixdepth: int
    input_utxo_ids: tuple[str, ...]
    change_utxo_id: str | None
    change_mixdepth: int | None


@dataclass(frozen=True)
class HonestCoinJoinRecord:
    """Observed facts for one successful CoinJoin."""

    round_index: int
    cj_amount_sats: int
    identified_makers: int
    taker_anon_set: int
    maker_events: tuple[MakerParticipation, ...]


@dataclass(frozen=True)
class NetworkSimulationResult:
    """Aggregate output of one network simulation run."""

    evil_taker_fraction: float
    n_rounds: int
    n_honest_rounds: int
    n_evil_rounds: int
    n_successful_coinjoins: int
    n_failed_coinjoins: int
    n_probe_actions: int
    n_probed_utxos: int
    maker_clustered_fraction: float
    probed_maker_fraction: float
    known_live_utxo_fraction: float
    avg_known_mixdepths_per_maker: float
    makers_with_2plus_known_mixdepths_fraction: float
    mean_taker_anon_set: float
    median_taker_anon_set: float
    p10_taker_anon_set: float
    min_taker_anon_set: float
    taker_deanonymized_fraction: float
    avg_identified_makers_per_coinjoin: float
    avg_identified_maker_fraction: float
    mean_cj_amount_btc: float
    std_cj_amount_btc: float

    # Mitigation/experiment metadata
    n_mixdepths: int = 5
    offer_slot_size: int | None = None
    slot_ttl_min_rounds: int = 4
    slot_ttl_max_rounds: int = 20
    initiation_fee_sats: int = 0

    # Probing cost metrics (only meaningful when initiation_fee_sats > 0)
    total_probing_cost_sats: int = 0
    total_honest_volume_sats: int = 0
    probing_cost_per_probe_sats: float = 0.0
    probing_cost_to_volume_ratio: float = 0.0

    # Strategy metadata
    pre_probe_all_makers: bool = False
    wallet_init_mode: WalletInitMode = "distributed"

    # Additional behavior metrics
    avg_inputs_per_maker: float = 0.0
    preprobe_actions: int = 0
    preprobe_utxos: int = 0

    # Top-N UTXO coverage: fraction of largest mixdepth balance captured by top-N UTXOs
    mean_top1_utxo_coverage: float = 0.0
    mean_top3_utxo_coverage: float = 0.0
    mean_top5_utxo_coverage: float = 0.0

    def to_dict(self) -> dict[str, float | int | bool | str | None]:
        return {
            "evil_taker_fraction": self.evil_taker_fraction,
            "n_rounds": self.n_rounds,
            "n_honest_rounds": self.n_honest_rounds,
            "n_evil_rounds": self.n_evil_rounds,
            "n_successful_coinjoins": self.n_successful_coinjoins,
            "n_failed_coinjoins": self.n_failed_coinjoins,
            "n_probe_actions": self.n_probe_actions,
            "n_probed_utxos": self.n_probed_utxos,
            "maker_clustered_fraction": self.maker_clustered_fraction,
            "probed_maker_fraction": self.probed_maker_fraction,
            "known_live_utxo_fraction": self.known_live_utxo_fraction,
            "avg_known_mixdepths_per_maker": self.avg_known_mixdepths_per_maker,
            "makers_with_2plus_known_mixdepths_fraction": (
                self.makers_with_2plus_known_mixdepths_fraction
            ),
            "mean_taker_anon_set": self.mean_taker_anon_set,
            "median_taker_anon_set": self.median_taker_anon_set,
            "p10_taker_anon_set": self.p10_taker_anon_set,
            "min_taker_anon_set": self.min_taker_anon_set,
            "taker_deanonymized_fraction": self.taker_deanonymized_fraction,
            "avg_identified_makers_per_coinjoin": self.avg_identified_makers_per_coinjoin,
            "avg_identified_maker_fraction": self.avg_identified_maker_fraction,
            "mean_cj_amount_btc": self.mean_cj_amount_btc,
            "std_cj_amount_btc": self.std_cj_amount_btc,
            "n_mixdepths": self.n_mixdepths,
            "offer_slot_size": self.offer_slot_size,
            "slot_ttl_min_rounds": self.slot_ttl_min_rounds,
            "slot_ttl_max_rounds": self.slot_ttl_max_rounds,
            "initiation_fee_sats": self.initiation_fee_sats,
            "total_probing_cost_sats": self.total_probing_cost_sats,
            "total_honest_volume_sats": self.total_honest_volume_sats,
            "probing_cost_per_probe_sats": self.probing_cost_per_probe_sats,
            "probing_cost_to_volume_ratio": self.probing_cost_to_volume_ratio,
            "pre_probe_all_makers": self.pre_probe_all_makers,
            "wallet_init_mode": self.wallet_init_mode,
            "avg_inputs_per_maker": self.avg_inputs_per_maker,
            "preprobe_actions": self.preprobe_actions,
            "preprobe_utxos": self.preprobe_utxos,
            "mean_top1_utxo_coverage": self.mean_top1_utxo_coverage,
            "mean_top3_utxo_coverage": self.mean_top3_utxo_coverage,
            "mean_top5_utxo_coverage": self.mean_top5_utxo_coverage,
        }


@dataclass(frozen=True)
class SustainedAttackConfig:
    """Configuration for sustained attack simulation with daily cost model.

    This models a more realistic attacker scenario:
    - Honest CoinJoins happen at a fixed rate (honest_cjs_per_day).
    - The attacker runs probe rounds that probe ALL makers simultaneously,
      each at their individual max offer size, paying only the initiation fee.
    - Probes do NOT complete a CoinJoin -- no wallet state changes for makers.
    - The attacker's daily cost = probes_per_day * n_makers * initiation_fee_sats.
    """

    n_days: int = 30
    honest_cjs_per_day: int = 100
    probes_per_day: int = 0  # 0 = no attack
    attack_start_day: int = 0  # Day when probing starts
    attack_end_day: int | None = None  # None = attack runs until end

    def __post_init__(self) -> None:
        if self.n_days <= 0:
            raise ValueError("n_days must be positive")
        if self.honest_cjs_per_day < 0:
            raise ValueError("honest_cjs_per_day must be non-negative")
        if self.probes_per_day < 0:
            raise ValueError("probes_per_day must be non-negative")
        if self.attack_start_day < 0:
            raise ValueError("attack_start_day must be non-negative")
        if self.attack_end_day is not None and self.attack_end_day < self.attack_start_day:
            raise ValueError("attack_end_day must be >= attack_start_day")


@dataclass(frozen=True)
class DailySnapshot:
    """Per-day metrics from a sustained attack simulation."""

    day: int
    phase: str  # "pre_attack", "attack", "recovery"
    honest_cjs_completed: int
    honest_cjs_failed: int
    probe_rounds: int
    probe_actions: int  # = probe_rounds * n_makers (each probe round hits all)
    probe_cost_sats: int
    cumulative_probe_cost_sats: int
    known_live_utxo_fraction: float
    mean_taker_anon_set: float
    median_taker_anon_set: float
    p10_taker_anon_set: float
    taker_deanonymized_fraction: float
    probed_maker_fraction: float
    avg_identified_maker_fraction: float


@dataclass(frozen=True)
class SustainedAttackResult:
    """Full output of a sustained attack simulation."""

    n_days: int
    n_makers: int
    honest_cjs_per_day: int
    probes_per_day: int
    attack_start_day: int
    attack_end_day: int | None
    initiation_fee_sats: int

    total_honest_cjs: int
    total_probe_rounds: int
    total_probe_actions: int
    total_probe_cost_sats: int
    total_probe_cost_btc: float

    daily_snapshots: tuple[DailySnapshot, ...]

    # Final state
    final_known_live_utxo_fraction: float
    final_probed_maker_fraction: float

    # Attack-phase averages
    attack_mean_taker_anon_set: float
    attack_taker_deanonymized_fraction: float
    attack_daily_cost_sats: float
    attack_daily_cost_btc: float

    # Recovery metrics (if applicable)
    recovery_day_known_live_le_10pct: int | None
    recovery_day_deanon_le_5pct: int | None

    # Policy metadata
    offer_slot_size: int | None = None
    slot_ttl_min_rounds: int = 4
    slot_ttl_max_rounds: int = 20
    wallet_init_mode: WalletInitMode = "distributed"
    policy_label: str = ""

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "n_days": self.n_days,
            "n_makers": self.n_makers,
            "honest_cjs_per_day": self.honest_cjs_per_day,
            "probes_per_day": self.probes_per_day,
            "attack_start_day": self.attack_start_day,
            "attack_end_day": self.attack_end_day,
            "initiation_fee_sats": self.initiation_fee_sats,
            "total_honest_cjs": self.total_honest_cjs,
            "total_probe_rounds": self.total_probe_rounds,
            "total_probe_actions": self.total_probe_actions,
            "total_probe_cost_sats": self.total_probe_cost_sats,
            "total_probe_cost_btc": self.total_probe_cost_btc,
            "final_known_live_utxo_fraction": self.final_known_live_utxo_fraction,
            "final_probed_maker_fraction": self.final_probed_maker_fraction,
            "attack_mean_taker_anon_set": self.attack_mean_taker_anon_set,
            "attack_taker_deanonymized_fraction": self.attack_taker_deanonymized_fraction,
            "attack_daily_cost_sats": self.attack_daily_cost_sats,
            "attack_daily_cost_btc": self.attack_daily_cost_btc,
            "recovery_day_known_live_le_10pct": self.recovery_day_known_live_le_10pct,
            "recovery_day_deanon_le_5pct": self.recovery_day_deanon_le_5pct,
            "offer_slot_size": self.offer_slot_size,
            "slot_ttl_min_rounds": self.slot_ttl_min_rounds,
            "slot_ttl_max_rounds": self.slot_ttl_max_rounds,
            "wallet_init_mode": self.wallet_init_mode,
            "policy_label": self.policy_label,
            "daily_snapshots": [
                {
                    "day": s.day,
                    "phase": s.phase,
                    "honest_cjs_completed": s.honest_cjs_completed,
                    "honest_cjs_failed": s.honest_cjs_failed,
                    "probe_rounds": s.probe_rounds,
                    "probe_actions": s.probe_actions,
                    "probe_cost_sats": s.probe_cost_sats,
                    "cumulative_probe_cost_sats": s.cumulative_probe_cost_sats,
                    "known_live_utxo_fraction": s.known_live_utxo_fraction,
                    "mean_taker_anon_set": s.mean_taker_anon_set,
                    "median_taker_anon_set": s.median_taker_anon_set,
                    "p10_taker_anon_set": s.p10_taker_anon_set,
                    "taker_deanonymized_fraction": s.taker_deanonymized_fraction,
                    "probed_maker_fraction": s.probed_maker_fraction,
                    "avg_identified_maker_fraction": s.avg_identified_maker_fraction,
                }
                for s in self.daily_snapshots
            ],
        }
        return d


def fetch_orderbook_snapshot(
    url: str = DEFAULT_ORDERBOOK_URL,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Fetch and decode a JoinMarket orderbook snapshot."""
    with urlopen(url, timeout=timeout_seconds) as response:
        payload = response.read().decode("utf-8")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("orderbook snapshot is not a JSON object")
    return decoded


def load_orderbook_snapshot(path: str) -> dict[str, Any]:
    """Load a previously fetched JoinMarket orderbook snapshot from disk."""
    with open(path, encoding="utf-8") as handle:
        decoded = json.load(handle)
    if not isinstance(decoded, dict):
        raise ValueError("orderbook snapshot is not a JSON object")
    return decoded


def extract_bonded_maker_profiles(orderbook_data: dict[str, Any]) -> list[BondedMakerProfile]:
    """Extract maker-level profiles from offers with positive fidelity bond value."""
    offers_raw = orderbook_data.get("offers")
    if not isinstance(offers_raw, list):
        raise ValueError("orderbook JSON missing offers list")

    by_counterparty: dict[str, BondedMakerProfile] = {}

    for offer_raw in offers_raw:
        if not isinstance(offer_raw, dict):
            continue

        counterparty = offer_raw.get("counterparty")
        if not isinstance(counterparty, str) or not counterparty:
            continue

        bond_value = _coerce_float(offer_raw.get("fidelity_bond_value"), 0.0)
        if bond_value <= 0:
            continue

        max_size = _coerce_int(offer_raw.get("maxsize"), 0)
        if max_size <= 0:
            continue

        ordertype_raw = offer_raw.get("ordertype")
        ordertype = ordertype_raw if isinstance(ordertype_raw, str) else ""
        fee_type: Literal["relative", "absolute"] = (
            "absolute" if "absoffer" in ordertype else "relative"
        )

        fee_value = _coerce_float(offer_raw.get("cjfee"), 0.0)
        existing = by_counterparty.get(counterparty)
        if existing is None or max_size > existing.max_size_sats:
            by_counterparty[counterparty] = BondedMakerProfile(
                counterparty=counterparty,
                max_size_sats=max_size,
                fidelity_bond_value=bond_value,
                fee_type=fee_type,
                fee_value=fee_value,
            )

    profiles = list(by_counterparty.values())
    if not profiles:
        raise ValueError("no bonded makers found in orderbook snapshot")
    return profiles


class RealisticNetworkSimulator:
    """Network simulation that tracks probing, clustering, and taker privacy.

    Supports configurable mixdepth count and the following mitigations:
    - offer_slot_size + slot_ttl_*: timed sticky offer slot. Each maker advertises
      a random N-UTXO subset of its active mixdepth, sticky for a randomized
      lifetime. Re-probing within one slot lifetime leaks nothing new; rotation
      happens on TTL expiry or when a slot UTXO is consumed by a successful CJ.
    - initiation_fee_sats: taker pays per maker to start the protocol (probing
      cost lever).
    """

    def __init__(
        self,
        config: NetworkSimulationConfig,
        maker_profiles: list[BondedMakerProfile],
    ) -> None:
        if not maker_profiles:
            raise ValueError("maker_profiles must not be empty")

        self.config = config
        self.rng = np.random.default_rng(config.random_seed)
        self._utxo_counter = 0
        self._n_mixdepths = config.n_mixdepths

        sampled_profiles = self._sample_profiles(maker_profiles, config.n_makers)
        self.makers = self._initialize_makers(sampled_profiles)

        self.known_utxos_by_maker: dict[str, set[str]] = {m.maker_id: set() for m in self.makers}
        self.known_utxo_depth_by_maker: dict[str, dict[str, int]] = {
            m.maker_id: {} for m in self.makers
        }
        self.known_mixdepths_by_maker: dict[str, set[int]] = {
            m.maker_id: set() for m in self.makers
        }
        self.probed_makers: set[str] = set()

        # Global round counter, incremented every probe round and every honest CJ.
        # Drives slot-TTL expiry so high-pressure probing can't freeze the slot.
        self._round_counter = 0

        # Timed sticky offer-slot state.
        # _offer_slots[maker_id] = list of WalletUTXO currently advertised
        # _slot_expiry_round[maker_id] = round at which the slot must be rotated
        # The slot is built on first need (_ensure_slot) so the initial expiry
        # uses the configured TTL window.
        self._offer_slots: dict[str, list[WalletUTXO]] = {}
        self._slot_expiry_round: dict[str, int] = {}
        for maker in self.makers:
            self._build_offer_slot(maker, current_round=0)

        # Probing cost accumulator
        self._total_probing_cost_sats = 0
        self._preprobe_actions = 0
        self._preprobe_utxos = 0

        # Additional behavior metrics
        self._total_inputs_used = 0
        self._total_maker_participations = 0

        if self.config.pre_probe_all_makers:
            self._pre_probe_all_makers_once()

    def _sample_profiles(
        self,
        maker_profiles: list[BondedMakerProfile],
        n_makers: int,
    ) -> list[BondedMakerProfile]:
        indices = self.rng.integers(0, len(maker_profiles), size=n_makers)
        return [maker_profiles[int(idx)] for idx in indices]

    def _new_utxo_id(self) -> str:
        self._utxo_counter += 1
        return f"u{self._utxo_counter:09d}"

    def _sample_total_balance(self, max_size_sats: int) -> int:
        mean = self.config.total_balance_ratio_mean
        std = self.config.total_balance_ratio_std
        cap = self.config.total_balance_ratio_cap
        ratio = float(self.rng.normal(mean, std))
        ratio = max(1.05, min(cap, ratio))
        return max(max_size_sats, int(max_size_sats * ratio))

    def _split_balance_into_fixed_utxos(
        self,
        mixdepth: int,
        balance_sats: int,
        n_utxos: int,
    ) -> list[WalletUTXO]:
        if balance_sats <= 0:
            return []
        if n_utxos <= 1:
            return [
                WalletUTXO(
                    utxo_id=self._new_utxo_id(),
                    value_sats=balance_sats,
                    mixdepth=mixdepth,
                    created_round=0,
                )
            ]

        dust = self.config.dust_threshold_sats
        n_utxos = min(n_utxos, max(1, balance_sats // dust))
        if n_utxos <= 1:
            return [
                WalletUTXO(
                    utxo_id=self._new_utxo_id(),
                    value_sats=balance_sats,
                    mixdepth=mixdepth,
                    created_round=0,
                )
            ]

        weights = self.rng.dirichlet(np.full(n_utxos, 8.0))
        values = [int(balance_sats * float(weight)) for weight in weights]
        for idx, value in enumerate(values):
            if value < dust:
                values[idx] = dust

        current_sum = sum(values)
        diff = balance_sats - current_sum
        if diff != 0:
            largest_idx = int(np.argmax(np.asarray(values)))
            values[largest_idx] = max(dust, values[largest_idx] + diff)

        while sum(values) > balance_sats:
            largest_idx = int(np.argmax(np.asarray(values)))
            if values[largest_idx] <= dust:
                break
            values[largest_idx] -= 1
        while sum(values) < balance_sats:
            largest_idx = int(np.argmax(np.asarray(values)))
            values[largest_idx] += 1

        return [
            WalletUTXO(
                utxo_id=self._new_utxo_id(),
                value_sats=value,
                mixdepth=mixdepth,
                created_round=0,
            )
            for value in values
        ]

    def _allocate_mixdepth_balances(self, max_size_sats: int) -> list[int]:
        n_depths = self._n_mixdepths

        total_balance = self._sample_total_balance(max_size_sats)
        remainder = max(0, total_balance - max_size_sats)

        largest_mixdepth = int(self.rng.integers(0, n_depths))
        balances = [0] * n_depths
        balances[largest_mixdepth] = max_size_sats

        other_indices = [idx for idx in range(n_depths) if idx != largest_mixdepth]
        n_others = len(other_indices)
        if remainder == 0 or n_others == 0:
            return balances

        sampled_others: list[int] | None = None
        for _ in range(100):
            weights = self.rng.dirichlet(np.full(n_others, 6.0))
            others = [int(remainder * float(w)) for w in weights]
            diff = remainder - sum(others)
            others[0] += diff
            if max(others) < max_size_sats:
                sampled_others = others
                break

        if sampled_others is None:
            base = remainder // n_others
            sampled_others = [base] * n_others
            sampled_others[0] += remainder - base * n_others
            sampled_others.sort(reverse=True)

        for idx, value in zip(other_indices, sampled_others, strict=False):
            balances[idx] = max(0, value)

        return balances

    def _split_balance_into_utxos(
        self,
        mixdepth: int,
        balance_sats: int,
    ) -> list[WalletUTXO]:
        if balance_sats <= 0:
            return []

        dust = self.config.dust_threshold_sats
        mean_amount_sats = max(1, int(self.config.mean_cj_amount_btc * SATS_PER_BTC))

        expected_count = max(1, int(round(balance_sats / mean_amount_sats)))
        sampled_count = int(
            self.rng.normal(loc=expected_count * 0.6, scale=max(1.0, expected_count * 0.2))
        )
        n_utxos = max(1, min(20, sampled_count))
        n_utxos = min(n_utxos, max(1, balance_sats // dust))

        if n_utxos == 1:
            return [
                WalletUTXO(
                    utxo_id=self._new_utxo_id(),
                    value_sats=balance_sats,
                    mixdepth=mixdepth,
                    created_round=0,
                )
            ]

        weights = self.rng.dirichlet(np.full(n_utxos, 2.0))
        values = [int(balance_sats * float(weight)) for weight in weights]

        for idx, value in enumerate(values):
            if value < dust:
                values[idx] = dust

        current_sum = sum(values)
        diff = balance_sats - current_sum
        if diff != 0:
            largest_idx = int(np.argmax(np.asarray(values)))
            values[largest_idx] = max(dust, values[largest_idx] + diff)

        while sum(values) > balance_sats:
            largest_idx = int(np.argmax(np.asarray(values)))
            if values[largest_idx] <= dust:
                break
            values[largest_idx] -= 1

        while sum(values) < balance_sats:
            largest_idx = int(np.argmax(np.asarray(values)))
            values[largest_idx] += 1

        utxos: list[WalletUTXO] = []
        for value in values:
            utxos.append(
                WalletUTXO(
                    utxo_id=self._new_utxo_id(),
                    value_sats=value,
                    mixdepth=mixdepth,
                    created_round=0,
                )
            )

        return utxos

    def _initialize_makers(
        self, sampled_profiles: list[BondedMakerProfile]
    ) -> list[SimulatedMaker]:
        makers: list[SimulatedMaker] = []
        for idx, profile in enumerate(sampled_profiles):
            maker_id = f"maker_{idx:03d}"
            mixdepths: list[list[WalletUTXO]]
            if self.config.wallet_init_mode == "seeded_depth0":
                total_balance = self._sample_total_balance(profile.max_size_sats)
                n_init = int(
                    self.rng.integers(
                        self.config.seed_depth0_min_initial_utxos,
                        self.config.seed_depth0_max_initial_utxos + 1,
                    )
                )
                depth0_utxos = self._split_balance_into_fixed_utxos(0, total_balance, n_init)
                mixdepths = [depth0_utxos] + [[] for _ in range(self._n_mixdepths - 1)]
            else:
                balances = self._allocate_mixdepth_balances(profile.max_size_sats)
                mixdepths = []
                for mixdepth in range(self._n_mixdepths):
                    depth_utxos = self._split_balance_into_utxos(mixdepth, balances[mixdepth])
                    mixdepths.append(depth_utxos)

            makers.append(
                SimulatedMaker(
                    maker_id=maker_id,
                    fidelity_bond_value=max(profile.fidelity_bond_value, 1e-9),
                    fee_type=profile.fee_type,
                    fee_value=profile.fee_value,
                    offer_cap_sats=profile.max_size_sats,
                    mixdepths=mixdepths,
                )
            )
        return makers

    def _maker_fee_sats(self, maker: SimulatedMaker, cj_amount_sats: int) -> int:
        if maker.fee_type == "absolute":
            return max(1, int(maker.fee_value))
        return max(1, int(cj_amount_sats * maker.fee_value))

    def _sample_cj_amount_sats(self) -> int:
        mean = self.config.mean_cj_amount_btc * SATS_PER_BTC
        std = self.config.std_cj_amount_btc * SATS_PER_BTC

        sampled = int(mean) if std <= 0 else int(self.rng.normal(mean, std))
        return max(self.config.min_cj_amount_sats, sampled)

    def _weighted_sample_makers(
        self,
        candidates: list[SimulatedMaker],
        n_select: int,
    ) -> list[SimulatedMaker]:
        if len(candidates) <= n_select:
            return list(candidates)

        weights = np.asarray([max(m.fidelity_bond_value, 1e-12) for m in candidates], dtype=float)
        weights = weights / weights.sum()
        selected_indices = self.rng.choice(len(candidates), size=n_select, replace=False, p=weights)
        return [candidates[int(i)] for i in selected_indices]

    def _select_makers_for_amount(
        self,
        cj_amount_sats: int,
        current_round: int,
    ) -> list[SimulatedMaker] | None:
        amount = cj_amount_sats
        for _ in range(8):
            # A maker can service the amount only if its current offer slot covers it
            candidates = [
                m
                for m in self.makers
                if sum(
                    u.value_sats for u in self._ensure_slot(m.maker_id, current_round)
                )
                >= amount
            ]
            if len(candidates) >= self.config.n_makers_per_coinjoin:
                return self._weighted_sample_makers(candidates, self.config.n_makers_per_coinjoin)
            amount = int(amount * 0.9)
            if amount < self.config.min_cj_amount_sats:
                break
        return None

    def _build_offer_slot(
        self,
        maker: SimulatedMaker,
        current_round: int,
    ) -> list[WalletUTXO]:
        """(Re)build the maker's advertised offer slot and reset its TTL.

        If offer_slot_size is None the slot is the full active mixdepth (baseline
        behavior). Otherwise the slot is a random N-UTXO subset. The slot
        lifetime is sampled uniformly from [slot_ttl_min_rounds,
        slot_ttl_max_rounds]; expiry is current_round + lifetime.
        """
        depth = maker.largest_mixdepth()
        utxos = list(maker.mixdepths[depth])
        cap = self.config.offer_slot_size
        if utxos and cap is not None and len(utxos) > cap:
            indices = self.rng.choice(len(utxos), size=cap, replace=False)
            slot = [utxos[int(i)] for i in indices]
        else:
            slot = utxos

        ttl = int(
            self.rng.integers(
                self.config.slot_ttl_min_rounds,
                self.config.slot_ttl_max_rounds + 1,
            )
        )
        self._offer_slots[maker.maker_id] = slot
        self._slot_expiry_round[maker.maker_id] = current_round + ttl
        return slot

    def _ensure_slot(self, maker_id: str, current_round: int) -> list[WalletUTXO]:
        """Return the current offer slot, rebuilding if expired or empty."""
        maker = next((m for m in self.makers if m.maker_id == maker_id), None)
        if maker is None:
            return []
        slot = self._offer_slots.get(maker_id, [])
        expiry = self._slot_expiry_round.get(maker_id, -1)
        # Filter to UTXOs still live in the maker's mixdepths
        depth = maker.largest_mixdepth()
        live_ids = {u.utxo_id for u in maker.mixdepths[depth]}
        live_slot = [u for u in slot if u.utxo_id in live_ids]
        if not live_slot or current_round >= expiry:
            return self._build_offer_slot(maker, current_round)
        # Slot still valid but some UTXOs may have been consumed elsewhere -- prune.
        if len(live_slot) != len(slot):
            self._offer_slots[maker_id] = live_slot
        return live_slot

    def _select_inputs_for_amount(
        self,
        maker_id: str,
        utxos: list[WalletUTXO],
        amount_sats: int,
        current_round: int,
    ) -> list[WalletUTXO] | None:
        # Use only the UTXOs currently in the maker's offer slot.
        # This is the crucial correctness fix: the taker CJ uses the same UTXO
        # set that was (or will be) advertised in the offer, not the full pool.
        slot = self._ensure_slot(maker_id, current_round)
        # Filter slot to only UTXOs still present in the mixdepth (race-condition safety)
        live_ids = {u.utxo_id for u in utxos}
        available = [u for u in slot if u.utxo_id in live_ids]
        if not available:
            return None
        # Greedy largest-first selection from slot
        ordered = sorted(available, key=lambda u: u.value_sats, reverse=True)
        selected: list[WalletUTXO] = []
        total = 0
        for utxo in ordered:
            selected.append(utxo)
            total += utxo.value_sats
            if total >= amount_sats:
                return selected
        return None

    def probe_maker_max_mixdepth(self, maker_id: str, current_round: int = 0) -> int:
        """Probe one maker and reveal the UTXOs in its current offer slot.

        Mitigation behavior:
        - Timed sticky slot (offer_slot_size + slot_ttl_*): re-probing within one
          slot lifetime reveals the same UTXOs and leaks nothing new. The slot
          rotates only when its TTL expires or one of its UTXOs is consumed in a
          successful CoinJoin.
        - Without offer_slot_size set, every probe reveals the entire active
          mixdepth (baseline behavior).
        """
        slot = self._ensure_slot(maker_id, current_round)
        if not slot:
            return 0
        maker = next(m for m in self.makers if m.maker_id == maker_id)
        depth = maker.largest_mixdepth()

        known = self.known_utxos_by_maker[maker_id]
        known_depth = self.known_utxo_depth_by_maker[maker_id]
        for utxo in slot:
            known.add(utxo.utxo_id)
            known_depth[utxo.utxo_id] = depth

        self.known_mixdepths_by_maker[maker_id].add(depth)
        self.probed_makers.add(maker_id)

        # NOTE: do NOT rotate the slot here. Re-probing within the TTL window
        # is exactly what timed-sticky-slot is designed to neutralise.
        return len(slot)

    def _pre_probe_all_makers_once(self) -> None:
        """Probe every maker once before normal rounds begin.

        This models a coordinated attacker snapshot where all makers are probed
        around the same time at max offer size.
        """
        for maker in self.makers:
            revealed = self.probe_maker_max_mixdepth(
                maker.maker_id, current_round=self._round_counter
            )
            self._preprobe_actions += 1
            self._preprobe_utxos += revealed
            if self.config.initiation_fee_sats > 0:
                self._total_probing_cost_sats += self.config.initiation_fee_sats

    def _run_evil_round(self) -> tuple[int, int]:
        n_targets = min(self.config.probes_per_evil_taker, len(self.makers))
        targets = self._weighted_sample_makers(self.makers, n_targets)

        # Initiation fee: evil taker pays per maker probed
        if self.config.initiation_fee_sats > 0:
            self._total_probing_cost_sats += n_targets * self.config.initiation_fee_sats

        probed_utxos = 0
        for maker in targets:
            probed_utxos += self.probe_maker_max_mixdepth(
                maker.maker_id, current_round=self._round_counter
            )
        return n_targets, probed_utxos

    def probe_all_makers_once(self, current_round: int | None = None) -> tuple[int, int]:
        """Probe every maker simultaneously at their individual max offer size.

        This models a single attacker probe round:
        - The attacker initiates with ALL makers at once.
        - Each maker reveals UTXOs from their largest mixdepth (subject to mitigations).
        - The attacker pays initiation_fee_sats per maker but does NOT complete any CJ.
        - No maker wallet state changes (no coins move).

        Returns (n_makers_probed, total_utxos_revealed).
        """
        if current_round is None:
            current_round = self._round_counter
        n_probed = 0
        total_utxos = 0
        fee_cost = 0
        for maker in self.makers:
            revealed = self.probe_maker_max_mixdepth(maker.maker_id, current_round=current_round)
            n_probed += 1
            total_utxos += revealed
            if self.config.initiation_fee_sats > 0:
                fee_cost += self.config.initiation_fee_sats

        self._total_probing_cost_sats += fee_cost
        return n_probed, total_utxos

    def compute_known_live_utxo_fraction(self) -> float:
        """Compute the fraction of currently-live UTXOs that the attacker knows about."""
        total_live = 0
        known_live = 0
        for maker in self.makers:
            live_ids: set[str] = set()
            for depth in maker.mixdepths:
                for utxo in depth:
                    live_ids.add(utxo.utxo_id)
            total_live += len(live_ids)
            known_live += len(self.known_utxos_by_maker[maker.maker_id] & live_ids)
        return (known_live / total_live) if total_live > 0 else 0.0

    def run_sustained_attack(
        self,
        attack_config: SustainedAttackConfig,
    ) -> SustainedAttackResult:
        """Run a sustained attack simulation with proper daily cost model.

        Each simulated day:
        1. The attacker runs `probes_per_day` probe rounds (if within attack window).
           Each probe round probes ALL makers simultaneously at their max offer size.
           The attacker pays initiation_fee per maker but does NOT complete any CJ.
        2. `honest_cjs_per_day` honest CoinJoins happen (interleaved with probes).
           These DO change maker wallet state.

        This correctly separates probe economics from honest CJ flow.
        """
        snapshots: list[DailySnapshot] = []
        total_honest_cjs = 0
        total_probe_rounds = 0
        total_probe_actions = 0
        cumulative_cost = 0
        round_counter = 0

        attack_end = (
            attack_config.attack_end_day
            if attack_config.attack_end_day is not None
            else attack_config.n_days
        )

        # Collect all attack-phase CJ records for aggregate metrics
        attack_anon_sets: list[float] = []
        attack_identified_fractions: list[float] = []

        for day in range(attack_config.n_days):
            is_attack_day = (
                attack_config.probes_per_day > 0
                and day >= attack_config.attack_start_day
                and day < attack_end
            )

            if is_attack_day:
                phase = "attack"
            elif day < attack_config.attack_start_day:
                phase = "pre_attack"
            else:
                phase = "recovery"

            day_probe_rounds = 0
            day_probe_actions = 0
            day_probe_cost = 0
            day_cjs_completed = 0
            day_cjs_failed = 0
            day_anon_sets: list[float] = []
            day_identified_fractions: list[float] = []

            # Interleave probes and honest CJs within the day.
            # Strategy: spread probes evenly across the day's CJ slots.
            n_probes = attack_config.probes_per_day if is_attack_day else 0
            n_cjs = attack_config.honest_cjs_per_day
            total_events = n_probes + n_cjs

            # Build an event schedule: 'p' for probe, 'c' for CJ
            events: list[str] = []
            if total_events > 0 and n_probes > 0:
                # Distribute probes evenly
                for i in range(total_events):
                    if (
                        n_probes > 0
                        and i % max(1, total_events // n_probes) == 0
                        and len([e for e in events if e == "p"]) < n_probes
                    ):
                        events.append("p")
                    else:
                        events.append("c")
                # Ensure we have exactly n_probes probes and n_cjs CJs
                actual_probes = events.count("p")
                actual_cjs = events.count("c")
                while actual_probes < n_probes and actual_cjs > n_cjs:
                    # Convert extra CJs to probes
                    for i in range(len(events)):
                        if events[i] == "c" and actual_probes < n_probes:
                            events[i] = "p"
                            actual_probes += 1
                            actual_cjs -= 1
                            break
                while actual_cjs < n_cjs:
                    events.append("c")
                    actual_cjs += 1
            else:
                events = ["c"] * n_cjs

            for event_type in events:
                if event_type == "p":
                    # Probe round: probe all makers, pay fee, no CJ.
                    # Advance the global round counter so slot TTLs can fire even
                    # under sustained probing with sparse honest activity.
                    self._round_counter += 1
                    n_probed, _ = self.probe_all_makers_once(
                        current_round=self._round_counter
                    )
                    day_probe_rounds += 1
                    day_probe_actions += n_probed
                    day_probe_cost += n_probed * self.config.initiation_fee_sats
                else:
                    # Honest CJ
                    self._round_counter += 1
                    record = self.simulate_single_honest_coinjoin(
                        round_index=self._round_counter
                    )
                    round_counter += 1
                    if record is not None:
                        day_cjs_completed += 1
                        day_anon_sets.append(float(record.taker_anon_set))
                        id_frac = (
                            record.identified_makers / self.config.n_makers_per_coinjoin
                            if self.config.n_makers_per_coinjoin > 0
                            else 0.0
                        )
                        day_identified_fractions.append(id_frac)
                        if is_attack_day:
                            attack_anon_sets.append(float(record.taker_anon_set))
                            attack_identified_fractions.append(id_frac)
                    else:
                        day_cjs_failed += 1

            total_honest_cjs += day_cjs_completed
            total_probe_rounds += day_probe_rounds
            total_probe_actions += day_probe_actions
            cumulative_cost += day_probe_cost

            known_live = self.compute_known_live_utxo_fraction()
            probed_fraction = len(self.probed_makers) / len(self.makers)

            if day_anon_sets:
                anon_arr = np.asarray(day_anon_sets)
                day_mean_anon = float(np.mean(anon_arr))
                day_median_anon = float(np.median(anon_arr))
                day_p10_anon = float(np.percentile(anon_arr, 10))
                day_deanon = float(np.mean(anon_arr <= 1))
            else:
                day_mean_anon = 0.0
                day_median_anon = 0.0
                day_p10_anon = 0.0
                day_deanon = 0.0

            avg_id_frac = (
                float(np.mean(day_identified_fractions)) if day_identified_fractions else 0.0
            )

            snapshots.append(
                DailySnapshot(
                    day=day,
                    phase=phase,
                    honest_cjs_completed=day_cjs_completed,
                    honest_cjs_failed=day_cjs_failed,
                    probe_rounds=day_probe_rounds,
                    probe_actions=day_probe_actions,
                    probe_cost_sats=day_probe_cost,
                    cumulative_probe_cost_sats=cumulative_cost,
                    known_live_utxo_fraction=known_live,
                    mean_taker_anon_set=day_mean_anon,
                    median_taker_anon_set=day_median_anon,
                    p10_taker_anon_set=day_p10_anon,
                    taker_deanonymized_fraction=day_deanon,
                    probed_maker_fraction=probed_fraction,
                    avg_identified_maker_fraction=avg_id_frac,
                )
            )

        # Compute final metrics
        final_known_live = self.compute_known_live_utxo_fraction()
        final_probed = len(self.probed_makers) / len(self.makers)

        # Attack-phase aggregates
        if attack_anon_sets:
            attack_mean_anon = float(np.mean(attack_anon_sets))
            attack_deanon = float(np.mean(np.asarray(attack_anon_sets) <= 1))
        else:
            attack_mean_anon = 0.0
            attack_deanon = 0.0

        # Attack daily cost
        attack_daily_cost = (
            attack_config.probes_per_day * len(self.makers) * self.config.initiation_fee_sats
        )

        # Recovery milestones
        recovery_known_live: int | None = None
        recovery_deanon: int | None = None
        for snap in snapshots:
            if snap.phase != "recovery":
                continue
            if recovery_known_live is None and snap.known_live_utxo_fraction <= 0.10:
                recovery_known_live = snap.day
            if recovery_deanon is None and snap.taker_deanonymized_fraction <= 0.05:
                recovery_deanon = snap.day

        total_cost = cumulative_cost

        return SustainedAttackResult(
            n_days=attack_config.n_days,
            n_makers=len(self.makers),
            honest_cjs_per_day=attack_config.honest_cjs_per_day,
            probes_per_day=attack_config.probes_per_day,
            attack_start_day=attack_config.attack_start_day,
            attack_end_day=attack_config.attack_end_day,
            initiation_fee_sats=self.config.initiation_fee_sats,
            total_honest_cjs=total_honest_cjs,
            total_probe_rounds=total_probe_rounds,
            total_probe_actions=total_probe_actions,
            total_probe_cost_sats=total_cost,
            total_probe_cost_btc=total_cost / SATS_PER_BTC,
            daily_snapshots=tuple(snapshots),
            final_known_live_utxo_fraction=final_known_live,
            final_probed_maker_fraction=final_probed,
            attack_mean_taker_anon_set=attack_mean_anon,
            attack_taker_deanonymized_fraction=attack_deanon,
            attack_daily_cost_sats=float(attack_daily_cost),
            attack_daily_cost_btc=float(attack_daily_cost) / SATS_PER_BTC,
            recovery_day_known_live_le_10pct=recovery_known_live,
            recovery_day_deanon_le_5pct=recovery_deanon,
            offer_slot_size=self.config.offer_slot_size,
            slot_ttl_min_rounds=self.config.slot_ttl_min_rounds,
            slot_ttl_max_rounds=self.config.slot_ttl_max_rounds,
            wallet_init_mode=self.config.wallet_init_mode,
        )

    def simulate_single_honest_coinjoin(
        self,
        round_index: int = 0,
        cj_amount_sats: int | None = None,
    ) -> HonestCoinJoinRecord | None:
        """Run one honest CoinJoin and update maker wallets if successful.

        Mitigation behavior:
        - The slot is rebuilt for any maker whose slot UTXO is consumed (and
          implicitly when its TTL has expired -- see _ensure_slot).
        """
        amount = cj_amount_sats if cj_amount_sats is not None else self._sample_cj_amount_sats()
        selected_makers = self._select_makers_for_amount(amount, current_round=round_index)
        if selected_makers is None:
            return None

        n_depths = self._n_mixdepths
        plans: list[tuple[SimulatedMaker, int, int, list[WalletUTXO], int]] = []
        for maker in selected_makers:
            source_depth = maker.largest_mixdepth()
            source_utxos = maker.mixdepths[source_depth]
            chosen = self._select_inputs_for_amount(
                maker.maker_id, source_utxos, amount, current_round=round_index
            )
            if chosen is None:
                return None
            fee_sats = self._maker_fee_sats(maker, amount)
            input_total = sum(u.value_sats for u in chosen)
            plans.append(
                (
                    maker,
                    source_depth,
                    (source_depth + 1) % n_depths,
                    chosen,
                    input_total + fee_sats,
                )
            )

        maker_events: list[MakerParticipation] = []
        for maker, source_depth, next_depth, chosen, spend_plus_fee in plans:
            chosen_ids = {u.utxo_id for u in chosen}
            self._total_inputs_used += len(chosen)
            self._total_maker_participations += 1
            maker.mixdepths[source_depth] = [
                utxo for utxo in maker.mixdepths[source_depth] if utxo.utxo_id not in chosen_ids
            ]

            equal_utxo = WalletUTXO(
                utxo_id=self._new_utxo_id(),
                value_sats=amount,
                mixdepth=next_depth,
                created_round=round_index,
            )
            maker.mixdepths[next_depth].append(equal_utxo)

            change_value = spend_plus_fee - amount
            change_utxo_id: str | None = None
            change_mixdepth: int | None = None
            if change_value >= self.config.dust_threshold_sats:
                change_utxo = WalletUTXO(
                    utxo_id=self._new_utxo_id(),
                    value_sats=change_value,
                    mixdepth=source_depth,
                    created_round=round_index,
                )
                maker.mixdepths[source_depth].append(change_utxo)
                change_utxo_id = change_utxo.utxo_id
                change_mixdepth = source_depth

            # Slot rebuild on spend: the spent UTXOs are gone (and the next-depth
            # equal_utxo lives in a different mixdepth), so rebuild a fresh slot
            # in the maker's new active mixdepth and reset its TTL.
            self._build_offer_slot(maker, current_round=round_index)

            maker_events.append(
                MakerParticipation(
                    maker_id=maker.maker_id,
                    source_mixdepth=source_depth,
                    next_mixdepth=next_depth,
                    input_utxo_ids=tuple(sorted(chosen_ids)),
                    change_utxo_id=change_utxo_id,
                    change_mixdepth=change_mixdepth,
                )
            )

        identified_makers = 0
        for event in maker_events:
            known_set = self.known_utxos_by_maker[event.maker_id]
            if known_set.intersection(event.input_utxo_ids):
                identified_makers += 1
                known_set.update(event.input_utxo_ids)
                if event.change_utxo_id is not None:
                    known_set.add(event.change_utxo_id)
                    if event.change_mixdepth is not None:
                        self.known_utxo_depth_by_maker[event.maker_id][event.change_utxo_id] = (
                            event.change_mixdepth
                        )
                        self.known_mixdepths_by_maker[event.maker_id].add(event.change_mixdepth)

        taker_anon_set = max(1, self.config.n_makers_per_coinjoin + 1 - identified_makers)

        return HonestCoinJoinRecord(
            round_index=round_index,
            cj_amount_sats=amount,
            identified_makers=identified_makers,
            taker_anon_set=taker_anon_set,
            maker_events=tuple(maker_events),
        )

    def run(self) -> NetworkSimulationResult:
        """Run full network simulation across honest and evil taker rounds."""
        n_honest_rounds = 0
        n_evil_rounds = 0
        n_successful_coinjoins = 0
        n_failed_coinjoins = 0
        n_probe_actions = 0
        n_probed_utxos = 0
        cj_records: list[HonestCoinJoinRecord] = []

        for round_index in range(self.config.n_rounds):
            self._round_counter = round_index
            if self.rng.random() < self.config.evil_taker_fraction:
                n_evil_rounds += 1
                targets, probed = self._run_evil_round()
                n_probe_actions += targets
                n_probed_utxos += probed
                continue

            n_honest_rounds += 1
            record = self.simulate_single_honest_coinjoin(round_index=round_index)
            if record is None:
                n_failed_coinjoins += 1
                continue
            n_successful_coinjoins += 1
            cj_records.append(record)

        maker_clustered = sum(
            1 for maker_id in self.known_utxos_by_maker if self.known_utxos_by_maker[maker_id]
        )
        maker_clustered_fraction = maker_clustered / len(self.makers)
        probed_maker_fraction = len(self.probed_makers) / len(self.makers)

        live_utxos_by_maker: dict[str, set[str]] = {}
        for maker in self.makers:
            live_ids: set[str] = set()
            for mixdepth in maker.mixdepths:
                for utxo in mixdepth:
                    live_ids.add(utxo.utxo_id)
            live_utxos_by_maker[maker.maker_id] = live_ids

        total_live_utxos = sum(len(ids) for ids in live_utxos_by_maker.values())
        known_live_utxos = 0
        for maker_id, live_ids in live_utxos_by_maker.items():
            known_live_utxos += len(self.known_utxos_by_maker[maker_id] & live_ids)
        known_live_utxo_fraction = (
            known_live_utxos / total_live_utxos if total_live_utxos > 0 else 0.0
        )

        known_depth_counts = [len(depths) for depths in self.known_mixdepths_by_maker.values()]
        avg_known_mixdepths = float(np.mean(known_depth_counts)) if known_depth_counts else 0.0
        makers_with_two_plus = sum(1 for count in known_depth_counts if count >= 2)
        makers_with_two_plus_fraction = makers_with_two_plus / len(self.makers)

        taker_anon_sets = [record.taker_anon_set for record in cj_records]
        identified_maker_counts = [record.identified_makers for record in cj_records]
        cj_amounts_btc = [record.cj_amount_sats / SATS_PER_BTC for record in cj_records]

        if taker_anon_sets:
            mean_taker_anon = float(np.mean(taker_anon_sets))
            median_taker_anon = float(np.median(taker_anon_sets))
            p10_taker_anon = float(np.percentile(taker_anon_sets, 10))
            min_taker_anon = float(np.min(taker_anon_sets))
            deanonymized_fraction = float(np.mean(np.asarray(taker_anon_sets) <= 1))
            avg_identified_makers = float(np.mean(identified_maker_counts))
        else:
            mean_taker_anon = 0.0
            median_taker_anon = 0.0
            p10_taker_anon = 0.0
            min_taker_anon = 0.0
            deanonymized_fraction = 0.0
            avg_identified_makers = 0.0

        avg_identified_fraction = (
            avg_identified_makers / self.config.n_makers_per_coinjoin
            if self.config.n_makers_per_coinjoin > 0
            else 0.0
        )

        mean_cj_amount_btc = float(np.mean(cj_amounts_btc)) if cj_amounts_btc else 0.0
        std_cj_amount_btc = float(np.std(cj_amounts_btc)) if cj_amounts_btc else 0.0

        # Probing cost metrics
        total_honest_volume_sats = sum(r.cj_amount_sats for r in cj_records)
        probing_cost_per_probe = (
            self._total_probing_cost_sats / n_probe_actions if n_probe_actions > 0 else 0.0
        )
        probing_cost_to_volume = (
            self._total_probing_cost_sats / total_honest_volume_sats
            if total_honest_volume_sats > 0
            else 0.0
        )
        avg_inputs_per_maker = (
            self._total_inputs_used / self._total_maker_participations
            if self._total_maker_participations > 0
            else 0.0
        )

        # Top-N UTXO coverage: fraction of largest mixdepth balance covered by top-N UTXOs
        top1_coverages: list[float] = []
        top3_coverages: list[float] = []
        top5_coverages: list[float] = []
        for maker in self.makers:
            depth = maker.largest_mixdepth()
            utxos = sorted(maker.mixdepths[depth], key=lambda u: u.value_sats, reverse=True)
            depth_balance = sum(u.value_sats for u in utxos)
            if depth_balance > 0:
                top1_coverages.append(sum(u.value_sats for u in utxos[:1]) / depth_balance)
                top3_coverages.append(sum(u.value_sats for u in utxos[:3]) / depth_balance)
                top5_coverages.append(sum(u.value_sats for u in utxos[:5]) / depth_balance)

        mean_top1 = float(np.mean(top1_coverages)) if top1_coverages else 0.0
        mean_top3 = float(np.mean(top3_coverages)) if top3_coverages else 0.0
        mean_top5 = float(np.mean(top5_coverages)) if top5_coverages else 0.0

        return NetworkSimulationResult(
            evil_taker_fraction=self.config.evil_taker_fraction,
            n_rounds=self.config.n_rounds,
            n_honest_rounds=n_honest_rounds,
            n_evil_rounds=n_evil_rounds,
            n_successful_coinjoins=n_successful_coinjoins,
            n_failed_coinjoins=n_failed_coinjoins,
            n_probe_actions=n_probe_actions,
            n_probed_utxos=n_probed_utxos,
            maker_clustered_fraction=maker_clustered_fraction,
            probed_maker_fraction=probed_maker_fraction,
            known_live_utxo_fraction=known_live_utxo_fraction,
            avg_known_mixdepths_per_maker=avg_known_mixdepths,
            makers_with_2plus_known_mixdepths_fraction=makers_with_two_plus_fraction,
            mean_taker_anon_set=mean_taker_anon,
            median_taker_anon_set=median_taker_anon,
            p10_taker_anon_set=p10_taker_anon,
            min_taker_anon_set=min_taker_anon,
            taker_deanonymized_fraction=deanonymized_fraction,
            avg_identified_makers_per_coinjoin=avg_identified_makers,
            avg_identified_maker_fraction=avg_identified_fraction,
            mean_cj_amount_btc=mean_cj_amount_btc,
            std_cj_amount_btc=std_cj_amount_btc,
            n_mixdepths=self._n_mixdepths,
            offer_slot_size=self.config.offer_slot_size,
            slot_ttl_min_rounds=self.config.slot_ttl_min_rounds,
            slot_ttl_max_rounds=self.config.slot_ttl_max_rounds,
            initiation_fee_sats=self.config.initiation_fee_sats,
            total_probing_cost_sats=self._total_probing_cost_sats,
            total_honest_volume_sats=total_honest_volume_sats,
            probing_cost_per_probe_sats=probing_cost_per_probe,
            probing_cost_to_volume_ratio=probing_cost_to_volume,
            pre_probe_all_makers=self.config.pre_probe_all_makers,
            wallet_init_mode=self.config.wallet_init_mode,
            avg_inputs_per_maker=avg_inputs_per_maker,
            preprobe_actions=self._preprobe_actions,
            preprobe_utxos=self._preprobe_utxos,
            mean_top1_utxo_coverage=mean_top1,
            mean_top3_utxo_coverage=mean_top3,
            mean_top5_utxo_coverage=mean_top5,
        )


def run_network_sweep(
    base_config: NetworkSimulationConfig,
    maker_profiles: list[BondedMakerProfile],
    evil_taker_fractions: list[float],
) -> list[NetworkSimulationResult]:
    """Run a sweep over evil-taker fractions with consistent baseline settings."""
    results: list[NetworkSimulationResult] = []
    base_seed = base_config.random_seed if base_config.random_seed is not None else 0

    for idx, evil_fraction in enumerate(evil_taker_fractions):
        config = replace(
            base_config,
            evil_taker_fraction=evil_fraction,
            random_seed=base_seed + idx,
        )
        simulator = RealisticNetworkSimulator(config=config, maker_profiles=maker_profiles)
        results.append(simulator.run())

    return results


def run_live_network_sweep(
    base_config: NetworkSimulationConfig,
    evil_taker_fractions: list[float],
    orderbook_url: str = DEFAULT_ORDERBOOK_URL,
    timeout_seconds: int = 30,
) -> tuple[list[NetworkSimulationResult], int]:
    """Fetch live orderbook data and run a full sweep."""
    snapshot = fetch_orderbook_snapshot(url=orderbook_url, timeout_seconds=timeout_seconds)
    profiles = extract_bonded_maker_profiles(snapshot)
    return run_network_sweep(base_config, profiles, evil_taker_fractions), len(profiles)
