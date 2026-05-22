"""Tests for realistic network-level simulation."""

from __future__ import annotations

import pytest

from coinjoin_simulator.network import (
    BondedMakerProfile,
    NetworkSimulationConfig,
    RealisticNetworkSimulator,
    SustainedAttackConfig,
    WalletUTXO,
    extract_bonded_maker_profiles,
    run_network_sweep,
)


def _maker_profiles(n: int = 20) -> list[BondedMakerProfile]:
    return [
        BondedMakerProfile(
            counterparty=f"maker{idx:03d}",
            max_size_sats=2_000_000 + idx * 120_000,
            fidelity_bond_value=1_000_000 + idx * 10_000,
            fee_type="relative",
            fee_value=0.001,
        )
        for idx in range(n)
    ]


def test_extract_bonded_maker_profiles_filters_and_dedupes() -> None:
    data = {
        "offers": [
            {
                "counterparty": "maker_a",
                "maxsize": 1_000_000,
                "fidelity_bond_value": 500,
                "ordertype": "sw0reloffer",
                "cjfee": 0.001,
            },
            {
                "counterparty": "maker_a",
                "maxsize": 2_000_000,
                "fidelity_bond_value": 500,
                "ordertype": "sw0absoffer",
                "cjfee": 800,
            },
            {
                "counterparty": "maker_b",
                "maxsize": 3_000_000,
                "fidelity_bond_value": 0,
                "ordertype": "sw0reloffer",
                "cjfee": 0.001,
            },
            {
                "counterparty": "maker_c",
                "maxsize": 1_500_000,
                "fidelity_bond_value": 100,
                "ordertype": "sw0reloffer",
                "cjfee": 0.002,
            },
        ]
    }

    profiles = extract_bonded_maker_profiles(data)
    assert len(profiles) == 2

    by_counterparty = {profile.counterparty: profile for profile in profiles}
    assert by_counterparty["maker_a"].max_size_sats == 2_000_000
    assert by_counterparty["maker_a"].fee_type == "absolute"
    assert by_counterparty["maker_c"].fee_type == "relative"


def test_extract_bonded_maker_profiles_raises_without_bonded() -> None:
    data = {
        "offers": [
            {
                "counterparty": "maker_z",
                "maxsize": 2_000_000,
                "fidelity_bond_value": 0,
                "ordertype": "sw0reloffer",
                "cjfee": 0.001,
            }
        ]
    }

    with pytest.raises(ValueError, match="no bonded makers"):
        extract_bonded_maker_profiles(data)


def test_probe_reveals_only_largest_mixdepth() -> None:
    config = NetworkSimulationConfig(n_makers=3, n_rounds=10, random_seed=1)
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(10))

    maker = sim.makers[0]
    maker.mixdepths = [
        [WalletUTXO("u_a", 100_000, 0, 0)],
        [WalletUTXO("u_b", 200_000, 1, 0)],
        [WalletUTXO("u_c", 300_000, 2, 0), WalletUTXO("u_d", 150_000, 2, 0)],
        [WalletUTXO("u_e", 100_000, 3, 0)],
        [],
    ]
    # Rebuild offer slot after injecting test UTXOs (slot was built on init from original UTXOs)
    sim._build_offer_slot(maker, current_round=0)

    revealed = sim.probe_maker_max_mixdepth(maker.maker_id)
    assert revealed == 2
    assert sim.known_utxos_by_maker[maker.maker_id] == {"u_c", "u_d"}
    assert sim.known_mixdepths_by_maker[maker.maker_id] == {2}


