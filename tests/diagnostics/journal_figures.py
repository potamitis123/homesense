#!/usr/bin/env python3
"""Figures for the journal report, drawn from data/results.json alone.

    python tests/diagnostics/journal_figures.py

Nothing is computed here that journal_experiments.py did not already write,
so a figure cannot disagree with the text that cites it.

Colour follows the dataviz reference palette in light mode, by the job each
colour does: a single sequential blue ramp for magnitudes (heatmaps),
categorical slots 1-3 (blue, orange, aqua) where series are identities --
speakers in Fig. 1, conditions elsewhere -- and grey for the de-emphasised
series of an emphasis pair. The categorical set was checked with the skill's
validate_palette.js (all pairs pass; aqua needs visible labels or a table,
and every chart that uses it has a table twin in the report).
"""
import json
import os

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt                                    # noqa: E402
from matplotlib.colors import LinearSegmentedColormap              # noqa: E402
from matplotlib.lines import Line2D                                # noqa: E402
from matplotlib.patches import Patch                               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(ROOT, "reports", "journal_2026-09-10")
FIG = os.path.join(OUT, "figures")

SURFACE = "#ffffff"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, WASH = "#e1e0d9", "#c3c2b7", "#f0efec"
SLOT = ("#2a78d6", "#eb6834", "#1baf7a")
RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
        "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
BLUES = LinearSegmentedColormap.from_list("ramp", RAMP)
BLUES.set_bad(WASH)
RESIDENTS = ("Ilyas", "Voula", "Rafael")
WHO = {"Ilyas": "Ilyas (adult)", "Voula": "Voula (adult)", "Rafael": "Rafael (child)"}
TEXT_LABEL, REC_LABEL = "Original text", "Recording"
# The relational dimensions the system kept. Connection and inclusion were
# withdrawn after this experiment was run -- connection could not vary, because
# an exchange reaches the interpreter only when two residents spoke in it, and
# inclusion was read two different ways by two careful readers. results.json
# still holds all six, and Tables 7 to 9 still report them; Figures 3, 4 and 6
# draw the four that remain, in the order the data lists them.
KEPT_DIMENSIONS = ("warmth", "joy", "withdrawal", "tension")


def kept(dimensions):
    return [d for d in dimensions if d in KEPT_DIMENSIONS]

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "Arial", "DejaVu Sans"],
    "font.size": 8.5, "axes.titlesize": 9, "axes.labelsize": 8.5,
    "axes.titleweight": "semibold", "axes.titlecolor": INK,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.labelcolor": INK2,
    "axes.spines.top": False, "axes.spines.right": False,
    "xtick.color": AXIS, "ytick.color": AXIS,
    "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
    "xtick.labelsize": 8, "ytick.labelsize": 8,
    "legend.frameon": False, "legend.fontsize": 8, "legend.labelcolor": INK2,
    "text.color": INK, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
})


def save(fig, name):
    os.makedirs(FIG, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(FIG, f"{name}.{ext}"), dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  {name}")


def _linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def ink_on(rgba):
    """White or ink, whichever clears contrast on a filled cell."""
    r, g, b = rgba[:3]
    lum = 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)
    return "#ffffff" if lum < 0.30 else INK


