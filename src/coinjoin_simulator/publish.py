"""Helpers for building a curated publishable result summary.

This module extracts a compact set of metrics from the large experiment outputs
and prepares a JSON-serializable payload for a GitHub Pages report.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

MPC_LABEL_PATTERN = re.compile(r"^mpc(?P<mpc>\d+)_(?P<mitigation>.+)_evil(?P<evil>\d+(?:\.\d+)?)$")
DEPTH_LABEL_PATTERN = re.compile(
    r"^depths(?P<depth>\d+)_(?P<mitigation>.+)_evil(?P<evil>\d+(?:\.\d+)?)_mpc(?P<mpc>\d+)$"
)


def _coerce_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


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


def parse_mitigation_label(label: str) -> tuple[int, int, float, str]:
    """Parse mitigation experiment labels.

    Returns:
        Tuple of (makers_per_coinjoin, n_mixdepths, evil_fraction, mitigation_name).
    """
    mpc_match = MPC_LABEL_PATTERN.match(label)
    if mpc_match is not None:
        return (
            int(mpc_match.group("mpc")),
            5,
            float(mpc_match.group("evil")),
            mpc_match.group("mitigation"),
        )

    depth_match = DEPTH_LABEL_PATTERN.match(label)
    if depth_match is not None:
        return (
            int(depth_match.group("mpc")),
            int(depth_match.group("depth")),
            float(depth_match.group("evil")),
            depth_match.group("mitigation"),
        )

    raise ValueError(f"unrecognized mitigation label format: {label}")


def _average_pairs(points: dict[float, list[float]]) -> tuple[list[float], list[float]]:
    x_values = sorted(points.keys())
    y_values: list[float] = []
    for x_value in x_values:
        values = points[x_value]
        if not values:
            y_values.append(0.0)
            continue
        y_values.append(sum(values) / len(values))
    return x_values, y_values


def _round_list(values: list[float], digits: int = 6) -> list[float]:
    return [round(v, digits) for v in values]


def _sanitize_rows(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        raise ValueError("expected a JSON list")

    rows: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, dict):
            rows.append({str(key): value for key, value in item.items()})
    return rows


def _load_json(path: Path) -> object:
    with path.open() as handle:
        return json.load(handle)


def build_mitigation_series(
    rows: list[dict[str, object]],
    target_mpc: int = 8,
    target_mixdepths: int = 5,
    mitigations: tuple[str, ...] = ("baseline", "max_utxos_3", "combined_full"),
    include_depth_labels: bool = False,
) -> dict[str, dict[str, list[float]]]:
    """Build deanonymization and anon-set series for selected mitigations."""
    grouped_deanon: dict[str, dict[float, list[float]]] = {
        mitigation: {} for mitigation in mitigations
    }
    grouped_anon: dict[str, dict[float, list[float]]] = {
        mitigation: {} for mitigation in mitigations
    }

    for row in rows:
        label_obj = row.get("label")
        if not isinstance(label_obj, str):
            continue

        if not include_depth_labels and label_obj.startswith("depths"):
            continue

        try:
            mpc, n_mixdepths, evil_fraction, mitigation = parse_mitigation_label(label_obj)
        except ValueError:
            continue

        if mpc != target_mpc or n_mixdepths != target_mixdepths:
            continue
        if mitigation not in grouped_deanon:
            continue

        deanon = _coerce_float(row.get("taker_deanonymized_fraction"))
        mean_anon = _coerce_float(row.get("mean_taker_anon_set"))

        grouped_deanon[mitigation].setdefault(evil_fraction, []).append(deanon)
        grouped_anon[mitigation].setdefault(evil_fraction, []).append(mean_anon)

    series: dict[str, dict[str, list[float]]] = {}
    for mitigation in mitigations:
        evil_deanon, deanon_values = _average_pairs(grouped_deanon[mitigation])
        evil_anon, anon_values = _average_pairs(grouped_anon[mitigation])
        if evil_deanon != evil_anon:
            raise ValueError("inconsistent mitigation aggregation state")
        series[mitigation] = {
            "evil_fractions": _round_list(evil_deanon),
            "deanon": _round_list(deanon_values),
            "mean_anon_set": _round_list(anon_values),
        }
    return series


def build_longrun_series(
    rows: list[dict[str, object]],
    fee_sats: int = 500,
    policies: tuple[str, ...] = ("baseline", "recommended"),
) -> dict[str, dict[str, list[float]]]:
    """Build sustained long-run attack series by policy."""
    grouped_deanon: dict[str, dict[float, list[float]]] = {policy: {} for policy in policies}
    grouped_anon: dict[str, dict[float, list[float]]] = {policy: {} for policy in policies}

    for row in rows:
        policy = row.get("policy_name")
        if not isinstance(policy, str) or policy not in grouped_deanon:
            continue
        if _coerce_int(row.get("initiation_fee_sats")) != fee_sats:
            continue

        evil = _coerce_float(row.get("evil_taker_fraction"))
        deanon = _coerce_float(row.get("taker_deanonymized_fraction"))
        mean_anon = _coerce_float(row.get("mean_taker_anon_set"))

        grouped_deanon[policy].setdefault(evil, []).append(deanon)
        grouped_anon[policy].setdefault(evil, []).append(mean_anon)

    output: dict[str, dict[str, list[float]]] = {}
    for policy in policies:
        evil_values, deanon_values = _average_pairs(grouped_deanon[policy])
        evil_values_anon, anon_values = _average_pairs(grouped_anon[policy])
        if evil_values != evil_values_anon:
            raise ValueError("inconsistent long-run aggregation state")
        output[policy] = {
            "evil_fractions": _round_list(evil_values),
            "deanon": _round_list(deanon_values),
            "mean_anon_set": _round_list(anon_values),
        }
    return output


def build_intensity_series(
    rows: list[dict[str, object]],
    policies: tuple[str, ...] = ("baseline", "recommended"),
) -> dict[str, dict[str, list[float]]]:
    """Build probe-intensity series by policy."""
    grouped_deanon: dict[str, dict[int, list[float]]] = {policy: {} for policy in policies}
    grouped_cost: dict[str, dict[int, list[float]]] = {policy: {} for policy in policies}

    for row in rows:
        policy = row.get("policy_label")
        if not isinstance(policy, str) or policy not in grouped_deanon:
            continue

        probes_per_day = _coerce_int(row.get("probes_per_day"))
        deanon = _coerce_float(row.get("attack_taker_deanonymized_fraction"))
        daily_cost_btc = _coerce_float(row.get("attack_daily_cost_btc"))

        grouped_deanon[policy].setdefault(probes_per_day, []).append(deanon)
        grouped_cost[policy].setdefault(probes_per_day, []).append(daily_cost_btc)

    output: dict[str, dict[str, list[float]]] = {}
    for policy in policies:
        x_deanon = sorted(grouped_deanon[policy].keys())
        x_cost = sorted(grouped_cost[policy].keys())
        if x_deanon != x_cost:
            raise ValueError("inconsistent intensity aggregation state")

        y_deanon: list[float] = []
        y_cost: list[float] = []
        for probes in x_deanon:
            deanon_values = grouped_deanon[policy].get(probes, [])
            cost_values = grouped_cost[policy].get(probes, [])
            y_deanon.append(sum(deanon_values) / len(deanon_values) if deanon_values else 0.0)
            y_cost.append(sum(cost_values) / len(cost_values) if cost_values else 0.0)

        output[policy] = {
            "probes_per_day": [float(probes) for probes in x_deanon],
            "deanon": _round_list(y_deanon),
            "daily_cost_btc": _round_list(y_cost),
        }
    return output


def build_recovery_series(
    rows: list[dict[str, object]],
    probes_per_day: int = 20,
    policies: tuple[str, ...] = ("baseline", "recommended"),
) -> dict[str, dict[str, object]]:
    """Build attack-and-recovery timeline series for selected probe pressure."""
    output: dict[str, dict[str, object]] = {}
    for policy in policies:
        matching = [
            row
            for row in rows
            if row.get("policy_label") == policy
            and _coerce_int(row.get("probes_per_day")) == probes_per_day
        ]

        if not matching:
            output[policy] = {
                "days": [],
                "deanon": [],
                "known_live": [],
                "attack_end_day": 0,
                "recovery_day_deanon_le_5pct": None,
                "recovery_day_known_live_le_10pct": None,
            }
            continue

        row = matching[0]
        snapshots_obj = row.get("daily_snapshots")
        snapshots = _sanitize_rows(snapshots_obj) if isinstance(snapshots_obj, list) else []

        days = [_coerce_float(snapshot.get("day")) for snapshot in snapshots]
        deanon = [
            _coerce_float(snapshot.get("taker_deanonymized_fraction")) for snapshot in snapshots
        ]
        known_live = [
            _coerce_float(snapshot.get("known_live_utxo_fraction")) for snapshot in snapshots
        ]

        output[policy] = {
            "days": _round_list(days),
            "deanon": _round_list(deanon),
            "known_live": _round_list(known_live),
            "attack_end_day": _coerce_int(row.get("attack_end_day")),
            "recovery_day_deanon_le_5pct": row.get("recovery_day_deanon_le_5pct"),
            "recovery_day_known_live_le_10pct": row.get("recovery_day_known_live_le_10pct"),
        }

    return output


def _value_at_x(x_values: list[float], y_values: list[float], target: float) -> float:
    if not x_values or not y_values or len(x_values) != len(y_values):
        return 0.0

    best_index = 0
    best_distance = abs(x_values[0] - target)
    for index, value in enumerate(x_values[1:], start=1):
        distance = abs(value - target)
        if distance < best_distance:
            best_distance = distance
            best_index = index
    return y_values[best_index]


def build_key_findings(
    mitigation_series: dict[str, dict[str, list[float]]],
    longrun_series: dict[str, dict[str, list[float]]],
    intensity_series: dict[str, dict[str, list[float]]],
    recovery_series: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Build headline metrics for the publish page."""
    baseline_longrun = longrun_series.get("baseline", {})
    recommended_longrun = longrun_series.get("recommended", {})
    baseline_intensity = intensity_series.get("baseline", {})
    recommended_intensity = intensity_series.get("recommended", {})

    baseline_deanon_evil_04 = _value_at_x(
        baseline_longrun.get("evil_fractions", []),
        baseline_longrun.get("deanon", []),
        0.4,
    )
    recommended_deanon_evil_04 = _value_at_x(
        recommended_longrun.get("evil_fractions", []),
        recommended_longrun.get("deanon", []),
        0.4,
    )

    baseline_deanon_10_probes = _value_at_x(
        baseline_intensity.get("probes_per_day", []),
        baseline_intensity.get("deanon", []),
        10.0,
    )
    recommended_deanon_10_probes = _value_at_x(
        recommended_intensity.get("probes_per_day", []),
        recommended_intensity.get("deanon", []),
        10.0,
    )
    daily_cost_10_probes_btc = _value_at_x(
        baseline_intensity.get("probes_per_day", []),
        baseline_intensity.get("daily_cost_btc", []),
        10.0,
    )

    mitigation_baseline = mitigation_series.get("baseline", {})
    mitigation_combined = mitigation_series.get("combined_full", {})
    mitigation_baseline_deanon_06 = _value_at_x(
        mitigation_baseline.get("evil_fractions", []),
        mitigation_baseline.get("deanon", []),
        0.6,
    )
    mitigation_combined_deanon_06 = _value_at_x(
        mitigation_combined.get("evil_fractions", []),
        mitigation_combined.get("deanon", []),
        0.6,
    )

    baseline_recovery = recovery_series.get("baseline", {})
    recommended_recovery = recovery_series.get("recommended", {})

    return {
        "baseline_deanon_evil_04": round(baseline_deanon_evil_04, 6),
        "recommended_deanon_evil_04": round(recommended_deanon_evil_04, 6),
        "baseline_deanon_10_probes": round(baseline_deanon_10_probes, 6),
        "recommended_deanon_10_probes": round(recommended_deanon_10_probes, 6),
        "daily_cost_10_probes_btc": round(daily_cost_10_probes_btc, 6),
        "mitigation_baseline_deanon_06": round(mitigation_baseline_deanon_06, 6),
        "mitigation_combined_deanon_06": round(mitigation_combined_deanon_06, 6),
        "baseline_recovery_day_deanon_le_5pct": baseline_recovery.get(
            "recovery_day_deanon_le_5pct"
        ),
        "recommended_recovery_day_deanon_le_5pct": recommended_recovery.get(
            "recovery_day_deanon_le_5pct"
        ),
    }


