#!/usr/bin/env python3
"""A day's discussions as a picture and a list of findings, without opening the viewer.

    python conversation_day/snapshot.py 2026-09-14 --from 11:59 --to 19:00 --model chatgpt
    python conversation_day/snapshot.py 2026-09-14 --from 11:59 --to 19:00

With --model, the window is read first unless a reading of it is already saved
-- by app.py's own reader, so it costs what the viewer costs -- and
--read-again reads it even then. Without --model, the newest saved reading of
the window is used. Either way the viewer's two bands -- who was talking, what
the model read in each discussion -- are drawn to a PNG beside the saved
reading in conversation_day/cache, and what a reviewer should look at first is
printed:

  discussions     each one: who took part, where, how long, and its one reading
  key moments     the sharpest problems and the warmest discussions, with the
                  model's evidence
  left out        lone speech (and how much was overheard from another room), and
                  discussions too short for the model to read
  unread speech   discussions judged uninterpretable despite a good number of words
  failed calls    readings lost at the model rather than read
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import matplotlib                                                  # noqa: E402
import numpy as np                                                 # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt                                    # noqa: E402
from matplotlib.cm import ScalarMappable                            # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, Normalize   # noqa: E402
from matplotlib.patches import Patch                               # noqa: E402

import timeline as T                                               # noqa: E402

GREEN, AMBER = 0.40, 0.40      # matches app.py; see SEMANTIC_PRESENT_FLOOR
PAGE = "#0B1016"
SHORT = {"warmth": "wrm", "joy": "joy", "withdrawal": "wdr", "tension": "ten"}


def _rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)])


def _ramp(stops, value):
    """Colour at `value` (0-1) along a list of [position, hex] stops."""
    value = min(max(value, 0.0), 1.0)
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if value <= p1:
            t = 0.0 if p1 == p0 else (value - p0) / (p1 - p0)
            return _rgb(c0) * (1 - t) + _rgb(c1) * t
    return _rgb(stops[-1][1])


def _scale(fig, ax, stops, low, high, ticks, labels, top, bottom):
    """A thin colour bar right of `ax`, from `top` to `bottom` of its height (0 is the top)."""
    box = ax.get_position()
    y_top, y_bottom = box.y1 - top * box.height, box.y1 - bottom * box.height
    cax = fig.add_axes([box.x1 + 0.012, y_bottom, 0.008, y_top - y_bottom])
    mappable = ScalarMappable(Normalize(low, high),
                              LinearSegmentedColormap.from_list("scale", [(p, c) for p, c in stops]))
    mappable.set_array([])
    bar = fig.colorbar(mappable, cax=cax, ticks=ticks)
    bar.ax.set_yticklabels(labels)
    bar.ax.tick_params(colors=T.DIM, labelsize=9, length=3, width=0.8)
    bar.outline.set_visible(False)


def load(day, start, end, silence):
    pattern = os.path.join(HERE, "cache", f"{day}_{start}-{end}_*_rooms-s{silence}.json")
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not files:
        raise SystemExit(f"No saved reading matches {pattern}; read the window in app.py first.")
    with open(files[-1], encoding="utf-8") as fh:
        return json.load(fh), files[-1]


def draw(data, first, last, minutes, path):
    """The two bands as one image, cropped to the slots of the window."""
    g = T.grid(data, data["day"], minutes)
    lo, hi = first, last + 1
    who_labels = [label for label, _p in g["rows"]]
    lamp_labels = [name for _k, name, _p in T.LAMPS]
    track, grey, ongoing = _rgb(T.TRACK), _rgb(T.UNSCORED), _rgb(T.ONGOING)
    fewest = T.MIN_EXCHANGES
    most = max([g["who"][label][i] for label in who_labels for i in range(lo, hi)
                if g["drawn"][label][i]] + [fewest + 1])

    who = np.zeros((len(who_labels), hi - lo, 3))
    for r, label in enumerate(who_labels):
        for j, i in enumerate(range(lo, hi)):
            who[r, j] = (_ramp(T.BLUE, (g["who"][label][i] - fewest) / (most - fewest))
                         if g["drawn"][label][i]
                         else ongoing if g["ongoing"][label][i] else track)
    lamps = np.zeros((len(lamp_labels), hi - lo, 3))
    for r, (key, _name, problem) in enumerate(T.LAMPS):
        for j, i in enumerate(range(lo, hi)):
            value = g["lamps"][key][i]
            lamps[r, j] = (_ramp(T.AMBER if problem else T.GREEN, value) if value is not None
                           else grey if g["unscored"][key][i] else track)

    width = max(9.0, (hi - lo) * 0.11 + 2.0)
    fig, axes = plt.subplots(2, 1, figsize=(width, 5.6), facecolor=PAGE,
                             gridspec_kw={"height_ratios": [len(who_labels), len(lamp_labels)],
                                          "hspace": 0.45})
    hours = [(j, g["start"] + g["step"] * i) for j, i in enumerate(range(lo, hi))
             if (g["start"] + g["step"] * i).minute == 0]
    for ax, image, labels, title in (
            (axes[0], who, who_labels, "Who was talking with whom"),
            (axes[1], lamps, lamp_labels, "What the model read in each discussion")):
        ax.imshow(image, aspect="auto", interpolation="nearest")
        ax.set_facecolor(PAGE)
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels, color=T.TEXT, fontsize=10)
        ax.set_yticks(np.arange(0.5, len(labels) - 1, 1), minor=True)
        ax.grid(which="minor", axis="y", color=PAGE, linewidth=3)
        ax.set_xticks([j for j, _t in hours])
        ax.set_xticklabels([f"{t:%H:%M}" for _j, t in hours], color=T.DIM, fontsize=9)
        ax.tick_params(length=0, which="both")
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_title(title, loc="left", color=T.TEXT, fontsize=12, fontweight="semibold")
    # Each colour's scale sits beside the rows it colours -- replies beside the
    # pairs, green beside the four hopes, amber beside the two worries -- each
    # lamp scale marked where the lamp lights.
    ticks = sorted({fewest, round((fewest + most) / 2), most})
    _scale(fig, axes[0], T.BLUE, fewest, most, ticks,
           [f"{v} replies" if v == most else str(v) for v in ticks], 0.0, 1.0)
    hopes = sum(1 for _k, _nm, problem in T.LAMPS if not problem) / len(T.LAMPS)
    _scale(fig, axes[1], T.GREEN, 0.0, 1.0, [0, GREEN, 1], ["0", f"{GREEN:.2f} lit", "1"],
           0.0, hopes - 0.035)
    _scale(fig, axes[1], T.AMBER, 0.0, 1.0, [0, AMBER, 1], ["0", f"{AMBER:.2f} lit", "1"],
           hopes + 0.035, 1.0)
    box = axes[1].get_position()
    fig.legend(handles=[Patch(facecolor=T.ONGOING, edgecolor="none",
                              label=f"conversation goes on, fewer than {fewest} replies in the slot"),
                        Patch(facecolor=T.UNSCORED, edgecolor="none",
                              label="discussion the model did not score")],
               loc="upper left", bbox_to_anchor=(box.x0, box.y0 - 0.07), ncol=2, frameon=False,
               fontsize=9, labelcolor=T.DIM, handlelength=1.4, columnspacing=2.0)
    fig.suptitle(f"{datetime.strptime(data['day'], '%Y-%m-%d'):%a %d-%m-%Y} · "
                 f"{g['start'] + g['step'] * lo:%H:%M}–{g['start'] + g['step'] * hi:%H:%M} · "
                 f"{minutes}-minute slots · a discussion ends after "
                 f"{data.get('silence_minutes', T.SILENCE_MINUTES)} min of silence and is read "
                 f"once, whole · {data.get('model_label', '')}",
                 color=T.DIM, fontsize=10, x=0.01, ha="left")
    fig.savefig(path, dpi=150, facecolor=PAGE, bbox_inches="tight")
    plt.close(fig)


def _score(value):
    return " –  " if value is None else f"{value:.2f}"


def findings(data, first, last, minutes):
    day = data["day"]
    g = T.grid(data, day, minutes)

    def in_window(item):
        i = int((T._parse(item["start"]) - g["start"]).total_seconds() // (minutes * 60))
        return first <= i <= last

    discussions = [d for d in T.discussions_on(data, day) if in_window(d)]
    fragments = [d for d in data.get("discussions") or []
                 if d.get("status") == "not_read" and in_window(d)]
    counts = Counter(d["status"] for d in discussions)
    read = counts["interpretable"] + counts["partially_interpretable"]
    print(f"{len(discussions)} discussions, one model reading each: {read} read, "
          f"{counts['uninterpretable']} judged uninterpretable, "
          f"{counts['not_attempted']} not read (no model), {counts['failed']} failed calls")
    aside = [s for s in data.get("left_out") or [] if in_window(s)]
    overheard = sum(1 for s in aside if s.get("overheard"))
    print(f"{sum(d['turn_count'] for d in discussions)} turns in discussions; left out: "
          f"{len(aside)} lone stretches ({sum(len(s['turns']) for s in aside)} turns, "
          f"{overheard} overheard from another room) and {len(fragments)} discussions too "
          f"short for the model to read")
    print("discussions by room:", ", ".join(
        f"{room} {n}" for room, n in Counter(d.get("room") or "?" for d in discussions).most_common()))
    if discussions:
        lengths = sorted((T._parse(d["end"]) - T._parse(d["start"])).total_seconds() / 60
                         for d in discussions)
        sizes = sorted(d["turn_count"] for d in discussions)
        print(f"length: median {lengths[len(lengths) // 2]:.1f} min, longest {lengths[-1]:.1f} min"
              f" · turns: median {sizes[len(sizes) // 2]}, most {sizes[-1]}")
    def slot(iso):
        return int((T._parse(iso) - g["start"]).total_seconds() // (minutes * 60))

    held = Counter(i for d in discussions
                   for i in range(max(first, slot(d["start"])), min(last, slot(d["end"])) + 1))
    print(f"{len(held)} slots inside a discussion, "
          f"{sum(1 for k in held.values() if k >= 2)} of them holding two or more at once")
    labels, window = [label for label, _p in g["rows"]], range(first, last + 1)
    print("pairs answering each other:", "; ".join(
        f"{label} {sum(g['drawn'][label][i] for i in window)} slots "
        f"({sum(g['who'][label][i] for i in window if g['drawn'][label][i])} replies)"
        for label in labels))
    print(f"slots showing all three pairs: "
          f"{sum(all(g['drawn'][lb][i] for lb in labels) for i in window)} of {len(held)}")

    print("\nDISCUSSIONS  (" + ", ".join(f"{SHORT[k]} = {name.lower()}" for k, name, _p in T.LAMPS)
          + ")")
    for d in discussions:
        usable = d["status"] in T.USABLE
        note = "" if usable else f"  [{T.STATUS.get(d['status'], (d['status'], ''))[0].lower()}]"
        print(f"  {T.span_text(d)}  {T.people(d):<22} {d.get('room') or '?':<10} "
              f"{d['turn_count']:>3} turns{note}")
        if usable:
            dims = d["dimensions"]
            print("      " + "  ".join(f"{SHORT[key]} {_score(dims.get(key))}"
                                       for key, _nm, _p in T.LAMPS)
                  + "   " + ", ".join(d["types"]))

    print("\nKEY MOMENTS")
    for d, lit in T.moments(data, day, GREEN, AMBER, limit=8):
        lamps = ", ".join(f"{name} {value:.2f}" for _k, name, _p, value in
                          sorted(lit, key=lambda item: -item[3]))
        print(f"  {T.span_text(d)}  {T.people(d):<22} {lamps}")
        print(f"      facts: {d.get('room') or '?'} · {T.fact_line(d)}")
        for t in T.facts(d)["quotes"]:
            print(f"      {t['time'][11:19]} {t['speaker']}: «{T._clip(t['text'], 110)}»")
        print(f"      types: {', '.join(d['types'])}")
        if d.get("complaints"):
            print(f"      {T.complaints_line(d)}")
        if d.get("evidence"):
            print(f"      model: {d['evidence'][:220]}")

    if aside:
        print("\nLEFT OUT: one resident alone on a sentinel")
        for s in aside[:12]:
            text = " / ".join(t["text"] for t in s["turns"])[:80]
            mark = "overheard" if s.get("overheard") else "alone    "
            print(f"    {T._parse(s['start']):%H:%M:%S}  {s['speaker']:<8} {s['room'] or '?':<10} "
                  f"{mark} {text}")

    if fragments:
        print("\nLEFT OUT: discussions too short for the model to read")
        for d in fragments:
            said = " / ".join(f"{t['speaker']}: {t['text']}" for t in d["turns"])[:90]
            print(f"    {T.span_text(d, seconds=True)}  {d.get('room') or '?':<10} {said}")

    unread = sorted((d for d in discussions if d["status"] == "uninterpretable" and d["words"] >= 20),
                    key=lambda d: -d["words"])
    print(f"\nUNREAD SPEECH: {len(unread)} discussions of 20+ words judged uninterpretable")
    for d in unread[:5]:
        print(f"    {T.span_text(d)}  {d['words']} words · {(d.get('reason') or '')[:110]}")
    failed = [d for d in discussions if d["status"] == "failed"]
    if failed:
        print(f"\nFAILED CALLS: {len(failed)} (first: {failed[0].get('reason', '')[:120]})")


def _clock(value):
    match = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", (value or "").strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2) or 0) > 59:
        raise SystemExit(f'"{value}" is not a time of day; write it as HH:MM.')
    return int(match.group(1)), int(match.group(2) or 0)


def read(day, start, end, model, fresh, silence):
    """Read the window with the viewer's own reader, saying how far the model has got."""
    os.chdir(os.path.dirname(HERE))
    import app as A

    began, calls = time.monotonic(), [0]
    call_model = A.call_model

    def counted(name, prompt):
        reply = call_model(name, prompt)
        calls[0] += 1
        if calls[0] % 10 == 0:
            print(f"  {calls[0]} discussions read, {time.monotonic() - began:.0f} s")
        return reply

    A.call_model = counted
    data, status = A.read_day(day, start, end, model, fresh, silence)
    if not isinstance(data, dict):
        raise SystemExit(f"Nothing read: {getattr(status, 'children', status)}")
    print(f"{A.summary(data)} · {calls[0]} model calls, {time.monotonic() - began:.0f} s\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("day", help="YYYY-MM-DD")
    parser.add_argument("--from", dest="start", default="00:00", help="HH:MM, default 00:00")
    parser.add_argument("--to", dest="end", default="23:59", help="HH:MM, default 23:59")
    parser.add_argument("--model", help="read the window first unless a reading is saved: "
                                        "chatgpt, none (speech only), or a local Ollama model")
    parser.add_argument("--read-again", action="store_true",
                        help="with --model, read the window even if a reading is saved")
    parser.add_argument("--slot", type=int, default=T.DEFAULT_MINUTES, help="minutes per slot")
    parser.add_argument("--silence", type=int, default=T.SILENCE_MINUTES,
                        help="minutes of silence that end a discussion, default 5")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

    (h0, m0), (h1, m1) = _clock(args.start), _clock(args.end)
    if args.model:
        read(args.day, f"{h0:02d}:{m0:02d}", f"{h1:02d}:{m1:02d}", args.model,
             args.read_again, args.silence)
    data, path = load(args.day, f"{h0:02d}{m0:02d}", f"{h1:02d}{m1:02d}", args.silence)
    first = (h0 * 60 + m0) // args.slot
    last = (h1 * 60 + m1) // args.slot
    print(f"reading: {os.path.basename(path)} ({data.get('model_label')}, read "
          f"{data.get('read_at', '')[:16].replace('T', ' ')})\n")
    findings(data, first, last, args.slot)
    picture = os.path.splitext(path)[0] + ".png"
    draw(data, first, last, args.slot, picture)
    print(f"\npicture: {picture}")


if __name__ == "__main__":
    main()