def hairline_grid(ax, axis="x"):
    ax.grid(True, axis=axis, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def heatmap(ax, shade, labels, rows, cols, vmin=0.0, vmax=1.0, fontsize=7.8):
    shade = np.array(shade, float)
    ax.imshow(np.ma.masked_invalid(shade), cmap=BLUES, vmin=vmin, vmax=vmax, aspect="auto")
    for i in range(shade.shape[0]):
        for j in range(shade.shape[1]):
            value = shade[i, j]
            color = INK2 if np.isnan(value) else ink_on(BLUES((value - vmin) / (vmax - vmin)))
            ax.text(j, i, labels[i][j], ha="center", va="center", fontsize=fontsize, color=color)
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels(cols)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(rows)
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_frame_on(False)
    # Separators between cells only: a gridline on the outer edge rendered as
    # a stray frame line beside the last column.
    ax.set_xticks(np.arange(0.5, len(cols) - 1, 1), minor=True)
    ax.set_yticks(np.arange(0.5, len(rows) - 1, 1), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    ax.tick_params(which="minor", length=0)


# ── Fig. 1 ─────────────────────────────────────────────────────────────
def fig1_lines_by_sentinel_set(res):
    """Every scripted line, by the set of sentinels that delivered it."""
    ov = res["overlap"]["by_subset"]
    order = ["B1", "B2", "P4", "B1+B2", "B1+P4", "B2+P4", "B1+B2+P4", "none"]
    names = ["B1 only", "B2 only", "P4 only", "B1 and B2", "B1 and P4", "B2 and P4",
             "All three", "None"]
    x = np.arange(len(order))
    fig, ax = plt.subplots(figsize=(6.6, 3.3))
    bottom = np.zeros(len(order))
    for k, who in enumerate(RESIDENTS):
        vals = np.array([ov[s][who] for s in order], float)
        ax.bar(x, vals, bottom=bottom, width=0.58, color=SLOT[k], edgecolor=SURFACE,
               linewidth=1.2, label=WHO[who], zorder=2)
        bottom += vals
    for xi, total in zip(x, bottom):
        ax.text(xi, total + 1.0, f"{int(total)}", ha="center", va="bottom",
                fontsize=7.5, color=INK2)
    ax.set_xticks(x)
    ax.set_xticklabels(names)
    ax.tick_params(axis="x", length=0)
    ax.set_ylim(0, max(bottom) * 1.15)
    hairline_grid(ax, "y")
    ax.set_ylabel("Scripted lines")
    ax.set_xlabel("Sentinels that delivered a recognisable copy of the line, each scored on its own")
    # Upper right is empty; upper left sat on the tallest bar and its label.
    ax.legend(loc="upper right")
    fig.tight_layout()
    save(fig, "fig1_lines_by_sentinel_set")


# ── Fig. 2 ─────────────────────────────────────────────────────────────
def fig2_speaker_confusion(res):
    sp = res["speaker"]["confusion"]
    cols = list(RESIDENTS) + ["Unknown"]
    S = np.array([[sp[r][c] for c in cols] for r in RESIDENTS], float)
    fig, ax = plt.subplots(figsize=(3.9, 2.5))
    heatmap(ax, S / np.maximum(S.sum(axis=1, keepdims=True), 1),
            [[f"{int(v)}" for v in row] for row in S], list(RESIDENTS), cols)
    ax.set_xlabel("Identified speaker")
    ax.set_ylabel("Scripted speaker")
    fig.tight_layout()
    save(fig, "fig2_speaker_confusion")


# ── Fig. 3 ─────────────────────────────────────────────────────────────
def fig3_recording_vs_text(res):
    sem = res["semantic"]
    # Two by two, each panel the size it had in the two-by-three grid, so the
    # markers and the type keep their scale beside the other figures.
    fig, axes = plt.subplots(2, 2, figsize=(4.9, 5.0), sharex=True, sharey=True)
    for ax, d in zip(axes.flat, kept(sem["dimensions"])):
        b = sem["between"][d]
        pts = np.round(np.array(b["points"], float), 3)
        uniq, counts = np.unique(pts, axis=0, return_counts=True)
        ax.fill_between([0, 1], [-0.25, 0.75], [0.25, 1.25], color=WASH, linewidth=0, zorder=0)
        ax.plot([0, 1], [0, 1], color=AXIS, linewidth=1, zorder=1)
        ax.scatter(uniq[:, 0], uniq[:, 1], s=16 * counts, color=SLOT[0],
                   edgecolors=SURFACE, linewidths=1, zorder=3)
        note = f"MAE {b['mae']:.2f}"
        if b["pearson_r"] is not None:
            note += f"\nr = {b['pearson_r']:.2f}"
        ax.text(0.04, 0.96, note, transform=ax.transAxes, va="top", ha="left",
                fontsize=7.5, color=INK2)
        ax.set_title(d.capitalize(), loc="left")
        ax.set_xlim(-0.06, 1.06)
        ax.set_ylim(-0.06, 1.06)
        ax.set_xticks([0, 0.5, 1])
        ax.set_yticks([0, 0.5, 1])
        ax.set_aspect("equal")
    fig.supxlabel("Reading of the original text (mean of three runs)", fontsize=8.5,
                  color=INK2, y=0.035)
    fig.supylabel("Reading of the recording (mean of three runs)", fontsize=8.5, color=INK2)
    handles = [Line2D([], [], marker="o", linestyle="", markersize=np.sqrt(16 * k),
                      markerfacecolor=SLOT[0], markeredgecolor=SURFACE,
                      label=f"{k} take{'s' if k > 1 else ''}") for k in (1, 3, 6)]
    handles.append(Patch(color=WASH, label="within ±0.25"))
    fig.legend(handles=handles, loc="upper center", ncol=4, bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0.02, 0.06, 1, 1), h_pad=1.6)
    save(fig, "fig3_recording_vs_text")


# ── Fig. 4 ─────────────────────────────────────────────────────────────
def fig4_agreement(res):
    sem = res["semantic"]
    dims = kept(sem["dimensions"])
    series = [(f"{TEXT_LABEL}, run vs run",
               [sem["retest"]["text"][d]["mean_abs_diff"] for d in dims]),
              (f"{REC_LABEL}, run vs run",
               [sem["retest"]["recorded"][d]["mean_abs_diff"] for d in dims]),
              (f"{REC_LABEL} vs original text",
               [sem["between"][d]["run_paired_mae"] for d in dims])]
    y = np.arange(len(dims))[::-1]
    h = 0.24
    # Four groups where there were six: the figure is shortened so the bars
    # keep the thickness they had, and the legend offset, which is a fraction
    # of the axis height, grows to stay clear of the axis label.
    fig, ax = plt.subplots(figsize=(5.8, 2.9))
    for k, (label, vals) in enumerate(series):
        vals = np.array([np.nan if v is None else v for v in vals], float)
        ax.barh(y + (1 - k) * (h + 0.03), vals, height=h, color=SLOT[k], label=label, zorder=2)
    ax.set_yticks(y)
    ax.set_yticklabels([d.capitalize() for d in dims])
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    hairline_grid(ax, "x")
    ax.set_xlabel("Mean absolute difference between readings (0–1 scale)")
    # Below the axis: inside the plot it sat on the longest bars.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.25), ncol=3,
              handlelength=1.2, columnspacing=1.4)
    fig.tight_layout()
    save(fig, "fig4_agreement_and_stability")