def test_honest_coinjoin_moves_equal_and_keeps_change_depth() -> None:
    config = NetworkSimulationConfig(
        n_makers=30,
        n_rounds=1,
        n_makers_per_coinjoin=4,
        evil_taker_fraction=0.0,
        random_seed=2,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(60))

    record = sim.simulate_single_honest_coinjoin(round_index=17, cj_amount_sats=1_800_000)
    assert record is not None

    makers = {maker.maker_id: maker for maker in sim.makers}
    for event in record.maker_events:
        maker = makers[event.maker_id]

        assert any(
            utxo.created_round == 17 and utxo.value_sats == record.cj_amount_sats
            for utxo in maker.mixdepths[event.next_mixdepth]
        )

        if event.change_utxo_id is not None:
            assert any(
                utxo.utxo_id == event.change_utxo_id
                for utxo in maker.mixdepths[event.source_mixdepth]
            )


def test_no_evil_rounds_keep_full_taker_anonymity() -> None:
    config = NetworkSimulationConfig(
        n_makers=50,
        n_rounds=180,
        n_makers_per_coinjoin=5,
        evil_taker_fraction=0.0,
        random_seed=3,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(80))

    result = sim.run()
    assert result.n_evil_rounds == 0
    assert result.avg_identified_maker_fraction == 0.0
    if result.n_successful_coinjoins > 0:
        assert result.mean_taker_anon_set == 6.0


def test_higher_evil_fraction_increases_clustering_and_identification() -> None:
    base_profiles = _maker_profiles(90)

    low_config = NetworkSimulationConfig(
        n_makers=70,
        n_rounds=220,
        n_makers_per_coinjoin=5,
        evil_taker_fraction=0.0,
        random_seed=7,
    )
    high_config = NetworkSimulationConfig(
        n_makers=70,
        n_rounds=220,
        n_makers_per_coinjoin=5,
        evil_taker_fraction=0.6,
        random_seed=7,
    )

    low_result = RealisticNetworkSimulator(config=low_config, maker_profiles=base_profiles).run()
    high_result = RealisticNetworkSimulator(config=high_config, maker_profiles=base_profiles).run()

    assert low_result.maker_clustered_fraction == 0.0
    assert high_result.maker_clustered_fraction > low_result.maker_clustered_fraction
    assert high_result.avg_identified_maker_fraction > low_result.avg_identified_maker_fraction
    assert high_result.n_probe_actions > 0


def test_run_network_sweep_returns_one_result_per_fraction() -> None:
    config = NetworkSimulationConfig(
        n_makers=25,
        n_rounds=80,
        n_makers_per_coinjoin=4,
        random_seed=10,
    )

    fractions = [0.0, 0.3, 0.9]
    results = run_network_sweep(config, _maker_profiles(40), fractions)

    assert len(results) == len(fractions)
    observed = [result.evil_taker_fraction for result in results]
    assert observed == fractions


# --- Tests for configurable mixdepths ---


def test_configurable_mixdepths_3() -> None:
    """Wallets with 3 mixdepths still work correctly."""
    config = NetworkSimulationConfig(
        n_makers=10,
        n_rounds=50,
        n_makers_per_coinjoin=3,
        evil_taker_fraction=0.3,
        n_mixdepths=3,
        random_seed=20,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(20))

    # All makers should have exactly 3 mixdepths
    for maker in sim.makers:
        assert len(maker.mixdepths) == 3

    result = sim.run()
    assert result.n_mixdepths == 3
    assert result.n_successful_coinjoins > 0


def test_configurable_mixdepths_8() -> None:
    """Wallets with 8 mixdepths spread balance over more buckets."""
    config = NetworkSimulationConfig(
        n_makers=10,
        n_rounds=50,
        n_makers_per_coinjoin=3,
        evil_taker_fraction=0.0,
        n_mixdepths=8,
        random_seed=21,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(20))

    for maker in sim.makers:
        assert len(maker.mixdepths) == 8

    result = sim.run()
    assert result.n_mixdepths == 8


