"""Minimal SVG line charts, generated from experiment results.

Charts on this blog are produced by the same script that produces the numbers,
for the same reason the numbers are: a chart drawn by hand from a table is a
second copy of the data that can silently drift from the first. Here there is
one source, `results.json`, and the figure is regenerated whenever it is.

The output is a self-contained SVG with no external references, meant to be
inlined into the page rather than loaded through an `<img>` tag. Inlining is
what makes it theme-aware: the marks are drawn in `var(--ink)`, `var(--accent)`
and friends, so the figure follows the site's light/dark toggle instead of
being a picture of one of the two. Every colour carries a literal fallback, so
the file still renders correctly opened on its own.

No dependencies. matplotlib would draw a nicer chart and would also be a
hundred megabytes of transitive dependency in a pipeline that has to keep
working unattended for months.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Sequence

# Series colours, in order. Chosen to stay distinguishable in both themes and
# in greyscale: the accent is warm and mid-toned, the ink pair differ in weight.
SERIES_COLORS = [
    ("var(--accent, #b4530a)", "#b4530a"),
    ("var(--ink, #171614)", "#171614"),
    ("var(--ink-faint, #86807a)", "#86807a"),
    ("var(--ok, #1f6b3f)", "#1f6b3f"),
]

W, H = 720, 400
PAD_L, PAD_R, PAD_T, PAD_B = 62, 116, 18, 46


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _nice_log_ticks(lo: float, hi: float) -> list[float]:
    """Decade ticks spanning [lo, hi], with 3x steps when the range is short."""
    out = []
    d = math.floor(math.log10(lo))
    while 10 ** d <= hi * 1.0001:
        for m in (1, 3):
            v = m * 10 ** d
            if lo * 0.999 <= v <= hi * 1.0001:
                out.append(v)
        d += 1
    return out or [lo, hi]


def _fmt_time(ns: float) -> str:
    if ns >= 1e6:
        return f"{ns / 1e6:g} ms"
    if ns >= 1e3:
        return f"{ns / 1e3:g} µs"
    return f"{ns:g} ns"


def _fmt_count(v: float) -> str:
    return f"{int(v):,}" if v >= 1 else f"{v:g}"


def log_line_chart(
    series: Sequence[dict],
    *,
    title: str,
    x_label: str,
    y_label: str,
    x_ticks: Iterable[float] | None = None,
) -> str:
    """A log-log line chart.

    `series` is a list of ``{"label": str, "points": [(x, y), ...]}``. Both axes
    are logarithmic because the interesting comparison here spans four orders of
    magnitude on each, and a linear axis would compress everything below n=1000
    into the origin.
    """
    xs = [x for s in series for x, _ in s["points"]]
    ys = [y for s in series for _, y in s["points"]]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    # Pad the value axis by a fifth of a decade so marks do not touch the frame.
    y0, y1 = y0 / 1.5, y1 * 1.5

    lx0, lx1 = math.log10(x0), math.log10(x1)
    ly0, ly1 = math.log10(y0), math.log10(y1)
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    def px(x): return PAD_L + (math.log10(x) - lx0) / (lx1 - lx0) * plot_w
    def py(y): return PAD_T + plot_h - (math.log10(y) - ly0) / (ly1 - ly0) * plot_h

    parts: list[str] = []
    grid = "var(--rule, #e2ded7)"
    faint = "var(--ink-faint, #86807a)"
    muted = "var(--ink-muted, #57534d)"

    # --- y grid and labels ---
    for v in _nice_log_ticks(y0, y1):
        y = py(v)
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{PAD_L + plot_w}" y2="{y:.1f}" '
                     f'stroke="{grid}" stroke-width="1"/>')
        parts.append(f'<text x="{PAD_L - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
                     f'font-size="11" fill="{faint}">{_esc(_fmt_time(v))}</text>')

    # --- x ticks ---
    for v in (x_ticks if x_ticks is not None else _nice_log_ticks(x0, x1)):
        if not (x0 <= v <= x1):
            continue
        x = px(v)
        parts.append(f'<line x1="{x:.1f}" y1="{PAD_T + plot_h}" x2="{x:.1f}" y2="{PAD_T + plot_h + 4}" '
                     f'stroke="{grid}" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{PAD_T + plot_h + 17}" text-anchor="middle" '
                     f'font-size="11" fill="{faint}">{_esc(_fmt_count(v))}</text>')

    # --- frame ---
    parts.append(f'<line x1="{PAD_L}" y1="{PAD_T + plot_h}" x2="{PAD_L + plot_w}" y2="{PAD_T + plot_h}" '
                 f'stroke="var(--rule-firm, #cbc5bb)" stroke-width="1"/>')

    # --- series ---
    for i, s in enumerate(series):
        # `color` lets two related series share a hue and be told apart by
        # dashing instead. Four distinct colours for two containers x two cases
        # reads as four unrelated things; two colours reads as the comparison
        # the chart is actually making.
        colour = SERIES_COLORS[s.get("color", i) % len(SERIES_COLORS)][0]
        dash = ' stroke-dasharray="5 3"' if s.get("dashed") else ""
        pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in s["points"])
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                     f'stroke-width="2" stroke-linejoin="round"{dash}/>')
        for x, y in s["points"]:
            parts.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="2.6" fill="{colour}"/>')
        # Label at the end of the line rather than in a legend box, so the eye
        # never has to travel between a swatch and a curve.
        lx, ly = s["points"][-1]
        # `label_dy` nudges a label off its line. Two series that converge --
        # which is the whole point of some of these charts -- would otherwise
        # print their labels on top of each other at the right-hand edge.
        parts.append(f'<text x="{px(lx) + 9:.1f}" y="{py(ly) + 4 + s.get("label_dy", 0):.1f}" '
                     f'font-size="12" font-weight="600" fill="{colour}">{_esc(s["label"])}</text>')

    # --- axis titles ---
    parts.append(f'<text x="{PAD_L + plot_w / 2:.0f}" y="{H - 6}" text-anchor="middle" '
                 f'font-size="12" fill="{muted}">{_esc(x_label)}</text>')
    parts.append(f'<text transform="translate(14,{PAD_T + plot_h / 2:.0f}) rotate(-90)" '
                 f'text-anchor="middle" font-size="12" fill="{muted}">{_esc(y_label)}</text>')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="100%" role="img" aria-label="{_esc(title)}" '
        f'font-family="system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif">\n'
        f'<title>{_esc(title)}</title>\n' + "\n".join(parts) + "\n</svg>\n"
    )


def write(svg: str, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg)
    print(f"wrote {out}", flush=True)
    return out
