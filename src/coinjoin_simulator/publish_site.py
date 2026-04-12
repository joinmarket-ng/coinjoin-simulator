"""Generate a curated GitHub Pages site from simulation outputs."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .publish import build_publish_payload


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _as_float_list(value: object) -> list[float]:
    if not isinstance(value, list):
        return []

    result: list[float] = []
    for item in value:
        if isinstance(item, (int, float)):
            result.append(float(item))
    return result


def _as_int(value: object, default: int = 0) -> int:
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


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _format_btc(value: float) -> str:
    return f"{value:.4f} BTC/day"


def _mitigation_chart(payload: dict[str, object]) -> str:
    mitigation = _as_dict(payload.get("mitigation"))
    series = _as_dict(mitigation.get("series"))

    color_map = {
        "baseline": "#c7472d",
        "max_utxos_3": "#1f7a8c",
        "combined_full": "#1f9366",
    }
    name_map = {
        "baseline": "Baseline",
        "max_utxos_3": "Cap revealed UTXOs (max 3)",
        "combined_full": "Combined full policy",
    }

    fig = go.Figure()
    for key in ("baseline", "max_utxos_3", "combined_full"):
        row = _as_dict(series.get(key))
        evil = _as_float_list(row.get("evil_fractions"))
        deanon = _as_float_list(row.get("deanon"))
        if not evil or not deanon:
            continue
        fig.add_trace(
            go.Scatter(
                x=evil,
                y=deanon,
                mode="lines+markers",
                name=name_map.get(key, key),
                line={"color": color_map.get(key, "#666")},
                marker={"size": 8},
            )
        )

    fig.update_layout(
        title="Mitigation Impact at 8 Makers/CoinJoin",
        xaxis_title="Evil taker fraction",
        yaxis_title="Taker deanonymization",
        yaxis={"tickformat": ".0%", "range": [0, 1]},
        xaxis={"tickformat": ".1f"},
        template="plotly_white",
        height=420,
        margin={"l": 55, "r": 25, "t": 56, "b": 50},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0.0},
    )
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


def _longrun_chart(payload: dict[str, object]) -> str:
    longrun = _as_dict(payload.get("longrun"))
    series = _as_dict(longrun.get("series"))

    fig = go.Figure()
    for key, color in (("baseline", "#c7472d"), ("recommended", "#1f9366")):
        row = _as_dict(series.get(key))
        evil = _as_float_list(row.get("evil_fractions"))
        deanon = _as_float_list(row.get("deanon"))
        if not evil or not deanon:
            continue

        fig.add_trace(
            go.Scatter(
                x=evil,
                y=deanon,
                mode="lines+markers",
                name=key.capitalize(),
                line={"color": color, "width": 3},
                marker={"size": 8},
            )
        )

    fig.update_layout(
        title="Sustained Attack (Fee = 500 sats)",
        xaxis_title="Evil taker fraction",
        yaxis_title="Taker deanonymization",
        yaxis={"tickformat": ".0%", "range": [0, 1]},
        xaxis={"tickformat": ".1f"},
        template="plotly_white",
        height=420,
        margin={"l": 55, "r": 25, "t": 56, "b": 50},
    )
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


def _intensity_chart(payload: dict[str, object]) -> str:
    daily = _as_dict(payload.get("daily_intensity"))
    series = _as_dict(daily.get("series"))

    baseline = _as_dict(series.get("baseline"))
    recommended = _as_dict(series.get("recommended"))

    probes = _as_float_list(baseline.get("probes_per_day"))
    base_deanon = _as_float_list(baseline.get("deanon"))
    rec_deanon = _as_float_list(recommended.get("deanon"))
    cost = _as_float_list(baseline.get("daily_cost_btc"))

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    if probes and base_deanon:
        fig.add_trace(
            go.Scatter(
                x=probes,
                y=base_deanon,
                mode="lines+markers",
                name="Baseline deanonymization",
                line={"color": "#c7472d", "width": 3},
                marker={"size": 8},
            ),
            secondary_y=False,
        )

    if probes and rec_deanon:
        fig.add_trace(
            go.Scatter(
                x=probes,
                y=rec_deanon,
                mode="lines+markers",
                name="Recommended deanonymization",
                line={"color": "#1f9366", "width": 3},
                marker={"size": 8},
            ),
            secondary_y=False,
        )

    if probes and cost:
        fig.add_trace(
            go.Bar(
                x=probes,
                y=cost,
                name="Attacker daily cost",
                marker={"color": "#2f557f", "opacity": 0.35},
            ),
            secondary_y=True,
        )

    fig.update_layout(
        title="Probe Intensity vs Privacy and Cost",
        xaxis_title="Probe rounds per day",
        template="plotly_white",
        height=430,
        margin={"l": 55, "r": 55, "t": 56, "b": 50},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0.0},
    )
    fig.update_yaxes(
        title_text="Taker deanonymization", tickformat=".0%", range=[0, 1], secondary_y=False
    )
    fig.update_yaxes(title_text="Daily attacker cost (BTC)", secondary_y=True)
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


def _recovery_chart(payload: dict[str, object]) -> str:
    recovery = _as_dict(payload.get("recovery"))
    series = _as_dict(recovery.get("series"))

    baseline = _as_dict(series.get("baseline"))
    recommended = _as_dict(series.get("recommended"))

    days = _as_float_list(baseline.get("days"))
    base_deanon = _as_float_list(baseline.get("deanon"))
    rec_deanon = _as_float_list(recommended.get("deanon"))
    base_known = _as_float_list(baseline.get("known_live"))
    rec_known = _as_float_list(recommended.get("known_live"))
    attack_end = _as_int(baseline.get("attack_end_day"), 0)

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.14,
        subplot_titles=(
            "Taker deanonymization timeline",
            "Known live UTXO fraction timeline",
        ),
    )

    if days and base_deanon:
        fig.add_trace(
            go.Scatter(
                x=days,
                y=base_deanon,
                mode="lines",
                name="Baseline",
                line={"color": "#c7472d", "width": 3},
            ),
            row=1,
            col=1,
        )
    if days and rec_deanon:
        fig.add_trace(
            go.Scatter(
                x=days,
                y=rec_deanon,
                mode="lines",
                name="Recommended",
                line={"color": "#1f9366", "width": 3},
            ),
            row=1,
            col=1,
        )
    if days and base_known:
        fig.add_trace(
            go.Scatter(
                x=days,
                y=base_known,
                mode="lines",
                name="Baseline known-live",
                line={"color": "#c7472d", "dash": "dot"},
                showlegend=False,
            ),
            row=2,
            col=1,
        )
    if days and rec_known:
        fig.add_trace(
            go.Scatter(
                x=days,
                y=rec_known,
                mode="lines",
                name="Recommended known-live",
                line={"color": "#1f9366", "dash": "dot"},
                showlegend=False,
            ),
            row=2,
            col=1,
        )

    if attack_end > 0:
        fig.add_vline(x=attack_end, line_dash="dash", line_color="#667085", row=1, col=1)
        fig.add_vline(x=attack_end, line_dash="dash", line_color="#667085", row=2, col=1)

    fig.update_yaxes(title_text="Deanonymization", tickformat=".0%", range=[0, 1], row=1, col=1)
    fig.update_yaxes(title_text="Known live UTXOs", tickformat=".0%", range=[0, 0.22], row=2, col=1)
    fig.update_xaxes(title_text="Day", row=2, col=1)
    fig.update_layout(
        title="Attack and Recovery Timeline (20 probes/day)",
        template="plotly_white",
        height=620,
        margin={"l": 55, "r": 30, "t": 70, "b": 50},
    )
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


def _render_html(payload: dict[str, object], data_href: str) -> str:
    context = _as_dict(payload.get("context"))
    findings = _as_dict(payload.get("key_findings"))

    baseline_longrun = _as_float(findings.get("baseline_deanon_evil_04"))
    recommended_longrun = _as_float(findings.get("recommended_deanon_evil_04"))
    baseline_intensity = _as_float(findings.get("baseline_deanon_10_probes"))
    cost_ten = _as_float(findings.get("daily_cost_10_probes_btc"))
    baseline_recovery_day = findings.get("baseline_recovery_day_deanon_le_5pct")
    recommended_recovery_day = findings.get("recommended_recovery_day_deanon_le_5pct")

    n_bonded = _as_int(context.get("n_bonded_profiles"), 0)
    honest_per_day = _as_int(context.get("honest_cjs_per_day"), 0)

    mitigation_baseline_06 = _as_float(findings.get("mitigation_baseline_deanon_06"))
    mitigation_combined_06 = _as_float(findings.get("mitigation_combined_deanon_06"))

    mitigation_chart = _mitigation_chart(payload)
    longrun_chart = _longrun_chart(payload)
    intensity_chart = _intensity_chart(payload)
    recovery_chart = _recovery_chart(payload)

    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    recovery_baseline_text = (
        str(baseline_recovery_day) if baseline_recovery_day is not None else "n/a"
    )
    recovery_recommended_text = (
        str(recommended_recovery_day) if recommended_recovery_day is not None else "n/a"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CoinJoin Simulator Findings</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

    :root {{
      --ink: #102231;
      --ink-soft: #2f4858;
      --accent: #1f7a8c;
      --accent-2: #1f9366;
      --warn: #c7472d;
      --paper: #f7fbfd;
      --panel: #ffffff;
      --line: #d7e3ea;
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      font-family: 'IBM Plex Sans', sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 420px at 95% -10%, rgba(31, 122, 140, 0.2), transparent 55%),
        radial-gradient(1100px 460px at -20% 0%, rgba(31, 147, 102, 0.16), transparent 58%),
        linear-gradient(180deg, #f8fcff 0%, #eef6f8 100%);
      line-height: 1.52;
    }}

    .wrap {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 28px 18px 40px;
      animation: fade-in 550ms ease-out;
    }}

    @keyframes fade-in {{
      from {{ opacity: 0; transform: translateY(8px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}

    .hero {{
      background: linear-gradient(135deg, #102231 0%, #214d63 70%, #1f7a8c 100%);
      color: #f6fcff;
      border-radius: 14px;
      padding: 24px;
      box-shadow: 0 12px 28px rgba(18, 40, 56, 0.18);
    }}

    h1, h2 {{
      font-family: 'Fraunces', serif;
      letter-spacing: 0.2px;
      margin: 0;
    }}

    h1 {{ font-size: clamp(1.6rem, 4vw, 2.2rem); margin-bottom: 10px; }}
    h2 {{ font-size: clamp(1.25rem, 3vw, 1.55rem); margin: 0 0 8px; }}

    .hero p {{ margin: 0; opacity: 0.95; max-width: 860px; }}

    .meta {{
      margin-top: 14px;
      font-size: 0.92rem;
      opacity: 0.9;
    }}

    .kpis {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}

    .kpi {{
      background: rgba(255, 255, 255, 0.11);
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 10px;
      padding: 11px 12px;
    }}

    .kpi strong {{
      display: block;
      font-size: 1.05rem;
      margin-bottom: 2px;
      font-weight: 600;
    }}

    .kpi span {{ font-size: 0.83rem; opacity: 0.88; }}

    .section {{
      margin-top: 24px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 4px 16px rgba(16, 34, 49, 0.06);
    }}

    .section p {{ margin: 0 0 10px; color: var(--ink-soft); }}

    .chart {{
      background: #fff;
      border: 1px solid #e5edf2;
      border-radius: 10px;
      padding: 6px;
    }}

    .insights {{
      margin-top: 24px;
      padding: 15px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #f8fbfd;
    }}

    .insights ul {{ margin: 8px 0 0; padding-left: 18px; }}
    .insights li {{ margin: 6px 0; color: var(--ink-soft); }}
    .insights strong {{ color: var(--ink); }}

    .footer {{
      margin-top: 24px;
      font-size: 0.9rem;
      color: #5f7482;
    }}

    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    @media (max-width: 900px) {{
      .kpis {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}

    @media (max-width: 620px) {{
      .kpis {{ grid-template-columns: 1fr; }}
      .wrap {{ padding: 18px 12px 26px; }}
      .hero {{ padding: 18px; }}
      .section {{ padding: 12px; }}
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero">
      <h1>CoinJoin Probing Risk: Curated Findings</h1>
      <p>
        This publish page keeps only the highest-signal outputs from the simulator.
        It focuses on sustained probing impact, mitigation effectiveness, attack economics,
        and recovery behavior.
      </p>
      <div class="meta">
        Generated {generated_at} |
        bonded profiles: {n_bonded} |
        honest CoinJoins/day: {honest_per_day}
      </div>
      <div class="kpis">
        <div class="kpi">
          <strong>{_pct(baseline_longrun)}</strong>
          <span>Baseline deanon at evil=0.4</span>
        </div>
        <div class="kpi">
          <strong>{_pct(recommended_longrun)}</strong>
          <span>Recommended deanon at evil=0.4</span>
        </div>
        <div class="kpi">
          <strong>{_pct(baseline_intensity)}</strong>
          <span>Baseline deanon at 10 probes/day</span>
        </div>
        <div class="kpi">
          <strong>{_format_btc(cost_ten)}</strong>
          <span>Attacker burn at 10 probes/day</span>
        </div>
      </div>
    </section>

    <section class="section">
      <h2>1) Mitigation Comparison</h2>
      <p>
        At 8 makers/CoinJoin, both `max_utxos_3` and `combined_full` collapse deanonymization to ~0
        across the tested evil-fraction range, while baseline degrades sharply.
        At evil=0.6: baseline is {_pct(mitigation_baseline_06)} vs
        combined policy {_pct(mitigation_combined_06)}.
      </p>
      <div class="chart">{mitigation_chart}</div>
    </section>

    <section class="section">
      <h2>2) Long-Run Sustained Attack</h2>
      <p>
        Over long-run attack rounds with 500 sat initiation fees,
        baseline remains highly vulnerable,
        while the recommended policy stays flat at 0 deanonymization in this dataset.
      </p>
      <div class="chart">{longrun_chart}</div>
    </section>

    <section class="section">
      <h2>3) Probe Intensity Economics</h2>
      <p>
        Privacy loss and attack cost are shown together to highlight where attacker spend grows
        without additional privacy gains against the recommended policy.
      </p>
      <div class="chart">{intensity_chart}</div>
    </section>

    <section class="section">
      <h2>4) Recovery Dynamics</h2>
      <p>
        Attack window ends at day 14. Baseline needs until day
        {recovery_baseline_text} to recover below 5% deanonymization;
        recommended is already below that threshold by day
        {recovery_recommended_text}.
      </p>
      <div class="chart">{recovery_chart}</div>
    </section>

    <section class="insights">
      <h2>What Matters</h2>
      <ul>
        <li>
          <strong>Baseline remains fragile:</strong>
          sustained probing drives deanonymization into high ranges
          even at moderate attacker share.
        </li>
        <li>
          <strong>Policy controls dominate:</strong>
          limiting disclosed UTXO exposure with combined safeguards
          removes the observed deanonymization path in these runs.
        </li>
        <li>
          <strong>Cost alone is weak:</strong>
          fee-based pressure increases attacker spend but does not,
          by itself, repair baseline privacy.
        </li>
        <li>
          <strong>Operationally useful:</strong>
          the recommended settings retain near-max anonymity while
          attacks continue in the background.
        </li>
      </ul>
    </section>

    <p class="footer">
      Raw curated dataset: <a href="{data_href}">{data_href}</a>
    </p>
  </main>
</body>
</html>
"""