# ── Fig. 5 ─────────────────────────────────────────────────────────────
def fig5_group_means(res):
    sem = res["semantic"]
    dims = sem["dimensions"]
    names = res["corpus"]["group_names"]
    sizes = res["corpus"]["groups"]
    gids = list(names)
    rows = [f"{g} {names[g]} (n={sizes[g]})" for g in gids]
    fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.3), sharey=True)
    for ax, cond, title in zip(axes, ("text", "recorded"),
                               (f"(a) {TEXT_LABEL}", f"(b) {REC_LABEL}")):
        M = np.array([[np.nan if sem["group_means"][cond][g][d] is None
                       else sem["group_means"][cond][g][d] for d in dims] for g in gids])
        labels = [["–" if np.isnan(v) else f"{v:.2f}" for v in row] for row in M]
        heatmap(ax, M, labels, rows, [d.capitalize() for d in dims])
        ax.set_title(title, loc="left")
        plt.setp(ax.get_xticklabels(), rotation=35, ha="right")
    fig.tight_layout()
    save(fig, "fig5_group_means")


# ── Fig. 6 ─────────────────────────────────────────────────────────────
def fig6_orderings(res):
    rules = [r for r in res["semantic"]["orderings"]
             if r["dimension"] in KEPT_DIMENSIONS]
    labels = [f"{r['dimension'].capitalize()}: {r['greater']} > {r['less']}" for r in rules]
    y = np.arange(len(rules))[::-1]
    # Nine orderings where there were ten; shortened to keep the row pitch.
    fig, ax = plt.subplots(figsize=(6.0, 3.6))
    for k, cond in enumerate(("text", "recorded")):
        offset = 0.16 if k == 0 else -0.16
        for yi, rule in zip(y, rules):
            v = rule.get(cond)
            if not v:
                continue
            ax.plot(v["ci"], [yi + offset] * 2, color=SLOT[k], linewidth=2,
                    solid_capstyle="round", zorder=2)
            ax.plot(v["delta"], yi + offset, "o", color=SLOT[k], markersize=6,
                    markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=3)
    ax.axvline(0, color=INK2, linewidth=0.9, zorder=1)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    hairline_grid(ax, "x")
    ax.set_xlabel("Difference between group means (0–1 scale), 95% bootstrap interval")
    handles = [Line2D([], [], color=SLOT[0], marker="o", markeredgecolor=SURFACE,
                      linewidth=2, label=TEXT_LABEL),
               Line2D([], [], color=SLOT[1], marker="o", markeredgecolor=SURFACE,
                      linewidth=2, label=REC_LABEL)]
    ax.legend(handles=handles, loc="lower right")
    fig.tight_layout()
    save(fig, "fig6_ordering_effects")


