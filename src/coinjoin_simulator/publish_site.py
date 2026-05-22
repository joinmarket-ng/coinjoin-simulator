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


# -- Chart builders ----------------------------------------------------------


def _baseline_vulnerability_chart(payload: dict[str, object]) -> str:
    """Baseline deanon vs evil fraction (mitigation experiments, mpc8)."""
    mitigation = _as_dict(payload.get("mitigation"))
    series = _as_dict(mitigation.get("series"))
    baseline = _as_dict(series.get("baseline"))

    evil = _as_float_list(baseline.get("evil_fractions"))
    deanon = _as_float_list(baseline.get("deanon"))

    fig = go.Figure()
    if evil and deanon:
        fig.add_trace(
            go.Scatter(
                x=evil,
                y=deanon,
                mode="lines+markers",
                name="Baseline (no mitigations)",
                line={"color": "#c7472d", "width": 3},
                marker={"size": 9},
                fill="tozeroy",
                fillcolor="rgba(199, 71, 45, 0.10)",
            )
        )

    fig.update_layout(
        title="Baseline: All-Maker Input Coverage vs Attacker Share",
        xaxis_title="Fraction of CoinJoin rounds initiated by attacker",
        yaxis_title="CJs where all makers are identifiable from inputs",
        yaxis={"tickformat": ".0%", "range": [0, 1]},
        xaxis={"tickformat": ".0%"},
        template="plotly_white",
        height=380,
        margin={"l": 55, "r": 25, "t": 56, "b": 55},
    )
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


def _longrun_degradation_chart(payload: dict[str, object]) -> str:
    """Compare 1000-round vs 5000-round baseline to show time degradation."""
    mitigation = _as_dict(payload.get("mitigation"))
    mit_series = _as_dict(mitigation.get("series"))
    baseline_1k = _as_dict(mit_series.get("baseline"))

    longrun = _as_dict(payload.get("longrun"))
    lr_series = _as_dict(longrun.get("series"))
    baseline_5k = _as_dict(lr_series.get("baseline"))

    fig = go.Figure()

    evil_1k = _as_float_list(baseline_1k.get("evil_fractions"))
    deanon_1k = _as_float_list(baseline_1k.get("deanon"))
    if evil_1k and deanon_1k:
        fig.add_trace(
            go.Scatter(
                x=evil_1k,
                y=deanon_1k,
                mode="lines+markers",
                name="1,000 rounds",
                line={"color": "#e8843c", "width": 2, "dash": "dot"},
                marker={"size": 7},
            )
        )

    evil_5k = _as_float_list(baseline_5k.get("evil_fractions"))
    deanon_5k = _as_float_list(baseline_5k.get("deanon"))
    if evil_5k and deanon_5k:
        fig.add_trace(
            go.Scatter(
                x=evil_5k,
                y=deanon_5k,
                mode="lines+markers",
                name="5,000 rounds (sustained)",
                line={"color": "#c7472d", "width": 3},
                marker={"size": 9},
            )
        )

    fig.update_layout(
        title="Baseline Degrades Over Time",
        xaxis_title="Attacker fraction",
        yaxis_title="All-maker input coverage",
        yaxis={"tickformat": ".0%", "range": [0, 1]},
        xaxis={"tickformat": ".0%"},
        template="plotly_white",
        height=380,
        margin={"l": 55, "r": 25, "t": 56, "b": 55},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0.0},
    )
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


def _individual_mitigations_chart(payload: dict[str, object]) -> str:
    """Show each individual mitigation's effectiveness vs baseline."""
    individual = _as_dict(payload.get("individual_mitigations"))
    series = _as_dict(individual.get("series"))

    configs = [
        ("baseline", "Baseline (no mitigations)", "#c7472d", "solid", 3),
        ("slot_size_3", "Sticky offer slot, size=3", "#5b8fb9", "solid", 2),
        ("slot_size_1", "Sticky offer slot, size=1", "#1f7a8c", "solid", 2),
        ("combined_light", "Combined (light)", "#2d8f6f", "solid", 2),
        ("combined_full", "Combined (full)", "#1a5276", "solid", 3),
    ]

    fig = go.Figure()
    for key, name, color, dash, width in configs:
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
                name=name,
                line={"color": color, "width": width, "dash": dash},
                marker={"size": 7},
            )
        )

    fig.update_layout(
        title=(
            "Individual Countermeasure Effectiveness (8 Makers/CoinJoin, 1000 Rounds)<br>"
            "<sub>Initiation-fee impact is covered separately in the cost section</sub>"
        ),
        xaxis_title="Attacker fraction",
        yaxis_title="All-maker input coverage",
        yaxis={"tickformat": ".0%", "range": [0, 0.85]},
        xaxis={"tickformat": ".0%"},
        template="plotly_white",
        height=440,
        margin={"l": 55, "r": 25, "t": 56, "b": 55},
        legend={"orientation": "v", "yanchor": "top", "y": 0.98, "x": 0.02},
    )
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


