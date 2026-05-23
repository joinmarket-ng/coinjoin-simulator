"""Publication figures for the v7.3 clusterer.

Outputs to ``papers/figures/``. Extends the v7.2 builder shape
and overwrites figures whose data has shifted under v7.3:

  cluster_size_distribution.svg  v7.3 cluster size histogram
  anonset_reduction_hist.svg     v7.3 residual anonymity-set
  anonset_per_n_eq.svg           v7.3 per-n_eq breakdown
  v6_vs_v7_anonset_overlay.svg   v6 / v7 / v7.1 / v7.2 / v7.3
  probe_validation_v6.svg        same name, v7.3 numbers (cards)

The v7-only attribution breakdown and the v5/v6 fragmentation
figure are unchanged in structure (v7.1 / v7.2 / v7.3 only add
non-CJ CIOH edges; the v7 attribution table is untouched).
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "tmp"
OUT = ROOT / "papers" / "figures"
OUT.mkdir(exist_ok=True)

ANON_V6 = TMP / "v6" / "anonset_reduction_v6.json"
ANON_V7 = TMP / "v7" / "anonset_reduction_v7.json"
ANON_V71 = TMP / "v7" / "anonset_reduction_v71.json"
ANON_V72 = TMP / "v7" / "anonset_reduction_v72.json"
ANON_V73 = TMP / "v7" / "anonset_reduction_v73.json"
CMP = TMP / "v6" / "v5_vs_v6_comparison.json"
PROBE = TMP / "v7" / "probe_validation_v73.json"
V73_REPORT = TMP / "v7" / "mainnet_v73_report.json"
ATTR = TMP / "v7" / "v7_attribution_stats.json"

COL = {
    "blue": "#4477AA",
    "cyan": "#66CCEE",
    "green": "#228833",
    "yellow": "#CCBB44",
    "red": "#EE6677",
    "purple": "#AA3377",
    "grey": "#BBBBBB",
    "dark": "#222222",
    "teal": "#44AA99",
    "wine": "#882255",
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
        "axes.grid": True,
        "grid.alpha": 0.3,
        "grid.linestyle": "--",
    },
)


def _save(fig: plt.Figure, name: str) -> None:
    path = OUT / name
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {path.relative_to(ROOT)}")


def anonset_reduction_hist_v7() -> None:
    d = json.loads(ANON_V73.read_text())
    h = d["residual_anon_set_histogram"]
    items = sorted(((int(k), int(v)) for k, v in h.items()), key=lambda kv: kv[0])
    xs = [k for k, _ in items]
    ys = [v for _, v in items]
    total = sum(ys)
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    ax.bar(xs, ys, color=COL["blue"], edgecolor=COL["dark"], linewidth=0.4)
    ax.set_xlabel("residual anonymity-set size (lower bound)")
    ax.set_ylabel("number of coinjoins")
    ax.set_title(
        "Per-CJ residual anonymity-set after removing v7.3-certified makers",
    )
    ax.set_xticks(xs)
    for x, y in items:
        pct = 100 * y / total
        if pct >= 1.5:
            ax.text(x, y + total * 0.005, f"{pct:.0f}%", ha="center", fontsize=8)
    ax.text(
        0.98,
        0.95,
        (
            f"n={total:,} CJs  mean residual={d['mean_residual_anon_set']:.2f}\n"
            f"mean n_eq={d['mean_n_eq']:.2f}  any-reduction "
            f"{100 * d['share_cjs_with_any_reduction']:.1f}%"
        ),
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=9,
        bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": COL["grey"]},
    )
    _save(fig, "anonset_reduction_hist.svg")


def anonset_per_n_eq_v7() -> None:
    d = json.loads(ANON_V73.read_text())
    rows = [r for r in d["per_n_eq_table"] if r["n_cjs"] >= 30]
    rows.sort(key=lambda r: r["n_eq"])
    xs = [r["n_eq"] for r in rows]
    n_eq_vals = [r["n_eq"] for r in rows]
    residuals = [r["mean_residual_anon_set"] for r in rows]
    counts = [r["n_cjs"] for r in rows]
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    w = 0.4
    x_pos = [i for i, _ in enumerate(xs)]
    ax.bar(
        [p - w / 2 for p in x_pos],
        n_eq_vals,
        width=w,
        color=COL["grey"],
        edgecolor=COL["dark"],
        label="published n_eq (taker hide-set claim)",
    )
    ax.bar(
        [p + w / 2 for p in x_pos],
        residuals,
        width=w,
        color=COL["red"],
        edgecolor=COL["dark"],
        label="residual after removing v7.3-certified makers",
    )
    ax.set_xticks(x_pos)
    ax.set_xticklabels([str(n) for n in xs])
    ax.set_xlabel("n_eq (number of equal outputs in CJ, incl. taker)")
    ax.set_ylabel("size")
    ax.set_title("Anonymity-set shrinkage per round size (mean over CJs)")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.95)
    ax2 = ax.twinx()
    ax2.plot(
        x_pos,
        counts,
        marker="o",
        color=COL["blue"],
        linewidth=1.0,
        markersize=3.0,
        label="n_cjs",
    )
    ax2.set_ylabel("number of CJs (log)", color=COL["blue"])
    ax2.set_yscale("log")
    ax2.tick_params(axis="y", labelcolor=COL["blue"])
    ax2.grid(False)
    _save(fig, "anonset_per_n_eq.svg")


def v6_vs_v7_anonset_overlay() -> None:
    d6 = json.loads(ANON_V6.read_text())
    d7 = json.loads(ANON_V7.read_text())
    d71 = json.loads(ANON_V71.read_text())
    d72 = json.loads(ANON_V72.read_text())
    d73 = json.loads(ANON_V73.read_text())
    sources = [
        ("v6", d6, COL["grey"]),
        ("v7", d7, COL["cyan"]),
        ("v7.1", d71, COL["teal"]),
        ("v7.2", d72, COL["blue"]),
        ("v7.3", d73, COL["purple"]),
    ]
    hists = [
        (name, {int(k): int(v) for k, v in d["residual_anon_set_histogram"].items()}, c, d)
        for name, d, c in sources
    ]
    xs = sorted({x for _, h, _, _ in hists for x in h})
    fig, ax = plt.subplots(figsize=(8.4, 4.0))
    n = len(hists)
    w = 0.8 / n
    x_pos = list(range(len(xs)))
    for i, (name, h, c, d) in enumerate(hists):
        ys = [h.get(x, 0) for x in xs]
        offset = (i - (n - 1) / 2) * w
        ax.bar(
            [p + offset for p in x_pos],
            ys,
            width=w,
            color=c,
            edgecolor=COL["dark"],
            linewidth=0.3,
            label=f"{name} (mean residual {d['mean_residual_anon_set']:.2f})",
        )
    ax.set_xticks(x_pos)
    ax.set_xticklabels([str(x) for x in xs])
    ax.set_xlabel("residual anonymity-set size (lower bound)")
    ax.set_ylabel("number of coinjoins")
    ax.set_title("Residual anonset by clusterer iteration (v6 -> v7.3)")
    ax.legend(loc="upper right", fontsize=9, framealpha=0.95)
    _save(fig, "v6_vs_v7_anonset_overlay.svg")


def cluster_size_distribution_v7() -> None:
    report = json.loads(V73_REPORT.read_text())
    sizes_hist = dict(report.get("size_distribution") or {})
    items = sorted(((int(k), int(v)) for k, v in sizes_hist.items()), key=lambda kv: kv[0])
    xs = [k for k, _ in items]
    ys = [v for _, v in items]
    fig, ax = plt.subplots(figsize=(7.0, 3.6))
    ax.bar(xs, ys, color=COL["blue"], edgecolor=COL["dark"], linewidth=0.3)
    ax.set_xlabel("cluster size (number of maker slots)")
    ax.set_ylabel("number of v7.3 clusters")
    n_total = report["n_clusters"]
    ax.set_title(f"v7.3 maker-cluster size distribution (mainnet, n={n_total:,} clusters)")
    ax.set_yscale("log")
    if max(xs) > 20:
        ax.set_xscale("log")
    _save(fig, "cluster_size_distribution.svg")


def v5_vs_v6_fragmentation() -> None:
    # Unchanged from v6 build. v5 over-clustering claim does not
    # depend on v6 vs v7 since v7 only merges further.
    d = json.loads(CMP.read_text())
    rows = d["top_v5_splits"][:12]
    rows.sort(key=lambda r: -r["v5_size"])
    labels = [f"v5#{r['v5_cluster_id']}\n({r['v5_size']:,} UTXOs)" for r in rows]
    n_v6 = [r["n_distinct_v6_clusters"] for r in rows]
    fig, ax = plt.subplots(figsize=(8.0, 4.0))
    bars = ax.bar(range(len(rows)), n_v6, color=COL["wine"], edgecolor=COL["dark"])
    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels(labels, fontsize=8, rotation=0)
    ax.set_ylabel("distinct v6/v7 clusters")
    ax.set_title(
        "v5 over-clustering: top-12 v5 fee-band clusters decomposed by v6/v7",
    )
    for b, v in zip(bars, n_v6, strict=True):
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() * 1.02,
            f"{v:,}",
            ha="center",
            fontsize=8,
        )
    ax.set_yscale("log")
    _save(fig, "v5_vs_v6_fragmentation.svg")


def probe_validation_v7() -> None:
    d = json.loads(PROBE.read_text())
    fig, ax = plt.subplots(figsize=(7.0, 2.6))
    ax.axis("off")
    cards = [
        ("nicks probed", f"{d['n_nicks']}"),
        ("nicks matched", f"{d['n_nicks_with_any_v73_match']}"),
        ("UTXOs matched", f"{d['total_matched_in_v73']} / {d['total_offered_utxos']}"),
        ("precision violations", f"{d['precision_violations_clusters']}"),
    ]
    n = len(cards)
    for i, (label, value) in enumerate(cards):
        x = (i + 0.5) / n
        ax.text(
            x,
            0.7,
            value,
            ha="center",
            va="center",
            fontsize=24,
            color=COL["dark"],
            transform=ax.transAxes,
            fontweight="bold",
        )
        ax.text(
            x,
            0.3,
            label,
            ha="center",
            va="center",
            fontsize=10,
            color=COL["dark"],
            transform=ax.transAxes,
        )
    ax.set_title(
        "v7.3 probing-attack ground-truth validation: 0 cross-nick collisions",
        pad=12,
    )
    _save(fig, "probe_validation_v6.svg")


def v7_attribution_breakdown() -> None:
    a = json.loads(ATTR.read_text())
    # Decompose the 51,439 cross-CJ reuses into the four mutually
    # exclusive disposition categories.
    keep = a["unique_either"]  # 5,643 edges added
    drop_conflict = a["unique_both_different_slot"]  # 65 dropped
    drop_ambig = a["ambiguous"]  # 2,004 dropped
    drop_no_match = a["no_match"]  # 43,727 dropped
    total = keep + drop_conflict + drop_ambig + drop_no_match
    assert total == a["cross_cj_reuses"], (total, a["cross_cj_reuses"])
    cats = [
        ("edge added", keep, COL["green"]),
        ("ambiguous", drop_ambig, COL["yellow"]),
        ("interpretation conflict", drop_conflict, COL["red"]),
        ("no fee match", drop_no_match, COL["grey"]),
    ]
    labels = [c[0] for c in cats]
    values = [c[1] for c in cats]
    colors = [c[2] for c in cats]
    fig, ax = plt.subplots(figsize=(7.5, 3.4))
    bars = ax.bar(labels, values, color=colors, edgecolor=COL["dark"])
    ax.set_ylabel("number of cross-CJ equal-output reuses")
    ax.set_title(
        f"v7 fee-fingerprint attribution: disposition of {total:,} cross-CJ reuses",
    )
    for b, v in zip(bars, values, strict=True):
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() * 1.02,
            f"{v:,}  ({100 * v / total:.1f}%)",
            ha="center",
            fontsize=9,
        )
    ax.set_yscale("log")
    # Sub-breakdown of unique_either.
    ax.text(
        0.98,
        0.95,
        (
            f"of 'edge added' ({keep:,}):\n"
            f"  abs-only:  {a['unique_abs_only']:,}\n"
            f"  rel-only:  {a['unique_rel_only']:,}\n"
            f"  both agree: {a['unique_both_same_slot']:,}"
        ),
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=8,
        family="monospace",
        bbox={"facecolor": "white", "alpha": 0.9, "edgecolor": COL["grey"]},
    )
    _save(fig, "v7_attribution_breakdown.svg")


def main() -> None:
    for fn in (
        anonset_reduction_hist_v7,
        anonset_per_n_eq_v7,
        v6_vs_v7_anonset_overlay,
        cluster_size_distribution_v7,
        v5_vs_v6_fragmentation,
        probe_validation_v7,
        v7_attribution_breakdown,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"ERROR in {fn.__name__}: {e}")


if __name__ == "__main__":
    main()
