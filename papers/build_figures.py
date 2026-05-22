"""Publication-quality figures for the JoinMarket maker-clustering paper.

All outputs go to ``papers/figures/``. Each figure follows a
consistent style: muted Tol-bright palette, white background, light
grid, clear titles + units, no spurious chart-junk.

Figures (12 total):

  cluster_volume.svg          maker clusters sorted by total volume
  cjfee_distribution.svg      joint (mean cjfee_r, size, volume) scatter
  cjfee_per_cluster.svg       per-cluster cjfee_r histograms for the
                              8 round-base single-maker clusters
  entity_disambiguation.svg   §6 co-occurrence entity-count bound
  cioh_entity_bound.svg       §6 / §11 CIOH entity-count bound
  role_change_exposure.svg    §9 takers bound to maker clusters
  spending_funnel.svg         §8 per-CJ classification (sankey-style)
  spending_per_cj_dist.svg    §8 distribution of non-CJ-spent eq
                              outputs per CJ (>= 2 = definitely maker)
  round_number_fees.svg       §11 round-base detection
  bond_vs_cj_share.svg        §10 bond share vs CJ-output share
  bond_vs_rate.svg            §10 bond share vs participation rate
                              (time-normalized)
  multi_input_bundles.svg     §11 CIOH multi-input bundle counts per
                              cluster, with multi-cluster wallets
                              highlighted

We use a tight Tol palette to stay accessible.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "tmp"
DATA = ROOT / "data"
OUT = ROOT / "papers" / "figures"
OUT.mkdir(exist_ok=True)

V5_REPORT = DATA / "mainnet_report_v5.json"
DISAMB = TMP / "cluster_disambiguation.json"
DEANON = TMP / "deanon_eq_v2.json"
ROUND_FEES = TMP / "round_number_fees.json"
BOND_CORR = TMP / "bond_cluster_correlation.json"
BOND_RATE = TMP / "bond_vs_rate.json"
FUNNEL = TMP / "spending_funnel.json"
FUNNEL_V2 = TMP / "spending_funnel_v2.json"
CJFEE_PC = TMP / "cjfee_r_per_cluster.json"
CIOH = TMP / "cioh_cluster_validation.json"
MIXDEPTH = TMP / "mixdepth_chains.json"
BUNDLES = TMP / "multi_input_bundles.json"

# Tol bright palette (accessible)
COL = {
    "blue": "#4477AA",
    "cyan": "#66CCEE",
    "green": "#228833",
    "yellow": "#CCBB44",
    "red": "#EE6677",
    "purple": "#AA3377",
    "grey": "#BBBBBB",
    "dark": "#222222",
    "olive": "#999933",
    "teal": "#44AA99",
    "wine": "#882255",
    "indigo": "#332288",
}

plt.rcParams.update(
    {
        "figure.dpi": 100,
        "savefig.dpi": 150,
        "font.size": 10,
        "axes.labelsize": 11,
        "axes.titlesize": 12,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.edgecolor": "#444444",
        "axes.grid": True,
        "grid.color": "#DDDDDD",
        "grid.alpha": 0.6,
        "grid.linewidth": 0.6,
        "font.family": "DejaVu Sans",
        "legend.frameon": False,
        "legend.fontsize": 9,
        "figure.facecolor": "white",
        "savefig.facecolor": "white",
        "savefig.bbox": "tight",
    }
)


def _ax_log_minor(ax) -> None:
    for which in ("x", "y"):
        if getattr(ax, f"get_{which}scale")() == "log":
            getattr(ax, f"{which}axis").set_minor_locator(
                mticker.LogLocator(numticks=20, subs="auto"))


def fig_cluster_volume() -> None:
    d = json.loads(V5_REPORT.read_text())
    cs = sorted(d["clusters"], key=lambda c: -c["total_value_sats"])
    ids = [c["cluster_id"] for c in cs]
    vol = [c["total_value_sats"] / 1e8 for c in cs]
    fees = [c.get("cjfee_r_mean") or 1e-9 for c in cs]
    # color by log10(cjfee_r)
    log_fees = [math.log10(f) for f in fees]
    fmin, fmax = min(log_fees), max(log_fees)
    norm = lambda x: (x - fmin) / max(1e-9, fmax - fmin)
    colors = [plt.cm.viridis(0.15 + 0.7 * norm(lf)) for lf in log_fees]
    fig, ax = plt.subplots(figsize=(8, 3.8))
    ax.bar(range(len(ids)), vol, color=colors, edgecolor="#333333", linewidth=0.4)
    ax.set_yscale("log")
    ax.set_xlabel("v5 cluster (sorted by total BTC volume)")
    ax.set_ylabel("clustered volume (BTC, log)")
    ax.set_title(f"v5 fee-band clusters: {len(ids)} bands, "
                 f"{sum(vol):,.0f} BTC total volume")
    ax.set_xticks(range(0, len(ids), 4))
    ax.set_xticklabels([f"c{ids[i]}" for i in range(0, len(ids), 4)],
                       fontsize=8, rotation=0)
    # colorbar legend
    sm = plt.cm.ScalarMappable(cmap=plt.cm.viridis,
                               norm=plt.Normalize(vmin=fmin, vmax=fmax))
    sm.set_array([])
    cb = fig.colorbar(sm, ax=ax, pad=0.01, aspect=18,
                      label="log10(mean cjfee_r)")
    cb.outline.set_visible(False)
    _ax_log_minor(ax)
    fig.tight_layout()
    fig.savefig(OUT / "cluster_volume.svg")
    plt.close(fig)


def fig_cjfee_distribution() -> None:
    d = json.loads(V5_REPORT.read_text())
    cs = d["clusters"]
    xs = [c.get("cjfee_r_mean") or 1e-9 for c in cs]
    ys = [c["n_outputs"] for c in cs]
    vols = [c["total_value_sats"] / 1e8 for c in cs]
    vmax = max(vols)
    sizes = [40 + 600 * (v / vmax) for v in vols]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    sc = ax.scatter(xs, ys, s=sizes, c=vols, cmap="plasma",
                    alpha=0.85, edgecolor="#222222", linewidth=0.5,
                    norm=plt.matplotlib.colors.LogNorm())
    # annotate top 5 by volume
    for c in sorted(cs, key=lambda c: -c["total_value_sats"])[:5]:
        ax.annotate(f"c{c['cluster_id']}",
                    xy=(c["cjfee_r_mean"], c["n_outputs"]),
                    xytext=(7, 3), textcoords="offset points", fontsize=9,
                    fontweight="bold")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("mean cjfee_r per cluster (log)")
    ax.set_ylabel("change outputs per cluster (log)")
    ax.set_title("v5 cluster fee-band distribution")
    cb = fig.colorbar(sc, ax=ax, pad=0.015, aspect=20, label="BTC volume (log)")
    cb.outline.set_visible(False)
    fig.tight_layout()
    fig.savefig(OUT / "cjfee_distribution.svg")
    plt.close(fig)


def fig_entity_disambiguation() -> None:
    d = json.loads(DISAMB.read_text())
    cs = d["clusters"]
    xs = [c["n_utxos"] for c in cs]
    ys = [c["entity_count_lower_bound"] for c in cs]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.scatter(xs, ys, s=60, alpha=0.85, color=COL["red"],
               edgecolor="#222222", linewidth=0.5)
    for c in sorted(cs, key=lambda c: -c["entity_count_lower_bound"])[:5]:
        ax.annotate(f"c{c['cluster_id']}",
                    xy=(c["n_utxos"], c["entity_count_lower_bound"]),
                    xytext=(7, 4), textcoords="offset points", fontsize=9,
                    fontweight="bold")
    ax.axhline(1, ls=":", color=COL["grey"], lw=1, label="single entity")
    ax.set_xscale("log")
    ax.set_xlabel("change outputs per cluster (log)")
    ax.set_ylabel("entities (co-occurrence lower bound)")
    s = d["summary"]
    ax.set_title(f"co-occurrence disambiguation: "
                 f"48 clusters carry at least {s['entities_lower_bound']} "
                 f"distinct maker entities")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "entity_disambiguation.svg")
    plt.close(fig)


def fig_role_change_exposure() -> None:
    d = json.loads(DEANON.read_text())
    hist = d["summary"]["taker_cluster_exposure_histogram"]
    cids = [h[0] for h in hist]
    counts = [h[1] for h in hist]
    total = sum(counts)
    # use cumulative + bars
    fig, ax = plt.subplots(figsize=(8, 4.0))
    ax.bar(range(len(cids)), counts, color=COL["green"], alpha=0.85,
           edgecolor="#222222", linewidth=0.4)
    cum = []
    s = 0
    for c in counts:
        s += c
        cum.append(100 * s / total)
    ax2 = ax.twinx()
    ax2.plot(range(len(cids)), cum, color=COL["wine"], lw=1.6,
             marker="o", ms=3.5)
    ax2.set_ylim(0, 105)
    ax2.set_ylabel("cumulative share of takers bound (%)",
                   color=COL["wine"])
    ax2.tick_params(axis="y", colors=COL["wine"])
    ax2.spines["right"].set_visible(True)
    ax2.spines["right"].set_color(COL["wine"])
    ax2.grid(False)
    ax.set_yscale("log")
    ax.set_xlabel("v5 cluster (ranked by n_takers_mapped)")
    ax.set_ylabel("takers bound (log)", color=COL["green"])
    ax.tick_params(axis="y", colors=COL["green"])
    ax.set_xticks(range(len(cids)))
    ax.set_xticklabels([f"c{c}" for c in cids], rotation=90, fontsize=7)
    ax.set_title(f"role-change exposure across {len(cids)} maker clusters "
                 f"(top 3 absorb {cum[2]:.0f}%)")
    fig.tight_layout()
    fig.savefig(OUT / "role_change_exposure.svg")
    plt.close(fig)


def fig_spending_funnel() -> None:
    """Sankey-style left-to-right flow diagram:
       sources (attributed / unattributed)
         -> classification (cj / non_cj / unseen)
       with proportional band widths and absolute counts."""
    d = json.loads(FUNNEL_V2.read_text())
    af = d["attributed_funnel"]
    uf = d["unattributed_funnel"]
    cats = ["cj", "non_cj", "unseen"]
    cat_label = {
        "cj": "CoinJoin remix",
        "non_cj": "non-CJ spend",
        "unseen": "no successor\nin JM corpus",
    }
    cat_color = {"cj": COL["teal"], "non_cj": COL["wine"], "unseen": COL["yellow"]}
    a_total = sum(af.values())
    u_total = sum(uf.values())
    grand = a_total + u_total

    fig, ax = plt.subplots(figsize=(10, 5.2))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, grand)
    ax.axis("off")

    # Left column: source bars
    left_x = 0.4
    left_w = 0.7
    a_top = grand
    a_bot = grand - a_total
    u_top = a_bot
    u_bot = 0
    ax.add_patch(plt.Rectangle((left_x, a_bot), left_w, a_total,
                               facecolor=COL["blue"], edgecolor="#222"))
    ax.text(left_x + left_w / 2, (a_top + a_bot) / 2,
            f"attributed makers\n{a_total:,} outputs",
            ha="center", va="center", color="white", fontsize=10,
            fontweight="bold")
    ax.add_patch(plt.Rectangle((left_x, u_bot), left_w, u_total,
                               facecolor=COL["purple"], edgecolor="#222"))
    ax.text(left_x + left_w / 2, (u_top + u_bot) / 2,
            f"unattributed\n(taker + missed makers)\n{u_total:,} outputs",
            ha="center", va="center", color="white", fontsize=10,
            fontweight="bold")

    # Right column: category bars
    right_x = 8.2
    right_w = 0.7
    cat_totals = {c: af.get(c, 0) + uf.get(c, 0) for c in cats}
    y_cursor = grand
    cat_y = {}
    for c in cats:
        h = cat_totals[c]
        cat_y[c] = (y_cursor - h, y_cursor)
        ax.add_patch(plt.Rectangle((right_x, y_cursor - h), right_w, h,
                                   facecolor=cat_color[c], edgecolor="#222",
                                   alpha=0.9))
        ax.text(right_x + right_w + 0.15, y_cursor - h / 2,
                f"{cat_label[c]}\n{cat_totals[c]:,} ({100*cat_totals[c]/grand:.1f}%)",
                ha="left", va="center", fontsize=10)
        y_cursor -= h

    # Flow bands left -> right
    import numpy as np
    a_cursor_top = grand
    u_cursor_top = u_total
    cat_in = {c: cat_y[c][1] for c in cats}  # top y of remaining slot

    def draw_band(x0, y0_top, h, x1, y1_top, color):
        # Smooth horizontal band: filled between two cubics
        n = 50
        xs = np.linspace(x0, x1, n)
        s = (xs - x0) / (x1 - x0)
        # smoothstep
        t = s * s * (3 - 2 * s)
        upper = y0_top + (y1_top - y0_top) * t
        lower = (y0_top - h) + ((y1_top - h) - (y0_top - h)) * t
        ax.fill_between(xs, lower, upper, color=color, alpha=0.45,
                        linewidth=0)

    for c in cats:
        # attributed -> c
        n_a = af.get(c, 0)
        if n_a > 0:
            draw_band(left_x + left_w, a_cursor_top, n_a,
                      right_x, cat_in[c], COL["blue"])
            a_cursor_top -= n_a
            cat_in[c] -= n_a
        # unattributed -> c
        n_u = uf.get(c, 0)
        if n_u > 0:
            draw_band(left_x + left_w, u_cursor_top, n_u,
                      right_x, cat_in[c], COL["purple"])
            u_cursor_top -= n_u
            cat_in[c] -= n_u

    fig.suptitle(
        f"per-output spending classification ({d['n_cjs']:,} CJs analyzed; "
        f"{grand:,} equal outputs)",
        fontsize=12, y=0.98)
    fig.text(0.02, 0.02,
             "Attributed makers always remix (by construction). The asymmetry"
             " on the unattributed side reveals taker-shaped outputs.",
             fontsize=9, color="#444")
    fig.tight_layout()
    fig.savefig(OUT / "spending_funnel.svg")
    plt.close(fig)


def fig_spending_per_cj_dist() -> None:
    """Per-CJ distribution of (non-CJ spends, unspent in corpus). The
    >= 2 region is a 'definitely a maker spent non-CJ' signal."""
    d = json.loads(FUNNEL_V2.read_text())
    dist_noncj = d["dist_noncj_per_cj"]
    dist_unseen = d["dist_unseen_per_cj"]
    # bin both up to 8+
    def collapse(d, cap=8):
        out = {i: 0 for i in range(cap + 1)}
        for k, v in d.items():
            k = int(k)
            out[min(k, cap)] += v
        return out
    a = collapse(dist_noncj)
    b = collapse(dist_unseen)
    keys = list(range(9))
    labels = [str(k) for k in keys[:-1]] + ["8+"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 3.8))
    bars1 = ax1.bar(labels, [a[k] for k in keys], color=COL["red"],
                    alpha=0.85, edgecolor="#222", linewidth=0.4)
    ax1.set_yscale("log")
    ax1.set_xlabel("# of equal outputs spent in NON-CoinJoin tx per CJ")
    ax1.set_ylabel("# CJs (log)")
    ax1.set_title("non-CJ spends per CJ\n>=2 implies a maker left the JM circuit")
    # shade definitely-maker region
    ax1.axvspan(1.5, 8.5, alpha=0.12, color=COL["green"])
    n_definite = sum(a[k] for k in keys if k >= 2)
    ax1.text(4.5, ax1.get_ylim()[1] / 3,
             f"{n_definite} CJs ({100*n_definite/d['n_cjs']:.1f}%)\nat least one maker\nspent non-CJ",
             ha="center", fontsize=9, color=COL["green"])

    bars2 = ax2.bar(labels, [b[k] for k in keys], color=COL["purple"],
                    alpha=0.85, edgecolor="#222", linewidth=0.4)
    ax2.set_yscale("log")
    ax2.set_xlabel("# of equal outputs with no successor in JM corpus per CJ")
    ax2.set_ylabel("# CJs (log)")
    ax2.set_title("unseen successors per CJ\n>=2 = bounded by corpus edge or non-JM tail")
    ax2.axvspan(1.5, 8.5, alpha=0.12, color=COL["green"])
    n_def2 = sum(b[k] for k in keys if k >= 2)
    ax2.text(4.5, ax2.get_ylim()[1] / 3,
             f"{n_def2} CJs ({100*n_def2/d['n_cjs']:.1f}%)",
             ha="center", fontsize=9, color=COL["green"])

    fig.suptitle("per-CJ taker-vs-maker signature distributions", y=1.02)
    fig.tight_layout()
    fig.savefig(OUT / "spending_per_cj_dist.svg")
    plt.close(fig)


def fig_cjfee_per_cluster() -> None:
    """Per-cluster cjfee_r histograms for the 8 round-base single-maker
    clusters: expect flat distribution around r_base in [0.9, 1.1]."""
    d = json.loads(CJFEE_PC.read_text())
    rn_ids = d["round_number_clusters"]
    pc = d["per_cluster"]
    n = len(rn_ids)
    cols = 4
    rows = math.ceil(n / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(11, 2.4 * rows))
    axes_flat = axes.flatten() if rows > 1 else axes
    for i, cid in enumerate(rn_ids):
        ax = axes_flat[i]
        p = pc[str(cid)]
        vals = p["values"]
        if not vals:
            ax.set_visible(False)
            continue
        # normalize by the mean: expect [0.9, 1.1] under f=0.1
        m = sum(vals) / len(vals)
        normed = [v / m for v in vals]
        ax.hist(normed, bins=24, color=COL["green"], alpha=0.85,
                edgecolor="#222", linewidth=0.4)
        ax.axvspan(0.9, 1.1, alpha=0.12, color=COL["yellow"],
                   label="default f=0.1")
        ax.axvline(1.0, color=COL["dark"], ls=":", lw=0.8)
        ax.set_title(f"c{cid}  mean={m:.2e}  n={p['n']}", fontsize=10)
        ax.set_xlabel("cjfee_r / mean", fontsize=9)
        ax.set_ylabel("count", fontsize=9)
        ax.tick_params(labelsize=8)
        if i == 0:
            ax.legend(loc="upper right", fontsize=8)
    for j in range(n, len(axes_flat)):
        axes_flat[j].set_visible(False)
    fig.suptitle(
        f"per-cluster cjfee_r distributions for {n} round-base single-maker clusters\n"
        "(default JM maker: symmetric uniform in [0.9, 1.1] around base)",
        y=1.02,
    )
    fig.tight_layout()
    fig.savefig(OUT / "cjfee_per_cluster.svg")
    plt.close(fig)


def fig_round_number_fees() -> None:
    d = json.loads(ROUND_FEES.read_text())
    rows = [r for r in d["clusters"] if r.get("verdict") != "empty"]
    verdicts = ["round_base", "near_round", "non_round", "multi_entity_or_drifting"]
    color_v = {
        "round_base": COL["green"],
        "near_round": COL["cyan"],
        "non_round": COL["yellow"],
        "multi_entity_or_drifting": COL["red"],
    }
    fig, ax = plt.subplots(figsize=(8, 4.4))
    for v in verdicts:
        sub = [r for r in rows if r["verdict"] == v]
        if not sub:
            continue
        xs = [r["cjfee_r_mean"] for r in sub]
        ys = [r["implied_factor"] * 100 for r in sub]
        sizes = [max(40, min(700, r["n_outputs"] / 40)) for r in sub]
        ax.scatter(xs, ys, s=sizes, alpha=0.8, color=color_v[v],
                   label=f"{v} (n={len(sub)})",
                   edgecolor="#222", linewidth=0.4)
    ax.axhline(10, ls=":", color=COL["dark"], lw=0.9,
               label="default factor 0.1 (10%)")
    ax.axhline(22, ls="--", color=COL["grey"], lw=0.9,
               label="cap 0.22 (multi-entity flag)")
    ax.set_xscale("log")
    ax.set_xlabel("cluster mean cjfee_r (log)")
    ax.set_ylabel("implied factor (max-min)/(max+min)  [%]")
    nr = d["summary"]["by_verdict"]["round_base"]
    ax.set_title(
        f"round-number fee heuristic: {nr}/{len(rows)} clusters resolve to "
        f"a round-base maker running default cjfee_factor")
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "round_number_fees.svg")
    plt.close(fig)


def fig_bond_vs_cj_share() -> None:
    d = json.loads(BOND_CORR.read_text())
    rows = [r for r in d["rows"] if r["bond_sat"] > 0]
    xs = [r["bond_share"] * 100 for r in rows]
    ys = [r["cj_share"] * 100 for r in rows]
    sizes = [max(40, min(700, r["n_outputs"] / 80)) for r in rows]
    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.scatter(xs, ys, s=sizes, alpha=0.8, color=COL["indigo"],
               edgecolor="#222", linewidth=0.5)
    for r in rows:
        ax.annotate(f"c{r['cluster_id']}",
                    xy=(r["bond_share"] * 100, r["cj_share"] * 100),
                    xytext=(5, 3), textcoords="offset points", fontsize=8)
    lo = max(1e-2, min(min(xs), min(ys)))
    hi = max(max(xs), max(ys))
    ax.plot([lo, hi], [lo, hi], ls="--", color=COL["grey"], lw=0.9,
            label="bond_share = cj_share")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("share of total fidelity-bond value (%, live snapshot)")
    ax.set_ylabel("share of v5 maker change outputs (%)")
    s = d["summary"]
    ax.set_title(
        f"bond share vs CJ share per fee band\n"
        f"({s['n_clusters_with_bond_match']}/{s['n_clusters_v5']} clusters matched; "
        f"snapshot {s['snapshot_timestamp'][:10]})")
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(OUT / "bond_vs_cj_share.svg")
    plt.close(fig)


def fig_bond_vs_rate() -> None:
    """Same as bond_vs_cj_share but normalized to outputs per active day."""
    d = json.loads(BOND_RATE.read_text())
    rows = [r for r in d["per_cluster"] if r.get("bond_btc") is not None
            and r["bond_btc"] > 0]
    if not rows:
        return
    xs = [r["bond_btc"] for r in rows]
    ys = [r["outputs_per_day"] for r in rows]
    sizes = [max(40, min(700, r["n_outputs"] / 80)) for r in rows]
    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.scatter(xs, ys, s=sizes, alpha=0.8, color=COL["purple"],
               edgecolor="#222", linewidth=0.5)
    for r in rows:
        ax.annotate(f"c{r['cluster_id']}",
                    xy=(r["bond_btc"], r["outputs_per_day"]),
                    xytext=(5, 3), textcoords="offset points", fontsize=8)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("fidelity bond (BTC, live snapshot, log)")
    ax.set_ylabel("change outputs per active day (log)")
    ax.set_title("time-normalized participation rate vs fidelity-bond size")
    fig.tight_layout()
    fig.savefig(OUT / "bond_vs_rate.svg")
    plt.close(fig)


def fig_multi_input_bundles() -> None:
    d = json.loads(BUNDLES.read_text())
    pc = d["per_cluster"]
    cids = sorted(pc, key=lambda c: -pc[c]["n_bundles"])
    cids = [c for c in cids if pc[c]["n_bundles"] > 0][:30]
    n_bundles = [pc[c]["n_bundles"] for c in cids]
    n_wallets = [pc[c]["n_distinct_wallets"] for c in cids]
    fees = [pc[c]["cjfee_r_mean"] for c in cids]

    fig, ax = plt.subplots(figsize=(9, 4.4))
    x = list(range(len(cids)))
    w = 0.4
    b1 = ax.bar([xi - w / 2 for xi in x], n_bundles, width=w,
                color=COL["blue"], label="multi-input bundles",
                edgecolor="#222", linewidth=0.4)
    b2 = ax.bar([xi + w / 2 for xi in x], n_wallets, width=w,
                color=COL["red"], label="distinct CIOH wallets",
                edgecolor="#222", linewidth=0.4)
    ax.set_yscale("log")
    ax.set_xlabel("v5 cluster (sorted by # bundles)")
    ax.set_ylabel("count (log)")
    ax.set_xticks(x)
    ax.set_xticklabels([f"c{c}" for c in cids], rotation=90, fontsize=8)
    nmc = d.get("top_multi_cluster_wallets", [])
    n_multi = len(nmc)
    ax.set_title(
        f"multi-input maker bundles per cluster ({d['n_cjs']:,} CJs scanned; "
        f"{n_multi} wallets contribute bundles to 2+ clusters)")
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(OUT / "multi_input_bundles.svg")
    plt.close(fig)


def fig_cioh_entity_bound() -> None:
    """For each cluster, plot single-cluster-only CIOH wallet count
    (entity-count lower bound from CIOH) vs n_outputs."""
    bd = json.loads(BUNDLES.read_text())
    pc = bd["per_cluster"]
    # wallets in 2+ clusters (top 50)
    multi = {row["wallet_root"] for row in bd["top_multi_cluster_wallets"]}
    # estimated single-cluster: n_distinct_wallets - multi-wallets-in-this-cluster
    # multi-wallets-in-this-cluster requires bundle_counts inspection.
    multi_per_c = {}
    for row in bd["top_multi_cluster_wallets"]:
        for c in row["clusters"]:
            multi_per_c[c] = multi_per_c.get(c, 0) + 1
    pts = []
    for cid_str, p in pc.items():
        cid = int(cid_str)
        n_single = p["n_distinct_wallets"] - multi_per_c.get(cid, 0)
        if n_single <= 0:
            continue
        pts.append((p["n_outputs"], n_single, cid))
    fig, ax = plt.subplots(figsize=(8, 4.2))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    ax.scatter(xs, ys, s=70, alpha=0.85, color=COL["purple"],
               edgecolor="#222", linewidth=0.5)
    for x, y, cid in sorted(pts, key=lambda p: -p[1])[:6]:
        ax.annotate(f"c{cid}", xy=(x, y), xytext=(6, 3),
                    textcoords="offset points", fontsize=9, fontweight="bold")
    ax.set_xscale("log")
    ax.set_xlabel("change outputs per cluster (log)")
    ax.set_ylabel("single-cluster CIOH wallets (maker-shaped)")
    total = sum(ys)
    ax.set_title(
        f"CIOH entity-count lower bound: at least {total} single-cluster maker wallets "
        f"observed via multi-input bundles")
    fig.tight_layout()
    fig.savefig(OUT / "cioh_entity_bound.svg")
    plt.close(fig)


def fig_mixdepth_chains() -> None:
    d = json.loads(MIXDEPTH.read_text())
    rows = sorted(d["per_cluster"], key=lambda r: -r["n_utxos"])[:25]
    cids = [r["cluster_id"] for r in rows]
    n_same = [r["same"] for r in rows]
    n_diff = [r["different"] for r in rows]
    n_off = [r["non_cj"] + r["unseen"] + r["no_change"] for r in rows]
    totals = [s + d_ + o for s, d_, o in zip(n_same, n_diff, n_off)]
    same_frac = [100 * s / t for s, t in zip(n_same, totals)]
    diff_frac = [100 * d_ / t for d_, t in zip(n_diff, totals)]
    off_frac = [100 * o / t for o, t in zip(n_off, totals)]

    fig, ax = plt.subplots(figsize=(10, 4.4))
    x = list(range(len(cids)))
    ax.bar(x, same_frac, color=COL["green"], label="same band",
           edgecolor="#222", linewidth=0.3)
    ax.bar(x, diff_frac, bottom=same_frac, color=COL["yellow"],
           label="other band (multi-band maker?)",
           edgecolor="#222", linewidth=0.3)
    ax.bar(x, off_frac,
           bottom=[a + b for a, b in zip(same_frac, diff_frac)],
           color=COL["grey"], label="exit / unseen / no change",
           edgecolor="#222", linewidth=0.3)
    ax.set_xticks(x)
    ax.set_xticklabels([f"c{c}" for c in cids], rotation=90, fontsize=8)
    ax.set_ylabel("share of cluster's change outputs (%)")
    ax.set_xlabel("v5 cluster (top 25 by # outputs)")
    g = d["global"]
    tot = sum(g.values())
    ax.set_title(
        f"mixdepth-chain forward classification "
        f"(global: same={100*g['same']/tot:.0f}%, "
        f"other={100*g['different']/tot:.0f}%, "
        f"exit/unseen={100*(g['non_cj']+g['unseen']+g['no_change'])/tot:.0f}%)")
    ax.legend(loc="upper right", ncols=3, fontsize=9)
    ax.set_ylim(0, 105)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(OUT / "mixdepth_chains.svg")
    plt.close(fig)


def main() -> None:
    fig_cluster_volume()
    fig_cjfee_distribution()
    fig_entity_disambiguation()
    fig_role_change_exposure()
    fig_spending_funnel()
    fig_spending_per_cj_dist()
    fig_cjfee_per_cluster()
    fig_round_number_fees()
    fig_bond_vs_cj_share()
    fig_bond_vs_rate()
    fig_multi_input_bundles()
    fig_cioh_entity_bound()
    fig_mixdepth_chains()
    print(f"wrote 13 figures to {OUT}")


if __name__ == "__main__":
    main()
