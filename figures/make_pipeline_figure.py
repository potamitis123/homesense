"""Draw the Level 0 -- Level 4 abstraction ladder, including Level 1.5.

    python make_pipeline_figure.py

Writes fig3-dynamics-pipeline.png and .svg next to this file. It is meant to
replace fig3-reflection-ladder.png, which was drawn before Level 1.5 existed
and which counts records rather than describing what each stage does.

Two things the older figure could not show, and this one has to:

  * Level 1.5 does not sit in the chain. It reads Level 0 directly -- the
    speech turns, not the semantic events above them -- and its output joins
    the pipeline again at Level 3, where episodes are already organised around
    people. So it is drawn as a branch, not as a sixth box in the row.

  * It is the only stage that needs a language model. Everything else is a
    pure function of the level below it, and the figure says so.

The worked example is one exchange carried through every stage. It is the case
the layer exists for: the words survive the deterministic levels intact and
only Level 1.5 registers that the reply closed the topic.

Palette and typography follow figures 1 and 2 of the same paper.
"""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle

HERE = os.path.dirname(os.path.abspath(__file__))

INK = "#10222A"        # headings
MUTE = "#56707A"       # mono detail, arrows
EDGE = "#C2D1D6"       # ordinary box border
TINT = "#DCEDEF"       # the interpretive stage
TEAL = "#0D6E79"
PAPER = "#FFFFFF"
PANEL = "#F4F8F9"      # inner panel of the worked example

SANS = ["Archivo", "Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"]
MONO = ["IBM Plex Mono", "Consolas", "Courier New", "DejaVu Sans Mono"]

# The figure is printed one text column wide, so the only thing that decides
# how large the type comes out is how many characters have to fit across it.
# Six stages in one row put the detail lines at about four points on paper,
# which is unreadable; wrapping the chain onto two rows halves the characters
# across and doubles the printed size. Hence the serpentine layout below.
W, H = 1560, 960

LABEL, HEAD, DETAIL, NOTE, SAMPLE = 17, 26, 19, 17, 18

BOX_W, BOX_H = 400, 210          # one stage
COL = (265, 690, 1115)           # the three columns both rows share
ROW_TOP, ROW_BOTTOM = 700, 390   # y of each row's floor


def box(ax, x, y, w, h, level, headline, lines, highlight=False):
    """One stage: a mono level label, a headline, and mono detail lines."""
    ax.add_patch(Rectangle(
        (x, y), w, h,
        facecolor=TINT if highlight else PAPER,
        edgecolor=TEAL if highlight else EDGE,
        linewidth=2.0 if highlight else 1.5, zorder=2))
    ax.text(x + 20, y + h - 38, level, family=MONO, fontsize=LABEL,
            color=TEAL if highlight else MUTE, va="center", zorder=3)
    ax.text(x + 20, y + h - 92, headline, family=SANS, fontsize=HEAD,
            fontweight="bold", color=INK, va="center", zorder=3)
    for index, line in enumerate(lines):
        ax.text(x + 20, y + h - 140 - index * 32, line, family=MONO,
                fontsize=DETAIL, color=MUTE, va="center", zorder=3)


def arrow(ax, points, color=MUTE):
    """A straight or elbowed connector, arrowhead on the last segment."""
    for start, end in zip(points, points[1:-1]):
        ax.plot([start[0], end[0]], [start[1], end[1]], color=color,
                linewidth=2.0, solid_capstyle="round", zorder=1)
    ax.add_patch(FancyArrowPatch(
        points[-2], points[-1], arrowstyle="-|>", mutation_scale=16,
        color=color, linewidth=2.0, shrinkA=0, shrinkB=0, zorder=1))


