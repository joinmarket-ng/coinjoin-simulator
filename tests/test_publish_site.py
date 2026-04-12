"""Tests for curated publish site generation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from coinjoin_simulator.publish_site import generate_publish_site

if TYPE_CHECKING:
    from pathlib import Path


def test_generate_publish_site_writes_html_and_data(tmp_path: Path) -> None:
    mitigation_rows = [
        {
            "label": "mpc8_baseline_evil0.4",
            "taker_deanonymized_fraction": 0.3,
            "mean_taker_anon_set": 2.0,
        },
        {
            "label": "mpc8_max_utxos_3_evil0.4",
            "taker_deanonymized_fraction": 0.0,
            "mean_taker_anon_set": 9.0,
        },
        {
            "label": "mpc8_combined_full_evil0.4",
            "taker_deanonymized_fraction": 0.0,
            "mean_taker_anon_set": 9.0,
        },
        {
            "label": "mpc8_baseline_evil0.6",
            "taker_deanonymized_fraction": 0.6,
            "mean_taker_anon_set": 1.4,
        },
        {
            "label": "mpc8_max_utxos_3_evil0.6",
            "taker_deanonymized_fraction": 0.0,
            "mean_taker_anon_set": 8.8,
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
                "evil_taker_fraction": 0.4,
                "taker_deanonymized_fraction": 0.8,
                "mean_taker_anon_set": 1.2,
            },
            {
                "policy_name": "baseline",
                "initiation_fee_sats": 500,
                "evil_taker_fraction": 0.6,
                "taker_deanonymized_fraction": 0.9,
                "mean_taker_anon_set": 1.1,
            },
            {
                "policy_name": "recommended",
                "initiation_fee_sats": 500,
                "evil_taker_fraction": 0.4,
                "taker_deanonymized_fraction": 0.0,
                "mean_taker_anon_set": 9.0,
            },
            {
                "policy_name": "recommended",
                "initiation_fee_sats": 500,
                "evil_taker_fraction": 0.6,
                "taker_deanonymized_fraction": 0.0,
                "mean_taker_anon_set": 9.0,
            },
        ]
    }

    daily_payload = {
        "n_bonded_profiles": 111,
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
                "recovery_day_deanon_le_5pct": 21,
                "recovery_day_known_live_le_10pct": 17,
                "daily_snapshots": [
                    {
                        "day": 0,
                        "taker_deanonymized_fraction": 0.9,
                        "known_live_utxo_fraction": 0.2,
                    },
                    {
                        "day": 21,
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
                        "known_live_utxo_fraction": 0.16,
                    },
                    {
                        "day": 21,
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
    output_path = tmp_path / "docs" / "index.html"
    data_output_path = tmp_path / "docs" / "publish_summary.json"

    mitigation_path.write_text(json.dumps(mitigation_rows))
    longrun_path.write_text(json.dumps(longrun_payload))
    daily_path.write_text(json.dumps(daily_payload))

    html_path, data_path = generate_publish_site(
        mitigation_path=mitigation_path,
        longrun_path=longrun_path,
        daily_path=daily_path,
        output_path=output_path,
        data_output_path=data_output_path,
    )

    assert html_path == output_path.resolve()
    assert data_path == data_output_path.resolve()
    assert output_path.exists()
    assert data_output_path.exists()

    html = output_path.read_text()
    assert "CoinJoin Probing Risk: Curated Findings" in html
    assert "publish_summary.json" in html