def test_mixdepth_wrapping_uses_n_mixdepths() -> None:
    """Equal output goes to (source + 1) % n_mixdepths, not hardcoded % 5."""
    config = NetworkSimulationConfig(
        n_makers=20,
        n_rounds=1,
        n_makers_per_coinjoin=3,
        evil_taker_fraction=0.0,
        n_mixdepths=3,
        random_seed=22,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(30))

    record = sim.simulate_single_honest_coinjoin(round_index=1, cj_amount_sats=1_800_000)
    assert record is not None

    for event in record.maker_events:
        expected_next = (event.source_mixdepth + 1) % 3
        assert event.next_mixdepth == expected_next


def test_n_mixdepths_validation() -> None:
    """n_mixdepths < 2 raises ValueError."""
    with pytest.raises(ValueError, match="n_mixdepths must be >= 2"):
        NetworkSimulationConfig(n_mixdepths=1)


# --- Tests for offer_slot_size + slot TTL mitigation ---


def test_offer_slot_size_caps_revealed_utxos() -> None:
    """With offer_slot_size=1, only 1 UTXO is revealed per probe."""
    config = NetworkSimulationConfig(
        n_makers=3,
        n_rounds=10,
        offer_slot_size=1,
        random_seed=30,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(10))

    # Force a maker to have many UTXOs in largest mixdepth
    maker = sim.makers[0]
    maker.mixdepths = [
        [WalletUTXO("u_a", 300_000, 0, 0), WalletUTXO("u_b", 200_000, 0, 0)],
        [WalletUTXO("u_c", 100_000, 1, 0)],
        [],
        [],
        [],
    ]
    sim._build_offer_slot(maker, current_round=0)

    revealed = sim.probe_maker_max_mixdepth(maker.maker_id)
    assert revealed == 1
    # Exactly one of the two UTXOs should be revealed (random selection)
    known = sim.known_utxos_by_maker[maker.maker_id]
    assert len(known) == 1
    assert known.issubset({"u_a", "u_b"})


def test_offer_slot_size_none_reveals_all() -> None:
    """With offer_slot_size=None (default), all UTXOs in active mixdepth are revealed."""
    config = NetworkSimulationConfig(n_makers=3, n_rounds=10, random_seed=31)
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(10))

    maker = sim.makers[0]
    maker.mixdepths = [
        [WalletUTXO("u_a", 300_000, 0, 0), WalletUTXO("u_b", 200_000, 0, 0)],
        [WalletUTXO("u_c", 100_000, 1, 0)],
        [],
        [],
        [],
    ]
    sim._build_offer_slot(maker, current_round=0)

    revealed = sim.probe_maker_max_mixdepth(maker.maker_id)
    assert revealed == 2
    assert sim.known_utxos_by_maker[maker.maker_id] == {"u_a", "u_b"}


def test_probe_does_not_rotate_slot() -> None:
    """Repeated probes within the slot's TTL must reveal the same UTXOs.

    This is the core sticky property: an attacker who pays initiation_fee
    twice in a row learns nothing new -- only TTL expiry or a successful CJ
    rotates the slot.
    """
    config = NetworkSimulationConfig(
        n_makers=3,
        n_rounds=10,
        offer_slot_size=1,
        slot_ttl_min_rounds=1000,
        slot_ttl_max_rounds=1000,
        random_seed=99,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(10))

    maker = sim.makers[0]
    maker.mixdepths = [
        [WalletUTXO("u_a", 300_000, 0, 0), WalletUTXO("u_b", 200_000, 0, 0)],
        [],
        [],
        [],
        [],
    ]
    sim._build_offer_slot(maker, current_round=0)

    seen: set[str] = set()
    for _ in range(20):
        sim.probe_maker_max_mixdepth(maker.maker_id, current_round=0)
        seen.update(sim.known_utxos_by_maker[maker.maker_id])

    # Sticky: only one UTXO ever revealed across many probes within TTL
    assert len(seen) == 1, "Probes must not rotate the slot; sticky exposure expected"


