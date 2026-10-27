"""Discussions through the day, and what the model read in each one.

People do not talk to themselves, and they talk where they are. A discussion
happens in a room and that room's sentinel hears it; people may then move to
another room and talk there, perhaps with someone else, and that room's
sentinel hears the next discussion. So:

  discussion    what one sentinel heard, from the first voice until a silence
                longer than SILENCE_MINUTES, with at least two residents in it.
                Everyone that sentinel heard in the span took part: Voula
                speaks at 12:00, Ilyas answers her for five minutes, and that
                is one Ilyas-Voula discussion.
  left out      speech a sentinel heard from one resident only, with nobody
                else on that sentinel within the silence. It is never read and
                never drawn. Often it is the loudest lines of a discussion next
                door, and those stretches are counted as overheard. A discussion
                too short for the model to read is left out of the picture too.

Discussions are never joined across rooms. Joining those that other sentinels
heard at the same time was tried on 14-09-2026 11:59-19:00 and dropped: of 86
pairs that overlapped on different sentinels, 44 shared not one line and none
shared 70% of its lines, yet joining the pairs with two residents in common
chained 50 discussions into 12, one 56 minutes long across five rooms. A line
two sentinels both heard stays in both rooms' discussions and is read by both;
only the picture counts it once.

A pair is drawn where its two people answer each other: a cell counts the
lines in which one of the two directly follows the other inside a discussion,
and is drawn from MIN_EXCHANGES such replies in the slot, with the pair's
conversation shown as going on between its drawn cells. One line from a third
resident does not make a pair. Drawing every pair of a discussion's residents
instead lit all three rows in 39 of the 46 slots with talk on 14-09-2026
11:59-19:00 -- an Ilyas-Rafael conversation of 31 replies became three couples
because Voula said one line -- and counting replies leaves 5.

The lamps belong to discussions, not to sentences. The model reads each
discussion once, whole -- every line in it and nothing from before or after
it -- and that one reading is drawn across the discussion's entire span. A slot
inside a quarrel is lit because the discussion was a quarrel, not because of
whatever sentence fell in that slot.

Standalone: used by conversation_day/app.py, imported by nothing in the
dashboard. It calls the pipeline's own functions -- turns, copy matching, the
L1.5 payload and the normalisation of the model's reply -- and changes none.
"""
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from html import escape as _escape
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (ROOT, os.path.join(ROOT, "ast_dash", "llm_report")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import dash_bootstrap_components as dbc                            # noqa: E402
import plotly.graph_objects as go                                  # noqa: E402
from dash import html                                              # noqa: E402
from plotly.subplots import make_subplots                          # noqa: E402

import derive_home_dynamics as H                                   # noqa: E402

DEFAULT_MINUTES = 5          # slot width in the picture
SILENCE_MINUTES = 5          # a silence longer than this ends a discussion
MIN_EXCHANGES = 2            # replies between two residents in a slot before the pair is drawn
QUOTE_MIN_WORDS = 4          # a shorter line is never quoted as someone's longest
USABLE = ("interpretable", "partially_interpretable")

# (dimension key, lamp name, names a problem), in the Part B panel's order.
# `connection` and `inclusion` were withdrawn as dimensions during the
# evaluation of 2026-09: connection could not vary, because a discussion
# reaches this panel only when two residents spoke in it, and inclusion was
# read two different ways by two careful readers. Connection is now reported
# at the daily level as a reciprocity rate instead.
LAMPS = (("warmth", "Warmth", False), ("joy", "Joy", False),
         ("withdrawal", "Withdrawal", True), ("tension", "Tension", True))

# The Part B panel's ground and lamp inks, so a lit cell reads as that lamp
# lit: green for the qualities it hopes to see, amber for the ones it watches
# for. Each ramp is a single hue rising from a dim step -- dim but
# visible, so a score of 0 stays distinct from an empty slot -- to the lamp.
NIGHT, TRACK, TEXT, DIM = "#131A22", "#1A232D", "#E7EDF3", "#93A3B2"
ONGOING = "#1B2B3D"
BLUE = [[0.0, "#1F4266"], [0.45, "#2A78D6"], [1.0, "#9EC5F4"]]
GREEN = [[0.0, "#1D4A34"], [0.5, "#35A56C"], [1.0, "#9BE8BE"]]
AMBER = [[0.0, "#4A3312"], [0.5, "#DB8E27"], [1.0, "#FFD49A"]]
UNSCORED = "#3A4652"

STATUS = {
    "interpretable": ("Read by the model", "success"),
    "partially_interpretable": ("Partly read by the model", "info"),
    "uninterpretable": ("Model judged it uninterpretable", "warning"),
    "not_attempted": ("Not read: no model chosen", "secondary"),
    "not_read": ("Never sent to the model", "secondary"),
    "failed": ("The model call failed", "danger"),
}


# ── discussions ────────────────────────────────────────────────────────
def _end_of(turns):
    return max(t.end or t.start for t in turns)


def interactions(level0, cfg, silence_minutes=SILENCE_MINUTES):
    """What each sentinel heard, cut wherever it heard nothing for too long.

    Every copy is used, not the pipeline's merged turns: a sentinel is the
    witness here, and removing a line because another sentinel heard it better
    would take a participant out of the room that heard them.
    """
    members = set(cfg.get("household_members") or {})
    by_device = defaultdict(list)
    for turn in H._collect_turns(level0, cfg, merge=False):
        if (turn.text or "").strip():
            by_device[turn.device or "?"].append(turn)
    gap = silence_minutes * 60
    stretches = []
    for device, turns in by_device.items():
        turns.sort(key=lambda t: (t.start, str(t.event_id)))
        current, end = [turns[0]], turns[0].end or turns[0].start
        for turn in turns[1:]:
            if (turn.start - end).total_seconds() > gap:
                stretches.append((device, current))
                current, end = [turn], turn.end or turn.start
            else:
                current.append(turn)
                end = max(end, turn.end or turn.start)
        stretches.append((device, current))
    out = []
    for device, turns in stretches:
        rooms = Counter(t.room for t in turns if t.room)
        out.append({"device": device, "room": rooms.most_common(1)[0][0] if rooms else None,
                    "turns": turns, "start": turns[0].start, "end": _end_of(turns),
                    "residents": {t.speaker for t in turns if t.speaker in members}})
    return sorted(out, key=lambda s: s["start"])


def anchor(level0, cfg, silence_minutes=SILENCE_MINUTES):
    """(discussions, left_out): each sentinel's discussions, and the lone speech left out.

    A discussion never leaves its room: no line another sentinel heard is ever
    added to it.
    """
    stretches = interactions(level0, cfg, silence_minutes)
    discussions = [{"sentinel": s["device"], "room": s["room"], "turns": s["turns"],
                    "residents": s["residents"], "start": s["start"], "end": s["end"]}
                   for s in stretches if len(s["residents"]) >= 2]
    left_out = [s for s in stretches if len(s["residents"]) < 2]
    spoken = [t for d in discussions for t in d["turns"]]

    # A line two sentinels heard is in both rooms' discussions, and each reads
    # it; the picture counts it once, where it was heard best.
    _kept, pairs = H.merge_cross_sentinel_turns(spoken, cfg)
    elsewhere = {id(dropped) for dropped, _copy in pairs}
    for discussion in discussions:
        discussion["heard_elsewhere"] = {id(t) for t in discussion["turns"] if id(t) in elsewhere}

    # Lone speech is left out either way, but a reviewer should know how much
    # of it is a discussion next door heard through the wall.
    in_discussion = {id(t) for t in spoken}
    _kept, pairs = H.merge_cross_sentinel_turns(
        spoken + [t for s in left_out for t in s["turns"]], cfg)
    overheard = {id(b) if id(a) in in_discussion else id(a)
                 for a, b in pairs if (id(a) in in_discussion) != (id(b) in in_discussion)}
    for stretch in left_out:
        stretch["overheard"] = sum(id(t) in overheard for t in stretch["turns"])
    return discussions, left_out


def _turn_dict(turn, heard_elsewhere=False):
    return {"time": H._iso(turn.start), "speaker": turn.speaker or "unidentified",
            "text": (turn.text or "").strip(), "device": turn.device, "room": turn.room,
            "heard_elsewhere": heard_elsewhere}


def left_out_dicts(stretches):
    return [{"start": H._iso(s["start"]), "end": H._iso(s["end"]), "device": s["device"],
             "room": s["room"], "speaker": next(iter(s["residents"]), None) or "unidentified",
             "overheard": s.get("overheard", 0),
             "turns": [_turn_dict(t) for t in s["turns"]]} for s in stretches]


# ── reading ────────────────────────────────────────────────────────────
def _no_reading(status, reason, context_hash=None):
    return {"status": status, "confidence": None, "types": [], "complaints": [],
            "dimensions": {key: None for key, _nm, _p in LAMPS}, "evidence": None,
            "reason": reason, "uncertainty": [], "context_hash": context_hash}


def read_discussions(level0, cfg, discussions, interpreter=None, failures=None):
    """Each discussion read once, whole, as JSON-ready dicts.

    The model is handed a discussion exactly as the LLM tab hands it an
    exchange -- the same payload, the same normalisation of its reply -- except
    that the exchange is the entire discussion: every line of every resident in
    it, all of them named as present, and no context from outside it, so the
    reading is of this discussion and nothing said before or after colours it.
    """
    thresholds = cfg.get("thresholds", {})
    min_words = int(thresholds.get("semantic_min_words", 8))
    min_words_exchange = int(thresholds.get("semantic_min_words_exchange", 4))
    min_turns_exchange = int(thresholds.get("semantic_min_turns_for_exchange", 2))
    failures = failures if failures is not None else {}
    out = []
    for number, discussion in enumerate(discussions):
        turns = discussion["turns"]
        start, end = discussion["start"], discussion["end"]
        voices = {t.speaker for t in turns if t.speaker}
        words = sum(H._turn_words(t) for t in turns)
        window = {"cluster_index": number, "start": start, "end": end,
                  "room": discussion["room"], "turns": turns,
                  "context_before": [], "context_after": [],
                  "named_speakers": sorted(discussion["residents"]),
                  "distinct_speakers": sorted(voices), "word_count": words,
                  "source_event_ids": sorted({t.event_id for t in turns}, key=str)}
        window["sounds"] = H.nearby_sounds(level0, window, cfg)

        readable = words >= min_words or (len(turns) >= min_turns_exchange
                                          and len(voices) >= 2 and words >= min_words_exchange)
        if not readable:
            reading = _no_reading("not_read", f"Not sent to the model: {words} word(s), too "
                                              f"little speech to pass the intelligibility gate.")
        elif interpreter is None:
            reading = _no_reading("not_attempted", "No model was chosen.")
        else:
            payload = H.window_payload(window)
            window["context_hash"] = H.window_hash(payload)
            try:
                reply = interpreter(payload)
            except Exception as exc:                                # noqa: BLE001
                failures.setdefault(window["context_hash"], str(exc)[:200])
                reply = None
            error = failures.get(window["context_hash"])
            if error:
                reading = _no_reading("failed", f"The model call failed: {error}",
                                      window["context_hash"])
            else:
                record = H.normalise_semantic_reply(reply, window, cfg)
                reading = {"status": record.semantic_status,
                           "confidence": record.semantic_confidence,
                           "types": list(record.interaction_types),
                           "complaints": [dict(c) for c in record.complaints],
                           "dimensions": dict(record.relational_dimensions),
                           "evidence": record.evidence, "reason": record.reason,
                           "uncertainty": list(record.uncertainty),
                           "context_hash": window["context_hash"]}

        elsewhere = discussion.get("heard_elsewhere") or set()
        out.append({
            "id": f"d{number}", "date": f"{start:%Y-%m-%d}",
            "start": H._iso(start), "end": H._iso(end),
            "participants": sorted(discussion["residents"]),
            "sentinel": discussion["sentinel"], "room": discussion["room"],
            "turn_count": len(turns), "words": words,
            "turns": [_turn_dict(t, id(t) in elsewhere) for t in turns],
            "sounds": [{"time": H._iso(s["time"]), "label": s["label"],
                        "confidence": s["confidence"]} for s in window["sounds"]],
            **reading,
        })
    return out


# ── helpers for the picture ────────────────────────────────────────────
def _parse(value):
    return datetime.fromisoformat(str(value)) if value else None


def _plural(n, word, many=None):
    return f"{n} {word if n == 1 else (many or word + 's')}"


def residents_of(data):
    names = data.get("residents") or {
        p for d in data.get("discussions") or [] for p in d.get("participants") or []}
    return sorted(names)


def pair_rows(residents):
    return [(f"{a} – {b}", frozenset((a, b))) for a, b in combinations(residents, 2)]


def people(discussion):
    return " – ".join(discussion.get("participants") or []) or "Unidentified voices"


def span_text(item, seconds=False):
    fmt = "%H:%M:%S" if seconds else "%H:%M"
    begins = _parse(item["start"])
    ends = _parse(item.get("end")) or begins
    return f"{begins.strftime(fmt)}–{ends.strftime(fmt)}"


def discussions_on(data, day):
    """The day's discussions that belong in the picture.

    A discussion too short for the model to read is left out, as lone speech
    is: with nothing read and no back-and-forth to draw, it could only put a
    grey mark under an empty slot. On 14-09-2026 the one that did was a single
    "Συγγνώμη" that one clip had split between two speaker names.
    """
    start = datetime.strptime(day, "%Y-%m-%d")
    end = start + timedelta(days=1)
    return [d for d in data.get("discussions") or []
            if d.get("status") != "not_read"
            and _parse(d["start"]) < end and (_parse(d.get("end")) or _parse(d["start"])) >= start]


def find(data, discussion_id):
    return next((d for d in data.get("discussions") or [] if d.get("id") == discussion_id), None)


def exchanges(discussion):
    """(pair, line) for every line in which a resident answers another resident.

    A reply is a line that directly follows a line by a different resident
    inside the same discussion. A line another room heard better is not counted
    as a reply here -- that room's discussion counts it -- so a conversation two
    sentinels heard is not counted twice. A transcription loop counts once.
    """
    members = set(discussion.get("participants") or [])
    _first, repeats = _loops(discussion.get("turns") or [])
    lines = [t for t in discussion.get("turns") or []
             if t.get("speaker") in members and id(t) not in repeats]
    return [(frozenset((before["speaker"], line["speaker"])), line)
            for before, line in zip(lines, lines[1:])
            if before["speaker"] != line["speaker"] and not line.get("heard_elsewhere")]


# ── facts ──────────────────────────────────────────────────────────────
def _words(text):
    return len((text or "").split())


def _clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _length(minutes):
    return f"{minutes * 60:.0f} s" if minutes < 1 else f"{minutes:.0f} min"


def _loops(turns):
    """The pipeline's own test for a transcription loop, on the viewer's turns."""
    return H.repeated_lines(turns, speaker_of=lambda t: t.get("speaker"),
                            text_of=lambda t: t.get("text"), time_of=lambda t: _parse(t["time"]))


def complaint_text(complaint):
    """One complaint in words: who complained, and where it pointed."""
    about, who = complaint.get("about"), complaint.get("who")
    if about == "someone_present":
        target = who or "someone taking part"
    elif about == "family_absent":
        target = f"{who}, who wasn't there" if who else "a family member who wasn't there"
    elif about == "outsider":
        target = "someone outside the family"
    else:
        target = "a situation, not a person"
    return f"{complaint.get('by')} about {target}"


def provenance(data):
    """Which reader produced a saved reading, in words a reviewer can check."""
    if not data or data.get("model") in (None, "none"):
        return "No model reading: speech only."
    read_at = _parse(data.get("read_at"))
    return (f"Level 1.5 reading by {data.get('model_label')} · prompt "
            f"{data.get('prompt_file') or 'LLM_prompt_L1_5.txt'}, version "
            f"{str(data.get('prompt_sha256') or '')[:8]} · read "
            f"{read_at:%d-%m-%Y %H:%M} · the whole discussion in one call, without context"
            if read_at else f"Level 1.5 reading by {data.get('model_label')}")


def facts(discussion):
    """What was measured in a discussion: no model, nothing inferred.

    lines         lines per resident, most first
    replies       lines in which a resident answered another (see exchanges)
    answered      ((who answered, whom), count), most first
    minutes       from the first line to the end of the last
    quiet         the resident who stopped first, when the others went on for
                  at least three more lines: who, since, others_lines, minutes
    quotes        each resident's longest line, in time order
    elsewhere     lines another room's sentinel heard too
    unidentified  lines from voices matched to no resident
    loops         transcription loops: a short line repeated within seconds,
                  counted once everywhere above -- speaker, text, count, time
    """
    turns = discussion.get("turns") or []
    members = list(discussion.get("participants") or [])
    loop_first, loop_repeats = _loops(turns)
    said = [t for t in turns if t.get("speaker") in members and id(t) not in loop_repeats]
    counts = Counter(t["speaker"] for t in said)
    order = sorted(members, key=lambda p: (-counts[p], p))
    answered = Counter((line["speaker"], next(iter(pair - {line["speaker"]})))
                       for pair, line in exchanges(discussion))
    start = _parse(discussion["start"])
    end = _parse(discussion.get("end")) or start

    quiet = None
    last = {p: max((_parse(t["time"]) for t in said if t["speaker"] == p), default=None)
            for p in members}
    if len(members) >= 2 and all(last.values()):
        first = min(members, key=lambda p: last[p])
        after = [t for t in said if t["speaker"] != first and _parse(t["time"]) > last[first]]
        if len(after) >= 3:
            quiet = {"who": first, "since": last[first], "others_lines": len(after),
                     "minutes": (_parse(after[-1]["time"]) - last[first]).total_seconds() / 60}

    quotes = []
    for person in members:
        own = [t for t in said if t["speaker"] == person and _words(t["text"]) >= QUOTE_MIN_WORDS]
        if own:
            quotes.append(max(own, key=lambda t: _words(t["text"])))
    return {"order": order, "lines": {p: counts[p] for p in order},
            "replies": sum(answered.values()), "answered": answered.most_common(),
            "minutes": (end - start).total_seconds() / 60, "quiet": quiet,
            "quotes": sorted(quotes, key=lambda t: t["time"]),
            "elsewhere": sum(1 for t in turns if t.get("heard_elsewhere")),
            "unidentified": sum(1 for t in turns
                                if t.get("speaker") not in members and id(t) not in loop_repeats),
            "loops": [{"speaker": t.get("speaker"), "text": t.get("text"),
                       "count": loop_first[id(t)], "time": t["time"]}
                      for t in turns if id(t) in loop_first]}


def _fact_bits(measured):
    bits = [" · ".join(f"{p} {n}" for p, n in measured["lines"].items()) + " lines",
            _plural(measured["replies"], "reply", "replies")]
    if measured["quiet"]:
        bits.append(f"no line from {measured['quiet']['who']} after "
                    f"{measured['quiet']['since']:%H:%M}")
    return bits


def fact_line(discussion, measured=None):
    """A discussion's measured facts in one line."""
    return " · ".join(_fact_bits(measured or facts(discussion)))


def fact_card(discussion, voice=None):
    """The measured facts of one discussion, as the top of its detail window."""
    measured = facts(discussion)
    tiles = [("Length", _length(measured["minutes"]), span_text(discussion, seconds=True)),
             ("Lines", " · ".join(f"{p} {n}" for p, n in measured["lines"].items()),
              f"{_plural(measured['unidentified'], 'line')} from unidentified voices"
              if measured["unidentified"] else ""),
             ("Replies", str(measured["replies"]),
              ", ".join(f"{a} answered {b} {n}" for (a, b), n in measured["answered"][:3]))]
    loud = voice(discussion) if voice else None
    if loud:
        tiles.append(("Voice", f"{loud['median']:+.0f} dB",
                      f"loudest clip {loud['loudest']:+.0f} dB · against {loud['usual']:.0f} dB, "
                      f"this sentinel's usual that day · {_plural(loud['clips'], 'clip')}"))
    body = [html.Div([html.Div([html.Div(name, className="ct-fact-name"),
                                html.Div(value, className="ct-fact-value"),
                                html.Div(detail, className="ct-fact-detail")], className="ct-fact")
                      for name, value, detail in tiles], className="ct-facts")]
    if measured["quiet"]:
        quiet = measured["quiet"]
        others = " and ".join(p for p in measured["order"] if p != quiet["who"])
        body.append(html.Div(
            f"After {quiet['who']}'s last line at {quiet['since']:%H:%M:%S}, {others} spoke "
            f"{_plural(quiet['others_lines'], 'more line')} over {_length(quiet['minutes'])}.",
            className="ct-fact-note"))
    if measured["quotes"]:
        body.append(html.Div("Longest line from each", className="ct-fact-name mt-2"))
        body += [html.Div([html.Span(f"{_parse(t['time']):%H:%M:%S}", className="ct-quote-time"),
                           html.Span(t["speaker"], className="ct-quote-who"),
                           html.Span(f"«{_clip(t['text'], 240)}»")], className="ct-quote")
                 for t in measured["quotes"]]
    if measured["elsewhere"]:
        body.append(html.Div(f"{_plural(measured['elsewhere'], 'line')} also heard in another "
                             f"room (dimmed in the transcript below)",
                             className="ct-muted small mt-1"))
    for loop in measured["loops"]:
        body.append(html.Div(
            f"Transcription loop: «{_clip(loop['text'], 60)}» came {loop['count']} times from "
            f"{loop['speaker']} within seconds; counted once here, and given to the model once.",
            className="ct-muted small mt-1"))
    return html.Div(body, className="ct-factcard")


def grid(data, day, minutes=DEFAULT_MINUTES):
    """The day cut into slots, and what each row holds in each slot.

    who[label][i]       replies between the pair's two people in the slot
    replies[label][i]   who answered, as a Counter of those replies
    drawn[label][i]     at least MIN_EXCHANGES replies: the two are talking
    ongoing[label][i]   between two drawn slots of one of their discussions, fewer replies
    pair_ids[label][i]  the discussions behind a drawn or ongoing pair cell
    lamps[key][i]       the strongest reading among the discussions spanning the slot
    lamp_ids[key][i]    the discussions scored for that lamp, spanning the slot
    unscored[key][i]    discussions spanning the slot with no score for that lamp

    Lamps are filled from discussion readings only. Every slot a discussion
    spans carries its one reading, whatever was said in that slot, and no slot
    is lit by speech that belongs to no discussion.
    """
    minutes = max(1, int(minutes))
    start = datetime.strptime(day, "%Y-%m-%d")
    n = (24 * 60) // minutes
    rows = pair_rows(residents_of(data))
    label_of = {pair: label for label, pair in rows}
    g = {"day": day, "minutes": minutes, "n": n, "start": start,
         "step": timedelta(minutes=minutes), "rows": rows,
         "who": {label: [0] * n for label, _p in rows},
         "replies": {label: [None] * n for label, _p in rows},
         "drawn": {label: [False] * n for label, _p in rows},
         "ongoing": {label: [False] * n for label, _p in rows},
         "pair_ids": {label: [[] for _ in range(n)] for label, _p in rows},
         "lamps": {key: [None] * n for key, _nm, _p in LAMPS},
         "lamp_ids": {key: [[] for _ in range(n)] for key, _nm, _p in LAMPS},
         "unscored": {key: [[] for _ in range(n)] for key, _nm, _p in LAMPS},
         "discussions": {}, "facts": {}}

    def slot(moment):
        return int((moment - start).total_seconds() // (minutes * 60))

    talked = []                    # (discussion id, pair label, replies per slot)
    for discussion in discussions_on(data, day):
        g["discussions"][discussion["id"]] = discussion
        g["facts"][discussion["id"]] = facts(discussion)
        d_start = _parse(discussion["start"])
        d_end = _parse(discussion.get("end")) or d_start
        span = range(max(0, slot(d_start)), min(n - 1, slot(d_end)) + 1)
        per_pair = defaultdict(Counter)
        for pair, line in exchanges(discussion):
            label, i = label_of.get(pair), slot(_parse(line["time"]))
            if label is None or not 0 <= i < n:
                continue
            per_pair[label][i] += 1
            g["who"][label][i] += 1
            if g["replies"][label][i] is None:
                g["replies"][label][i] = Counter()
            g["replies"][label][i][line["speaker"]] += 1
        talked += [(discussion["id"], label, counts) for label, counts in per_pair.items()]
        usable = discussion.get("status") in USABLE
        for key, _nm, _p in LAMPS:
            value = (discussion.get("dimensions") or {}).get(key) if usable else None
            for i in span:
                if value is None:
                    g["unscored"][key][i].append(discussion["id"])
                else:
                    held = g["lamps"][key][i]
                    g["lamps"][key][i] = value if held is None else max(held, value)
                    g["lamp_ids"][key][i].append(discussion["id"])

    for label, _pair in rows:
        g["drawn"][label] = [count >= MIN_EXCHANGES for count in g["who"][label]]
    for d_id, label, counts in talked:
        shown = [i for i in counts if g["drawn"][label][i]]
        if not shown:
            continue
        for i in range(min(shown), max(shown) + 1):
            g["pair_ids"][label][i].append(d_id)
            if not g["drawn"][label][i]:
                g["ongoing"][label][i] = True
    return g


def slot_label(g, i):
    begins = g["start"] + g["step"] * i
    ends = begins + g["step"]
    return f"{begins:%H:%M}–{'24:00' if ends.date() > g['start'].date() else f'{ends:%H:%M}'}"


# ── the figure ─────────────────────────────────────────────────────────
def _who_text(g, label, i):
    ids = list(dict.fromkeys(g["pair_ids"][label][i]))
    if not ids:
        return ""
    discussion = g["discussions"][ids[0]]
    count = g["who"][label][i]
    if g["drawn"][label][i]:
        split = ", ".join(f"{name} answered {k}"
                          for name, k in sorted(g["replies"][label][i].items()))
        said = f"{_plural(count, 'reply', 'replies')} between them in this slot ({split})"
    else:
        said = (f"their conversation goes on; {_plural(count, 'reply', 'replies')} between "
                f"them in this slot")
    more = ""
    if len(ids) > 1:
        rooms = ", ".join(g["discussions"][d_id].get("room") or "?" for d_id in ids[1:])
        more = f"<br>also talking in {rooms}"
    measured = g["facts"][discussion["id"]]
    longest = max(measured["quotes"], key=lambda t: _words(t["text"]), default=None)
    quote = (f"<br>{longest['speaker']}: «{_escape(_clip(longest['text'], 70))}»"
             if longest else "")
    return (f"<b>{slot_label(g, i)}</b> · {label}<br>{said}<br>"
            f"{span_text(discussion)} · {discussion.get('room') or '?'} · "
            f"{fact_line(discussion, measured)}{quote}{more}<br><i>click to open</i>")


def _lamp_text(g, key, name, i):
    ids = g["lamp_ids"][key][i]
    if not ids:
        return ""
    ordered = sorted(ids, key=lambda d_id: -g["discussions"][d_id]["dimensions"][key])
    lines = []
    for d_id in ordered[:3]:
        discussion = g["discussions"][d_id]
        types = ", ".join(t.replace("_", " ") for t in discussion.get("types") or [])
        complaints = ("; ".join(complaint_text(c) for c in discussion.get("complaints") or [])
                      if key == "tension" else "")
        lines.append(f"{people(discussion)} · {discussion.get('room') or '?'} · "
                     f"{span_text(discussion)} · {discussion['dimensions'][key]:.2f}"
                     + (f" · {types}" if types else "")
                     + (f"<br>&nbsp;&nbsp;complaint: {_escape(complaints)}" if complaints else ""))
    if len(ids) > 3:
        lines.append(f"+{len(ids) - 3} more")
    return (f"<b>{slot_label(g, i)}</b> · {name} {g['lamps'][key][i]:.2f}<br>"
            + "<br>".join(lines)
            + "<br><i>one reading of the whole discussion · click to open</i>")


def _unscored_text(g, key, name, i):
    ids = g["unscored"][key][i]
    if not ids:
        return ""
    lines = []
    for d_id in ids[:3]:
        discussion = g["discussions"][d_id]
        status = discussion.get("status")
        why = (f"no {name.lower()} score" if status in USABLE
               else STATUS.get(status, (status, ""))[0].lower())
        lines.append(f"{people(discussion)} · {discussion.get('room') or '?'} · "
                     f"{span_text(discussion)} · {why}")
    if len(ids) > 3:
        lines.append(f"+{len(ids) - 3} more")
    return (f"<b>{slot_label(g, i)}</b> · {name}: not scored<br>"
            + "<br>".join(lines) + "<br><i>click to open</i>")


def _scale(y0, y1, ticks, labels):
    """A thin colour bar in the right margin, spanning the rows between y0 and y1."""
    return dict(x=1.0, xanchor="left", xpad=10, y=y1, yanchor="top", len=y1 - y0,
                lenmode="fraction", ypad=0, thickness=10, outlinewidth=0,
                tickvals=ticks, ticktext=labels, ticks="outside", ticklen=3, tickcolor=DIM,
                tickfont=dict(color=DIM, size=11))


def build_figure(data, day, minutes=DEFAULT_MINUTES, green_lit=0.55, amber_lit=0.40):
    """The two bands, each colour with its scale beside the rows it colours.

    green_lit and amber_lit are where a lamp lights (the LLM tab's
    SEMANTIC_GREEN and SEMANTIC_AMBER); they are marked on the green and amber
    scales so a reader can see how far a cell is from lighting.
    """
    g = grid(data, day, minutes)
    n = g["n"]
    x = [(g["start"] + g["step"] * i + g["step"] / 2).strftime("%Y-%m-%d %H:%M:%S")
         for i in range(n)]
    who_labels = [label for label, _p in g["rows"]]
    lamp_labels = [name for _k, name, _p in LAMPS]
    # A gap between slots only reads while the slots are wide enough to see.
    xgap, ygap = (1 if n <= 96 else 0), 5

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.1,
                        row_heights=[len(who_labels), len(lamp_labels)],
                        subplot_titles=("Who was talking with whom",
                                        "What the model read in each discussion"))
    top, bottom = fig.layout.yaxis.domain, fig.layout.yaxis2.domain
    row_height = (bottom[1] - bottom[0]) / len(lamp_labels)
    hopes = sum(1 for _k, _nm, problem in LAMPS if not problem)
    for row, labels in ((1, who_labels), (2, lamp_labels)):
        fig.add_trace(go.Heatmap(
            z=[[0] * n for _ in labels], x=x, y=labels,
            colorscale=[[0, TRACK], [1, TRACK]], showscale=False,
            hoverinfo="skip", xgap=xgap, ygap=ygap), row=row, col=1)

    ongoing = [[1 if g["ongoing"][label][i] else None for i in range(n)] for label in who_labels]
    fig.add_trace(go.Heatmap(
        z=ongoing, x=x, y=who_labels, colorscale=[[0, ONGOING], [1, ONGOING]],
        showscale=False, hoverongaps=False, xgap=xgap, ygap=ygap,
        text=[[_who_text(g, label, i) if ongoing[r][i] else "" for i in range(n)]
              for r, label in enumerate(who_labels)],
        hovertemplate="%{text}<extra></extra>"), row=1, col=1)
    talking = [[g["who"][label][i] if g["drawn"][label][i] else None for i in range(n)]
               for label in who_labels]
    most = max([c for values in talking for c in values if c] + [MIN_EXCHANGES + 1])
    reply_ticks = sorted({MIN_EXCHANGES, round((MIN_EXCHANGES + most) / 2), most})
    fig.add_trace(go.Heatmap(
        z=talking, x=x, y=who_labels, zmin=MIN_EXCHANGES, zmax=most, colorscale=BLUE,
        showscale=True,
        colorbar=_scale(top[0], top[1], reply_ticks,
                        [f"{v} replies" if v == most else str(v) for v in reply_ticks]),
        text=[[_who_text(g, label, i) if talking[r][i] else "" for i in range(n)]
              for r, label in enumerate(who_labels)],
        hovertemplate="%{text}<extra></extra>", hoverongaps=False,
        xgap=xgap, ygap=ygap), row=1, col=1)

    grey = [[1 if g["unscored"][key][i] and g["lamps"][key][i] is None else None
             for i in range(n)] for key, _nm, _p in LAMPS]
    fig.add_trace(go.Heatmap(
        z=grey, x=x, y=lamp_labels, colorscale=[[0, UNSCORED], [1, UNSCORED]],
        showscale=False, hoverongaps=False, xgap=xgap, ygap=ygap,
        text=[[_unscored_text(g, key, name, i) if grey[r][i] else ""
               for i in range(n)] for r, (key, name, _p) in enumerate(LAMPS)],
        hovertemplate="%{text}<extra></extra>"), row=2, col=1)
    for problem, scale in ((False, GREEN), (True, AMBER)):
        lit = amber_lit if problem else green_lit
        # Beside the rows it colours: the hopes on top, the worries below.
        span = ((bottom[0], bottom[0] + row_height * (len(LAMPS) - hopes - 0.3)) if problem
                else (bottom[1] - row_height * (hopes - 0.3), bottom[1]))
        # Every lamp row is given, the other colour's rows left empty: plotly.js
        # 3.4 throws on a heatmap with fewer rows than its category axis, and a
        # thrown figure leaves the viewer with empty axes and no heatmap at all.
        fig.add_trace(go.Heatmap(
            z=[g["lamps"][key] if p == problem else [None] * n for key, _nm, p in LAMPS],
            x=x, y=lamp_labels, zmin=0, zmax=1, colorscale=scale,
            showscale=True,
            colorbar=_scale(span[0], span[1], [0, lit, 1], ["0", f"{lit:.2f} lit", "1"]),
            hoverongaps=False, xgap=xgap, ygap=ygap,
            text=[[_lamp_text(g, key, name, i) for i in range(n)] if p == problem else [""] * n
                  for key, name, p in LAMPS],
            hovertemplate="%{text}<extra></extra>"), row=2, col=1)

    fig.update_xaxes(type="date", range=[g["start"], g["start"] + timedelta(days=1)],
                     tickformat="%H:%M", showgrid=True, gridcolor="rgba(255,255,255,0.08)",
                     layer="above traces", zeroline=False, showline=False, ticks="",
                     tickfont=dict(color=DIM, size=12))
    fig.update_yaxes(showgrid=False, zeroline=False, ticks="", fixedrange=True,
                     tickfont=dict(color=TEXT, size=13))
    fig.update_yaxes(categoryorder="array", categoryarray=list(reversed(who_labels)),
                     row=1, col=1)
    fig.update_yaxes(categoryorder="array", categoryarray=list(reversed(lamp_labels)),
                     row=2, col=1)
    fig.update_annotations(x=0, xanchor="left", font=dict(size=14, color=TEXT))
    fig.update_layout(
        height=140 + 34 * (len(who_labels) + len(lamp_labels)),
        margin=dict(l=120, r=110, t=36, b=30),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor=NIGHT, showlegend=False,
        font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif", size=12, color=DIM),
        hoverlabel=dict(bgcolor="#0E141B", bordercolor="#2A3643",
                        font=dict(color=TEXT, size=12), align="left"),
        dragmode="zoom")
    return fig


# ── key moments ────────────────────────────────────────────────────────
def lit_lamps(discussion, green, amber):
    """(key, name, names a problem, score) for each lamp a discussion's reading lit."""
    if discussion.get("status") not in USABLE:
        return []
    dims = discussion.get("dimensions") or {}
    return [(key, name, problem, dims[key]) for key, name, problem in LAMPS
            if dims.get(key) is not None and dims[key] >= (amber if problem else green)]


def moments(data, day, green, amber, limit=6):
    """(discussion, lit lamps) for the day's clearest readings, in time order.

    Split half and half between problems and warm discussions, so a warm
    evening cannot crowd a quarrel off the list.
    """
    candidates = []
    for discussion in discussions_on(data, day):
        lit = lit_lamps(discussion, green, amber)
        if lit:
            candidates.append((discussion, lit))

    def strongest(item, problem):
        return max([v for _k, _nm, p, v in item[1] if p == problem] + [-1.0])

    problems = sorted((c for c in candidates if strongest(c, True) >= 0),
                      key=lambda c: (-strongest(c, True), -c[0].get("words", 0)))
    positives = sorted((c for c in candidates if strongest(c, True) < 0),
                       key=lambda c: (-strongest(c, False), -c[0].get("words", 0)))
    half = limit // 2
    chosen = problems[:half] + positives[:half]
    rest = problems[half:] + positives[half:]
    chosen += rest[:limit - len(chosen)]
    return sorted(chosen, key=lambda c: c[0]["start"])


def card(discussion, green, amber, expanded=False, voice=None, provenance=None):
    """One discussion as a card: its facts in brief, opening in place to the full detail.

    Only the summary at the top is clickable, so reading, selecting or
    scrolling inside the open detail never closes the card.
    """
    lit = lit_lamps(discussion, green, amber)
    problem = any(p for _k, _nm, p, _v in lit)
    measured = facts(discussion)
    bits = _fact_bits(measured)
    longest = max(measured["quotes"], key=lambda t: _words(t["text"]), default=None)
    if lit:
        lamps = [html.Span(f"{name} {value:.2f}", className="ct-chip",
                           **{"data-kind": "problem" if p else "positive"})
                 for _k, name, p, value in sorted(lit, key=lambda item: -item[3])]
    else:
        status = discussion.get("status")
        lamps = [html.Span("no lamp lit" if status in USABLE
                           else STATUS.get(status, (status, ""))[0], className="ct-chip ct-chip-none")]
    summary = html.Div([
        html.Div([html.Span(className="ct-dot",
                            **({"data-kind": "problem" if problem else "positive"} if lit else {})),
                  f"{span_text(discussion)} · {discussion.get('room') or '?'}",
                  html.Span("close" if expanded else "open", className="ct-card-toggle")],
                 className="ct-card-time"),
        html.Div(bits[0], className="ct-card-title"),
        html.Div(" · ".join(bits[1:]), className="ct-card-room"),
        html.Div(lamps, className="ct-chips"),
        html.Div(complaints_line(discussion), className="ct-card-types"),
        html.Div(f"{longest['speaker']}: «{_clip(longest['text'], 140)}»" if longest else "",
                 className="ct-card-text"),
    ], id={"type": "conv-card", "index": discussion["id"]}, n_clicks=0,
        className="ct-card-head", role="button", tabIndex=0)
    body = [summary]
    if expanded:
        body.append(discussion_detail(discussion, green, amber, voice, show_head=False,
                                      provenance=provenance))
    return html.Div(body, className="ct-card ct-card-open" if expanded else "ct-card")


def complaints_line(discussion):
    complaints = discussion.get("complaints") or []
    if not complaints:
        return ""
    return (f"{'Complaints' if len(complaints) > 1 else 'Complaint'}: "
            + "; ".join(complaint_text(c) for c in complaints))


def render_cards(data, day, green, amber, selected=(), expanded=(), voice=None, provenance=None):
    """(cards chosen from the figure, key moment cards), each opening in place.

    A discussion chosen from the figure is shown once, on top, and is not
    repeated among the key moments below it.
    """
    picked = [d for d in (find(data, d_id) for d_id in selected) if d is not None]
    shown = {d["id"] for d in picked}
    others = [d for d, _lit in moments(data, day, green, amber) if d["id"] not in shown]

    def make(discussion):
        return card(discussion, green, amber, discussion["id"] in expanded, voice, provenance)

    if others:
        key_moments = [make(d) for d in others]
    else:
        key_moments = [html.Div(
            f"No {'other ' if picked else ''}discussion on this day lit a lamp clearly enough "
            f"to single out.", className="ct-empty")]
    return [make(d) for d in picked], key_moments


# ── detail ─────────────────────────────────────────────────────────────
def _stream(discussion):
    """The discussion as the model was given it: speech and sounds in one time order."""
    items = [(_parse(t["time"]), 0, t) for t in discussion.get("turns") or []]
    items += [(_parse(s["time"]), 1, s) for s in discussion.get("sounds") or []]
    rows = []
    for when, is_sound, item in sorted(items, key=lambda entry: (entry[0], entry[1])):
        if is_sound:
            score = item.get("confidence")
            rows.append(html.Div([
                html.Span(f"{when:%H:%M:%S}", className="ct-turn-time"),
                html.Span("sound", className="ct-turn-speaker"),
                html.Span(item["label"] + (f" ({score:.2f})" if score is not None else "")),
            ], className="ct-turn ct-sound"))
            continue
        cells = [html.Span(f"{when:%H:%M:%S}", className="ct-turn-time"),
                 html.Span(item.get("speaker") or "unidentified", className="ct-turn-speaker"),
                 html.Span(item.get("text") or "")]
        if item.get("heard_elsewhere"):
            rows.append(html.Div(cells, className="ct-turn ct-copy",
                                 title="Another room's sentinel heard this line too; "
                                       "the picture counts it there."))
        else:
            rows.append(html.Div(cells, className="ct-turn"))
    return rows


def discussion_detail(discussion, green, amber, voice=None, show_head=True, provenance=None):
    """What was measured, what the model concluded and what it was given, for one discussion.

    voice, when given, looks up how loud the discussion's clips were (app.voice).
    show_head=False leaves out the time, people and room, for a card that
    already shows them above. provenance names the reader (see provenance()).
    """
    status = discussion.get("status") or "not_read"
    label, colour = STATUS.get(status, (status, "secondary"))
    summary = [_plural(discussion.get("turn_count", 0), "turn"),
               _plural(discussion.get("words", 0), "word"),
               f"sentinel {discussion.get('sentinel') or '?'}"]

    concluded = [html.Div([
        dbc.Badge(label, color=colour, className="me-2"),
        html.Span(f"confidence {discussion['confidence']:.2f}"
                  if discussion.get("confidence") is not None else "",
                  className="ct-muted small"),
    ], className="mb-2")]
    if provenance:
        concluded.insert(0, html.Div(provenance, className="ct-muted small mb-1"))
    if status in USABLE:
        scores = []
        for key, name, problem in LAMPS:
            value = (discussion.get("dimensions") or {}).get(key)
            lit = value is not None and value >= (amber if problem else green)
            scores.append(html.Div(
                [html.Div(name, className="ct-score-name"),
                 html.B("—" if value is None else f"{value:.2f}")],
                className="ct-score",
                **({"data-lit": "problem" if problem else "positive"} if lit else {})))
        concluded.append(html.Div(scores, className="ct-scores"))
        if discussion.get("types"):
            concluded.append(html.Div(
                ["Interaction types: ",
                 html.Span(", ".join(t.replace("_", " ") for t in discussion["types"]),
                           className="fw-semibold")], className="small mb-1"))
        if discussion.get("complaints"):
            concluded.append(html.Div(complaints_line(discussion), className="small mb-1"))
        if discussion.get("evidence"):
            concluded.append(html.Blockquote(discussion["evidence"], className="ct-evidence"))
    if discussion.get("reason"):
        concluded.append(html.Div(["Reason: ", discussion["reason"]],
                                  className="ct-muted small mb-1"))
    for note in discussion.get("uncertainty") or []:
        concluded.append(html.Div(f"Pipeline adjustment: {note}", className="ct-muted small"))

    given = ("What it was given: the whole discussion, nothing before or after"
             if status not in ("not_read", "not_attempted") else "What was said")
    head = ([html.Div([html.Span(span_text(discussion, seconds=True), className="ct-detail-time"),
                       html.Span(people(discussion), className="ct-detail-who"),
                       html.Span(discussion.get("room") or "?", className="ct-detail-where")],
                      className="ct-detail-head")] if show_head else [])
    return html.Div([
        *head,
        html.Div(" · ".join(summary), className="ct-muted small"),
        html.Div("What was measured", className="ct-detail-label"),
        fact_card(discussion, voice),
        html.Div("What the model concluded", className="ct-detail-label"),
        *concluded,
        html.Div(given, className="ct-detail-label"),
        html.Div(_stream(discussion), className="ct-turns"),
    ], className="ct-detail")


def cell_discussions(data, day, minutes, click):
    """(title, discussions) behind a clicked cell, or (None, []) when it holds nothing."""
    try:
        point = click["points"][0]
        label = point["y"]
        moment = _parse(point["x"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None, []
    start = datetime.strptime(day, "%Y-%m-%d")
    g = {"start": start, "step": timedelta(minutes=int(minutes))}
    i = int((moment - start).total_seconds() // g["step"].total_seconds())
    begins = start + g["step"] * i
    ends = begins + g["step"]
    pairs = dict(pair_rows(residents_of(data)))
    lamps = {name: key for key, name, _p in LAMPS}

    spanning = [d for d in discussions_on(data, day)
                if _parse(d["start"]) < ends and (_parse(d.get("end")) or _parse(d["start"])) >= begins]
    if label in pairs:
        # Exactly the discussions behind the drawn cell, not every discussion
        # that happens to include both people.
        drawn = grid(data, day, minutes)
        ids = drawn["pair_ids"][label][i] if 0 <= i < drawn["n"] else []
        found = [drawn["discussions"][d_id] for d_id in dict.fromkeys(ids)]
    elif label in lamps:
        key = lamps[label]

        def strongest_first(discussion):
            value = ((discussion.get("dimensions") or {}).get(key)
                     if discussion.get("status") in USABLE else None)
            return (value is None, -(value or 0.0), discussion["start"])
        found = sorted(spanning, key=strongest_first)
    else:
        found = []
    if not found:
        return None, []
    return f"{label} · {slot_label(g, i)} · {start:%d-%m-%Y}", found


# ── page pieces ────────────────────────────────────────────────────────
def legend():
    # The blue, green and amber scales are colour bars beside the figure; these
    # are the two flat fills that sit on no scale.
    items = [(ONGOING, f"Their conversation goes on, fewer than {MIN_EXCHANGES} replies "
                       f"in the slot"),
             (UNSCORED, "A discussion the model did not score")]
    return html.Div([html.Span([html.Span(className="ct-swatch", style={"background": bg}),
                                text], className="ct-legend-item")
                     for bg, text in items], className="ct-legend")