def _combined_vs_baseline_chart(payload: dict[str, object]) -> str:
    """Show recommended policy vs baseline across long-run sustained attack."""
    longrun = _as_dict(payload.get("longrun"))
    series = _as_dict(longrun.get("series"))

    fig = go.Figure()
    for key, name, color, fill_rgba in (
        ("baseline", "Baseline (fee=0, no slot)", "#c7472d", "rgba(199,71,45,0.18)"),
        ("recommended", "Recommended (slot+fee=500)", "#1f9366", "rgba(31,147,102,0.18)"),
    ):
        row = _as_dict(series.get(key))
        evil = _as_float_list(row.get("evil_fractions"))
        deanon = _as_float_list(row.get("deanon"))
        deanon_lo = _as_float_list(row.get("deanon_lo"))
        deanon_hi = _as_float_list(row.get("deanon_hi"))
        if not evil or not deanon:
            continue
        # CI band (drawn first so the line sits on top)
        if deanon_lo and deanon_hi and len(deanon_lo) == len(evil) == len(deanon_hi):
            fig.add_trace(
                go.Scatter(
                    x=list(evil) + list(reversed(evil)),
                    y=list(deanon_hi) + list(reversed(deanon_lo)),
                    fill="toself",
                    fillcolor=fill_rgba,
                    line={"color": "rgba(0,0,0,0)"},
                    hoverinfo="skip",
                    showlegend=False,
                    name=f"{name} (95% CI)",
                )
            )
        fig.add_trace(
            go.Scatter(
                x=evil,
                y=deanon,
                mode="lines+markers",
                name=name,
                line={"color": color, "width": 3},
                marker={"size": 9},
            )
        )

    fig.update_layout(
        title="Sustained Attack: Baseline vs Recommended (5000 Rounds, Pre-probed)",
        xaxis_title="Attacker fraction",
        yaxis_title="All-maker input coverage",
        yaxis={"tickformat": ".0%", "range": [0, 1]},
        xaxis={"tickformat": ".0%"},
        template="plotly_white",
        height=380,
        margin={"l": 55, "r": 25, "t": 56, "b": 55},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0.0},
    )
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


def _intensity_chart(payload: dict[str, object]) -> str:
    """Probe intensity: deanon and cost for baseline vs recommended."""
    daily = _as_dict(payload.get("daily_intensity"))
    series = _as_dict(daily.get("series"))

    baseline = _as_dict(series.get("baseline"))
    recommended = _as_dict(series.get("recommended"))

    probes = _as_float_list(baseline.get("probes_per_day"))
    base_deanon = _as_float_list(baseline.get("deanon"))
    base_lo = _as_float_list(baseline.get("deanon_lo"))
    base_hi = _as_float_list(baseline.get("deanon_hi"))
    rec_deanon = _as_float_list(recommended.get("deanon"))
    rec_lo = _as_float_list(recommended.get("deanon_lo"))
    rec_hi = _as_float_list(recommended.get("deanon_hi"))
    cost = _as_float_list(recommended.get("daily_cost_btc"))

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    def _add_band(
        x: list[float],
        lo: list[float],
        hi: list[float],
        fill_rgba: str,
        name: str,
    ) -> None:
        if not (x and lo and hi and len(x) == len(lo) == len(hi)):
            return
        fig.add_trace(
            go.Scatter(
                x=list(x) + list(reversed(x)),
                y=list(hi) + list(reversed(lo)),
                fill="toself",
                fillcolor=fill_rgba,
                line={"color": "rgba(0,0,0,0)"},
                hoverinfo="skip",
                showlegend=False,
                name=name,
            ),
            secondary_y=False,
        )

    _add_band(probes, base_lo, base_hi, "rgba(199,71,45,0.18)", "Baseline 95% CI")

    if probes and base_deanon:
        fig.add_trace(
            go.Scatter(
                x=probes,
                y=base_deanon,
                mode="lines+markers",
                name="Baseline: all-maker input coverage",
                line={"color": "#c7472d", "width": 3},
                marker={"size": 8},
            ),
            secondary_y=False,
        )

    _add_band(probes, rec_lo, rec_hi, "rgba(31,147,102,0.18)", "Recommended 95% CI")

    if probes and rec_deanon:
        fig.add_trace(
            go.Scatter(
                x=probes,
                y=rec_deanon,
                mode="lines+markers",
                name="Recommended: all-maker input coverage",
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
                name="Attacker daily cost (BTC)",
                marker={"color": "#2f557f", "opacity": 0.30},
            ),
            secondary_y=True,
        )

    fig.update_layout(
        title="Probe Intensity: Privacy Impact and Attacker Cost",
        xaxis_title="Probe rounds per day",
        template="plotly_white",
        height=420,
        margin={"l": 55, "r": 55, "t": 56, "b": 50},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0.0},
    )
    fig.update_yaxes(
        title_text="All-maker input coverage",
        tickformat=".0%",
        range=[0, 1],
        secondary_y=False,
    )
    fig.update_yaxes(title_text="Daily attacker cost (BTC)", secondary_y=True)
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