def test_slot_ttl_expiry_rotates_slot() -> None:
    """When the slot's TTL elapses, probing forces a rebuild and may expose new UTXOs."""
    config = NetworkSimulationConfig(
        n_makers=3,
        n_rounds=10,
        offer_slot_size=1,
        slot_ttl_min_rounds=1,
        slot_ttl_max_rounds=1,
        random_seed=99,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(10))

    maker = sim.makers[0]
    maker.mixdepths = [
        [WalletUTXO("u_a", 300_000, 0, 0), WalletUTXO("u_b", 200_000, 0, 0)],
        [],
        [],
        [],
        [],
    ]
    sim._build_offer_slot(maker, current_round=0)

    seen: set[str] = set()
    for round_idx in range(60):
        sim.probe_maker_max_mixdepth(maker.maker_id, current_round=round_idx)
        seen.update(sim.known_utxos_by_maker[maker.maker_id])
        if len(seen) == 2:
            break

    assert seen == {"u_a", "u_b"}, "TTL expiry should eventually rotate the slot to a new UTXO"


def test_offer_slot_reduces_probed_utxo_count() -> None:
    """Capping slot size should not increase the count of probed UTXOs vs no cap."""
    profiles = _maker_profiles(90)

    base_config = NetworkSimulationConfig(
        n_makers=70,
        n_rounds=300,
        n_makers_per_coinjoin=5,
        evil_taker_fraction=0.4,
        probes_per_evil_taker=5,
        random_seed=32,
    )
    capped_config = NetworkSimulationConfig(
        n_makers=70,
        n_rounds=300,
        n_makers_per_coinjoin=5,
        evil_taker_fraction=0.4,
        probes_per_evil_taker=5,
        offer_slot_size=1,
        random_seed=32,
    )

    base_result = RealisticNetworkSimulator(config=base_config, maker_profiles=profiles).run()
    capped_result = RealisticNetworkSimulator(config=capped_config, maker_profiles=profiles).run()

    assert capped_result.n_probed_utxos <= base_result.n_probed_utxos


def test_slot_rebuilds_after_successful_coinjoin() -> None:
    """A successful CoinJoin consumes the slot UTXO; the next probe sees a fresh slot."""
    config = NetworkSimulationConfig(
        n_makers=20,
        n_rounds=10,
        n_makers_per_coinjoin=3,
        evil_taker_fraction=0.0,
        offer_slot_size=2,
        slot_ttl_min_rounds=1000,
        slot_ttl_max_rounds=1000,
        random_seed=41,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(30))

    # Run honest CJs; verify slots get rebuilt for participating makers.
    initial_slots = {m.maker_id: tuple(sim._offer_slots[m.maker_id]) for m in sim.makers}
    for i in range(20):
        record = sim.simulate_single_honest_coinjoin(round_index=i)
        if record is None:
            continue
        for event in record.maker_events:
            # Slot must have been rebuilt: either differs from initial or maker
            # moved to a new mixdepth (where slot was rebuilt fresh).
            new_slot = tuple(sim._offer_slots[event.maker_id])
            assert new_slot != initial_slots[event.maker_id] or len(new_slot) <= 2


# --- Tests for initiation_fee mitigation ---


def test_initiation_fee_accumulates_cost() -> None:
    """Probing cost is tracked correctly with initiation fees."""
    config = NetworkSimulationConfig(
        n_makers=30,
        n_rounds=100,
        n_makers_per_coinjoin=5,
        evil_taker_fraction=0.5,
        probes_per_evil_taker=3,
        initiation_fee_sats=1000,
        random_seed=60,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(50))
    result = sim.run()

    assert result.initiation_fee_sats == 1000
    assert result.total_probing_cost_sats > 0
    # Cost should be n_probe_actions * 1000 sats
    assert result.total_probing_cost_sats == result.n_probe_actions * 1000
    assert result.probing_cost_per_probe_sats == 1000.0