# ── Fig. 7 ─────────────────────────────────────────────────────────────
def fig7_group_assignment(res):
    """Nearest-centroid assignment of each take, original text against recording."""
    sem = res["semantic"]
    gids = list(res["corpus"]["group_names"])
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.4), sharey=True)
    for ax, cond, title in zip(axes, ("text", "recorded"),
                               (f"(a) {TEXT_LABEL}", f"(b) {REC_LABEL}")):
        conf = sem["centroid"][cond]["confusion"]
        M = np.array([[conf[g][h] for h in gids] for g in gids], float)
        share = M / np.maximum(M.sum(axis=1, keepdims=True), 1)
        heatmap(ax, share, [["" if v == 0 else f"{int(v)}" for v in row] for row in M],
                gids, gids, fontsize=7.5)
        ax.set_title(title, loc="left")
        ax.set_xlabel("Assigned group")
    axes[0].set_ylabel("Design group")
    fig.tight_layout()
    save(fig, "fig7_group_assignment")


# ── Fig. 8 ─────────────────────────────────────────────────────────────
def fig8_take_level(res):
    wer = res["take_recognition"]
    dev = res["semantic"]["take_deviation"]
    takes = [n for n in wer if dev.get(n) is not None]
    x = np.array([wer[n]["wer"] * 100 for n in takes])
    y = np.array([dev[n] for n in takes])
    fig, ax = plt.subplots(figsize=(4.4, 3.3))
    ax.scatter(x, y, s=30, color=SLOT[0], edgecolors=SURFACE, linewidths=1, zorder=3)
    for i in np.argsort(-y)[:3]:
        ax.annotate(f"#{takes[i]}", (x[i], y[i]), xytext=(4, 3), textcoords="offset points",
                    fontsize=7.5, color=INK2)
    stats_ = res["wer_vs_deviation"]
    ax.text(0.97, 0.96, f"Spearman ρ = {stats_['spearman_rho']:.2f}\np = {stats_['spearman_p']:.2f}",
            transform=ax.transAxes, ha="right", va="top", fontsize=7.5, color=INK2)
    hairline_grid(ax, "both")
    ax.set_xlabel("Word error rate of the take (%)")
    ax.set_ylabel("Mean |recording − original text|")
    fig.tight_layout()
    save(fig, "fig8_take_level_error")


# ── Fig. 9 ─────────────────────────────────────────────────────────────
def fig9_segmentation(res):
    seg = res["segmentation"]
    off = [r["exchanges_merge_off"] for r in seg]
    on = [r["exchanges_merge_on"] for r in seg]
    xs = np.arange(1, max(off + on) + 1)
    w = 0.38
    fig, ax = plt.subplots(figsize=(5.4, 2.8))
    ax.bar(xs - w / 2 - 0.02, [off.count(x) for x in xs], width=w, color=AXIS,
           label="Every copy kept", zorder=2)
    ax.bar(xs + w / 2 + 0.02, [on.count(x) for x in xs], width=w, color=SLOT[0],
           label="Copies merged (deployed)", zorder=2)
    ax.set_xticks(xs)
    hairline_grid(ax, "y")
    ax.set_xlabel("Exchanges the deployed report forms from one take")
    ax.set_ylabel("Takes")
    ax.legend(loc="upper right")
    fig.tight_layout()
    save(fig, "fig9_segmentation")


def main():
    with open(os.path.join(OUT, "data", "results.json"), encoding="utf-8") as fh:
        res = json.load(fh)
    print(f"figures -> {FIG}")
    fig1_lines_by_sentinel_set(res)
    fig2_speaker_confusion(res)
    if "semantic" in res:
        fig3_recording_vs_text(res)
        fig4_agreement(res)
        fig5_group_means(res)
        fig6_orderings(res)
        fig7_group_assignment(res)
        fig8_take_level(res)
    fig9_segmentation(res)


if __name__ == "__main__":
    main()