def _recovery_chart(payload: dict[str, object]) -> str:
    """Recovery timeline after attack ends."""
    recovery = _as_dict(payload.get("recovery"))
    series = _as_dict(recovery.get("series"))

    baseline = _as_dict(series.get("baseline"))
    recommended = _as_dict(series.get("recommended"))

    days = _as_float_list(baseline.get("days"))
    base_deanon = _as_float_list(baseline.get("deanon"))
    rec_deanon = _as_float_list(recommended.get("deanon"))
    attack_end = _as_int(baseline.get("attack_end_day"), 0)

    fig = go.Figure()

    if days and base_deanon:
        fig.add_trace(
            go.Scatter(
                x=days,
                y=base_deanon,
                mode="lines",
                name="Baseline",
                line={"color": "#c7472d", "width": 3},
                fill="tozeroy",
                fillcolor="rgba(199, 71, 45, 0.08)",
            )
        )
    if days and rec_deanon:
        fig.add_trace(
            go.Scatter(
                x=days,
                y=rec_deanon,
                mode="lines",
                name="Recommended",
                line={"color": "#1f9366", "width": 3},
            )
        )

    if attack_end > 0:
        fig.add_vline(
            x=attack_end,
            line_dash="dash",
            line_color="#667085",
            annotation_text="Attack ends",
            annotation_position="top right",
        )

    fig.update_layout(
        title="Recovery After a 14-Day Attack (20 Probes/Day)",
        xaxis_title="Day",
        yaxis_title="All-maker input coverage",
        yaxis={"tickformat": ".0%", "range": [0, 1]},
        template="plotly_white",
        height=400,
        margin={"l": 55, "r": 30, "t": 56, "b": 50},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0.0},
    )
    return str(fig.to_html(full_html=False, include_plotlyjs=False))


# -- HTML rendering ----------------------------------------------------------