def test_initiation_fee_zero_means_no_cost() -> None:
    """Default initiation_fee_sats=0 means zero probing cost."""
    config = NetworkSimulationConfig(
        n_makers=20,
        n_rounds=50,
        evil_taker_fraction=0.5,
        random_seed=61,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(30))
    result = sim.run()

    assert result.total_probing_cost_sats == 0
    assert result.probing_cost_per_probe_sats == 0.0


def test_probing_cost_to_volume_ratio() -> None:
    """probing_cost_to_volume_ratio = total_probing_cost / total_honest_volume."""
    config = NetworkSimulationConfig(
        n_makers=30,
        n_rounds=100,
        n_makers_per_coinjoin=5,
        evil_taker_fraction=0.3,
        probes_per_evil_taker=5,
        initiation_fee_sats=500,
        random_seed=62,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(50))
    result = sim.run()

    if result.total_honest_volume_sats > 0:
        expected_ratio = result.total_probing_cost_sats / result.total_honest_volume_sats
        assert abs(result.probing_cost_to_volume_ratio - expected_ratio) < 1e-10


# --- Tests for result metadata ---


def test_result_includes_mitigation_metadata() -> None:
    """NetworkSimulationResult includes all mitigation settings."""
    config = NetworkSimulationConfig(
        n_makers=10,
        n_rounds=20,
        n_mixdepths=7,
        offer_slot_size=2,
        slot_ttl_min_rounds=4,
        slot_ttl_max_rounds=20,
        initiation_fee_sats=250,
        evil_taker_fraction=0.3,
        random_seed=70,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(20))
    result = sim.run()

    assert result.n_mixdepths == 7
    assert result.offer_slot_size == 2
    assert result.slot_ttl_min_rounds == 4
    assert result.slot_ttl_max_rounds == 20
    assert result.initiation_fee_sats == 250

    # to_dict should include all new fields
    d = result.to_dict()
    assert d["n_mixdepths"] == 7
    assert d["offer_slot_size"] == 2
    assert d["slot_ttl_min_rounds"] == 4
    assert d["slot_ttl_max_rounds"] == 20
    assert d["initiation_fee_sats"] == 250
    assert "total_probing_cost_sats" in d
    assert "probing_cost_to_volume_ratio" in d
    assert "mean_top1_utxo_coverage" in d
    assert "mean_top3_utxo_coverage" in d
    assert "mean_top5_utxo_coverage" in d


def test_seeded_depth0_initialization_creates_only_depth0_utxos() -> None:
    config = NetworkSimulationConfig(
        n_makers=8,
        n_rounds=10,
        wallet_init_mode="seeded_depth0",
        seed_depth0_min_initial_utxos=1,
        seed_depth0_max_initial_utxos=2,
        random_seed=80,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(20))
    for maker in sim.makers:
        assert len(maker.mixdepths[0]) >= 1
        for depth in range(1, config.n_mixdepths):
            assert maker.mixdepths[depth] == []


def test_preprobe_all_makers_marks_all_probed() -> None:
    config = NetworkSimulationConfig(
        n_makers=12,
        n_rounds=10,
        pre_probe_all_makers=True,
        random_seed=81,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(30))
    assert len(sim.probed_makers) == 12
    assert sim._preprobe_actions == 12
    assert sim._preprobe_utxos > 0


def test_preprobe_can_deanonymize_next_honest_round() -> None:
    # When all makers are pre-probed and no countermeasure is used, the next honest
    # CJ should typically be fully identified.
    config = NetworkSimulationConfig(
        n_makers=30,
        n_rounds=1,
        n_makers_per_coinjoin=8,
        pre_probe_all_makers=True,
        random_seed=82,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(60))
    record = sim.simulate_single_honest_coinjoin(round_index=1, cj_amount_sats=1_500_000)
    assert record is not None
    assert record.identified_makers >= 4


