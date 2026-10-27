"""What the residents said about the incidents the system listed.

    python -m graphrag.judgements                      # the count so far
    python -m graphrag.judgements --since 2026-10-01

An incident on the list is a claim -- "Ilyas and Rafael quarrelled from 20:11
to 20:18" -- and the only people who can say whether it is true were there.
The report page shows the list with two buttons on every row; this module is
where the answers are kept, and where they are counted.

THE RECORD IS A LOG

Every answer appends one line to common_files/incident_judgements.jsonl and no
line is ever rewritten. The last line about an incident is its judgement, and a
line with no verdict withdraws one. So a changed mind is on file as a changed
mind, and a crash half way through a write can cost one line and never the
record.

Each line carries the incident AS IT WAS SHOWN -- kind, span, who, about --
and which reading of the day it came from. It has to. Incident ids number a
day's list in order, so reading a day again (`ingest --add-incidents`) hands
the same ids to different incidents. A judgement is therefore tied to the day,
the reading and the id together: a day that is read again starts unjudged,
while what was said about the old list stays on file and still says what it
was said about.

It lives beside the household memory and not in graphrag/store/, because the
store can be rebuilt from the database and this cannot be rebuilt from
anything.

WHAT THE COUNT IS, AND IS NOT

confirmed / (confirmed + rejected) is the PRECISION of the list: of what it
claimed and somebody judged, how much had happened. It says nothing of what
the list missed. That needs the residents' own note of what happened in the
house, which nothing here collects.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta

from .engine import Graph
from .engine import incidents as incidents_op

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
JUDGEMENTS_PATH = os.path.join(PROJECT_ROOT, "common_files",
                               "incident_judgements.jsonl")

VERDICTS = ("confirmed", "rejected")

# What of an incident is copied into its judgement: enough to know what was
# being judged after the list it stood in has been replaced.
_KEPT = ("id", "day", "kind", "start", "end", "participants", "about", "what",
         "rooms", "span_method")

_writing = threading.Lock()      # the page answers clicks on several threads


# ── the key ─────────────────────────────────────────────────────────

def key_of(day: str, read_at: str, incident_id: str) -> str:
    """One incident of one reading of one day. See THE RECORD IS A LOG."""
    return f"{day}|{read_at}|{incident_id}"


def split_key(key: str) -> tuple[str, str, str] | None:
    parts = str(key or "").split("|", 2)
    return tuple(parts) if len(parts) == 3 and all(parts) else None


# ── the log ─────────────────────────────────────────────────────────

def record(row: dict, reading: dict, verdict: str | None,
           path: str | None = None, now: datetime | None = None,
           source: str = "report page") -> dict:
    """Append one answer. `verdict` None withdraws the one that stands."""
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f"not a verdict: {verdict!r}")
    line = {
        "judged_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "verdict": verdict,
        "key": key_of(row["day"], str(reading.get("read_at") or ""), row["id"]),
        "incident": {name: row.get(name) for name in _KEPT},
        "reading": {name: reading.get(name)
                    for name in ("read_at", "model", "household_memory_lines")},
        "source": source,
    }
    path = path or JUDGEMENTS_PATH
    with _writing:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def standing(path: str | None = None) -> dict[str, dict]:
    """key -> the judgement that stands for it. Withdrawn ones are absent.

    A line that cannot be read is passed over: one damaged line must not
    hide every answer given before it.
    """
    out: dict[str, dict] = {}
    try:
        with open(path or JUDGEMENTS_PATH, encoding="utf-8") as fh:
            for text in fh:
                try:
                    line = json.loads(text)
                    key = line["key"]
                except (ValueError, KeyError, TypeError):
                    continue
                if line.get("verdict") in VERDICTS:
                    out[key] = line
                else:
                    out.pop(key, None)
    except OSError:
        pass
    return out


# ── the list for a window ───────────────────────────────────────────

def _days_between(start: datetime, end: datetime) -> list[str]:
    span = (end.date() - start.date()).days
    return [(start.date() + timedelta(days=n)).isoformat()
            for n in range(max(0, span) + 1)]


def _load(days: list[str]) -> Graph | None:
    try:
        return Graph.load(days=days)
    except FileNotFoundError:            # no store has been built at all
        return None


def listed_between(start: datetime, end: datetime, graph: Graph | None = None,
                   path: str | None = None) -> dict:
    """The incidents stored for a window, each with what was said about it.

    Read from the store, never read afresh: two readings of one day do not
    list the same incidents, and a list that changed between two clicks could
    not be judged at all.

    A day with no list is reported as such and never as a quiet day. Two
    reasons are told apart, because they are mended differently: the day was
    never ingested, or it was ingested without an incident reader.
    """
    days = _days_between(start, end)
    g = graph or _load(days)
    if g is None:
        return {"days": days, "read": {}, "not_listed": [], "not_stored": days,
                "rows": [], "tally": tally([])}

    held = standing(path)
    lo = start.strftime("%Y-%m-%dT%H:%M:%S")
    hi = end.strftime("%Y-%m-%dT%H:%M:%S")
    rows = []
    for row in incidents_op(g, top_k=10 ** 6)["incidents"]:      # all, in time order
        if row["day"] not in days:
            continue
        if f"{row['day']}T{row['end']}" < lo or f"{row['day']}T{row['start']}" > hi:
            continue
        reading = g.incident_layer.get(row["day"]) or {}
        key = key_of(row["day"], str(reading.get("read_at") or ""), row["id"])
        said = sorted(g.said_in(row["id"]), key=lambda s: s.get("timestamp") or "")
        judged = held.get(key)
        rows.append({
            **row, "key": key,
            "verdict": judged["verdict"] if judged else None,
            "judged_at": judged["judged_at"] if judged else None,
            "lines": [{"time": str(s.get("timestamp") or "")[11:19],
                       "speaker": s.get("speaker"), "text": s.get("text"),
                       "room": s.get("room")} for s in said],
        })

    read = {d: g.incident_layer[d] for d in days if d in g.incident_layer}
    stored = {d for d in days if f"day::{d}" in g.nodes}
    return {
        "days": days,
        "read": read,
        "not_listed": [d for d in days if d in stored and d not in read],
        "not_stored": [d for d in days if d not in stored],
        "rows": rows,
        "tally": tally(rows),
    }


def tally(rows: list[dict]) -> dict:
    counts = Counter(r.get("verdict") for r in rows)
    return {"listed": len(rows), "confirmed": counts["confirmed"],
            "rejected": counts["rejected"],
            "unjudged": len(rows) - counts["confirmed"] - counts["rejected"]}


# ── answering ───────────────────────────────────────────────────────

def judge(key: str, verdict: str | None, path: str | None = None,
          graph: Graph | None = None, now: datetime | None = None) -> bool:
    """Set the verdict on the incident a key names; None withdraws it.

    The caller says what the verdict is to BE, not "flip it": a page left open
    in a second window shows an older state, and a click there must do what
    its button said. Returns False, writing nothing, when that verdict already
    stands.

    Raises LookupError when the key no longer names an incident in the store:
    the day was read again after the page was drawn, and an answer about a
    list that has been replaced must not be filed against its successor.
    """
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f"not a verdict: {verdict!r}")
    parts = split_key(key)
    if not parts:
        raise LookupError("not an incident key")
    day, read_at, incident_id = parts
    g = graph or _load([day])
    reading = (g.incident_layer.get(day) if g else None) or {}
    row = None
    if g and str(reading.get("read_at") or "") == read_at:
        row = next((r for r in incidents_op(g, day=day, top_k=10 ** 6)["incidents"]
                    if r["id"] == incident_id), None)
    if row is None:
        raise LookupError(f"{day} has been read again since this list was shown")

    if (standing(path).get(key) or {}).get("verdict") == verdict:
        return False
    record(row, reading, verdict, path=path, now=now)
    return True


# ── counting ────────────────────────────────────────────────────────

def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """A 95% interval for a proportion that behaves at small n and at 0 or 1."""
    if not n:
        return None
    p = hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def summarise(path: str | None = None, since: str | None = None,
              until: str | None = None) -> dict:
    """Standing judgements counted by kind, over the incidents' own days."""
    by_kind: dict[str, Counter] = {}
    days, readings = set(), set()
    for line in standing(path).values():
        incident = line.get("incident") or {}
        day = str(incident.get("day") or "")
        if (since and day < since) or (until and day > until):
            continue
        days.add(day)
        readings.add((day, (line.get("reading") or {}).get("read_at")))
        by_kind.setdefault(incident.get("kind") or "?", Counter())[line["verdict"]] += 1
    total = Counter()
    for counts in by_kind.values():
        total.update(counts)
    return {"by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
            "confirmed": total["confirmed"], "rejected": total["rejected"],
            "days": sorted(days), "readings": len(readings)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=None, help="first incident day, YYYY-MM-DD")
    ap.add_argument("--until", default=None, help="last incident day, YYYY-MM-DD")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                        # pragma: no cover
        pass

    s = summarise(since=args.since, until=args.until)
    judged = s["confirmed"] + s["rejected"]
    if not judged:
        print("No incident has been judged yet. They are judged on the report "
              "page, under the lamps.")
        return 0

    def line(name, confirmed, rejected):
        n = confirmed + rejected
        low, high = wilson(confirmed, n)
        return (f"  {name:<16}{confirmed:>10}{rejected:>10}{n:>8}"
                f"{confirmed / n:>11.0%}   {low:.0%} to {high:.0%}")

    print(f"Incidents judged by the residents: {judged}, listed on "
          f"{len(s['days'])} day(s) ({s['days'][0]} .. {s['days'][-1]}), "
          f"from {s['readings']} reading(s).\n")
    print(f"  {'kind':<16}{'confirmed':>10}{'rejected':>10}{'judged':>8}"
          f"{'precision':>11}   95% interval")
    for kind, counts in s["by_kind"].items():
        print(line(kind, counts.get("confirmed", 0), counts.get("rejected", 0)))
    print(line("all", s["confirmed"], s["rejected"]))
    print("\nPrecision only: of what the list claimed and somebody judged, how "
          "much had happened.\nWhat the list missed is not in this count.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
