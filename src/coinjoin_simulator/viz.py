"""Visualization module for CoinJoin simulation results.

Produces Plotly figures for analyzing privacy metrics across scenarios.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import plotly.graph_objects as go
from plotly.subplots import make_subplots

if TYPE_CHECKING:
    from .models import AnonymityMetrics, SurveillanceResult, SybilAttackResult


def plot_anonymity_comparison(
    scenario_metrics: dict[str, dict[str, AnonymityMetrics]],
    title: str = "Anonymity Set Comparison Across Scenarios",
) -> go.Figure:
    """Compare anonymity sets across different scenarios.

    Args:
        scenario_metrics: {scenario_name: {outpoint: metrics}}
        title: Plot title.
    """
    fig = go.Figure()

    for scenario_name, metrics in scenario_metrics.items():
        [m.naive_anon_set for m in metrics.values()]
        effective_sets = [m.effective_anon_set for m in metrics.values()]
        [m.entropy_bits for m in metrics.values()]

        fig.add_trace(
            go.Box(
                y=effective_sets,
                name=scenario_name,
                boxmean=True,
            )
        )

    fig.update_layout(
        title=title,
        yaxis_title="Effective Anonymity Set",
        showlegend=True,
        template="plotly_white",
    )
    return fig


def plot_sybil_resistance(
    results: list[SybilAttackResult],
    title: str = "Sybil Attack Resistance vs Counterparty Count",
) -> go.Figure:
    """Plot sybil resistance metrics vs number of counterparties."""
    fig = make_subplots(
        rows=2,
        cols=1,
        subplot_titles=("Required Burned BTC (95% success)", "Required Locked BTC (6 months)"),
        vertical_spacing=0.15,
    )

    counterparties = [r.n_counterparties for r in results]
    burned = [r.required_burned_btc for r in results]
    locked = [r.required_locked_btc_6mo for r in results]

    fig.add_trace(
        go.Scatter(
            x=counterparties,
            y=burned,
            mode="lines+markers",
            name="Burned BTC",
            line={"color": "red"},
        ),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=counterparties,
            y=locked,
            mode="lines+markers",
            name="Locked BTC (6mo)",
            line={"color": "blue"},
        ),
        row=2,
        col=1,
    )

    fig.update_layout(
        title=title,
        template="plotly_white",
        height=700,
    )
    fig.update_xaxes(title_text="Number of Counterparties", row=2, col=1)
    fig.update_yaxes(title_text="BTC", row=1, col=1)
    fig.update_yaxes(title_text="BTC", row=2, col=1)

    return fig


def plot_sybil_enemies_within(
    results: list[SybilAttackResult],
    title: str = "Sybil Attack Success (Enemies Within)",
) -> go.Figure:
    """Plot success probability when top-N makers are actually sybils."""
    fig = go.Figure()

    counterparties = [r.n_counterparties for r in results]
    probs = [r.entity_success_rates.get("enemies_within", 0.0) for r in results]

    fig.add_trace(
        go.Scatter(
            x=counterparties,
            y=[p * 100 for p in probs],
            mode="lines+markers",
            name="Success probability (%)",
            line={"color": "orange"},
        )
    )

    fig.add_hline(y=50, line_dash="dash", line_color="gray", annotation_text="50%")
    fig.add_hline(y=5, line_dash="dash", line_color="green", annotation_text="5%")

    fig.update_layout(
        title=title,
        xaxis_title="Number of Counterparties (= Number of Sybil Bots)",
        yaxis_title="Success Probability (%)",
        template="plotly_white",
    )
    return fig


def plot_surveillance_impact(
    results_no_mitigation: SurveillanceResult,
    results_with_mitigation: SurveillanceResult | None = None,
    title: str = "Surveillance Impact on Anonymity",
) -> go.Figure:
    """Plot the impact of surveillance on anonymity sets."""
    fig = go.Figure()

    categories = [
        "Makers Clustered",
        "UTXOs Clustered",
        "CJs with Reduced Anon",
        "Avg Anon Reduction (%)",
    ]

    no_mit_values = [
        results_no_mitigation.n_makers_clustered,
        results_no_mitigation.n_utxos_clustered,
        results_no_mitigation.coinjoins_deanonymized,
        results_no_mitigation.avg_anon_set_reduction * 100,
    ]

    fig.add_trace(
        go.Bar(
            x=categories,
            y=no_mit_values,
            name="No Mitigation",
            marker_color="red",
        )
    )

    if results_with_mitigation is not None:
        mit_values = [
            results_with_mitigation.n_makers_clustered,
            results_with_mitigation.mitigated_utxos_clustered,
            results_with_mitigation.coinjoins_deanonymized,
            results_with_mitigation.mitigated_anon_set_reduction * 100,
        ]
        fig.add_trace(
            go.Bar(
                x=categories,
                y=mit_values,
                name="With Mitigations",
                marker_color="green",
            )
        )

    fig.update_layout(
        title=title,
        barmode="group",
        template="plotly_white",
    )
    return fig


def plot_role_identification(
    results: dict[str, dict[str, float]],
    title: str = "Taker Identification Accuracy Across Scenarios",
) -> go.Figure:
    """Plot role identification accuracy for different scenarios."""
    fig = make_subplots(
        rows=2,
        cols=1,
        subplot_titles=(
            "Taker Correctly Identified (Rank 1)",
            "Role Entropy (higher = more private)",
        ),
        vertical_spacing=0.15,
    )

    scenarios = list(results.keys())
    rank1_fracs = [r.get("taker_identified_rank1_frac", 0) for r in results.values()]
    entropies = [r.get("avg_role_entropy_bits", 0) for r in results.values()]

    fig.add_trace(
        go.Bar(
            x=scenarios,
            y=[f * 100 for f in rank1_fracs],
            name="Rank 1 (%)",
            marker_color="red",
        ),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Bar(
            x=scenarios,
            y=entropies,
            name="Entropy (bits)",
            marker_color="blue",
        ),
        row=2,
        col=1,
    )

    fig.update_layout(
        title=title,
        template="plotly_white",
        height=700,
        showlegend=False,
    )
    fig.update_yaxes(title_text="Identification Rate (%)", row=1, col=1)
    fig.update_yaxes(title_text="Shannon Entropy (bits)", row=2, col=1)

    return fig


def plot_entropy_distribution(
    entropies_by_scenario: dict[str, list[float]],
    title: str = "Entropy Distribution by Scenario",
) -> go.Figure:
    """Plot distribution of entropy values across scenarios."""
    fig = go.Figure()

    for name, entropies in entropies_by_scenario.items():
        fig.add_trace(
            go.Histogram(
                x=entropies,
                name=name,
                opacity=0.6,
                nbinsx=30,
            )
        )

    fig.update_layout(
        title=title,
        xaxis_title="Shannon Entropy (bits)",
        yaxis_title="Count",
        barmode="overlay",
        template="plotly_white",
    )
    return fig