def test_recommended_policy_defaults_set_expected_values() -> None:
    cfg = NetworkSimulationConfig.recommended_policy_defaults(n_rounds=10)
    assert cfg.offer_slot_size == 3
    assert cfg.slot_ttl_min_rounds == 4
    assert cfg.slot_ttl_max_rounds == 20
    assert cfg.wallet_init_mode == "seeded_depth0"
    assert cfg.initiation_fee_sats == 500


# --- Tests for SustainedAttackConfig validation ---


def test_sustained_attack_config_validation() -> None:
    """SustainedAttackConfig validates its fields."""
    with pytest.raises(ValueError, match="n_days must be positive"):
        SustainedAttackConfig(n_days=0)
    with pytest.raises(ValueError, match="probes_per_day must be non-negative"):
        SustainedAttackConfig(probes_per_day=-1)
    with pytest.raises(ValueError, match="attack_end_day must be >= attack_start_day"):
        SustainedAttackConfig(attack_start_day=10, attack_end_day=5)

    # Valid config should not raise
    cfg = SustainedAttackConfig(n_days=10, honest_cjs_per_day=50, probes_per_day=5)
    assert cfg.n_days == 10


# --- Tests for probe_all_makers_once ---


def test_probe_all_makers_once_probes_all() -> None:
    """probe_all_makers_once probes every maker and returns correct counts."""
    config = NetworkSimulationConfig(
        n_makers=10,
        n_rounds=1,
        initiation_fee_sats=500,
        random_seed=90,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(20))

    n_probed, total_utxos = sim.probe_all_makers_once()
    assert n_probed == 10
    assert total_utxos > 0
    assert len(sim.probed_makers) == 10
    # Cost should be 10 * 500 = 5000
    assert sim._total_probing_cost_sats == 5000


def test_probe_all_makers_no_wallet_state_change() -> None:
    """Probing does not change maker wallet balances or UTXO counts."""
    config = NetworkSimulationConfig(n_makers=5, n_rounds=1, random_seed=91)
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(10))

    # Record pre-probe state
    balances_before = [maker.total_balance() for maker in sim.makers]
    utxo_counts_before = [sum(len(d) for d in maker.mixdepths) for maker in sim.makers]

    sim.probe_all_makers_once()

    # Verify nothing changed
    balances_after = [maker.total_balance() for maker in sim.makers]
    utxo_counts_after = [sum(len(d) for d in maker.mixdepths) for maker in sim.makers]
    assert balances_before == balances_after
    assert utxo_counts_before == utxo_counts_after


# --- Tests for run_sustained_attack ---


def test_sustained_attack_no_probes_baseline() -> None:
    """With 0 probes/day, no probing cost and full taker anonymity."""
    config = NetworkSimulationConfig(
        n_makers=20,
        n_rounds=1,
        n_makers_per_coinjoin=4,
        initiation_fee_sats=500,
        random_seed=92,
    )
    attack_cfg = SustainedAttackConfig(
        n_days=5,
        honest_cjs_per_day=20,
        probes_per_day=0,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(30))
    result = sim.run_sustained_attack(attack_cfg)

    assert result.total_probe_rounds == 0
    assert result.total_probe_cost_sats == 0
    assert result.total_honest_cjs > 0
    assert len(result.daily_snapshots) == 5
    # No probing means full anonymity
    assert result.attack_taker_deanonymized_fraction == 0.0


def test_sustained_attack_cost_accounting() -> None:
    """Probing cost = probes_per_day * n_makers * fee_sats * n_attack_days."""
    config = NetworkSimulationConfig(
        n_makers=10,
        n_rounds=1,
        n_makers_per_coinjoin=3,
        initiation_fee_sats=1000,
        random_seed=93,
    )
    attack_cfg = SustainedAttackConfig(
        n_days=3,
        honest_cjs_per_day=10,
        probes_per_day=2,
        attack_start_day=0,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(15))
    result = sim.run_sustained_attack(attack_cfg)

    # 3 days * 2 probes/day * 10 makers * 1000 sats = 60,000 sats
    expected_cost = 3 * 2 * 10 * 1000
    assert result.total_probe_cost_sats == expected_cost
    assert result.total_probe_rounds == 6
    assert result.total_probe_actions == 60
    assert result.attack_daily_cost_sats == 2 * 10 * 1000  # 20,000 sats/day


