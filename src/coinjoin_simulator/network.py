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

MergeAlgorithm = Literal["default", "gradual", "greedy", "random"]
DisclosedInputPolicy = Literal[
    "ignore",
    "all_disclosed",
    "minimal_disclosed",
    "avoid_disclosed",
    "randomized",
    "adaptive",
]
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

    # Mitigation: cap UTXOs revealed per probe
    max_utxos_per_offer: int | None = None

    # Mitigation: after a failed CJ/probe, keep offering the same UTXOs
    sticky_disclosed_utxos: bool = False

    # Mitigation: disclosed UTXOs and their change descendants are never spent
    # together with equal-amount CoinJoin outputs (prevents cross-mixdepth clustering)
    flagged_utxo_isolation: bool = False

    # Mitigation: taker pays this fee (sats) to start the protocol with each maker
    initiation_fee_sats: int = 0

    # Optional setup: pre-probe every maker before normal rounds start.
    pre_probe_all_makers: bool = False

    # Maker input/merge behavior controls
    merge_algorithm: MergeAlgorithm = "default"
    greediest_maker_fraction: float = 0.0
    disclosed_input_policy: DisclosedInputPolicy = "ignore"

    # Adaptive disclosed-input policy controls
    adaptive_flush_base_probability: float = 0.05
    adaptive_flush_max_probability: float = 0.35
    adaptive_flush_backlog_target: float = 0.30

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
        if self.max_utxos_per_offer is not None and self.max_utxos_per_offer <= 0:
            raise ValueError("max_utxos_per_offer must be positive or None")
        if self.initiation_fee_sats < 0:
            raise ValueError("initiation_fee_sats must be non-negative")
        if not (0.0 <= self.greediest_maker_fraction <= 1.0):
            raise ValueError("greediest_maker_fraction must be in [0, 1]")
        if self.seed_depth0_min_initial_utxos <= 0:
            raise ValueError("seed_depth0_min_initial_utxos must be positive")
        if self.seed_depth0_max_initial_utxos < self.seed_depth0_min_initial_utxos:
            raise ValueError(
                "seed_depth0_max_initial_utxos must be >= seed_depth0_min_initial_utxos"
            )
        if self.merge_algorithm not in ("default", "gradual", "greedy", "random"):
            raise ValueError("invalid merge_algorithm")
        if self.disclosed_input_policy not in (
            "ignore",
            "all_disclosed",
            "minimal_disclosed",
            "avoid_disclosed",
            "randomized",
            "adaptive",
        ):
            raise ValueError("invalid disclosed_input_policy")
        if self.wallet_init_mode not in ("distributed", "seeded_depth0"):
            raise ValueError("invalid wallet_init_mode")
        if not (0.0 <= self.adaptive_flush_base_probability <= 1.0):
            raise ValueError("adaptive_flush_base_probability must be in [0, 1]")
        if not (0.0 <= self.adaptive_flush_max_probability <= 1.0):
            raise ValueError("adaptive_flush_max_probability must be in [0, 1]")
        if self.adaptive_flush_max_probability < self.adaptive_flush_base_probability:
            raise ValueError(
                "adaptive_flush_max_probability must be >= adaptive_flush_base_probability"
            )
        if not (0.0 <= self.adaptive_flush_backlog_target <= 1.0):
            raise ValueError("adaptive_flush_backlog_target must be in [0, 1]")

    @classmethod
    def recommended_policy_defaults(cls, **overrides: Any) -> NetworkSimulationConfig:
        """Create a config with recommended maker-policy defaults.

        These defaults target a pragmatic privacy/profit balance:
        - moderate reveal cap
        - sticky disclosed set
        - adaptive disclosed-input handling
        - gradual consolidation
        - realistic seeded depth-0 wallet start
        """
        base: dict[str, Any] = {
            "n_makers_per_coinjoin": 8,
            "n_mixdepths": 5,
            "max_utxos_per_offer": 3,
            "sticky_disclosed_utxos": True,
            "flagged_utxo_isolation": True,
            "merge_algorithm": "gradual",
            "disclosed_input_policy": "adaptive",
            "wallet_init_mode": "seeded_depth0",
            "seed_depth0_min_initial_utxos": 1,
            "seed_depth0_max_initial_utxos": 3,
            "adaptive_flush_base_probability": 0.05,
            "adaptive_flush_max_probability": 0.35,
            "adaptive_flush_backlog_target": 0.30,
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
    max_utxos_per_offer: int | None = None
    sticky_disclosed_utxos: bool = False
    flagged_utxo_isolation: bool = False
    initiation_fee_sats: int = 0

    # Probing cost metrics (only meaningful when initiation_fee_sats > 0)
    total_probing_cost_sats: int = 0
    total_honest_volume_sats: int = 0
    probing_cost_per_probe_sats: float = 0.0
    probing_cost_to_volume_ratio: float = 0.0

    # Strategy metadata
    pre_probe_all_makers: bool = False
    merge_algorithm: MergeAlgorithm = "default"
    greediest_maker_fraction: float = 0.0
    disclosed_input_policy: DisclosedInputPolicy = "ignore"
    wallet_init_mode: WalletInitMode = "distributed"
    adaptive_flush_base_probability: float = 0.05
    adaptive_flush_max_probability: float = 0.35
    adaptive_flush_backlog_target: float = 0.30

    # Additional behavior metrics
    avg_inputs_per_maker: float = 0.0
    avg_disclosed_inputs_used_per_maker: float = 0.0
    disclosed_input_usage_fraction: float = 0.0
    preprobe_actions: int = 0
    preprobe_utxos: int = 0

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
            "max_utxos_per_offer": self.max_utxos_per_offer,
            "sticky_disclosed_utxos": self.sticky_disclosed_utxos,
            "flagged_utxo_isolation": self.flagged_utxo_isolation,
            "initiation_fee_sats": self.initiation_fee_sats,
            "total_probing_cost_sats": self.total_probing_cost_sats,
            "total_honest_volume_sats": self.total_honest_volume_sats,
            "probing_cost_per_probe_sats": self.probing_cost_per_probe_sats,
            "probing_cost_to_volume_ratio": self.probing_cost_to_volume_ratio,
            "pre_probe_all_makers": self.pre_probe_all_makers,
            "merge_algorithm": self.merge_algorithm,
            "greediest_maker_fraction": self.greediest_maker_fraction,
            "disclosed_input_policy": self.disclosed_input_policy,
            "wallet_init_mode": self.wallet_init_mode,
            "adaptive_flush_base_probability": self.adaptive_flush_base_probability,
            "adaptive_flush_max_probability": self.adaptive_flush_max_probability,
            "adaptive_flush_backlog_target": self.adaptive_flush_backlog_target,
            "avg_inputs_per_maker": self.avg_inputs_per_maker,
            "avg_disclosed_inputs_used_per_maker": self.avg_disclosed_inputs_used_per_maker,
            "disclosed_input_usage_fraction": self.disclosed_input_usage_fraction,
            "preprobe_actions": self.preprobe_actions,
            "preprobe_utxos": self.preprobe_utxos,
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
    merge_algorithm: MergeAlgorithm = "default"
    disclosed_input_policy: DisclosedInputPolicy = "ignore"
    max_utxos_per_offer: int | None = None
    sticky_disclosed_utxos: bool = False
    flagged_utxo_isolation: bool = False
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
            "merge_algorithm": self.merge_algorithm,
            "disclosed_input_policy": self.disclosed_input_policy,
            "max_utxos_per_offer": self.max_utxos_per_offer,
            "sticky_disclosed_utxos": self.sticky_disclosed_utxos,
            "flagged_utxo_isolation": self.flagged_utxo_isolation,
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

    Supports configurable mixdepth count and four mitigations:
    - max_utxos_per_offer: cap UTXOs revealed per probe
    - sticky_disclosed_utxos: re-offer the same UTXOs until successful CJ
    - flagged_utxo_isolation: disclosed UTXOs never mix with equal outputs
    - initiation_fee_sats: taker pays to start the protocol (probing cost)
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

        # Per-maker merge behavior (for mixed strategy populations)
        self._merge_algorithm_by_maker: dict[str, MergeAlgorithm] = {}
        for maker in self.makers:
            if self.rng.random() < config.greediest_maker_fraction:
                self._merge_algorithm_by_maker[maker.maker_id] = "greedy"
            else:
                self._merge_algorithm_by_maker[maker.maker_id] = config.merge_algorithm

        self.known_utxos_by_maker: dict[str, set[str]] = {m.maker_id: set() for m in self.makers}
        self.known_utxo_depth_by_maker: dict[str, dict[str, int]] = {
            m.maker_id: {} for m in self.makers
        }
        self.known_mixdepths_by_maker: dict[str, set[int]] = {
            m.maker_id: set() for m in self.makers
        }
        self.probed_makers: set[str] = set()

        # Sticky mitigation state: UTXOs to keep offering until successful CJ
        # Maps maker_id -> set of utxo_ids that were disclosed in a failed CJ/probe
        self._sticky_utxos: dict[str, set[str]] = {m.maker_id: set() for m in self.makers}

        # Flagged UTXO tracking: UTXOs that were disclosed (probed/failed) and their
        # change descendants. These must NOT be spent together with equal-amount outputs.
        self._flagged_utxos: dict[str, set[str]] = {m.maker_id: set() for m in self.makers}

        # Probing cost accumulator
        self._total_probing_cost_sats = 0
        self._preprobe_actions = 0
        self._preprobe_utxos = 0

        # Additional behavior metrics
        self._total_inputs_used = 0
        self._total_disclosed_inputs_used = 0
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

    def _select_makers_for_amount(self, cj_amount_sats: int) -> list[SimulatedMaker] | None:
        amount = cj_amount_sats
        for _ in range(8):
            candidates = [m for m in self.makers if m.advertised_max_size() >= amount]
            if len(candidates) >= self.config.n_makers_per_coinjoin:
                return self._weighted_sample_makers(candidates, self.config.n_makers_per_coinjoin)
            amount = int(amount * 0.9)
            if amount < self.config.min_cj_amount_sats:
                break
        return None

    def _select_inputs_for_amount(
        self,
        maker_id: str,
        utxos: list[WalletUTXO],
        amount_sats: int,
    ) -> list[WalletUTXO] | None:
        algorithm = self._merge_algorithm_by_maker.get(maker_id, self.config.merge_algorithm)
        policy = self.config.disclosed_input_policy
        known = self.known_utxos_by_maker.get(maker_id, set())

        disclosed = [u for u in utxos if u.utxo_id in known]
        undisclosed = [u for u in utxos if u.utxo_id not in known]

        def ordered_by_strategy(
            items: list[WalletUTXO],
            use_algorithm: MergeAlgorithm,
        ) -> list[WalletUTXO]:
            if use_algorithm in ("default", "greedy"):
                return sorted(items, key=lambda u: u.value_sats, reverse=True)
            if use_algorithm == "random":
                shuffled = list(items)
                self.rng.shuffle(shuffled)
                return shuffled
            # gradual: start from larger inputs too, then append one smaller later
            return sorted(items, key=lambda u: u.value_sats, reverse=True)

        def greedy_pick(pool: list[WalletUTXO]) -> list[WalletUTXO] | None:
            ordered = ordered_by_strategy(pool, algorithm)
            selected: list[WalletUTXO] = []
            total = 0
            for utxo in ordered:
                selected.append(utxo)
                total += utxo.value_sats
                if total >= amount_sats:
                    return selected
            return None

        def apply_merge_extras(
            selected: list[WalletUTXO],
            remaining_pool: list[WalletUTXO],
        ) -> list[WalletUTXO]:
            if not remaining_pool:
                return selected

            if algorithm == "greedy":
                ordered_remaining = sorted(remaining_pool, key=lambda u: u.value_sats, reverse=True)
                return selected + ordered_remaining

            if algorithm == "gradual":
                remaining_sorted = sorted(remaining_pool, key=lambda u: u.value_sats)
                return selected + [remaining_sorted[0]]

            if algorithm == "random":
                remaining_sorted = sorted(remaining_pool, key=lambda u: u.value_sats)
                extra_count = int(self.rng.integers(0, min(2, len(remaining_sorted)) + 1))
                if extra_count > 0:
                    return selected + remaining_sorted[:extra_count]

            # default
            return selected

        if policy == "all_disclosed":
            if not disclosed:
                picks = greedy_pick(utxos)
                if picks is None:
                    return None
                picked_ids = {u.utxo_id for u in picks}
                remaining = [u for u in utxos if u.utxo_id not in picked_ids]
                return apply_merge_extras(picks, remaining)
            picks = greedy_pick(disclosed)
            if picks is not None:
                picked_ids = {u.utxo_id for u in picks}
                remaining = [u for u in disclosed if u.utxo_id not in picked_ids]
                return apply_merge_extras(picks, remaining)
            picks = greedy_pick(disclosed + undisclosed)
            if picks is None:
                return None
            picked_ids = {u.utxo_id for u in picks}
            remaining = [u for u in (disclosed + undisclosed) if u.utxo_id not in picked_ids]
            return apply_merge_extras(picks, remaining)

        if policy == "minimal_disclosed":
            # Use disclosed inputs first but as few as possible, then top up from others.
            if not disclosed:
                picks = greedy_pick(utxos)
                if picks is None:
                    return None
                picked_ids = {u.utxo_id for u in picks}
                remaining = [u for u in utxos if u.utxo_id not in picked_ids]
                return apply_merge_extras(picks, remaining)
            disclosed_sorted = sorted(disclosed, key=lambda u: u.value_sats, reverse=True)
            picked: list[WalletUTXO] = []
            total = 0
            for utxo in disclosed_sorted:
                picked.append(utxo)
                total += utxo.value_sats
                if total >= amount_sats:
                    picked_ids = {u.utxo_id for u in picked}
                    remaining = [u for u in disclosed if u.utxo_id not in picked_ids]
                    return apply_merge_extras(picked, remaining)
            remaining_needed = amount_sats - total
            rest = greedy_pick([u for u in utxos if u.utxo_id not in {p.utxo_id for p in picked}])
            if rest is None:
                return None
            # Add only as many as needed from rest (strategy order already deterministic)
            topup: list[WalletUTXO] = []
            topup_total = 0
            for utxo in rest:
                topup.append(utxo)
                topup_total += utxo.value_sats
                if topup_total >= remaining_needed:
                    break
            merged = picked + topup
            merged_ids = {u.utxo_id for u in merged}
            remaining = [u for u in utxos if u.utxo_id not in merged_ids]
            return apply_merge_extras(merged, remaining)

        if policy == "avoid_disclosed":
            picks = greedy_pick(undisclosed)
            if picks is not None:
                picked_ids = {u.utxo_id for u in picks}
                remaining = [u for u in undisclosed if u.utxo_id not in picked_ids]
                return apply_merge_extras(picks, remaining)
            picks = greedy_pick(utxos)
            if picks is None:
                return None
            picked_ids = {u.utxo_id for u in picks}
            remaining = [u for u in utxos if u.utxo_id not in picked_ids]
            return apply_merge_extras(picks, remaining)

        if policy == "randomized":
            # Favor undisclosed but occasionally consume disclosed to "flush" known coins.
            if self.rng.random() < 0.7:
                picks = greedy_pick(undisclosed)
                if picks is not None:
                    picked_ids = {u.utxo_id for u in picks}
                    remaining = [u for u in undisclosed if u.utxo_id not in picked_ids]
                    return apply_merge_extras(picks, remaining)
            mixed = list(utxos)
            self.rng.shuffle(mixed)
            picks = greedy_pick(mixed)
            if picks is None:
                return None
            picked_ids = {u.utxo_id for u in picks}
            remaining = [u for u in mixed if u.utxo_id not in picked_ids]
            return apply_merge_extras(picks, remaining)

        if policy == "adaptive":
            total_count = len(utxos)
            disclosed_count = len(disclosed)
            backlog_ratio = (disclosed_count / total_count) if total_count > 0 else 0.0

            base = self.config.adaptive_flush_base_probability
            max_p = self.config.adaptive_flush_max_probability
            target = max(1e-9, self.config.adaptive_flush_backlog_target)

            pressure = min(1.0, backlog_ratio / target)
            flush_prob = base + (max_p - base) * pressure

            # Usually avoid disclosed inputs, but flush some when backlog is high
            if self.rng.random() > flush_prob:
                picks = greedy_pick(undisclosed)
                if picks is not None:
                    picked_ids = {u.utxo_id for u in picks}
                    remaining = [u for u in undisclosed if u.utxo_id not in picked_ids]
                    return apply_merge_extras(picks, remaining)

            # Flush mode: intentionally consume disclosed inputs, but not only disclosed
            # when avoidable; this keeps maker functional under high pressure.
            mixed = disclosed + undisclosed
            picks = greedy_pick(mixed)
            if picks is None:
                return None
            picked_ids = {u.utxo_id for u in picks}
            remaining = [u for u in mixed if u.utxo_id not in picked_ids]
            return apply_merge_extras(picks, remaining)

        # "ignore" policy: strategy only
        picks = greedy_pick(utxos)
        if picks is None:
            return None
        picked_ids = {u.utxo_id for u in picks}
        remaining = [u for u in utxos if u.utxo_id not in picked_ids]
        return apply_merge_extras(picks, remaining)

    def probe_maker_max_mixdepth(self, maker_id: str) -> int:
        """Probe one maker and reveal UTXOs in its current largest mixdepth.

        Mitigations applied:
        - sticky_disclosed_utxos: if the maker has sticky UTXOs from a previous
          probe/failed CJ, only those are re-disclosed (no new info leaked).
        - max_utxos_per_offer: cap how many UTXOs are revealed.
        """
        maker = next((m for m in self.makers if m.maker_id == maker_id), None)
        if maker is None:
            return 0

        # Sticky mitigation: if maker already has sticky UTXOs, re-disclose only those.
        # The evil taker learns nothing new.
        if self.config.sticky_disclosed_utxos and self._sticky_utxos[maker_id]:
            # Re-disclose existing sticky UTXOs (they are already known)
            return len(self._sticky_utxos[maker_id])

        depth = maker.largest_mixdepth()
        utxos = list(maker.mixdepths[depth])

        # max_utxos_per_offer: only reveal a limited number of UTXOs
        if self.config.max_utxos_per_offer is not None:
            cap = self.config.max_utxos_per_offer
            if len(utxos) > cap:
                # Sort by value descending: reveal the largest UTXOs (greedy for amount)
                utxos = sorted(utxos, key=lambda u: u.value_sats, reverse=True)[:cap]

        known = self.known_utxos_by_maker[maker_id]
        known_depth = self.known_utxo_depth_by_maker[maker_id]
        for utxo in utxos:
            known.add(utxo.utxo_id)
            known_depth[utxo.utxo_id] = depth

        self.known_mixdepths_by_maker[maker_id].add(depth)
        self.probed_makers.add(maker_id)

        # Sticky mitigation: mark these UTXOs as sticky
        if self.config.sticky_disclosed_utxos:
            self._sticky_utxos[maker_id].update(u.utxo_id for u in utxos)

        # Flagged mitigation: mark disclosed UTXOs as flagged
        if self.config.flagged_utxo_isolation:
            self._flagged_utxos[maker_id].update(u.utxo_id for u in utxos)

        return len(utxos)

    def _pre_probe_all_makers_once(self) -> None:
        """Probe every maker once before normal rounds begin.

        This models a coordinated attacker snapshot where all makers are probed
        around the same time at max offer size.
        """
        for maker in self.makers:
            revealed = self.probe_maker_max_mixdepth(maker.maker_id)
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
            probed_utxos += self.probe_maker_max_mixdepth(maker.maker_id)
        return n_targets, probed_utxos

    def probe_all_makers_once(self) -> tuple[int, int]:
        """Probe every maker simultaneously at their individual max offer size.

        This models a single attacker probe round:
        - The attacker initiates with ALL makers at once.
        - Each maker reveals UTXOs from their largest mixdepth (subject to mitigations).
        - The attacker pays initiation_fee_sats per maker but does NOT complete any CJ.
        - No maker wallet state changes (no coins move).

        Returns (n_makers_probed, total_utxos_revealed).
        """
        n_probed = 0
        total_utxos = 0
        fee_cost = 0
        for maker in self.makers:
            revealed = self.probe_maker_max_mixdepth(maker.maker_id)
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
                    # Probe round: probe all makers, pay fee, no CJ
                    n_probed, _ = self.probe_all_makers_once()
                    day_probe_rounds += 1
                    day_probe_actions += n_probed
                    day_probe_cost += n_probed * self.config.initiation_fee_sats
                else:
                    # Honest CJ
                    record = self.simulate_single_honest_coinjoin(round_index=round_counter)
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
            merge_algorithm=self.config.merge_algorithm,
            disclosed_input_policy=self.config.disclosed_input_policy,
            max_utxos_per_offer=self.config.max_utxos_per_offer,
            sticky_disclosed_utxos=self.config.sticky_disclosed_utxos,
            flagged_utxo_isolation=self.config.flagged_utxo_isolation,
            wallet_init_mode=self.config.wallet_init_mode,
        )

    def simulate_single_honest_coinjoin(
        self,
        round_index: int = 0,
        cj_amount_sats: int | None = None,
    ) -> HonestCoinJoinRecord | None:
        """Run one honest CoinJoin and update maker wallets if successful.

        Mitigations applied:
        - flagged_utxo_isolation: if a maker's input set includes flagged UTXOs,
          do NOT count the equal-amount output as linked (it goes to a separate
          "clean" flow). The attacker cannot cluster flagged inputs with the
          equal output because the maker ensures they are never spent together.
        - sticky_disclosed_utxos: clear sticky state after successful participation.
        """
        amount = cj_amount_sats if cj_amount_sats is not None else self._sample_cj_amount_sats()
        selected_makers = self._select_makers_for_amount(amount)
        if selected_makers is None:
            return None

        n_depths = self._n_mixdepths
        plans: list[tuple[SimulatedMaker, int, int, list[WalletUTXO], int]] = []
        for maker in selected_makers:
            source_depth = maker.largest_mixdepth()
            source_utxos = maker.mixdepths[source_depth]
            chosen = self._select_inputs_for_amount(maker.maker_id, source_utxos, amount)
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
            disclosed_used = sum(
                1 for u in chosen if u.utxo_id in self.known_utxos_by_maker[maker.maker_id]
            )
            self._total_inputs_used += len(chosen)
            self._total_disclosed_inputs_used += disclosed_used
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

                # Flagged mitigation: if any input was flagged, the change descendant
                # inherits the flag
                if self.config.flagged_utxo_isolation:
                    flagged = self._flagged_utxos[maker.maker_id]
                    if flagged.intersection(chosen_ids):
                        flagged.add(change_utxo.utxo_id)

            # Sticky mitigation: successful CJ clears sticky state
            if self.config.sticky_disclosed_utxos:
                # Remove spent UTXOs from sticky set
                self._sticky_utxos[maker.maker_id] -= chosen_ids

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
                # Flagged UTXO isolation: if the intersection is ONLY flagged UTXOs,
                # the attacker cannot link the equal output to the maker because the
                # maker ensures flagged UTXOs are never co-spent with equal outputs.
                # In practice, this means flagged inputs don't identify the maker.
                if self.config.flagged_utxo_isolation:
                    flagged = self._flagged_utxos[event.maker_id]
                    intersecting = known_set.intersection(event.input_utxo_ids)
                    # Only count as identified if there are non-flagged known inputs
                    non_flagged_intersecting = intersecting - flagged
                    if not non_flagged_intersecting:
                        # All known inputs are flagged -> maker is NOT identified
                        # (the equal output is isolated from flagged UTXOs)
                        continue

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
        avg_disclosed_inputs_per_maker = (
            self._total_disclosed_inputs_used / self._total_maker_participations
            if self._total_maker_participations > 0
            else 0.0
        )
        disclosed_input_usage_fraction = (
            self._total_disclosed_inputs_used / self._total_inputs_used
            if self._total_inputs_used > 0
            else 0.0
        )

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
            max_utxos_per_offer=self.config.max_utxos_per_offer,
            sticky_disclosed_utxos=self.config.sticky_disclosed_utxos,
            flagged_utxo_isolation=self.config.flagged_utxo_isolation,
            initiation_fee_sats=self.config.initiation_fee_sats,
            total_probing_cost_sats=self._total_probing_cost_sats,
            total_honest_volume_sats=total_honest_volume_sats,
            probing_cost_per_probe_sats=probing_cost_per_probe,
            probing_cost_to_volume_ratio=probing_cost_to_volume,
            pre_probe_all_makers=self.config.pre_probe_all_makers,
            merge_algorithm=self.config.merge_algorithm,
            greediest_maker_fraction=self.config.greediest_maker_fraction,
            disclosed_input_policy=self.config.disclosed_input_policy,
            wallet_init_mode=self.config.wallet_init_mode,
            adaptive_flush_base_probability=self.config.adaptive_flush_base_probability,
            adaptive_flush_max_probability=self.config.adaptive_flush_max_probability,
            adaptive_flush_backlog_target=self.config.adaptive_flush_backlog_target,
            avg_inputs_per_maker=avg_inputs_per_maker,
            avg_disclosed_inputs_used_per_maker=avg_disclosed_inputs_per_maker,
            disclosed_input_usage_fraction=disclosed_input_usage_fraction,
            preprobe_actions=self._preprobe_actions,
            preprobe_utxos=self._preprobe_utxos,
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