def generate_publish_site(
    mitigation_path: Path,
    longrun_path: Path,
    daily_path: Path,
    output_path: Path,
    data_output_path: Path,
) -> tuple[Path, Path]:
    """Generate the publish HTML page and its compact data payload."""
    payload = build_publish_payload(
        mitigation_path=mitigation_path,
        longrun_path=longrun_path,
        daily_path=daily_path,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    data_output_path.parent.mkdir(parents=True, exist_ok=True)

    data_output_path.write_text(json.dumps(payload, indent=2) + "\n")

    relative_data_href = data_output_path.name
    if data_output_path.parent != output_path.parent:
        relative_data_href = str(data_output_path.relative_to(output_path.parent))

    html = _render_html(payload, relative_data_href)
    output_path.write_text(html)

    return output_path.resolve(), data_output_path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate curated GitHub Pages site")
    parser.add_argument(
        "--mitigation",
        type=Path,
        default=Path("mitigation_experiments.json"),
        help="Path to mitigation experiment JSON",
    )
    parser.add_argument(
        "--longrun",
        type=Path,
        default=Path("longrun_policy_results.json"),
        help="Path to long-run policy JSON",
    )
    parser.add_argument(
        "--daily",
        type=Path,
        default=Path("daily_cost_study_results.json"),
        help="Path to daily cost study JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/index.html"),
        help="Path to generated HTML page",
    )
    parser.add_argument(
        "--data-output",
        type=Path,
        default=Path("docs/publish_summary.json"),
        help="Path to generated compact JSON payload",
    )
    args = parser.parse_args()

    html_path, data_path = generate_publish_site(
        mitigation_path=args.mitigation,
        longrun_path=args.longrun,
        daily_path=args.daily,
        output_path=args.output,
        data_output_path=args.data_output,
    )
    print(f"Publish page generated: {html_path}")
    print(f"Curated data generated: {data_path}")


if __name__ == "__main__":
    main()