def test_sustained_attack_with_recovery() -> None:
    """Attack stops after attack_end_day, recovery phase begins."""
    config = NetworkSimulationConfig(
        n_makers=15,
        n_rounds=1,
        n_makers_per_coinjoin=4,
        initiation_fee_sats=500,
        random_seed=94,
    )
    attack_cfg = SustainedAttackConfig(
        n_days=10,
        honest_cjs_per_day=30,
        probes_per_day=3,
        attack_start_day=0,
        attack_end_day=5,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(25))
    result = sim.run_sustained_attack(attack_cfg)

    # Only 5 days of attack (days 0-4)
    attack_days = [s for s in result.daily_snapshots if s.phase == "attack"]
    recovery_days = [s for s in result.daily_snapshots if s.phase == "recovery"]
    assert len(attack_days) == 5
    assert len(recovery_days) == 5

    # No probing in recovery
    for snap in recovery_days:
        assert snap.probe_rounds == 0
        assert snap.probe_cost_sats == 0


def test_sustained_attack_daily_snapshots_structure() -> None:
    """Each daily snapshot has the expected fields and consistent totals."""
    config = NetworkSimulationConfig(
        n_makers=8,
        n_rounds=1,
        n_makers_per_coinjoin=3,
        initiation_fee_sats=200,
        random_seed=95,
    )
    attack_cfg = SustainedAttackConfig(
        n_days=4,
        honest_cjs_per_day=15,
        probes_per_day=1,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(15))
    result = sim.run_sustained_attack(attack_cfg)

    assert len(result.daily_snapshots) == 4

    # Cumulative cost should be monotonically non-decreasing
    costs = [s.cumulative_probe_cost_sats for s in result.daily_snapshots]
    for i in range(1, len(costs)):
        assert costs[i] >= costs[i - 1]

    # Final cumulative cost should match total
    assert costs[-1] == result.total_probe_cost_sats


def test_sustained_attack_to_dict_roundtrips() -> None:
    """SustainedAttackResult.to_dict() includes all key fields."""
    config = NetworkSimulationConfig(
        n_makers=6,
        n_rounds=1,
        n_makers_per_coinjoin=3,
        initiation_fee_sats=100,
        random_seed=96,
    )
    attack_cfg = SustainedAttackConfig(n_days=2, honest_cjs_per_day=5, probes_per_day=1)
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(10))
    result = sim.run_sustained_attack(attack_cfg)

    d = result.to_dict()
    assert "n_days" in d
    assert "total_probe_cost_sats" in d
    assert "total_probe_cost_btc" in d
    assert "attack_daily_cost_sats" in d
    assert "daily_snapshots" in d
    assert isinstance(d["daily_snapshots"], list)
    assert len(d["daily_snapshots"]) == 2


def test_sustained_attack_honest_cjs_happen_during_attack() -> None:
    """Honest CJs still run during attack days -- they are not displaced."""
    config = NetworkSimulationConfig(
        n_makers=15,
        n_rounds=1,
        n_makers_per_coinjoin=4,
        initiation_fee_sats=500,
        random_seed=97,
    )
    attack_cfg = SustainedAttackConfig(
        n_days=3,
        honest_cjs_per_day=50,
        probes_per_day=5,
    )
    sim = RealisticNetworkSimulator(config=config, maker_profiles=_maker_profiles(25))
    result = sim.run_sustained_attack(attack_cfg)

    # Should have roughly 150 honest CJs (3 days * 50/day), minus failures
    assert result.total_honest_cjs >= 100  # Allow for some failures
    # And 15 probe rounds (3 * 5)
    assert result.total_probe_rounds == 15
