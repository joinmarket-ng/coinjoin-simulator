"""Tests for curated publish payload generation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from coinjoin_simulator.publish import (
    build_publish_payload,
    parse_mitigation_label,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_parse_mitigation_label_mpc_format() -> None:
    parsed = parse_mitigation_label("mpc8_combined_full_evil0.4")
    assert parsed == (8, 5, 0.4, "combined_full")


def test_parse_mitigation_label_depth_format() -> None:
    parsed = parse_mitigation_label("depths3_baseline_evil0.6_mpc8")
    assert parsed == (8, 3, 0.6, "baseline")


def test_build_publish_payload_extracts_compact_metrics(tmp_path: Path) -> None:
    mitigation_rows = [
        {
            "label": "mpc8_baseline_evil0.4",
            "taker_deanonymized_fraction": 0.30,
            "mean_taker_anon_set": 2.2,
        },
        {
            "label": "mpc8_baseline_evil0.4",
            "taker_deanonymized_fraction": 0.50,
            "mean_taker_anon_set": 1.8,
        },
        {
            "label": "mpc8_baseline_evil0.6",
            "taker_deanonymized_fraction": 0.60,
            "mean_taker_anon_set": 1.3,
        },
        {
            "label": "mpc8_max_utxos_3_evil0.4",
            "taker_deanonymized_fraction": 0.0,
            "mean_taker_anon_set": 9.0,
        },
        {
            "label": "mpc8_max_utxos_3_evil0.6",
            "taker_deanonymized_fraction": 0.0,
            "mean_taker_anon_set": 8.9,
        },
        {
            "label": "mpc8_combined_full_evil0.4",
            "taker_deanonymized_fraction": 0.0,
            "mean_taker_anon_set": 9.0,
        },
        {
            "label": "mpc8_combined_full_evil0.6",
            "taker_deanonymized_fraction": 0.0,
            "mean_taker_anon_set": 9.0,
        },
    ]

    longrun_payload = {
        "sustained_attack_results": [
            {
                "policy_name": "baseline",
                "initiation_fee_sats": 500,
                "evil_taker_fraction": 0.2,
                "taker_deanonymized_fraction": 0.70,
                "mean_taker_anon_set": 1.4,
            },
            {
                "policy_name": "baseline",
                "initiation_fee_sats": 500,
                "evil_taker_fraction": 0.4,
                "taker_deanonymized_fraction": 0.80,
                "mean_taker_anon_set": 1.2,
            },
            {
                "policy_name": "recommended",
                "initiation_fee_sats": 500,
                "evil_taker_fraction": 0.2,
                "taker_deanonymized_fraction": 0.0,
                "mean_taker_anon_set": 9.0,
            },
            {
                "policy_name": "recommended",
                "initiation_fee_sats": 500,
                "evil_taker_fraction": 0.4,
                "taker_deanonymized_fraction": 0.0,
                "mean_taker_anon_set": 9.0,
            },
        ]
    }

    daily_payload = {
        "n_bonded_profiles": 123,
        "honest_cjs_per_day": 100,
        "intensity_sweep": [
            {
                "policy_label": "baseline",
                "probes_per_day": 10,
                "attack_taker_deanonymized_fraction": 0.88,
                "attack_daily_cost_btc": 0.005,
            },
            {
                "policy_label": "recommended",
                "probes_per_day": 10,
                "attack_taker_deanonymized_fraction": 0.0,
                "attack_daily_cost_btc": 0.005,
            },
            {
                "policy_label": "baseline",
                "probes_per_day": 20,
                "attack_taker_deanonymized_fraction": 0.92,
                "attack_daily_cost_btc": 0.010,
            },
            {
                "policy_label": "recommended",
                "probes_per_day": 20,
                "attack_taker_deanonymized_fraction": 0.0,
                "attack_daily_cost_btc": 0.010,
            },
        ],
        "recovery_timelines": [
            {
                "policy_label": "baseline",
                "probes_per_day": 20,
                "attack_end_day": 14,
                "recovery_day_deanon_le_5pct": 22,
                "recovery_day_known_live_le_10pct": 17,
                "daily_snapshots": [
                    {
                        "day": 0,
                        "taker_deanonymized_fraction": 0.9,
                        "known_live_utxo_fraction": 0.2,
                    },
                    {
                        "day": 22,
                        "taker_deanonymized_fraction": 0.03,
                        "known_live_utxo_fraction": 0.08,
                    },
                ],
            },
            {
                "policy_label": "recommended",
                "probes_per_day": 20,
                "attack_end_day": 14,
                "recovery_day_deanon_le_5pct": 14,
                "recovery_day_known_live_le_10pct": 14,
                "daily_snapshots": [
                    {
                        "day": 0,
                        "taker_deanonymized_fraction": 0.0,
                        "known_live_utxo_fraction": 0.17,
                    },
                    {
                        "day": 22,
                        "taker_deanonymized_fraction": 0.0,
                        "known_live_utxo_fraction": 0.01,
                    },
                ],
            },
        ],
    }

    mitigation_path = tmp_path / "mitigation.json"
    longrun_path = tmp_path / "longrun.json"
    daily_path = tmp_path / "daily.json"

    mitigation_path.write_text(json.dumps(mitigation_rows))
    longrun_path.write_text(json.dumps(longrun_payload))
    daily_path.write_text(json.dumps(daily_payload))

    payload = build_publish_payload(
        mitigation_path=mitigation_path,
        longrun_path=longrun_path,
        daily_path=daily_path,
    )

    context = payload["context"]
    assert isinstance(context, dict)
    assert context["n_bonded_profiles"] == 123
    assert context["honest_cjs_per_day"] == 100

    mitigation = payload["mitigation"]
    assert isinstance(mitigation, dict)
    series = mitigation["series"]
    assert isinstance(series, dict)

    baseline_series = series["baseline"]
    assert isinstance(baseline_series, dict)
    assert baseline_series["evil_fractions"] == [0.4, 0.6]
    assert baseline_series["deanon"] == [0.4, 0.6]

    findings = payload["key_findings"]
    assert isinstance(findings, dict)
    assert findings["baseline_deanon_evil_04"] == 0.8
    assert findings["recommended_deanon_evil_04"] == 0.0
    assert findings["baseline_deanon_10_probes"] == 0.88
    assert findings["daily_cost_10_probes_btc"] == 0.005
    assert findings["baseline_recovery_day_deanon_le_5pct"] == 22
