"""What the residents say happened, written down BEFORE they see the list.

    python -m graphrag.diary                     # recall so far
    python -m graphrag.diary --margin 5

Confirm and Reject (judgements.py) measure precision: of what the system
listed, how much had happened. They cannot measure what it MISSED, because
nobody judges an incident that was never shown. This is the other half. Before
a day's list is shown, the report page asks what happened that day -- kind,
rough start and end, who -- and only when the resident says "that is all" is
the list revealed.

THE ORDER IS THE POINT

A diary written after reading the list is the list again. So a day is SEALED
when its list is revealed: entries for it can no longer be added or removed,
and the seal is in the log with its time, so the order can be shown and not
just asserted. "Nothing happened" is a seal with no entries, which is how a
quiet day is told from a day nobody filled in.

The log is append-only, like the judgements, and lives beside them.

HOW AN ENTRY IS MATCHED

A diary entry is FOUND when the system listed an incident of the same kind on
the same day whose span comes within MARGIN minutes of the entry's. Both sets
of times are approximate -- the residents' from memory, the system's from the
first and last recorded line -- so the margin is deliberate, five minutes by
default. Recall is entries found over entries written. A second figure ignores
the kind, because a resident's "worry" and the reader's "complaint" may be one
event under two names, and the difference between the two figures says how
much of the missing is naming.

Only days sealed before any of their incidents was judged are counted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import Counter
from datetime import datetime

from .judgements import standing, wilson
from .schema import INCIDENT_KINDS

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIARY_PATH = os.path.join(PROJECT_ROOT, "common_files", "incident_diary.jsonl")
MARGIN_MINUTES = 5

_writing = threading.Lock()


# ── the log ─────────────────────────────────────────────────────────

def _append(line: dict, path: str | None, now: datetime | None) -> dict:
    line = {"at": (now or datetime.now()).isoformat(timespec="seconds"), **line}
    path = path or DIARY_PATH
    with _writing:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def _lines(path: str | None) -> list[dict]:
    out = []
    try:
        with open(path or DIARY_PATH, encoding="utf-8") as fh:
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


def day_state(day: str, path: str | None = None) -> dict:
    """{"entries": [...], "sealed_at": iso or None} for one day."""
    entries, sealed = {}, None
    for line in _lines(path):
        if line["day"] != day:
            continue
        if line["do"] == "add" and not sealed:
            entries[line["id"]] = line
        elif line["do"] == "remove" and not sealed:
            entries.pop(line.get("id"), None)
        elif line["do"] == "seal" and not sealed:
            sealed = line["at"]
    return {"entries": sorted(entries.values(), key=lambda e: (e["start"], e["id"])),
            "sealed_at": sealed}


def _clock(value) -> str:
    """HH:MM from HH:MM or HH:MM:SS, or ValueError."""
    return datetime.strptime(str(value)[:5], "%H:%M").strftime("%H:%M")


def add(day: str, kind: str, start, end=None, who=(), note: str = "",
        path: str | None = None, now: datetime | None = None) -> dict:
    datetime.strptime(day, "%Y-%m-%d")
    if kind not in INCIDENT_KINDS:
        raise ValueError(f"not a kind of incident: {kind!r}")
    start = _clock(start)
    end = _clock(end) if end else start
    if end < start:
        raise ValueError("it ends before it starts")
    state = day_state(day, path)
    if state["sealed_at"]:
        raise ValueError("the list for that day has been shown; its diary is closed")
    entry_id = f"{day}#{len(_lines(path)) + 1}"
    return _append({"day": day, "do": "add", "id": entry_id, "kind": kind,
                    "start": start, "end": end, "who": sorted(who or []),
                    "note": (note or "").strip()}, path, now)


def remove(day: str, entry_id: str, path: str | None = None,
           now: datetime | None = None) -> None:
    if day_state(day, path)["sealed_at"]:
        raise ValueError("the list for that day has been shown; its diary is closed")
    _append({"day": day, "do": "remove", "id": entry_id}, path, now)


def seal(day: str, path: str | None = None, now: datetime | None = None,
         judged_path: str | None = None) -> dict:
    """Close a day's diary. Records whether the list had already been judged."""
    state = day_state(day, path)
    if state["sealed_at"]:
        return state
    already = sum(1 for l in standing(judged_path).values()
                  if (l.get("incident") or {}).get("day") == day)
    _append({"day": day, "do": "seal", "entries": len(state["entries"]),
             "judged_before_seal": already}, path, now)
    return day_state(day, path)