def build_publish_payload(
    mitigation_path: Path,
    longrun_path: Path,
    daily_path: Path,
) -> dict[str, object]:
    """Load raw result files and build the publish summary payload."""
    mitigation_rows = _sanitize_rows(_load_json(mitigation_path))

    longrun_obj = _load_json(longrun_path)
    if not isinstance(longrun_obj, dict):
        raise ValueError("long-run result file must be a JSON object")
    longrun_map = {str(key): value for key, value in longrun_obj.items()}

    daily_obj = _load_json(daily_path)
    if not isinstance(daily_obj, dict):
        raise ValueError("daily result file must be a JSON object")
    daily_map = {str(key): value for key, value in daily_obj.items()}

    sustained_rows = _sanitize_rows(longrun_map.get("sustained_attack_results"))
    intensity_rows = _sanitize_rows(daily_map.get("intensity_sweep"))
    recovery_rows = _sanitize_rows(daily_map.get("recovery_timelines"))

    mitigation_series = build_mitigation_series(mitigation_rows)
    longrun_series = build_longrun_series(sustained_rows)
    intensity_series = build_intensity_series(intensity_rows)
    recovery_series = build_recovery_series(recovery_rows)
    key_findings = build_key_findings(
        mitigation_series=mitigation_series,
        longrun_series=longrun_series,
        intensity_series=intensity_series,
        recovery_series=recovery_series,
    )

    return {
        "context": {
            "n_bonded_profiles": _coerce_int(daily_map.get("n_bonded_profiles")),
            "honest_cjs_per_day": _coerce_int(daily_map.get("honest_cjs_per_day")),
        },
        "mitigation": {
            "target_makers_per_coinjoin": 8,
            "target_mixdepths": 5,
            "series": mitigation_series,
        },
        "longrun": {
            "target_fee_sats": 500,
            "series": longrun_series,
        },
        "daily_intensity": {
            "series": intensity_series,
        },
        "recovery": {
            "target_probes_per_day": 20,
            "series": recovery_series,
        },
        "key_findings": key_findings,
    }