def _render_html(payload: dict[str, object], data_href: str) -> str:
    findings = _as_dict(payload.get("key_findings"))

    baseline_longrun = _as_float(findings.get("baseline_deanon_evil_04"))
    recommended_longrun = _as_float(findings.get("recommended_deanon_evil_04"))
    baseline_intensity = _as_float(findings.get("baseline_deanon_10_probes"))
    recommended_intensity = _as_float(findings.get("recommended_deanon_10_probes"))
    cost_ten = _as_float(findings.get("daily_cost_10_probes_btc"))

    recovery = _as_dict(payload.get("recovery"))
    rec_series = _as_dict(recovery.get("series"))
    baseline_recovery = _as_dict(rec_series.get("baseline"))
    recovery_day = baseline_recovery.get("recovery_day_deanon_le_5pct")
    recovery_day_text = str(recovery_day) if recovery_day is not None else "n/a"

    # Charts
    baseline_chart = _baseline_vulnerability_chart(payload)
    degradation_chart = _longrun_degradation_chart(payload)
    individual_chart = _individual_mitigations_chart(payload)
    combined_chart = _combined_vs_baseline_chart(payload)
    intensity_chart = _intensity_chart(payload)
    recovery_chart = _recovery_chart(payload)

    generated_at = time.strftime("%Y-%m-%d")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CoinJoin Probing Attack: Analysis and Countermeasures</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono&display=swap');

    :root {{
      --ink: #102231;
      --ink-soft: #3a5568;
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
        radial-gradient(1200px 420px at 95% -10%, rgba(31, 122, 140, 0.15), transparent 55%),
        radial-gradient(1100px 460px at -20% 0%, rgba(31, 147, 102, 0.12), transparent 58%),
        linear-gradient(180deg, #f8fcff 0%, #eef6f8 100%);
      line-height: 1.6;
      font-size: 15.5px;
    }}

    .wrap {{
      max-width: 860px;
      margin: 0 auto;
      padding: 28px 20px 60px;
    }}

    h1, h2, h3 {{
      font-family: 'Fraunces', serif;
      letter-spacing: 0.2px;
    }}

    h1 {{
      font-size: clamp(1.7rem, 4vw, 2.3rem);
      margin: 0 0 8px;
      line-height: 1.2;
    }}

    h2 {{
      font-size: clamp(1.3rem, 3vw, 1.6rem);
      margin: 32px 0 10px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
    }}

    h3 {{
      font-size: 1.1rem;
      margin: 20px 0 6px;
      color: var(--ink);
    }}

    p {{ margin: 0 0 12px; color: var(--ink-soft); }}

    .hero {{
      background: linear-gradient(135deg, #102231 0%, #1a3d52 60%, #1f7a8c 100%);
      color: #f6fcff;
      border-radius: 14px;
      padding: 28px 26px;
      margin-bottom: 8px;
      box-shadow: 0 12px 28px rgba(18, 40, 56, 0.18);
    }}

    .hero p {{ color: rgba(246, 252, 255, 0.92); margin: 0 0 6px; }}
    .hero .subtitle {{ font-size: 1.05rem; margin-bottom: 16px; max-width: 720px; }}
    .hero .meta {{ font-size: 0.88rem; opacity: 0.75; margin-top: 12px; }}

    .kpis {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-top: 18px;
    }}

    .kpi {{
      background: rgba(255, 255, 255, 0.11);
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 10px;
      padding: 10px 12px;
    }}

    .kpi strong {{
      display: block;
      font-size: 1.15rem;
      font-weight: 700;
    }}

    .kpi span {{ font-size: 0.8rem; opacity: 0.85; }}
    .kpi.danger strong {{ color: #ff9b8a; }}
    .kpi.safe strong {{ color: #8eecc0; }}

    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 18px 20px;
      margin-top: 16px;
      box-shadow: 0 3px 12px rgba(16, 34, 49, 0.05);
    }}

    .card p {{ margin: 0 0 10px; }}

    .chart {{
      background: #fff;
      border: 1px solid #e5edf2;
      border-radius: 10px;
      padding: 4px;
      margin-top: 10px;
    }}

    .callout {{
      background: #f0f7f4;
      border-left: 4px solid var(--accent-2);
      border-radius: 0 8px 8px 0;
      padding: 12px 16px;
      margin: 14px 0;
    }}

    .callout p {{ margin: 0; color: var(--ink); }}

    .callout-warn {{
      background: #fdf4f2;
      border-left-color: var(--warn);
    }}

    .param-table {{
      width: 100%;
      border-collapse: collapse;
      margin: 10px 0;
      font-size: 0.92rem;
    }}

    .param-table th {{
      text-align: left;
      padding: 8px 10px;
      background: #f2f7fa;
      border-bottom: 2px solid var(--line);
      font-weight: 600;
    }}

    .param-table td {{
      padding: 7px 10px;
      border-bottom: 1px solid #eaf0f4;
      color: var(--ink-soft);
    }}

    .param-table code {{
      font-family: 'IBM Plex Mono', monospace;
      font-size: 0.88em;
      background: #f0f5f8;
      padding: 1px 5px;
      border-radius: 4px;
    }}

    .effect-table {{
      width: 100%;
      border-collapse: collapse;
      margin: 10px 0;
      font-size: 0.92rem;
    }}

    .effect-table th {{
      text-align: left;
      padding: 8px 10px;
      background: #f2f7fa;
      border-bottom: 2px solid var(--line);
      font-weight: 600;
    }}

    .effect-table td {{
      padding: 7px 10px;
      border-bottom: 1px solid #eaf0f4;
      color: var(--ink-soft);
    }}

    .effect-table .effective {{ color: #1a7a4f; font-weight: 600; }}
    .effect-table .partial {{ color: #b8860b; font-weight: 600; }}
    .effect-table .ineffective {{ color: #c7472d; font-weight: 600; }}

    .step-list {{
      counter-reset: step;
      list-style: none;
      padding: 0;
      margin: 10px 0;
    }}

    .step-list li {{
      counter-increment: step;
      padding: 8px 0 8px 38px;
      position: relative;
      color: var(--ink-soft);
      border-bottom: 1px solid #f0f4f7;
    }}

    .step-list li::before {{
      content: counter(step);
      position: absolute;
      left: 0;
      width: 26px;
      height: 26px;
      line-height: 26px;
      text-align: center;
      background: var(--accent);
      color: #fff;
      border-radius: 50%;
      font-size: 0.82rem;
      font-weight: 600;
    }}

    .footer {{
      margin-top: 32px;
      padding-top: 16px;
      border-top: 1px solid var(--line);
      font-size: 0.88rem;
      color: #6a8090;
    }}

    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    strong {{ color: var(--ink); }}

    @media (max-width: 700px) {{
      .kpis {{ grid-template-columns: repeat(2, 1fr); }}
      .wrap {{ padding: 18px 14px 40px; }}
      .hero {{ padding: 20px 16px; }}
    }}

    @media (max-width: 480px) {{
      .kpis {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main class="wrap">

    <!-- HERO -->
    <section class="hero">
      <h1>CoinJoin Probing Attack</h1>
      <p class="subtitle">
        A practical analysis of how a malicious actor builds a live UTXO
        database of JoinMarket makers by probing, the conditions under which
        this enables maker identification in honest CoinJoins, and the
        countermeasures that limit the leakage.
      </p>
      <div class="kpis">
        <div class="kpi danger">
          <strong>{_pct(baseline_longrun)}</strong>
          <span>Baseline: CJs where all makers are identifiable from inputs (40% attacker share, 5000 rounds)</span>
        </div>
        <div class="kpi safe">
          <strong>{_pct(recommended_longrun)}</strong>
          <span>Recommended policy (same conditions)</span>
        </div>
        <div class="kpi danger">
          <strong>{_pct(baseline_intensity)}</strong>
          <span>Baseline at 10 probes/day</span>
        </div>
        <div class="kpi">
          <strong>{_format_btc(cost_ten)}</strong>
          <span>Attacker cost at 10 probes/day (recommended policy)</span>
        </div>
      </div>
      <div class="meta">
        Simulation results generated {generated_at}
      </div>
    </section>

    <!-- 1. THE PROBLEM -->
    <h2>1. The Problem: Privacy in CoinJoin</h2>

    <p>
      <strong>CoinJoin</strong> is a collaborative Bitcoin transaction where
      multiple participants combine their inputs and outputs into a single
      transaction. When done correctly, an outside observer cannot determine
      which input funded which output, providing <strong>transaction
      privacy</strong>.
    </p>

    <p>
      In JoinMarket-style CoinJoins, there are two roles:
    </p>

    <div class="card">
      <p>
        <strong>Makers</strong> are always-online bots that advertise
        offers on a public orderbook. Each maker has a wallet divided into
        multiple <em>mixdepths</em> (separate accounts, typically 5). They
        hold funds across these mixdepths and earn fees for participating
        in CoinJoins. Makers are selected weighted by their
        <em>fidelity bond</em>, a time-locked Bitcoin deposit that proves
        commitment and makes Sybil attacks expensive.
      </p>
      <p>
        <strong>Takers</strong> initiate CoinJoin transactions. They select
        makers from the orderbook, agree on a CoinJoin amount, and
        coordinate the transaction. The taker's goal is privacy: after
        the CoinJoin, their output should be indistinguishable from
        the makers' outputs.
      </p>
      <p>
        The <strong>anonymity set</strong> of a CoinJoin with
        <em>N</em> makers is <em>N + 1</em>: an observer cannot tell which
        of the N+1 equal-amount outputs belongs to the taker. A CoinJoin
        with 8 makers gives an anonymity set of 9.
      </p>
    </div>

    <p>
      That worst-case anonymity holds against a passive on-chain
      observer. This study isolates a different threat: an
      <strong>active attacker</strong> who participates in the protocol
      itself to build a live database of maker UTXOs. By repeatedly
      probing makers off-chain, the attacker learns which inputs each
      maker will contribute, enabling them to <em>identify</em> makers
      in subsequent honest CoinJoins (matching known UTXOs to transaction
      inputs). Identifying all N makers in a CoinJoin is a necessary
      condition for the taker's equal-output to become isolable by
      exclusion &mdash; but it is not sufficient on its own: that final
      step also requires knowing which equal-output belongs to each
      identified maker, which demands additional on-chain clustering
      (covered in the companion
      <a href="mainnet-deanon.html">JoinMarket equal-output anonymity in
      practice</a> study). For the on-chain side &mdash; equal-output
      reuse, change heuristics, and clustering across many
      transactions &mdash; see that companion study.
    </p>

    <!-- 2. HOW PROBING WORKS -->
    <h2>2. How Probing Works</h2>

    <p>
      A probing attack exploits the CoinJoin negotiation protocol. Before
      a CoinJoin transaction is signed and broadcast, makers must reveal
      their input UTXOs to the taker. Normally this is fine because the
      taker is honest and completes the transaction. But a
      <strong>malicious taker</strong> can abuse this:
    </p>

    <ol class="step-list">
      <li>
        <strong>Probe:</strong> The attacker initiates a CoinJoin with a
        maker, requesting the maker's maximum offer amount. The maker
        reveals all UTXOs in their largest mixdepth. The attacker records
        them and <strong>aborts</strong> the transaction.
      </li>
      <li>
        <strong>Accumulate:</strong> The attacker repeats this with
        many or all makers, building a database of
        <em>known UTXOs per maker</em>.
      </li>
      <li>
        <strong>Identify:</strong> When an honest taker later creates a
        CoinJoin, the attacker observes the on-chain transaction. If any
        input matches a known UTXO from the attacker's database, the
        corresponding maker is <strong>identified</strong>.
      </li>
      <li>
        <strong>Cascade:</strong> When a maker is identified, the attacker
        also learns their <em>co-spent inputs</em> (other UTXOs the maker
        contributed) and their <em>change output</em> (distinguishable by
        value from the equal-amount outputs). These new UTXOs are added to
        the database, enabling identification in future CoinJoins. The
        attacker's knowledge <strong>snowballs</strong>.
      </li>
      <li>
        <strong>Deanonymize:</strong> Probing alone reveals maker inputs
        and change outputs, but does <em>not</em> directly identify which
        equal-amount output belongs to each maker &mdash; all equal outputs
        are the same size. To isolate the taker's equal-output, the
        attacker must additionally map each identified maker to their
        equal-output, for example by tracking the maker's wallet across
        multiple CoinJoins or by applying on-chain clustering (see the
        companion <a href="mainnet-deanon.html">mainnet-deanon study</a>).
        Once all <em>N</em> maker equal-outputs are accounted for, the
        remaining one is the taker's: the anonymity set collapses from
        <em>N + 1</em> to 1.
      </li>
    </ol>

    <div class="callout">
      <p>
        <strong>Scope of this study.</strong> The simulator models only
        the probe-to-cascade leak: how rapidly the attacker's known-UTXO
        database grows as a function of probe rate, attacker share, and
        countermeasures. It does <em>not</em> model on-chain heuristics
        (change attribution, equal-output reuse, address reuse, timing).
        Those compound the leak in practice; the
        <a href="mainnet-deanon.html">mainnet-deanon study</a>
        quantifies them on a real corpus. Read the two studies as
        complementary lower bounds on a real attacker's capability.
      </p>
      <p>
        The anonymity-set metric used here (<em>N + 1 &minus; identified
        makers</em>) is a <strong>worst-case estimate</strong>. It assumes
        that for every identified maker the attacker can also determine
        which equal-amount output is theirs &mdash; an additional step
        that in practice requires on-chain wallet clustering on top of the
        probe database. The metric reported in this study (referred to as
        "all-maker input coverage") is therefore strictly the fraction of
        CoinJoins in which the attacker successfully matches every
        maker's known UTXOs to their inputs. Full taker deanonymization
        additionally requires the on-chain clustering step described in
        the companion <a href="mainnet-deanon.html">mainnet-deanon study</a>.
      </p>
    </div>

    <h3>Existing protections and their limits</h3>
    <p>
      JoinMarket already has a protection against probing:
      <strong>PoDLE</strong> (Proof of Discrete Logarithm Equivalence).
      To initiate a CoinJoin, the taker must commit a real UTXO worth at
      least 20% of the requested amount. Each UTXO can only be used for
      up to 3 PoDLE commitments (to allow for honest failures). Makers
      broadcast used commitments to each other as a blacklist.
    </p>

    <div class="callout callout-warn">
      <p>
        <strong>The problem:</strong> PoDLE works against casual abuse, but
        a well-resourced attacker can probe all makers simultaneously,
        before the blacklist propagates. Each maker is probed for their
        maximum offer amount, revealing as many UTXOs as possible. The
        attacker spends their PoDLE commitment UTXOs, but in return
        they get a complete snapshot of every maker's largest
        mixdepth. This is the attack scenario we simulate.
      </p>
    </div>

    <!-- 3. BASELINE VULNERABILITY -->
    <h2>3. Baseline: How Bad Is It?</h2>

    <p>
      With no additional countermeasures beyond PoDLE, the CoinJoin
      protocol is highly vulnerable to probing. We simulate a network of
      100 makers with realistic wallet structures sampled from the live
      JoinMarket orderbook.
    </p>

      <div class="card">
      <p>
        Even at moderate attacker shares, in a meaningful fraction of honest
        CoinJoins the attacker can identify every maker from their inputs
        within 1,000 rounds. At 20% attacker share (1 in 5 rounds is a
        probe), roughly <strong>32% of CoinJoins have all makers
        identifiable</strong>; at 40% it is about <strong>48%</strong>, and
        at 80% it climbs to <strong>83%</strong>. (This is the necessary
        precondition for taker deanonymization; the sufficient condition
        also requires on-chain clustering.)
      </p>
      <div class="chart">{baseline_chart}</div>
    </div>

    <div class="card">
      <p>
        The picture worsens with sustained probing. Because each
        identification reveals more UTXOs (the cascade effect), the
        attacker's database grows with every honest CoinJoin it
        observes. Over 5,000 rounds of mixed probe + honest traffic,
        the unmitigated baseline reaches roughly <strong>40%
         all-maker input coverage at 10% attacker share</strong>, <strong>57% at
        20%</strong>, and <strong>64% at 40%</strong>. This is the
        long-term steady-state risk an unmitigated network converges
        to. Variance across seeds is shown as a 95% CI band on the
        sustained-attack chart in section 5.
      </p>
      <div class="chart">{degradation_chart}</div>
    </div>

    <!-- 4. COUNTERMEASURES -->
    <h2>4. Countermeasures</h2>

    <p>
      We evaluate two classes of countermeasure. The first targets the
      structural information leak (what each probe reveals); the second
      raises the attacker's economic cost. Neither eliminates the UTXO
      leakage on its own; together they shrink the attack window enough that
      sustained probing stops being economically rational.
    </p>

    <h3>4.1 Timed sticky offer slot (<code>offer_slot_size</code>)</h3>
    <p>
      <strong>What it does:</strong> Each maker pre-selects a random
      subset of <em>N</em> UTXOs from its active mixdepth &mdash; the
      <em>offer slot</em> &mdash; and advertises only those. The advertised
      <code>maxsize</code> equals the slot's total. The slot is sticky:
      it stays fixed for a randomized lifetime drawn uniformly from
      <code>[slot_ttl_min_rounds, slot_ttl_max_rounds]</code>
      (default 4-20 rounds, roughly 1-5 hours at typical traffic).
      Probes do <em>not</em> rotate the slot; only TTL expiry or a
      successful CoinJoin (which spends a slot UTXO) rebuilds it.
    </p>
    <p>
      <strong>Why it helps:</strong> Re-probing the same maker within
      its TTL reveals nothing new &mdash; the attacker pays the initiation
      fee again and learns the same <em>N</em> UTXOs. Across many makers
      the union of disclosed UTXOs grows much more slowly. With
      <em>N</em>=3, each maker exposes at most 3 UTXOs per TTL window
      regardless of how often it is probed, so the attacker's ability to
      pre-build a complete known-UTXO database is sharply
      throttled. Random TTLs prevent the attacker from synchronizing
      probes with rotation events.
    </p>

    <h3>4.2 Initiation fee (<code>initiation_fee_sats</code>)</h3>
    <p>
      <strong>What it does:</strong> Each maker charges a small fee (in
      sats) just to start the CoinJoin negotiation, paid regardless of
      whether the transaction completes. Both honest and malicious takers
      pay it.
    </p>
    <p>
      <strong>Why it helps:</strong> It makes probing expensive. At 500
      sats per maker with 100 makers, each full probe round costs 50,000
      sats; at 10 rounds per day the attacker spends roughly 0.005 BTC/day.
      It is purely economic: on its own it does <strong>not reduce
      information leakage</strong>, but it forces the attacker into a
      cost-vs-coverage trade-off and, combined with the sticky slot,
      makes the per-bit-of-information cost rise sharply.
    </p>

    <h3>Individual effectiveness</h3>
    <p>
      The chart below shows each countermeasure's standalone impact
      against the probing attack, tested at 8 makers per CoinJoin
      over 1,000 rounds:
    </p>

    <div class="card">
      <div class="chart">{individual_chart}</div>

      <table class="effect-table">
        <tr>
          <th>Countermeasure</th>
          <th>Effect on all-maker input coverage (40% evil share)</th>
          <th>Mechanism</th>
        </tr>
        <tr>
          <td>Sticky offer slot, size=1</td>
          <td class="effective">large reduction</td>
          <td>Each maker exposes 1 UTXO per TTL window</td>
        </tr>
        <tr>
          <td>Sticky offer slot, size=3</td>
          <td class="effective">large reduction</td>
          <td>Each maker exposes up to 3 UTXOs per TTL window</td>
        </tr>
        <tr>
          <td>Initiation fee (500 sats)</td>
          <td class="ineffective">modest reduction (cost lever only)</td>
          <td>Raises probing cost but does not reduce per-probe leakage</td>
        </tr>
        <tr>
          <td>Initiation fee (1000 sats)</td>
          <td class="ineffective">modest reduction (cost lever only)</td>
          <td>Raises probing cost but does not reduce per-probe leakage</td>
        </tr>
        <tr>
          <td>Combined (slot=3 + 500 sat fee)</td>
          <td class="effective">large reduction</td>
          <td>Defense in depth: structural cap + economic friction</td>
        </tr>
      </table>
    </div>

    <div class="callout">
      <p>
        The structural lever &mdash; the timed sticky offer slot &mdash; is what
        drives the all-maker input coverage metric down. Initiation fees alone
        barely move the needle, but they raise the per-bit-of-information cost when
        layered on top of a sticky slot. Long-running attacks (Section 5)
        show that combining the structural and economic defenses matters,
        because at very high probe intensity each one alone degrades over
        time.
      </p>
    </div>

    <!-- 5. THE RECOMMENDED POLICY -->
    <h2>5. Recommended Policy</h2>

    <p>
      Rather than relying on any single countermeasure, we combine them
      into a defense-in-depth policy. The recommended configuration is:
    </p>

    <div class="card">
      <table class="param-table">
        <tr>
          <th>Parameter</th>
          <th>Value</th>
          <th>Purpose</th>
        </tr>
        <tr>
          <td><code>offer_slot_size</code></td>
          <td><code>3</code></td>
          <td>Cap UTXOs per offer slot</td>
        </tr>
        <tr>
          <td><code>slot_ttl_min_rounds</code></td>
          <td><code>4</code></td>
          <td>Minimum slot lifetime (sticky)</td>
        </tr>
        <tr>
          <td><code>slot_ttl_max_rounds</code></td>
          <td><code>20</code></td>
          <td>Maximum slot lifetime (random rotation)</td>
        </tr>
        <tr>
          <td><code>initiation_fee_sats</code></td>
          <td><code>500</code></td>
          <td>Economic deterrent</td>
        </tr>
        <tr>
          <td><code>n_makers_per_coinjoin</code></td>
          <td><code>8</code></td>
          <td>Larger anonymity set</td>
        </tr>
        <tr>
          <td><code>n_mixdepths</code></td>
          <td><code>5</code></td>
          <td>Wallet compartmentalization</td>
        </tr>
      </table>
    </div>

    <h3>Sustained attack resistance</h3>
    <p>
      We test the recommended policy under harsh conditions: 5,000 rounds
      with a pre-probed attacker that snapshots every maker's offer slot
      before the first honest CoinJoin. At low to moderate attacker
      shares the recommended policy <strong>roughly halves the
      all-maker input coverage rate</strong> versus the unmitigated baseline; at high
      shares the absolute reduction stays meaningful but shrinks in
      relative terms.
    </p>

    <div class="card">
      <div class="chart">{combined_chart}</div>
      <p>
        At 10% attacker share, baseline ~40% vs recommended ~19%
        all-maker input coverage; at 20% share, ~57% vs ~22%; at 40% share,
        baseline {_pct(baseline_longrun)} vs recommended
        {_pct(recommended_longrun)}; at 60% share, ~85% vs ~60%.
        Beyond about 100 probes/day or attacker shares above ~60%, the
        gap narrows further (see Section 6) &mdash; defence-in-depth slows
        UTXO leakage rather than abolishing it.
      </p>
    </div>

    <!-- 6. ATTACK ECONOMICS -->
    <h2>6. Attack Economics</h2>

    <p>
      Mitigations don't make the attack impossible — they make it more
      expensive per unit of information extracted. The chart below
      tracks final-day all-maker input coverage and attacker cost across probe
      intensities for a 14-day attack window:
    </p>

    <div class="card">
      <div class="chart">{intensity_chart}</div>
      <p>
        The picture is sobering. Against the baseline, a single probe
        round per day already drives final-day all-maker input coverage to
        roughly <strong>{_pct(baseline_intensity)}</strong>, and 50
        probes/day reaches <strong>~96%</strong>. Against the recommended
        policy, the same intensities yield <strong>~53%</strong> and
        <strong>~87%</strong> respectively &mdash; a real reduction, but far
        from a fix. The attacker pays linearly with intensity (about
        500 sats per probe round at the recommended fee, so 50
        probes/day costs ~0.025 BTC/day), and the marginal information
        per probe drops once the slot has been seen, but a patient
        attacker still maps most of the network's UTXOs over a fortnight.
      </p>
    </div>

    <div class="callout callout-warn">
      <p>
        <strong>Initiation fees alone are not enough.</strong> Even at
        1,000 sats per maker, an attacker still achieves all-maker input
        coverage for well over half of CoinJoins at moderate evil shares
        in our 1,000-round runs.
        Fees raise the per-probe cost but do not change what the
        attacker learns. The real defence comes from the timed sticky
        slot, which caps information-per-probe and forces the attacker
        to pay again to learn anything new.
      </p>
    </div>

    <!-- 7. RECOVERY -->
    <h2>7. Recovery After an Attack</h2>

    <p>
      What happens when the attacker stops? As honest CoinJoins continue,
      makers generate new UTXOs the attacker does not know about. The
      attacker's database becomes stale and identification rates drop.
    </p>

    <div class="card">
      <div class="chart">{recovery_chart}</div>
      <p>
        After a 14-day attack at 20 probes/day, the baseline takes
        until approximately day {recovery_day_text} to recover below 5%
        all-maker input coverage, and the recommended policy recovers only
        marginally faster (around 19 days versus 21 in our runs). The
        recommended policy's main benefit is starting recovery from a
        lower peak, not accelerating the recovery itself: rotation of
        the offer slot is what eventually flushes the attacker's
        database, and that proceeds at the same rate either way.
      </p>
    </div>

    <!-- 8. KEY TAKEAWAYS -->
    <h2>8. Key Takeaways</h2>

    <div class="card">
      <ol style="margin: 0; padding-left: 20px; color: var(--ink-soft);">
        <li style="margin-bottom: 8px;">
          <strong>CoinJoin probing is a real privacy threat.</strong>
          Without countermeasures, a moderately resourced attacker
          can identify all makers in a substantial fraction of honest CoinJoins
          through the information cascade triggered by probing. This is
          a necessary precondition for full taker deanonymization (which
          additionally requires on-chain clustering &mdash; see the companion
          <a href="mainnet-deanon.html">mainnet-deanon study</a>). In our
          5,000-round sustained-attack runs, the baseline all-maker input
          coverage climbs to roughly 40% at 10% attacker share and 64% at
          40% attacker share, with an 85%+ tail at 60% attacker share.
        </li>
        <li style="margin-bottom: 8px;">
          <strong>The timed sticky offer slot is the structural lever:</strong>
          capping the offer to <em>N</em> UTXOs and holding it fixed for a
          random TTL (<code>offer_slot_size</code> +
          <code>slot_ttl_*</code>) directly limits how much an attacker can
          learn per maker per time window. Re-probing within the TTL
          yields no new information, breaking the "probe everything every
          day" strategy without requiring per-attempt economic friction.
        </li>
        <li style="margin-bottom: 8px;">
          <strong>Initiation fees are necessary but not sufficient.</strong>
          They raise the attacker's cost but do not stop the information
          leakage. Fees only make sense as a complement to
          information-limiting measures.
        </li>
        <li style="margin-bottom: 8px;">
          <strong>The recommended policy combines two layers.</strong>
          The timed sticky slot caps information leakage per probe (structural
          defence); the initiation fee raises the attacker's per-probe cost
          (economic friction). Under sustained, pre-probed, multi-thousand-round
          attacks this combination roughly halves the all-maker input
          coverage rate compared to the baseline. That is a meaningful
          improvement, but a determined long-running attacker can still
          map a notable fraction of the network's UTXOs &mdash; defence
          in depth is necessary, not optional.
        </li>
        <li>
          <strong>Recovery is slow either way.</strong>
          After a 2-week attack ends, both baseline and recommended
          policies need roughly 19-21 days to recover below 5%
          all-maker input coverage in our runs. The recommended policy's win is
          starting recovery from a lower peak, not converging faster:
          recovery rate is set by how quickly makers naturally rotate
          their slot UTXOs, which is independent of the policy in
          place.
        </li>
      </ol>
    </div>

    <p class="footer">
      Simulation source and raw data:
      <a href="https://github.com/joinmarket-ng/coinjoin-simulator">github.com/joinmarket-ng/coinjoin-simulator</a>
      | Curated dataset: <a href="{data_href}">{data_href}</a>
      | Generated {generated_at}
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