# ── recall ──────────────────────────────────────────────────────────

def _minutes(clock: str) -> int:
    return int(clock[:2]) * 60 + int(clock[3:5])


def within(entry: dict, incident: dict, margin: int = MARGIN_MINUTES) -> bool:
    """The two spans overlap once each is allowed `margin` minutes of error."""
    return (_minutes(entry["start"]) - margin <= _minutes(incident["end"])
            and _minutes(incident["start"]) - margin <= _minutes(entry["end"]))


def blind_days(path: str | None = None) -> list[str]:
    """Days sealed before any of their incidents had been judged."""
    days = []
    for line in _lines(path):
        if line["do"] == "seal" and not line.get("judged_before_seal") \
                and line["day"] not in days:
            days.append(line["day"])
    return sorted(days)


def recall(listed: dict[str, list[dict]], path: str | None = None,
           margin: int = MARGIN_MINUTES) -> dict:
    """Diary entries the system found. `listed` is day -> incident rows."""
    rows, quiet = [], 0
    for day in blind_days(path):
        if day not in listed:           # sealed, but the day was never read
            continue
        entries = day_state(day, path)["entries"]
        quiet += not entries
        for entry in entries:
            near = [i for i in listed[day] if within(entry, i, margin)]
            rows.append({**entry,
                         "found": any(i["kind"] == entry["kind"] for i in near),
                         "found_any_kind": bool(near),
                         "matched": [i["id"] for i in near]})
    by_kind: dict[str, Counter] = {}
    for row in rows:
        counts = by_kind.setdefault(row["kind"], Counter())
        counts["written"] += 1
        counts["found"] += row["found"]
        counts["found_any_kind"] += row["found_any_kind"]
    return {"days": [d for d in blind_days(path) if d in listed], "quiet_days": quiet,
            "entries": rows, "margin": margin,
            "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
            "written": len(rows), "found": sum(r["found"] for r in rows),
            "found_any_kind": sum(r["found_any_kind"] for r in rows)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--margin", type=int, default=MARGIN_MINUTES,
                    help="minutes of error allowed on each span (default 5)")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                        # pragma: no cover
        pass
    from .engine import Graph
    from .engine import incidents as incidents_op

    days = blind_days()
    listed = {}
    if days:
        try:
            g = Graph.load(days=days)
            for day in days:
                if day in g.incident_layer:
                    listed[day] = incidents_op(g, day=day, top_k=10 ** 6)["incidents"]
        except FileNotFoundError:
            pass
    r = recall(listed, margin=args.margin)
    if not r["days"]:
        print("No day has a diary written before its list was shown, so there "
              "is no recall to report yet.")
        return 0

    def show(name, found, n, either):
        if not n:
            return
        low, high = wilson(found, n)
        print(f"  {name:<16}{n:>8}{found:>7}{found / n:>9.0%}   {low:.0%} to {high:.0%}"
              f"{either:>12}")

    print(f"Diary entries on {len(r['days'])} day(s) ({r['days'][0]} .. {r['days'][-1]}), "
          f"{r['quiet_days']} of them with nothing to report; spans matched "
          f"within {r['margin']} min.\n")
    print(f"  {'kind':<16}{'written':>8}{'found':>7}{'recall':>9}   95% interval"
          f"{'any kind':>11}")
    for kind, c in r["by_kind"].items():
        show(kind, c.get("found", 0), c["written"], c.get("found_any_kind", 0))
    show("all", r["found"], r["written"], r["found_any_kind"])
    missed = [e for e in r["entries"] if not e["found_any_kind"]]
    if missed:
        print("\nWritten in the diary, and nothing listed near it:")
        for e in missed:
            print(f"  {e['day']} {e['start']}-{e['end']} {e['kind']} "
                  f"{'+'.join(e['who'])} {e['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
