"""Blind labelling: the residents tag stretches of a day without seeing the list.

    python -m graphrag.blind                      # recall and precision so far

Confirm and Reject measure whether what the system listed had happened. They
cannot measure what it MISSED, and a diary kept by a resident did not work as
the remedy: nobody is present for everything and nobody takes notes all day.

So the question is put the other way round. A day is cut into the same
five-minute windows the reader was given. The resident is shown some of them
-- the transcript, and the recordings to listen to -- with NO label, and says
what happened in each: one or more of the six kinds, or nothing. Shown are

  * every window in which the system listed something, and
  * a random sample of the windows in which it listed nothing,

mixed together, so a window being shown says nothing about what the system
thought of it. The first group gives precision without the resident having
read the system's answer first. The second gives RECALL: an incident tagged
in a window the system passed over is a miss, and because those windows are a
random sample, each one found stands for several not shown.

WHAT IS ESTIMATED, AND HOW

Per kind, at the level of a window: the window HAS the kind if the resident
tagged it, and the system FOUND it if an incident of that kind overlaps the
window. A window the system flagged counts once; a sampled quiet window counts
for N/n quiet windows of its day, N being how many there were and n how many
were shown. Recall is weighted hits over weighted windows that have the kind.
Intervals are from resampling the labelled windows within each day and group.

The draw for a day is made once, written down, and never changed, and the day
is sealed when its list is revealed. Like the judgements, the log is
append-only.

WHAT THIS CANNOT SEE

What was never recorded, or was recorded with no resident's name on it, is in
no window. This is the recall of the reading, over what was heard.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
from collections import defaultdict
from datetime import datetime

from .engine import DAYS_DIR
from .incidents import WINDOW_SECONDS, windows_for
from .schema import INCIDENT_KINDS

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LABELS_PATH = os.path.join(PROJECT_ROOT, "common_files", "incident_blind_labels.jsonl")

# Quiet windows shown per day. With five to eight flagged ones this is a
# sitting of about a quarter of an hour.
QUIET_SHOWN = 8

_writing = threading.Lock()


def _secs(clock: str) -> int:
    h, m, s = (int(x) for x in clock[:8].split(":"))
    return h * 3600 + m * 60 + s


def clock_of(lo: int) -> str:
    return f"{lo // 3600:02d}:{lo % 3600 // 60:02d}"


# ── the day as stored ───────────────────────────────────────────────

def load_day(day: str) -> dict | None:
    try:
        with open(os.path.join(DAYS_DIR, f"{day}.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def day_windows(stored: dict) -> list[dict]:
    """Every five-minute window of a stored day, with what the system found in it."""
    nodes = stored.get("nodes", [])
    segments = [n for n in nodes if n.get("kind") == "segment"]
    incidents = [n for n in nodes if n.get("kind") == "incident"]
    files = defaultdict(list)
    for seg in sorted(segments, key=lambda s: s.get("timestamp") or ""):
        clock = str(seg.get("timestamp") or "")[11:19]
        if len(clock) == 8 and seg.get("audio_file"):
            name = seg["audio_file"]
            bucket = files[_secs(clock) // WINDOW_SECONDS * WINDOW_SECONDS]
            if name not in bucket:
                bucket.append(name)
    out = []
    for window in windows_for(segments):
        lo, hi = window["lo"], window["lo"] + WINDOW_SECONDS
        found = sorted({i["type"] for i in incidents
                        if _secs(i["start"][11:19]) < hi and _secs(i["end"][11:19]) >= lo})
        out.append({"lo": lo, "lines": window["lines"], "system": found,
                    "audio": files.get(lo, [])})
    return out


# ── the log ─────────────────────────────────────────────────────────

def _append(line: dict, path: str | None, now: datetime | None = None) -> dict:
    line = {"at": (now or datetime.now()).isoformat(timespec="seconds"), **line}
    path = path or LABELS_PATH
    with _writing:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def _lines(path: str | None) -> list[dict]:
    out = []
    try:
        with open(path or LABELS_PATH, encoding="utf-8") as fh:
            for text in fh:
                try:
                    line = json.loads(text)
                    line["day"], line["do"]
                except (ValueError, KeyError, TypeError):
                    continue
                out.append(line)
    except OSError:
        pass
    return out


def state(day: str, path: str | None = None) -> dict:
    """{"draw": the windows shown or None, "labels": {lo: [kinds]}, "sealed_at"}."""
    draw, labels, sealed = None, {}, None
    for line in _lines(path):
        if line["day"] != day:
            continue
        if line["do"] == "draw" and not sealed:
            draw, labels = line, {}          # a later draw is a later reading
        elif line["do"] == "label" and draw and not sealed:
            labels[int(line["window"])] = list(line.get("kinds") or [])
        elif line["do"] == "seal" and draw and not sealed:
            sealed = line["at"]
    return {"draw": draw, "labels": labels, "sealed_at": sealed}


def draw(day: str, stored: dict | None = None, path: str | None = None,
         now: datetime | None = None, quiet: int = QUIET_SHOWN) -> dict | None:
    """The windows to show for a day: made once, then read back from the log.

    Made again only if the day has been read again since -- a new list has new
    flagged windows -- and never once the day is sealed.
    """
    stored = stored or load_day(day)
    if not stored or not stored.get("incident_layer"):
        return None
    read_at = stored["incident_layer"].get("read_at")
    have = state(day, path)
    if have["draw"] and (have["sealed_at"] or have["draw"].get("read_at") == read_at):
        return have["draw"]

    windows = day_windows(stored)
    flagged = [w["lo"] for w in windows if w["system"]]
    silent = [w["lo"] for w in windows if not w["system"]]
    rng = random.Random(f"{day}|{read_at}")
    shown_quiet = sorted(rng.sample(silent, min(quiet, len(silent))))
    order = flagged + shown_quiet
    rng.shuffle(order)                      # so position says nothing either
    return _append({"day": day, "do": "draw", "read_at": read_at,
                    "windows": order, "flagged": flagged, "quiet_shown": shown_quiet,
                    "quiet_total": len(silent)}, path, now)


def label(day: str, window: int, kinds, path: str | None = None,
          now: datetime | None = None) -> None:
    """What the resident says happened in a window. [] is "nothing"."""
    kinds = sorted(set(kinds or []))
    bad = [k for k in kinds if k not in INCIDENT_KINDS]
    if bad:
        raise ValueError(f"not a kind of incident: {bad[0]!r}")
    have = state(day, path)
    if not have["draw"] or int(window) not in have["draw"]["windows"]:
        raise ValueError("that window is not one being shown for this day")
    if have["sealed_at"]:
        raise ValueError("the list for that day has been shown; its labels are closed")
    _append({"day": day, "do": "label", "window": int(window), "kinds": kinds}, path, now)


def toggle(day: str, window: int, kind: str, path: str | None = None) -> None:
    """Add or take off one kind; "nothing" clears the window to an explicit none."""
    current = state(day, path)["labels"].get(int(window), [])
    if kind == "nothing":
        label(day, window, [], path)
    elif kind in current:
        label(day, window, [k for k in current if k != kind], path)
    else:
        label(day, window, current + [kind], path)


def unlabelled(day: str, path: str | None = None) -> list[int]:
    have = state(day, path)
    return [w for w in (have["draw"] or {}).get("windows", []) if w not in have["labels"]]


def seal(day: str, path: str | None = None, now: datetime | None = None) -> None:
    have = state(day, path)
    if not have["draw"] or have["sealed_at"]:
        return
    if unlabelled(day, path):
        raise ValueError("some windows have no answer yet")
    _append({"day": day, "do": "seal"}, path, now)


# ── scoring ─────────────────────────────────────────────────────────

def labelled_windows(path: str | None = None, days_dir_loader=load_day) -> list[dict]:
    """Every labelled window of every sealed day, with its weight."""
    out = []
    for day in sorted({l["day"] for l in _lines(path)}):
        have = state(day, path)
        stored = days_dir_loader(day)
        if not have["sealed_at"] or not stored:
            continue
        if (stored.get("incident_layer") or {}).get("read_at") != have["draw"].get("read_at"):
            continue                        # read again since: another list
        system = {w["lo"]: w["system"] for w in day_windows(stored)}
        shown = len(have["draw"]["quiet_shown"]) or 1
        for lo in have["draw"]["windows"]:
            flagged = lo in have["draw"]["flagged"]
            out.append({"day": day, "lo": lo, "flagged": flagged,
                        "weight": 1.0 if flagged else have["draw"]["quiet_total"] / shown,
                        "human": have["labels"].get(lo, []),
                        "system": system.get(lo, [])})
    return out


def _rates(rows: list[dict], kind: str | None) -> dict:
    def has(row, who):
        return bool(row[who]) if kind is None else kind in row[who]
    tp = sum(r["weight"] for r in rows if has(r, "human") and has(r, "system"))
    fn = sum(r["weight"] for r in rows if has(r, "human") and not has(r, "system"))
    fp = sum(r["weight"] for r in rows if not has(r, "human") and has(r, "system"))
    return {"tp": tp, "fn": fn, "fp": fp,
            "recall": tp / (tp + fn) if tp + fn else None,
            "precision": tp / (tp + fp) if tp + fp else None,
            "tagged": sum(1 for r in rows if has(r, "human"))}


def score(rows: list[dict], resamples: int = 2000, seed: int = 7) -> dict:
    """Recall and precision per kind, with intervals from resampling windows."""
    rng = random.Random(seed)
    groups = defaultdict(list)
    for row in rows:
        groups[(row["day"], row["flagged"])].append(row)
    out = {}
    for kind in [None] + sorted(INCIDENT_KINDS):
        point = _rates(rows, kind)
        draws = {"recall": [], "precision": []}
        for _ in range(resamples if rows else 0):
            again = [rng.choice(g) for g in groups.values() for _ in g]
            got = _rates(again, kind)
            for name in draws:
                if got[name] is not None:
                    draws[name].append(got[name])
        for name, values in draws.items():
            values.sort()
            point[name + "_interval"] = (
                (values[int(.025 * len(values))], values[min(len(values) - 1,
                                                             int(.975 * len(values)))])
                if len(values) >= 100 else None)
        out[kind or "any"] = point
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                        # pragma: no cover
        pass
    rows = labelled_windows()
    if not rows:
        print("No day has been labelled blind and then revealed yet.")
        return 0
    days = sorted({r["day"] for r in rows})
    quiet = [r for r in rows if not r["flagged"]]
    print(f"{len(rows)} windows labelled blind on {len(days)} day(s) "
          f"({days[0]} .. {days[-1]}): {len(rows) - len(quiet)} the system flagged, "
          f"{len(quiet)} sampled from those it did not; "
          f"{sum(1 for r in quiet if r['human'])} of the sampled ones were tagged.\n")

    def pct(value, interval):
        if value is None:
            return f"{'-':>22}"
        span = f"({interval[0]:.0%}-{interval[1]:.0%})" if interval else ""
        return f"{value:>7.0%} {span:<14}"

    print(f"  {'kind':<16}{'windows tagged':>15}   {'recall':<22} {'precision':<22}")
    for kind, s in score(rows).items():
        if s["tagged"] or s["fp"]:
            print(f"  {kind:<16}{s['tagged']:>15}   "
                  f"{pct(s['recall'], s['recall_interval'])} "
                  f"{pct(s['precision'], s['precision_interval'])}")
    print("\nPer window: it HAS a kind if the resident tagged it, and the system "
          "FOUND it if an incident of\nthat kind overlaps it. A sampled quiet "
          "window stands for all the quiet windows of its day.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
