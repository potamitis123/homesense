"""The incident layer: what happened between people, and when.

    python -m graphrag.ingest --day 2026-10-02 --add-incidents --incident-model gpt-4o-mini

An INCIDENT is something that happened between the residents -- a quarrel, a
complaint, a worry voiced, a making-up -- with who was in it, what it was
about, and the first and last recorded line that show it. It is called an
incident and not an event because in this codebase an event is a recorded clip.

WHY THIS LAYER EXISTS

Level 1.5 reads each exchange once and writes one line about it: four scores,
some labels, one sentence. An exchange can run for twenty minutes and cover
five subjects, and only its own start and end are known. On 2 October 2026 a
resident complained about her workplace inside a 24-minute, 1,188-word exchange
that was read as "neutral conversation"; the complaint could not be retrieved
by any operation, with either a local model or GPT-4o doing the asking.

Two remedies were tried on that whole day:

  * reading every five-minute window WITH the question, at ask time. It found
    the complaint and the day's quarrel, and it is not usable: it said yes to
    "quarrel" in 34 of 66 windows, and reported talk of a dentist three times on
    a day in which the word never occurs. A question pulls the reader to yes.
  * listing incidents at ingest, with NO question, then choosing from the list.
    It found both, returned nothing for the dentist, and brought one and four
    other incidents along where the first brought eight and twenty.

This module is the second. The reading is done once, at ingest, like every
other reading in this package; a question then selects from a list.

START AND END

The list gives a good start and too short an end -- the quarrel came back as
one minute long. A second pass settles the span, and it has to depend on the
kind, because the rule that is right for one is wrong for the other:

  * a QUARREL is followed into the neighbouring windows for as long as the
    conflict between the same people goes on (20:11:24-20:12:24 became
    20:11:24-20:18:13; the residents put its end between 20:15 and 20:19);
  * a COMPLAINT is one person on one subject inside a longer conversation, so
    the ten minutes either side are re-read and its first and last line marked
    (one line at 15:26:52 became 15:22:09-15:27:24; the residents said about
    15:21 to 15:28). Followed like a quarrel, it swallowed the unrelated
    complaint before it and the talk after it and came out 21 minutes long.

Each was stable over three runs, on one example of each kind. A WORRY is
treated as a complaint, being the same shape, though that is untested; the
other kinds keep the span the list gave them. `span_method` records which
happened to every incident, so none of this has to be taken on trust.

Times are those of recorded lines. Recording is triggered by sound, so the
true start can be earlier and the true end later than what is written.

The model is passed IN as a callable, as the Level 1.5 interpreter is, so
nothing here opens a connection and the tests run without one.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .schema import INCIDENT_KINDS

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MEMORY_PATH = os.path.join(PROJECT_ROOT, "common_files", "household_memory.txt")

# How each kind's span is settled after the list is made. See the docstring.
FOLLOWED = {"quarrel"}
DELIMITED = {"complaint", "worry"}

WINDOW_SECONDS = 300          # what the reader is given at a time
DELIMIT_MARGIN = 600          # how far either side a complaint is re-read
FOLLOW_LIMIT = 4              # windows a quarrel may be followed, each way
LAYER_VERSION = 1


# ── the household memory ────────────────────────────────────────────

# What the reader of a day is given from the memory: who the residents are and
# what "work" and "school" mean for each of them. An entry is named by its
# label, the words before the colon, as the template writes them.
#
# Everything else a resident fills in is kept from the reader, on evidence. One
# day, 2026-10-02, was read with four versions of the same filled-in memory:
#
#   these entries only, 12 lines     17 to 22 incidents, and both incidents the
#                                    residents confirmed, in 5 readings of 5
#   with ages, hours, friends,       17 to 20 incidents, and the confirmed work
#     visitors, 43 and 46 lines      complaint in 0 readings of 6
#   with "recurring worries" too,    35 to 39 incidents in 3 readings, among
#     all 49 lines                   them the memory copied out: one resident's
#                                    line of recurring worries listed, in its
#                                    own words, as a worry voiced in ninety
#                                    seconds of a quarrel over something else
#
# A line that says what tends to happen here is a question put to the reader,
# and a reader asked a question finds its answer (WHY THIS LAYER EXISTS, above).
# Why plain background loses the complaint is not known; it was measured, not
# explained. Admitting another entry to this list is a new experiment.
READER_ENTRIES = ("who lives here", "language at home", "work, and where",
                  "her role there", "his role there", 'what "work" means',
                  "school")


def _named(entry: str, labels) -> bool:
    label = entry.split(":", 1)[0].strip().lower()
    return any(label == name or label.startswith(name + " ") for name in labels)


def load_household_memory(path: str | None = None, only=None) -> str:
    """What the residents wrote down about themselves, ready for a prompt.

    Only the lines somebody filled in. An entry still holding its blank, a
    note line and a section left with nothing under it are all dropped: an
    empty "Work, and where: ____" tells a model nothing and invites it to
    guess. Returns "" when there is no file, which every caller treats as
    having no memory.

    `only` keeps the entries whose label is one of those given, and with them
    the headings they stand under.
    """
    try:
        with open(path or MEMORY_PATH, encoding="utf-8") as fh:
            raw = fh.read().splitlines()
    except OSError:
        return ""

    sections, current = [], None
    for line in raw:
        text = line.rstrip()
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        if not text.startswith((" ", "\t")):          # a section heading
            current = [text.strip(), []]
            sections.append(current)
            continue
        if "____" in text or current is None:
            continue
        # The hint in brackets after an entry is advice to the person filling
        # it in. Dropped only when it trails several spaces, as the template
        # writes it, so a bracket in someone's own answer survives.
        entry = re.sub(r"\s{3,}\(.*\)\s*$", "", text.strip())
        entry = re.sub(r"\s{2,}", " ", entry)
        if only is None or _named(entry, only):
            current[1].append(entry)
    kept = [f"{head}\n" + "\n".join(f"  {e}" for e in entries)
            for head, entries in sections if entries]
    return "\n".join(kept)


def reader_memory(path: str | None = None) -> str:
    """The part of the memory given to the reader of a day: READER_ENTRIES."""
    return load_household_memory(path, only=READER_ENTRIES)


def memory_block(memory: str) -> str:
    if not memory.strip():
        return ""
    return ("WHAT IS KNOWN ABOUT THIS HOUSEHOLD (written by the residents):\n"
            f"{memory.strip()}\n\n")


# ── prompts ─────────────────────────────────────────────────────────
#
# These are the texts that were measured. Rewording one is a new experiment.

HEAD = (
    "You are reading speech recorded in a home: a few minutes of utterances, "
    "written as speech recognition output in Greek with many recognition "
    "errors. One sentence can appear more than once, because several "
    "microphones heard it. Use only what is written in the lines.\n\n")

EXTRACT = (
    "List every notable event between people in these lines. Kinds of event:\n"
    "  quarrel         people here in conflict with each other\n"
    "  complaint       someone voices a grievance; say about what or whom\n"
    "  worry           someone voices worry or distress; say about what\n"
    "  reconciliation  making up after a conflict\n"
    "  warm_moment     affection, praise, support, shared laughter\n"
    "  rebuff          someone's approach is turned away\n"
    "Ordinary conversation is not an event. Return an empty list when nothing "
    "of these kinds happens, or when the text is too garbled to tell.\n\n"
    "Return JSON only:\n"
    "{\"events\": [{\"kind\": one of the six kinds,\n"
    "   \"who\": [speakers involved, as named on the lines],\n"
    "   \"about\": \"short English phrase: what or whom it concerns\",\n"
    "   \"first_time\": \"HH:MM:SS of the first line that shows it\",\n"
    "   \"last_time\": \"HH:MM:SS of the last line that shows it\",\n"
    "   \"what\": \"one short English sentence\"}]}\n\n"
    "LINES:\n")

# What each kind means, in the reader's own words: the same phrases as in
# EXTRACT above, and a test holds the two together. The diary form shows these
# to the resident, so that an incident the reader missed is one that fits the
# definition the reader was given, and not one the two understood differently.
KIND_DEFINITIONS = {
    "quarrel": "people here in conflict with each other",
    "complaint": "someone voices a grievance",
    "worry": "someone voices worry or distress",
    "reconciliation": "making up after a conflict",
    "warm_moment": "affection, praise, support, shared laughter",
    "rebuff": "someone's approach is turned away",
}

DELIMIT = (
    "An event was found somewhere in these lines: {what} (involving {who}).\n"
    "Find where it begins and where it ends. It may begin earlier, or go on "
    "longer, than the lines that show it most clearly; include the lead-up "
    "only if it is part of the same event.\n\n"
    "Return JSON only: {{\"first_time\": \"HH:MM:SS of the first line that "
    "belongs to it\", \"last_time\": \"HH:MM:SS of the last line that belongs "
    "to it\"}}\n\nLINES:\n")

FOLLOW = (
    "An event was found {where} these lines: {what} (involving {who}).\n"
    "Do these lines belong to that same event? They do only if the conflict "
    "between the same people is {sense}, even if what they argue about "
    "shifts.\n\n"
    "Return JSON only: {{\"continues\": \"yes\" | \"no\", \"{edge}_time\": "
    "\"HH:MM:SS of the {edge} line that still belongs to it\" or null}}\n\n"
    "LINES:\n")


# ── lines and windows ───────────────────────────────────────────────

def _clock(segment: dict) -> str:
    return str(segment.get("timestamp") or "")[11:19]


def _secs(clock: str) -> int:
    h, m, s = (int(x) for x in clock.split(":"))
    return h * 3600 + m * 60 + s


def lines_between(segments: list[dict], lo: int, hi: int) -> list[tuple]:
    """(clock, line, segment) for utterances with lo <= time < hi, in order.

    House-wide, not per room: one conversation moves between rooms and is
    heard by several sentinels at once. An utterance the same speaker repeats
    word for word is shown once, which removes the copies two microphones
    make without touching what was actually said twice in different words.
    """
    seen, out = set(), []
    for seg in sorted(segments, key=lambda s: s.get("timestamp") or ""):
        clock = _clock(seg)
        if len(clock) != 8 or not lo <= _secs(clock) < hi:
            continue
        mark = (seg.get("speaker"), (seg.get("text") or "")[:40])
        if mark in seen:
            continue
        seen.add(mark)
        out.append((clock, f"{clock} {seg.get('speaker')}: {seg.get('text')}", seg))
    return out


def windows_for(segments: list[dict]) -> list[dict]:
    """The day cut into five-minute windows, each with its lines."""
    by_bin = defaultdict(list)
    for seg in segments:
        clock = _clock(seg)
        if len(clock) == 8:
            by_bin[_secs(clock) // WINDOW_SECONDS].append(seg)
    out = []
    for index in sorted(by_bin):
        lo = index * WINDOW_SECONDS
        rows = lines_between(by_bin[index], lo, lo + WINDOW_SECONDS)
        if rows:
            out.append({"lo": lo, "times": [c for c, _l, _s in rows],
                        "lines": [line for _c, line, _s in rows],
                        "speakers": {s.get("speaker") for _c, _l, s in rows}})
    return out


def _on_the_lines(value, times) -> str | None:
    """A time the model gave, kept only if a line really carries it.

    A model asked for a time will always produce one. Checking it against the
    lines is what stops an incident being placed at a moment nothing was
    recorded.
    """
    if isinstance(value, str) and value[:8] in times:
        return value[:8]
    return None


# ── reading ─────────────────────────────────────────────────────────

def _many(reader, prompts: list[str], workers: int) -> list:
    if workers <= 1 or len(prompts) <= 1:
        return [reader(p) for p in prompts]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(reader, prompts))


def list_incidents(segments: list[dict], reader, memory: str = "",
                   workers: int = 1) -> tuple[list[dict], dict]:
    """Every incident in a day's utterances, as the reader lists them.

    `reader` takes a prompt and returns the decoded JSON object, or None. A
    window whose reading failed is counted and reported, never read as a quiet
    five minutes.
    """
    windows = windows_for(segments)
    prompts = [HEAD + memory_block(memory) + EXTRACT + "\n".join(w["lines"])
               for w in windows]
    replies = _many(reader, prompts, workers)
    found, failed = [], 0
    for window, reply in zip(windows, replies):
        if not isinstance(reply, dict):
            failed += 1
            continue
        for item in reply.get("events") or []:
            if not isinstance(item, dict) or item.get("kind") not in INCIDENT_KINDS:
                continue
            first = _on_the_lines(item.get("first_time"), window["times"]) \
                or window["times"][0]
            last = _on_the_lines(item.get("last_time"), window["times"]) \
                or window["times"][-1]
            if _secs(last) < _secs(first):
                first, last = last, first
            # Only people who are on the lines. A name from nowhere is the
            # same mistake as a time from nowhere.
            who = [p for p in (item.get("who") or [])
                   if isinstance(p, str) and p in window["speakers"]]
            found.append({
                "kind": item["kind"], "who": sorted(set(who)),
                "about": str(item.get("about") or "").strip() or None,
                "what": str(item.get("what") or "").strip() or None,
                "first": first, "last": last,
                "seen_first": first, "seen_last": last,
                "span_method": "listed",
            })
    return found, {"windows": len(windows), "windows_failed": failed}


def _delimit(incident: dict, segments: list[dict], reader, memory: str) -> bool:
    """Re-read the minutes either side and mark the first and last line.

    Returns False only when the reader did not answer. An answer that names
    times no line carries is an answer, and leaves the span as it was listed.
    """
    rows = lines_between(segments,
                         _secs(incident["first"]) - DELIMIT_MARGIN,
                         _secs(incident["last"]) + DELIMIT_MARGIN + 1)
    if not rows:
        return True
    times = [c for c, _l, _s in rows]
    reply = reader(HEAD + memory_block(memory) + DELIMIT.format(
        what=incident["what"] or incident["kind"],
        who="+".join(incident["who"]) or "the people speaking")
        + "\n".join(line for _c, line, _s in rows))
    if not isinstance(reply, dict):
        return False
    first = _on_the_lines(reply.get("first_time"), times)
    last = _on_the_lines(reply.get("last_time"), times)
    if first and last and _secs(first) <= _secs(last):
        incident.update(first=first, last=last, span_method="delimited")
    return True


def _follow(incident: dict, segments: list[dict], reader, memory: str) -> bool:
    """Follow a quarrel into the windows after it and before it.

    Returns False when the reader failed to answer at any step. The span is
    then put back exactly as it was listed: a quarrel followed half way is a
    span nobody can account for, and "followed" must mean the pass finished.
    """
    who = "+".join(incident["who"]) or "the people speaking"
    what = incident["what"] or incident["kind"]

    def step(rows, where, sense, edge):
        """'yes' with the edge time, 'no', or None for a reader that failed."""
        reply = reader(HEAD + memory_block(memory) + FOLLOW.format(
            where=where, what=what, who=who, sense=sense, edge=edge)
            + "\n".join(line for _c, line, _s in rows))
        if not isinstance(reply, dict):
            return None, None
        if str(reply.get("continues") or "").lower() != "yes":
            return "no", None
        return "yes", _on_the_lines(reply.get(f"{edge}_time"),
                                    [c for c, _l, _s in rows])

    answered = True
    lo = _secs(incident["last"]) // WINDOW_SECONDS * WINDOW_SECONDS + WINDOW_SECONDS
    for _ in range(FOLLOW_LIMIT):
        rows = lines_between(segments, lo, lo + WINDOW_SECONDS)
        if not rows:
            break
        verdict, time = step(rows, "just before", "still going on", "last")
        if verdict is None:
            answered = False
        if verdict != "yes":
            break
        incident["last"] = time or rows[-1][0]
        lo += WINDOW_SECONDS

    hi = _secs(incident["first"]) // WINDOW_SECONDS * WINDOW_SECONDS
    for _ in range(FOLLOW_LIMIT if answered else 0):
        rows = lines_between(segments, hi - WINDOW_SECONDS, hi)
        if not rows:
            break
        verdict, time = step(rows, "just after", "already under way", "first")
        if verdict is None:
            answered = False
        if verdict != "yes":
            break
        incident["first"] = time or rows[0][0]
        hi -= WINDOW_SECONDS

    if answered:
        incident["span_method"] = "followed"
    else:
        incident.update(first=incident["seen_first"], last=incident["seen_last"],
                        span_method="listed")
    return answered


def settle_spans(found: list[dict], segments: list[dict], reader,
                 memory: str = "", workers: int = 1) -> int:
    """Give each incident its start and end, by the rule for its kind.

    Returns how many could not be settled because the reader did not answer.
    Those keep the span they were listed with, and are counted so that a day
    read through a failing connection cannot pass for a day read properly.
    """
    def settle(incident):
        if incident["kind"] in FOLLOWED:
            return _follow(incident, segments, reader, memory)
        if incident["kind"] in DELIMITED:
            return _delimit(incident, segments, reader, memory)
        return True

    if workers <= 1 or len(found) <= 1:
        done = [settle(incident) for incident in found]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = list(pool.map(settle, found))
    return sum(1 for ok in done if not ok)


# ── the layer in a day ──────────────────────────────────────────────

def strip_layer(day: dict) -> None:
    """Remove an existing incident layer, so adding one twice replaces it."""
    gone = {n["id"] for n in day.get("nodes", []) if n.get("kind") == "incident"}
    if gone:
        day["nodes"] = [n for n in day["nodes"] if n["id"] not in gone]
        day["edges"] = [e for e in day["edges"]
                        if e.get("src") not in gone and e.get("dst") not in gone]
    day.pop("incident_layer", None)


def add_incident_layer(day: dict, reader, memory: str = "", model: str = "",
                       workers: int = 1, quiet: bool = True) -> dict:
    """Read a built day for incidents and write them into it.

    Works on the day as stored -- its utterance nodes -- so a day built this
    morning and a day built last month go through the same code, and adding
    the layer to an old day changes nothing else in it. Only utterances
    attributed to a resident are in a day, so only those are read.
    """
    strip_layer(day)
    date = day["date"]
    segments = [n for n in day.get("nodes", []) if n.get("kind") == "segment"]
    found, stats = list_incidents(segments, reader, memory, workers)
    unsettled = settle_spans(found, segments, reader, memory, workers)
    found.sort(key=lambda i: (i["first"], i["last"]))

    for number, incident in enumerate(found, 1):
        inside = [seg for _c, _l, seg in lines_between(
            segments, _secs(incident["first"]), _secs(incident["last"]) + 1)]
        # The words it rests on: the participants' lines inside the span, or
        # everybody's when the reader named nobody.
        said = [s for s in inside if s.get("speaker") in incident["who"]] or inside
        node_id = f"incident::{date}::{number:04d}"
        day["nodes"].append({
            "id": node_id, "kind": "incident", "type": incident["kind"],
            "start": f"{date}T{incident['first']}",
            "end": f"{date}T{incident['last']}",
            "participants": incident["who"],
            "about": incident["about"], "what": incident["what"],
            "rooms": sorted({s.get("room") for s in said if s.get("room")}),
            "span_method": incident["span_method"],
            "listed_start": f"{date}T{incident['seen_first']}",
            "listed_end": f"{date}T{incident['seen_last']}",
            "source_event_ids": sorted({str(s.get("event_id")) for s in said})[:12],
        })
        day["edges"].append({"src": node_id, "dst": "day::" + date, "kind": "on_day"})
        for person in incident["who"]:
            day["edges"].append({"src": "person::" + person, "dst": node_id,
                                 "kind": "participated"})
        for seg in said:
            day["edges"].append({"src": node_id, "dst": seg["id"],
                                 "kind": "evidenced_by"})

    day["incident_layer"] = {
        "version": LAYER_VERSION,
        "read_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "household_memory": bool(memory.strip()),
        # The list depends on what the reader was told, so how much it was
        # told is kept with the list.
        "household_memory_lines": len(memory.strip().splitlines()),
        "windows": stats["windows"],
        "windows_failed": stats["windows_failed"],
        "incidents": len(found),
        "spans_unsettled": unsettled,
    }
    if not quiet:
        print(f"  {date}: {len(found)} incident(s) from {stats['windows']} "
              f"window(s)"
              + (f", {stats['windows_failed']} window(s) UNREAD"
                 if stats["windows_failed"] else "")
              + (f", {unsettled} span(s) left as listed because the reader "
                 f"did not answer" if unsettled else ""))
    return day["incident_layer"]
