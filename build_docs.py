"""Build the multi-page docs site.

Pages produced under ``docs/``:

* ``index.html`` — landing hub linking to both studies.
* ``probing-attack.html`` — the previous single-page report
  (preserved verbatim from the prior ``docs/index.html``).
* ``mainnet-deanon.html`` — the JoinMarket equal-output anonymity
  study, rendered from ``papers/maker-clustering.md`` plus live KPIs
  pulled from ``data/mainnet_report_v5.json``,
  ``tmp/mainnet_clusters_v5/`` and ``tmp/deanon_eq_stats.json``.

Run with ``python build_docs.py``. Idempotent: rewrites only the
three target files. Distinct from the older
``coinjoin_simulator.publish_site`` module, which generates a
single-page report for the probing-attack study.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
PAPERS = ROOT / "papers"
TMP = ROOT / "tmp"
DATA = ROOT / "data"

INDEX = DOCS / "index.html"
PROBING_PAGE = DOCS / "probing-attack.html"
DEANON_PAGE = DOCS / "mainnet-deanon.html"
PAPER_MD = PAPERS / "maker-clustering.md"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    s = s.lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or "section"


def _md_to_html(md: str) -> str:
    """Markdown -> HTML for the paper.

    Supported:

    * ATX headings (``# ... ######``) with auto-generated anchors.
    * Fenced code blocks (``` ``` ```), inline ``code``.
    * Blockquotes (``>``).
    * Tables (``| ... |`` with ``|---|`` separator row).
    * Unordered (``- ``) and ordered (``1. ``) lists with indented
      continuation lines (lazy continuation: any line indented past
      the marker gets folded into the previous item).
    * Display math (``$$...$$``) emitted as a block-level
      ``<div class="math display">`` (not wrapped in a paragraph,
      so MathJax centers it cleanly).
    * Inline math (``$...$``).
    * Bold (``**``) and italic (``*``) emphasis.
    * Links ``[text](url)``.

    The previous implementation broke list items as soon as a
    continuation line wrapped to the next text line and emitted
    display math inside ``<p>`` tags. Both issues are fixed here:
    list parsing now consumes lazy continuation lines (indented or
    plain text following a marker), and display math is detected
    *before* paragraph gathering so it can be a block element.
    """
    out: list[str] = []
    i = 0
    lines = md.splitlines()

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def render_inline(s: str) -> str:
        s = esc(s)
        # Protect display math from the inline math pass
        display_slots: list[str] = []

        def _stash_display(m: re.Match[str]) -> str:
            display_slots.append(m.group(1))
            return f"\x00DISPLAY{len(display_slots) - 1}\x00"

        s = re.sub(r"\$\$(.+?)\$\$", _stash_display, s, flags=re.S)
        # inline math
        s = re.sub(
            r"\$([^$\n]+?)\$",
            lambda m: rf'<span class="math inline">${m.group(1)}$</span>',
            s,
        )
        # restore display math (only used by inline-display fallback)
        s = re.sub(
            r"\x00DISPLAY(\d+)\x00",
            lambda m: f'<span class="math display">$${display_slots[int(m.group(1))]}$$</span>',
            s,
        )
        # inline code
        s = re.sub(r"`([^`]+?)`", r"<code>\1</code>", s)
        # bold then italic
        s = re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", s)
        # images ![alt](url) -- must run before the plain-link pass
        # so the leading "!" is not left dangling.
        s = re.sub(
            r"!\[([^\]]*)\]\(([^)]+)\)",
            r'<img src="\2" alt="\1" loading="lazy">',
            s,
        )
        # links [text](url)
        s = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            r'<a href="\2">\1</a>',
            s,
        )
        return s

    def _is_block_boundary(text: str) -> bool:
        """True for lines that start a new top-level block."""
        if not text or not text.strip():
            return True
        if text.startswith("#") or text.startswith("```") or text.startswith(">"):
            return True
        if text.lstrip().startswith("|"):
            return True
        if text.lstrip().startswith("$$"):
            return True
        if re.match(r"^\s*-\s+", text):
            return True
        return bool(re.match(r"^\s*\d+\.\s+", text))

    def _consume_list(start_idx: int, *, ordered: bool) -> tuple[int, str]:
        """Greedy list consumer that folds indented and lazy
        continuation lines into the previous item, and recognizes
        nested lists of the same kind at deeper indent.

        Returns (new_idx, html).
        """
        marker_re = re.compile(r"^(?P<indent>\s*)(?:\d+\.|-)\s+(?P<body>.*)$")
        base_indent: int | None = None
        # We treat a "list" as a sequence of items at the same
        # indent, possibly with nested same-style sub-lists.
        # Each item is a list of fragments. A fragment is either:
        #   ("text", raw_text)  - raw markdown text to be rendered
        #                         AFTER all lazy continuations are
        #                         concatenated, so that inline spans
        #                         (``**bold**``, ``$...$``) can wrap.
        #   ("html", html)      - already-rendered nested list HTML.
        items: list[list[tuple[str, str]]] = []
        j = start_idx
        while j < len(lines):
            ln = lines[j]
            m = marker_re.match(ln)
            indent = len(ln) - len(ln.lstrip())
            if m and (
                (ordered and re.match(r"^\s*\d+\.\s+", ln))
                or (not ordered and re.match(r"^\s*-\s+", ln))
            ):
                if base_indent is None:
                    base_indent = indent
                if indent < base_indent:
                    break
                if indent == base_indent:
                    items.append([("text", m.group("body"))])
                    j += 1
                    continue
                # Deeper indent of the same style => nested list of
                # the same kind. Recurse.
                sub_idx, sub_html = _consume_list(j, ordered=ordered)
                if items:
                    items[-1].append(("html", sub_html))
                j = sub_idx
                continue
            # Opposite-style nested list at deeper indent? Handle both.
            other_marker = re.match(r"^\s+(?:-|\d+\.)\s+", ln)
            if other_marker and base_indent is not None and indent > base_indent and items:
                nested_ordered = re.match(r"^\s+\d+\.\s+", ln) is not None
                sub_idx, sub_html = _consume_list(j, ordered=nested_ordered)
                items[-1].append(("html", sub_html))
                j = sub_idx
                continue
            # Lazy continuation: a non-empty line that's not a new
            # block boundary at the base indent. Fold into the
            # previous item.
            if (
                items
                and ln.strip() != ""
                and not _is_block_boundary(ln.lstrip())
                and indent >= (base_indent or 0)
            ):
                items[-1].append(("text", ln.strip()))
                j += 1
                continue
            # Blank line: a single blank is allowed *inside* a list
            # if the next line continues at the right indent.
            if ln.strip() == "":
                if j + 1 < len(lines):
                    nxt = lines[j + 1]
                    nxt_indent = len(nxt) - len(nxt.lstrip())
                    nxt_m = re.match(r"^\s*(?:-|\d+\.)\s+", nxt)
                    if nxt_m and nxt_indent == (base_indent or 0):
                        j += 1
                        continue
                    if (
                        nxt.strip()
                        and nxt_indent > (base_indent or 0)
                        and not _is_block_boundary(nxt.lstrip())
                    ):
                        j += 1
                        continue
                break
            break
        tag = "ol" if ordered else "ul"
        parts = [f"<{tag}>"]
        for it in items:
            # Concatenate consecutive text fragments first so inline
            # spans (bold/italic/math) can wrap across continuation
            # lines, then render them as a single inline string.
            inner_parts: list[str] = []
            text_buf: list[str] = []
            for kind, val in it:
                if kind == "html":
                    if text_buf:
                        inner_parts.append(render_inline(" ".join(text_buf)))
                        text_buf = []
                    inner_parts.append(val)
                else:
                    text_buf.append(val)
            if text_buf:
                inner_parts.append(render_inline(" ".join(text_buf)))
            parts.append("<li>" + "".join(inner_parts) + "</li>")
        parts.append(f"</{tag}>")
        return j, "".join(parts)

    while i < len(lines):
        line = lines[i]

        # Headings
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            text = m.group(2)
            slug = _slugify(re.sub(r"^[\d.§\s]+", "", text))
            out.append(f'<h{lvl} id="{slug}">{render_inline(text)}</h{lvl}>')
            i += 1
            continue

        # Fenced code
        if line.startswith("```"):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(esc(lines[i]))
                i += 1
            i += 1  # skip closing fence
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            continue

        # Blockquote
        if line.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").lstrip())
                i += 1
            out.append("<blockquote><p>" + render_inline(" ".join(buf)) + "</p></blockquote>")
            continue

        # Table
        if line.lstrip().startswith("|") and (
            i + 1 < len(lines) and re.match(r"^\s*\|\s*[:\-| ]+\|\s*$", lines[i + 1])
        ):
            header_cells = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            html = ["<table><thead><tr>"]
            html += [f"<th>{render_inline(c)}</th>" for c in header_cells]
            html.append("</tr></thead><tbody>")
            for r in rows:
                html.append("<tr>")
                html += [f"<td>{render_inline(c)}</td>" for c in r]
                html.append("</tr>")
            html.append("</tbody></table>")
            out.append("".join(html))
            continue

        # Display math as a block (a line starting with $$, possibly
        # closing on the same line or on a later line). Emit it as a
        # block-level <div> so MathJax centers it.
        if line.lstrip().startswith("$$"):
            stripped = line.strip()
            if stripped.endswith("$$") and len(stripped) > 4:
                # one-line $$ ... $$
                expr = stripped[2:-2].strip()
                out.append(f'<div class="math display">$$ {expr} $$</div>')
                i += 1
                continue
            buf2 = [stripped.lstrip("$").strip()]
            i += 1
            while i < len(lines) and not lines[i].strip().endswith("$$"):
                buf2.append(lines[i])
                i += 1
            if i < len(lines):
                tail = lines[i].rstrip().rstrip("$").rstrip()
                if tail:
                    buf2.append(tail)
                i += 1
            expr = "\n".join(b for b in buf2 if b is not None)
            out.append(f'<div class="math display">$$ {expr} $$</div>')
            continue

        # Unordered list
        if re.match(r"^\s*-\s+", line):
            i, html = _consume_list(i, ordered=False)
            out.append(html)
            continue

        # Ordered list
        if re.match(r"^\s*\d+\.\s+", line):
            i, html = _consume_list(i, ordered=True)
            out.append(html)
            continue

        if line.strip() == "":
            i += 1
            continue

        # Paragraph (gather until blank line or block boundary)
        buf = [line.rstrip()]
        i += 1
        while i < len(lines) and lines[i].strip() != "" and not _is_block_boundary(lines[i]):
            buf.append(lines[i].rstrip())
            i += 1
        out.append("<p>" + render_inline(" ".join(buf)) + "</p>")

    return "\n".join(out)


def _load_kpis() -> dict[str, Any]:
    """Pull live numbers from the v7 reports (best-effort).

    Headline framing: maker-wallet clustering + taker anonymity-set
    reduction under the v7 chain-edge clusterer (change-chain plus
    fee-fingerprint equal-chain, precision = 1.0 by construction,
    validated by simulator ARI, within-CJ sybil-dedup, and probing
    ground truth).
    """
    kpis: dict[str, Any] = {
        "n_corpus_txs": None,
        "n_decoded_txs": None,
        "n_ilp_failed": None,
        "n_slots": None,
        "n_clusters": None,
        "n_singletons": None,
        "n_nontrivial": None,
        "largest_cluster": None,
        "n_same_cj_collisions": None,
        "n_analysed": None,
        "mean_n_eq": None,
        "mean_certified": None,
        "mean_residual": None,
        "share_any_reduction": None,
        "share_all_certified": None,
        "median_residual": None,
        "probe_nicks_total": None,
        "probe_nicks_matched": None,
        "probe_cross_nick_collisions": None,
    }
    # v7 cluster report
    v7_report_p = TMP / "v7" / "mainnet_v7_report.json"
    if v7_report_p.exists():
        s = json.loads(v7_report_p.read_text())
        kpis["n_decoded_txs"] = s.get("n_ok_records")
        kpis["n_ilp_failed"] = (
            (s.get("n_records") or 0) - (s.get("n_ok_records") or 0)
        ) or None
        kpis["n_corpus_txs"] = s.get("n_records")
        kpis["n_slots"] = s.get("n_maker_slots")
        kpis["n_clusters"] = s.get("n_clusters")
        kpis["n_singletons"] = s.get("n_singletons")
        kpis["n_nontrivial"] = s.get("n_nontrivial")
        kpis["largest_cluster"] = s.get("largest_cluster_size")
        kpis["n_same_cj_collisions"] = s.get("same_cj_collisions", 0)

    # Anonymity-set reduction (v7 headline)
    anon_p = TMP / "v7" / "anonset_reduction_v7.json"
    if anon_p.exists():
        a = json.loads(anon_p.read_text())
        kpis["n_analysed"] = a.get("n_cjs_analyzed")
        kpis["mean_n_eq"] = a.get("mean_n_eq")
        kpis["mean_certified"] = a.get("mean_certified_makers")
        kpis["mean_residual"] = a.get("mean_residual_anon_set")
        kpis["share_any_reduction"] = a.get("share_cjs_with_any_reduction")
        kpis["share_all_certified"] = a.get("share_cjs_all_makers_certified")
        # median from histogram
        hist = {int(k): int(v) for k, v in a.get("residual_anon_set_histogram", {}).items()}
        total = sum(hist.values())
        if total:
            half = total / 2
            cum = 0
            for k in sorted(hist.keys()):
                cum += hist[k]
                if cum >= half:
                    kpis["median_residual"] = k
                    break

    # Probe-attack validation (v7 ground truth)
    probe_p = TMP / "v7" / "probe_validation_v7.json"
    if probe_p.exists():
        p = json.loads(probe_p.read_text())
        kpis["probe_nicks_total"] = p.get("n_nicks")
        kpis["probe_nicks_matched"] = p.get("n_nicks_with_any_v7_match")
        kpis["probe_cross_nick_collisions"] = p.get("precision_violations_clusters", 0)

    # Probing study KPIs (from pinned study data files)
    try:
        from coinjoin_simulator.publish import build_publish_payload  # type: ignore[import]

        probing_payload = build_publish_payload(
            mitigation_path=ROOT / "mitigation_experiments.json",
            longrun_path=ROOT / "longrun_policy_results.json",
            daily_path=ROOT / "daily_cost_study_results.json",
        )
        pkf = probing_payload.get("key_findings", {})
        kpis["probing_baseline_evil_04"] = pkf.get("baseline_deanon_evil_04")
        kpis["probing_recommended_evil_04"] = pkf.get("recommended_deanon_evil_04")
        kpis["probing_baseline_10ppd"] = pkf.get("baseline_deanon_10_probes")
        kpis["probing_daily_cost_btc"] = pkf.get("daily_cost_10_probes_btc")
    except Exception:
        kpis["probing_baseline_evil_04"] = None
        kpis["probing_recommended_evil_04"] = None
        kpis["probing_baseline_10ppd"] = None
        kpis["probing_daily_cost_btc"] = None

    return kpis


COMMON_CSS = """
body { font-family: 'IBM Plex Sans', -apple-system, system-ui, sans-serif;
       max-width: 980px; margin: 0 auto; padding: 0 1.2rem 4rem;
       color: #102231; line-height: 1.6; font-size: 15.5px;
       background: #f7fbfd; }
nav.topnav { display: flex; gap: 1.4rem; align-items: center;
             padding: 1rem 0; border-bottom: 1px solid #d7e3ea;
             margin-bottom: 1.6rem; font-size: 0.9rem; }
nav.topnav a { color: #1f7a8c; text-decoration: none; }
nav.topnav a:hover { text-decoration: underline; }
nav.topnav .brand { font-weight: 600; color: #102231; }
h1, h2, h3 { font-family: 'Fraunces', Georgia, serif; }
h1 { font-size: 2rem; margin: 1rem 0 0.5rem; line-height: 1.2; }
h2 { font-size: 1.4rem; margin: 2rem 0 0.6rem;
     padding-top: 0.6rem; border-top: 1px solid #e0e8ee; }
h3 { font-size: 1.1rem; margin: 1.4rem 0 0.4rem; }
p { color: #3a5568; }
strong { color: #102231; }
table { border-collapse: collapse; margin: 1rem 0; width: 100%; }
th, td { padding: 0.45rem 0.7rem; border: 1px solid #d7e3ea;
         text-align: left; }
th { background: #f0f4f7; }
code { background: #f0f5f8; padding: 0.1rem 0.3rem; border-radius: 3px;
       font-family: 'IBM Plex Mono', ui-monospace, monospace;
       font-size: 0.9em; }
pre { background: #f3f6f8; padding: 0.8rem 1rem; border-radius: 6px;
      overflow-x: auto; }
pre code { background: transparent; padding: 0; }
blockquote { background: #f0f7f4; border-left: 4px solid #1f9366;
             padding: 0.8rem 1rem; margin: 1rem 0;
             border-radius: 0 6px 6px 0; }
blockquote p { color: #102231; margin: 0; }
.math.display { display: block; margin: 1.1rem 0;
                text-align: center; overflow-x: auto; }
ul, ol { padding-left: 1.6rem; margin: 0.6rem 0 0.9rem; }
ul li, ol li { margin: 0.25rem 0; }
ul ul, ol ol, ul ol, ol ul { margin: 0.25rem 0 0.25rem; }
li > p { margin: 0.2rem 0; }
h2 a.anchor, h3 a.anchor { color: #b3c6d2; text-decoration: none;
                           margin-left: 0.4rem; font-size: 0.85em;
                           opacity: 0; transition: opacity 0.15s; }
h2:hover a.anchor, h3:hover a.anchor { opacity: 1; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr);
        gap: 0.7rem; margin: 1.5rem 0; }
.kpi { background: #fff; border: 1px solid #d7e3ea; padding: 0.7rem 0.9rem;
       border-radius: 8px; }
.kpi b { display: block; font-size: 1.5rem; color: #102231; }
.kpi span { font-size: 0.78rem; color: #5a7080; }
.kpi.danger b { color: #c7472d; }
.kpi.safe b { color: #1a7a4f; }
.note { background: #fff7e0; padding: 0.7rem 0.9rem; border-radius: 6px;
        border-left: 4px solid #c8a800; margin: 1rem 0;
        font-size: 0.92rem; }
footer { margin-top: 3rem; padding-top: 1rem;
         border-top: 1px solid #d7e3ea; color: #6a8090;
         font-size: 0.85rem; }
@media (max-width: 700px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 480px) { .kpis { grid-template-columns: 1fr; } }
img, svg.fig { display: block; max-width: 100%; height: auto;
               margin: 1.2rem auto; background: #fff;
               border: 1px solid #e0e8ee; border-radius: 6px;
               padding: 0.4rem; box-sizing: border-box; }
p:has(> img:only-child) { text-align: center; margin: 1.4rem 0; }
"""

NAV = """
<nav class="topnav">
  <span class="brand">CoinJoin Simulator</span>
  <a href="index.html">Home</a>
  <a href="mainnet-deanon.html">Mainnet deanonymization</a>
  <a href="probing-attack.html">Probing attack &amp; mitigations</a>
  <a href="https://github.com/joinmarket-ng/coinjoin-simulator">GitHub</a>
</nav>
"""

MATHJAX = """
<script>
window.MathJax = {
  tex: {inlineMath: [['$','$']], displayMath: [['$$','$$']]},
  svg: {fontCache: 'global'}
};
</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
"""


def _page(title: str, body: str, with_math: bool = False) -> str:
    head_extra = MATHJAX if with_math else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
  <style>{COMMON_CSS}</style>
  {head_extra}
</head>
<body>
{NAV}
<main>
{body}
</main>
<footer>
  Generated by <code>publish_site.py</code> from
  <code>papers/maker-clustering.md</code> and on-disk reports.
  Source: <a href="https://github.com/joinmarket-ng/coinjoin-simulator">joinmarket-ng/coinjoin-simulator</a>
  &middot;
  <a href="https://github.com/joinmarket-ng/joinmarket-ng">joinmarket-ng/joinmarket-ng</a>
</footer>
</body>
</html>
"""


def build_index(kpis: dict[str, Any]) -> str:
    n_txs = kpis.get("n_corpus_txs") or 0
    n_decoded = kpis.get("n_decoded_txs") or 0
    n_clusters = kpis.get("n_clusters") or 0
    n_nontrivial = kpis.get("n_nontrivial") or 0
    n_collisions = kpis.get("n_same_cj_collisions") or 0
    mean_n_eq = kpis.get("mean_n_eq") or 0.0
    mean_residual = kpis.get("mean_residual") or 0.0
    share_any = (kpis.get("share_any_reduction") or 0) * 100

    def _pct(v: Any) -> str:
        if v is None:
            return "n/a"
        return f"{float(v) * 100:.1f}%"

    def _btc(v: Any) -> str:
        if v is None:
            return "n/a"
        return f"{float(v):.4f} BTC/day"

    p_base_evil04 = _pct(kpis.get("probing_baseline_evil_04"))
    p_rec_evil04 = _pct(kpis.get("probing_recommended_evil_04"))
    p_base_10ppd = _pct(kpis.get("probing_baseline_10ppd"))
    p_cost_10ppd = _btc(kpis.get("probing_daily_cost_btc"))

    body = f"""
<h1>CoinJoin Simulator &mdash; studies</h1>
<p>This site collects two studies of CoinJoin privacy run on top of the
<a href="https://github.com/joinmarket-ng/coinjoin-simulator">coinjoin-simulator</a>
+ <a href="https://github.com/joinmarket-ng/joinmarket-ng">joinmarket-ng</a>
codebases.</p>

<h2><a href="mainnet-deanon.html">JoinMarket maker clustering &amp; taker anonymity-set reduction (May 2026)</a></h2>
<p>A passive on-chain adversary clusters JoinMarket maker wallets
through protocol-mandated mixdepth-rotating change outputs, at
precision = 1.0 by construction. On the full mainnet corpus
({n_txs:,} JM CoinJoins, {n_decoded:,} ILP-decoded), the v7
clusterer (change-chain + fee-fingerprint equal-chain) recovers
{n_clusters:,} certified wallet components. Each certified maker
shrinks the taker's per-CJ anonymity set.</p>
<div class="kpis">
  <div class="kpi"><b>{mean_n_eq:.2f} &rarr; {mean_residual:.2f}</b><span>Mean taker anonymity set (published &rarr; v7 residual)</span></div>
  <div class="kpi danger"><b>{share_any:.1f}%</b><span>CJs where at least one maker is certified</span></div>
  <div class="kpi"><b>{n_clusters:,}</b><span>v7 maker clusters ({n_nontrivial:,} non-trivial)</span></div>
  <div class="kpi safe"><b>{n_collisions}</b><span>Same-CJ precision violations (out of {n_decoded:,} CJs)</span></div>
</div>
<p><a href="mainnet-deanon.html">&rarr; full study</a></p>

<h2><a href="probing-attack.html">Probing attack &amp; countermeasures (May 2026)</a></h2>
<p>A simulator-only study quantifying how a malicious service-provider
builds a live UTXO database of JoinMarket makers by probing, the
conditions under which this enables maker identification in honest
CoinJoins, and which mitigations limit the leakage.</p>
<div class="kpis">
  <div class="kpi danger"><b>{p_base_evil04}</b><span>Baseline: all-maker input coverage (40% attacker share, 5,000 rounds)</span></div>
  <div class="kpi safe"><b>{p_rec_evil04}</b><span>Recommended policy (same conditions)</span></div>
  <div class="kpi danger"><b>{p_base_10ppd}</b><span>Baseline at 10 probes/day</span></div>
  <div class="kpi"><b>{p_cost_10ppd}</b><span>Attacker cost at 10 probes/day (recommended policy)</span></div>
</div>
<p><a href="probing-attack.html">&rarr; full study</a></p>
"""
    return _page("CoinJoin Simulator — studies", body)


def build_deanon_page(kpis: dict[str, Any]) -> str:
    md = PAPER_MD.read_text() if PAPER_MD.exists() else "# (paper missing)"
    body_md = _md_to_html(md)
    n_txs = kpis.get("n_corpus_txs") or 0
    n_decoded = kpis.get("n_decoded_txs") or 0
    n_slots = kpis.get("n_slots") or 0
    n_clusters = kpis.get("n_clusters") or 0
    n_nontrivial = kpis.get("n_nontrivial") or 0
    largest = kpis.get("largest_cluster") or 0
    n_collisions = kpis.get("n_same_cj_collisions") or 0
    mean_n_eq = kpis.get("mean_n_eq") or 0.0
    mean_residual = kpis.get("mean_residual") or 0.0
    share_any = (kpis.get("share_any_reduction") or 0) * 100
    share_all = (kpis.get("share_all_certified") or 0) * 100
    median_residual = kpis.get("median_residual")
    median_residual_s = "n/a" if median_residual is None else str(median_residual)
    probe_total = kpis.get("probe_nicks_total") or 0
    probe_matched = kpis.get("probe_nicks_matched") or 0
    probe_collisions = kpis.get("probe_cross_nick_collisions") or 0

    kpi_card = f"""
<h1>JoinMarket Maker Clustering and Taker Anonymity-Set Reduction</h1>
<p><em>May 2026 (coinjoin-simulator + joinmarket-analyzer, v7 clusterer)</em></p>
<div class="kpis">
  <div class="kpi"><b>{n_txs:,}</b><span>JM CoinJoin txs in corpus ({n_decoded:,} ILP-decoded)</span></div>
  <div class="kpi"><b>{n_slots:,}</b><span>Maker slots recovered</span></div>
  <div class="kpi"><b>{n_clusters:,}</b><span>v7 clusters ({n_nontrivial:,} non-trivial, largest {largest})</span></div>
  <div class="kpi safe"><b>{n_collisions}</b><span>Same-CJ precision violations (out of {n_decoded:,})</span></div>
  <div class="kpi"><b>{mean_n_eq:.2f} &rarr; {mean_residual:.2f}</b><span>Mean taker anonymity set (published &rarr; v7 residual)</span></div>
  <div class="kpi danger"><b>{share_any:.1f}%</b><span>CJs where at least one maker is certified</span></div>
  <div class="kpi"><b>{median_residual_s}</b><span>Median residual anonymity set ({share_all:.1f}% reach residual = 1, taker alone)</span></div>
  <div class="kpi safe"><b>{probe_collisions} / {probe_matched}</b><span>Cross-nick collisions / probed nicks with v7 matches (of {probe_total} probed)</span></div>
</div>
"""
    body = kpi_card + body_md
    return _page("JoinMarket Maker Clustering and Taker Anonymity-Set Reduction", body, with_math=True)


def build_probing_page() -> str:
    """Pass through the prior single-page report."""
    src = INDEX.read_text() if INDEX.exists() else ""
    if not src.startswith("<!DOCTYPE html>") and not src.startswith("<!doctype"):
        # already rebuilt; bail out gracefully
        return _page(
            "Probing Attack &mdash; CoinJoin Simulator",
            "<p>Source page <code>docs/index.html</code> already replaced; "
            "restore from git to re-publish.</p>",
        )
    # Inject our nav at the top of <body>
    nav_html = NAV
    out = re.sub(r"(<body[^>]*>)", r"\1\n" + nav_html, src, count=1)
    return out


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    kpis = _load_kpis()

    # Mirror paper figures into docs/ so the HTML's relative img paths work.
    figs_src = PAPERS / "figures"
    figs_dst = DOCS / "figures"
    if figs_src.is_dir():
        figs_dst.mkdir(parents=True, exist_ok=True)
        for svg in figs_src.glob("*.svg"):
            (figs_dst / svg.name).write_bytes(svg.read_bytes())

    # Snapshot the current index.html (probing study) BEFORE we overwrite it.
    if INDEX.exists() and not PROBING_PAGE.exists():
        PROBING_PAGE.write_text(build_probing_page())
        print(f"wrote {PROBING_PAGE}")
    else:
        # Refresh probing page from index.html only if index hasn't been
        # rebuilt yet.
        if INDEX.exists():
            txt = INDEX.read_text()
            if "<title>CoinJoin Probing Attack" in txt:
                PROBING_PAGE.write_text(build_probing_page())
                print(f"updated {PROBING_PAGE}")

    DEANON_PAGE.write_text(build_deanon_page(kpis))
    print(f"wrote {DEANON_PAGE}")

    INDEX.write_text(build_index(kpis))
    print(f"wrote {INDEX}")

    summary = {
        "kpis": kpis,
        "pages": [str(p.relative_to(ROOT)) for p in (INDEX, DEANON_PAGE, PROBING_PAGE)],
    }
    (DOCS / "publish_summary.json").write_text(json.dumps(summary, indent=2))
    print("wrote", DOCS / "publish_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