def draw():
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(30, H)
    ax.axis("off")
    fig.patch.set_facecolor(PAPER)

    ax.text(40, 932, "each stage is a pure function of the level below it",
            family=MONO, fontsize=NOTE, color=MUTE, va="center", zorder=3)

    # ---- row one: raw events up to activity episodes ------------------
    box(ax, 40, ROW_TOP, 200, BOX_H, "RAW", "Events",
        ["one row", "per clip"])
    top_row = [
        ("LEVEL 0", "Observations",
         ["time · room · speaker", "“how was your day?”"]),
        ("LEVEL 1", "Semantic events",
         ["Dishes → food_activity", "voice → known speaker"]),
        ("LEVEL 2", "Activities",
         ["merged by time, room", "and kind of activity"]),
    ]
    for x, (level, headline, lines) in zip(COL, top_row):
        box(ax, x, ROW_TOP, BOX_W, BOX_H, level, headline, lines)

    mid_top = ROW_TOP + BOX_H / 2
    arrow(ax, [(240, mid_top), (265, mid_top)])
    for x in COL[:-1]:
        arrow(ax, [(x + BOX_W, mid_top), (x + BOX_W + 25, mid_top)])

    # ---- row two: social episodes, the day, what is shown -------------
    bottom_row = [
        ("LEVEL 3", "Social episodes",
         ["who with whom", "turns · balance · room"]),
        ("LEVEL 4", "The day",
         ["reciprocity · pairs", "baseline · unknowns"]),
        ("REPORT", "What is shown",
         ["narrative · indicators", "three questions"]),
    ]
    for x, (level, headline, lines) in zip(COL, bottom_row):
        box(ax, x, ROW_BOTTOM, BOX_W, BOX_H, level, headline, lines)

    mid_bottom = ROW_BOTTOM + BOX_H / 2
    for x in COL[:-1]:
        arrow(ax, [(x + BOX_W, mid_bottom), (x + BOX_W + 25, mid_bottom)])

    # the chain wraps from the end of row one to the start of row two
    wrap_x = COL[2] + BOX_W / 2
    arrow(ax, [(wrap_x, ROW_TOP), (wrap_x, 645), (COL[0] + BOX_W / 2, 645),
               (COL[0] + BOX_W / 2, ROW_BOTTOM + BOX_H)])

    # ---- the interpretive branch --------------------------------------
    lx, ly, lw, lh = 265, 60, 1250, 240
    arrow(ax, [(COL[0] + 100, ROW_TOP), (COL[0] + 100, 668), (150, 668),
               (150, 180), (lx, 180)])
    ax.text(128, 470, "the speech turns", family=MONO, fontsize=NOTE,
            color=TEAL, va="center", ha="center", rotation=90, zorder=3)
    arrow(ax, [(COL[0] + BOX_W / 2, ly + lh), (COL[0] + BOX_W / 2, ROW_BOTTOM)])
    ax.text(COL[0] + BOX_W / 2 + 22, 345, "attached to the people in it",
            family=MONO, fontsize=NOTE, color=TEAL, va="center", zorder=3)

    box(ax, lx, ly, lw, lh, "LEVEL 1.5", "Interpreted exchange", [],
        highlight=True)
    ax.text(lx + 500, ly + lh - 92, "the one stage that reads meaning",
            family=MONO, fontsize=NOTE, color=TEAL, va="center", zorder=3)

    ax.add_patch(Rectangle((lx + 20, ly + 18), 540, 104, facecolor=PANEL,
                           edgecolor=EDGE, linewidth=1.25, zorder=3))
    sample = [
        ("Ilyas   “how was your day?”", INK),
        ("Rafael  “fine.”  (4 s later)", INK),
        ("Dishes, cutlery · kitchen", MUTE),
    ]
    for index, (line, colour) in enumerate(sample):
        ax.text(lx + 38, ly + 96 - index * 30, line, family=MONO,
                fontsize=SAMPLE, color=colour, va="center", zorder=4)

    ax.add_patch(FancyArrowPatch(
        (lx + 576, ly + 70), (lx + 624, ly + 70), arrowstyle="-|>",
        mutation_scale=22, color=TEAL, linewidth=3.0, shrinkA=0, shrinkB=0,
        zorder=4))
    reading = [
        ("withdrawal 0.50 · tension 0.00", INK),
        ("warmth 0.25 · joy 0.00", INK),
        ("a bid answered but closed", MUTE),
    ]
    for index, (line, colour) in enumerate(reading):
        ax.text(lx + 640, ly + 96 - index * 30, line, family=MONO,
                fontsize=SAMPLE, color=colour, va="center", zorder=4)

    png = os.path.join(HERE, "fig3-dynamics-pipeline.png")
    fig.savefig(png, dpi=300, facecolor=PAPER)
    fig.savefig(os.path.join(HERE, "fig3-dynamics-pipeline.svg"),
                facecolor=PAPER)
    plt.close(fig)
    print("wrote", png)


if __name__ == "__main__":
    draw()
