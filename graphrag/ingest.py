#!/usr/bin/env python3
"""Build the searchable graph from a window of Sentinel events.

    python -m graphrag.ingest --days 7

Nothing in the running application is modified. The database is opened
read-only, derive_home_dynamics.py is imported and used exactly as the LLM tab
uses it, and everything produced lands in graphrag/store/.

WHAT THIS ADDS TO THE PIPELINE

Levels 1 to 4 abstract away from words on purpose: "Water tap" becomes
water_use, a conversation becomes an episode with counts. That is what makes
them summarisable, and it is why they cannot answer a question about what
somebody talks about. So one stage is added here: every diarised utterance
that carries text is passed once to a local model, which returns a few short
subject labels. Those become topic nodes.

Extraction happens at ingest and is cached by the hash of the utterance, so a
re-run costs nothing and a longer window only pays for what is new. This is
the whole economic argument for building a graph rather than enlarging the
prompt: the expensive reading is done once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store")
GRAPH_PATH = os.path.join(STORE_DIR, "graph.json")
TOPIC_CACHE = os.path.join(STORE_DIR, "topic_cache.json")
SOURCE_DB = os.path.join(PROJECT_ROOT, "ast_audio_db", "ast_audio.db")
PIPELINE_DIR = os.path.join(PROJECT_ROOT, "ast_dash", "llm_report")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # pragma: no cover
    pass

# Raw column -> the dashboard names derive_home_dynamics is written against.
# Mirrors ast_dash/pages/llm_report.py; the pipeline is not taught a second
# schema just because a different caller is driving it.
DB_TO_DASHBOARD = {
    "id": "ID", "date": "Timestamp", "device_name": "Device",
    "sound1": "Class 1", "conf_sound1": "Prob 1",
    "sound2": "Class 2", "conf_sound2": "Prob 2",
    "sound3": "Class 3", "conf_sound3": "Prob 3",
    "rms": "rms", "transcription": "transcription",
    "emotion": "emotion", "room": "room", "file_name": "file_name",
}

# Matches only the HEADER of a segment. The spoken text is then taken as
# everything up to the next header, rather than to the end of the line.
#
# The writer does not reliably put one segment per line: several arrive on a
# single line, and a pattern ending in `.*` swallowed the following headers
# whole, so a quoted piece of evidence read
#     "Αχα, αχα, αχα. [ 00: 11. 04 - 00: 13. 86 ] Rafael: Ω, τι είπε..."
# and topic labelling was reading two or three utterances as one.
SEGMENT_HEADER_RE = re.compile(
    r"\[\s*(?P<start>[\d:.\s]+?)\s*-\s*(?P<end>[\d:.\s]+?)\s*\]\s*"
    r"(?P<speaker>[^:\[\]]{0,40}?)\s*:")

RESIDENTS = ["Ilyas", "Voula", "Rafael"]

EMOTION_CODES = {"HAP": "happy", "HAPPY": "happy", "SAD": "sad",
                 "ANG": "angry", "ANGRY": "angry",
                 "NEU": "neutral", "NEUTRAL": "neutral"}
EMOTION_RE = re.compile(r"\s*([A-Za-z]+)\s*(?:\(\s*([\d.]+)\s*\))?")


def parse_emotion(value) -> tuple[str | None, float | None]:
    """'NEU (0.61)' -> ('neutral', 0.61)."""
    if not value or str(value).strip() in ("None", ""):
        return None, None
    m = EMOTION_RE.match(str(value))
    if not m:
        return None, None
    return EMOTION_CODES.get(m.group(1).upper()), (
        float(m.group(2)) if m.group(2) else None)
MIN_TOPIC_WORDS = 4          # below this an utterance carries no subject


# ── reading events ──────────────────────────────────────────────────

def load_events_between(start: str, end: str) -> list:
    """Events in [start, end], in the shape the pipeline expects."""
    conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cols = ", ".join(DB_TO_DASHBOARD)
    rows = conn.execute(
        f"SELECT {cols} FROM ast_table WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end)).fetchall()
    conn.close()
    return [{DB_TO_DASHBOARD[k]: r[k] for k in DB_TO_DASHBOARD} for r in rows]


def days_with_events(since: str | None = None) -> list[str]:
    """Every calendar date the database holds events for."""
    conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    sql = "SELECT DISTINCT substr(date,1,10) d FROM ast_table WHERE length(date)>=10"
    args = ()
    if since:
        sql += " AND date >= ?"
        args = (since,)
    rows = conn.execute(sql + " ORDER BY d", args).fetchall()
    conn.close()
    # Guards against the odd malformed timestamp already in the table.
    return [r[0] for r in rows if re.fullmatch(r"\d{4}-\d{2}-\d{2}", r[0] or "")]


def load_roles() -> dict:
    """Who is who, from the application's own household configuration.

    home_dynamics_config.json records role and sex ("adult"/"child",
    "male"/"female") rather than kinship, so the family words are derived:
    the adult male is the husband and father, the adult female the wife and
    mother, the child their son or daughter. That inference is right for this
    household and is stated here rather than hidden, because every question
    this system is asked is phrased in exactly these terms.
    """
    cfg_path = os.path.join(PIPELINE_DIR, "home_dynamics_config.json")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            members = (json.load(fh) or {}).get("household_members") or {}
    except Exception:
        members = {}

    roles = {}
    for name, spec in members.items():
        role, sex = (spec.get("role") or "").lower(), (spec.get("sex") or "").lower()
        if role == "adult" and sex == "male":
            words = ["father", "dad", "husband", "adult male", "parent"]
        elif role == "adult" and sex == "female":
            words = ["mother", "mum", "mom", "wife", "adult female", "parent"]
        elif role == "child" and sex == "male":
            words = ["son", "child", "boy", "kid"]
        elif role == "child" and sex == "female":
            words = ["daughter", "child", "girl", "kid"]
        else:
            words = [w for w in (role, sex) if w]
        roles[name] = {"role": role, "sex": sex, "words": words}
    return roles


def _semantic_fields(summary: dict | None) -> dict:
    """Flatten one L1.5 rollup into day-series columns.

    Flat scalars rather than the nested rollup, because these become a time
    series the engine reads day by day; a nested shape would have every
    operation reaching two levels down for a mean. `None` is preserved as
    None -- a day with no interpreted exchange is unmeasured, not cold.
    """
    if not summary:
        return {"interpreted_exchanges": 0}
    dims = summary.get("dimensions") or {}
    out = {"interpreted_exchanges": summary.get("interpreted_exchanges", 0),
           "uninterpretable_exchanges": summary.get("uninterpretable_exchanges", 0)}
    for name in ("warmth", "joy", "withdrawal", "tension"):
        scored = dims.get(name)
        out[name] = (scored or {}).get("mean") if isinstance(scored, dict) else None
        if isinstance(scored, dict) and name == "tension":
            # The day's sharpest exchange, kept beside the mean: one argument
            # in a calm week is a real event that an average erases.
            out["tension_peak"] = scored.get("max")
    peaks = summary.get("tension_peaks") or {}
    if peaks:
        out["tension_peak_count"] = peaks.get("count")
        out["tension_repaired_count"] = peaks.get("followed_by_repair")
    # Complaints as counts by where they point, so a trend can distinguish a
    # week of grievances aimed at each other from a week of grumbling about
    # buses. The exchanges themselves keep the wording; this is the series.
    lodged = summary.get("complaints") or {}
    if lodged:
        out["complaint_count"] = lodged.get("count", 0)
        for target in ("someone_present", "family_absent", "outsider", "circumstance"):
            out[f"complaints_{target}"] = (lodged.get("by_target") or {}).get(target, 0)
    return out


def make_semantic_interpreter(model: str, host: str):
    """L1.5's reader, built on this package's own LLM layer.

    Deliberately not imported from ast_dash: that module pulls in Dash, and
    graphrag is meant to run without the application. The prompt file is
    shared, so both paths interpret by the same rules.
    """
    from . import llm as gllm

    prompt_path = os.path.join(PROJECT_ROOT, "assets", "LLM_prompt_L1_5.txt")
    try:
        with open(prompt_path, encoding="utf-8") as fh:
            prompt = fh.read()
    except OSError as exc:
        print(f"  [l1.5] prompt unavailable ({exc}); semantics skipped")
        return None

    def interpret(payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        reply = gllm._call(model, f"{prompt}\n\n{body}", host, as_json=True)
        if not reply:
            raise RuntimeError("no reply from the semantic interpreter")
        return json.loads(reply)

    return interpret


def make_incident_reader(model: str, host: str):
    """The incident layer's reader: a prompt in, a decoded JSON object out.

    Returns None for a reply that is missing or is not JSON, which the layer
    counts as a window nobody read rather than as five quiet minutes.
    """
    from . import llm as gllm

    def read(prompt):
        reply = gllm._call(model, prompt, host, as_json=True)
        if not reply:
            return None
        try:
            return json.loads(reply)
        except json.JSONDecodeError:
            return None

    return read


def incident_workers(model: str) -> int:
    """How many readings may run at once.

    A local model has one GPU and gains nothing from being asked twice at the
    same moment. A hosted one answers in parallel, which is the difference
    between reading a day in ten seconds and in two minutes.
    """
    from . import llm as gllm
    return 8 if gllm._is_openai(model) else 1


def run_pipeline(events: list, interpreter=None) -> dict:
    """Levels 0 to 4, via the application's own module, unmodified.

    L1.5 runs here too when an interpreter is supplied. Without one the level
    still runs and marks every exchange "not_attempted", so a store built
    without a model records that the semantics are missing rather than
    implying an absence of warmth -- which is what the dyad counts alone would
    have said.
    """
    if PIPELINE_DIR not in sys.path:
        sys.path.insert(0, PIPELINE_DIR)
    import derive_home_dynamics as dhd

    cfg = dhd.load_config(os.path.join(PIPELINE_DIR, "home_dynamics_config.json"))
    level0 = dhd.derive_level0(events, cfg)
    level1 = dhd.derive_level1(level0, cfg)
    level1_5 = dhd.derive_level1_5(level0, cfg, interpreter=interpreter, cache={})
    level2 = dhd.derive_level2(level1, cfg)
    level3 = dhd.derive_level3(level0, level2, cfg, level1_5=level1_5)
    gaps = dhd.derive_observation_gaps(level0, cfg)
    level4 = dhd.derive_level4(level0, level2, level3, gaps, cfg)
    return {"level0": level0, "level1_5": level1_5, "level2": level2,
            "level3": level3, "level4": level4, "gaps": gaps}


# ── utterances ──────────────────────────────────────────────────────

def extract_segments(events: list) -> list[dict]:
    """Every diarised utterance that carries text, with its provenance.

    The segment is the unit of evidence: a claim in an answer resolves to one
    of these, and from there to an event id, a timestamp, a room and the audio
    file on disk.
    """
    out = []
    for ev in events:
        text_blob = ev.get("transcription") or ""
        if not text_blob.startswith("["):
            continue
        stamp = str(ev.get("Timestamp") or "")
        # The recogniser labels a whole clip, not each utterance inside it, so
        # every utterance from one clip inherits that clip's emotion. Stated
        # rather than hidden: it is an approximation, and it means two people
        # speaking in one clip are given the same label.
        emo, emo_conf = parse_emotion(ev.get("emotion"))
        # Scan the whole blob for headers, then take each utterance as the
        # span between one header and the next.
        marks = list(SEGMENT_HEADER_RE.finditer(text_blob))
        for i, m in enumerate(marks):
            stop = marks[i + 1].start() if i + 1 < len(marks) else len(text_blob)
            said = text_blob[m.end():stop].strip()
            if not said:
                continue
            speaker = (m.group("speaker") or "").strip() or "Unknown"
            out.append({
                "id": f"seg::{ev.get('ID')}::{len(out)}",
                "event_id": ev.get("ID"),
                "speaker": speaker,
                "text": said,
                "words": len(said.split()),
                "timestamp": stamp,
                "day": stamp[:10],
                "room": ev.get("room") or "unknown",
                "device": ev.get("Device"),
                "audio_file": ev.get("file_name"),
                "emotion": emo,
                "emotion_confidence": emo_conf,
            })
    return out


# ── topics ──────────────────────────────────────────────────────────

TOPIC_SYSTEM = """You label what each numbered household utterance is ABOUT.

Return JSON only, mapping every number given to a list of topics:
{"1": ["dinner"], "2": [], "3": ["football", "school"]}

Rules:
- 1 to 3 topics per utterance, each 1-3 words, lower case.
- A topic is a SUBJECT (football, homework, dinner, the car, sleep, money,
  music, health, school, holidays), never an emotion and never a description
  of the speaking.
- Input may be Greek or English; ALWAYS answer with English topic labels, so
  the same subject from different days groups together.
- If an utterance is filler, a greeting, or too fragmentary to have a subject,
  give it an empty list.
- Answer for every number, and invent no others.

UTTERANCES:
{items}

JSON:"""

# One call per utterance was the first design and it was unusable: a single
# short classification costs 20-27s against a local 9B model, almost all of it
# fixed overhead rather than generation, which put a single week at over three
# hours and a year out of reach entirely. Batching amortises that overhead
# across many utterances and is the difference between a tool that can ingest
# years and one that cannot.
TOPIC_BATCH = 25

# Two models, two jobs. Extraction is a high-volume, low-reasoning
# classification, and a 3B model does it 9x faster than a 9B one at
# indistinguishable quality: benchmarked on 25 real utterances from this
# household, llama3.2 took 10.5s against gemma2:9b's 93.2s, with both
# returning identical labels on the clear cases and neither leaking Greek.
# Planning and narration are low-volume and reasoning-bound, so ask.py uses
# gemma2:9b there instead.
TOPIC_MODEL = "llama3.2:latest"


def ollama_json(model: str, prompt: str, host: str,
                timeout: float = 300.0) -> dict | None:
    """One constrained JSON call. Returns None rather than raising."""
    import urllib.request
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.0},
    }).encode("utf-8")
    req = urllib.request.Request(f"{host}/api/generate", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return json.loads(body.get("response") or "{}")
    except Exception:
        return None


def label_batch(texts: list[str], model: str, host: str) -> list[list[str]]:
    """Topics for a batch of utterances, in the order given.

    A batch that fails to parse returns empty lists rather than raising: an
    unlabelled utterance costs a little recall, a crashed ingest costs the run.
    """
    items = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))
    # Substitution, not str.format: the prompt shows the model an example of
    # the JSON it should return, and format() would try to read those braces
    # as fields of its own.
    result = ollama_json(model, TOPIC_SYSTEM.replace("{items}", items), host)
    out = [[] for _ in texts]
    if not isinstance(result, dict):
        return out
    for i in range(len(texts)):
        raw = result.get(str(i + 1))
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list):
            out[i] = [str(t).strip().lower() for t in raw if str(t).strip()][:3]
    return out


def label_topics(segments: list[dict], model: str, host: str,
                 quiet: bool = False, batch: int = TOPIC_BATCH) -> dict:
    """Attach topics to every substantial utterance, caching by content hash.

    The cache is keyed on the text, not the segment id, so the same sentence
    said twice is paid for once and a re-ingest of an overlapping window is
    free.
    """
    cache = {}
    if os.path.exists(TOPIC_CACHE):
        with open(TOPIC_CACHE, encoding="utf-8") as fh:
            cache = json.load(fh)

    os.makedirs(STORE_DIR, exist_ok=True)

    def flush():
        # Written to a temporary file and moved into place, so an interrupted
        # write cannot leave a truncated cache behind.
        tmp = TOPIC_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, TOPIC_CACHE)

    def key_of(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    todo = [s for s in segments if s["words"] >= MIN_TOPIC_WORDS]

    # Only utterances never seen before cost anything, and identical text said
    # twice is paid for once.
    pending, seen = [], set()
    for seg in todo:
        k = key_of(seg["text"])
        if k not in cache and k not in seen:
            seen.add(k)
            pending.append(seg["text"])

    started = time.time()
    for start in range(0, len(pending), batch):
        chunk = pending[start:start + batch]
        for text, topics in zip(chunk, label_batch(chunk, model, host)):
            cache[key_of(text)] = topics
        # Checkpoint every batch. This loop is the only slow part of the whole
        # system; losing an hour of it to a dropped connection would be the
        # difference between a tool that can ingest years and one that cannot.
        flush()
        if not quiet:
            done = min(start + batch, len(pending))
            rate = done / max(1e-9, time.time() - started)
            left = (len(pending) - done) / max(rate, 1e-9)
            print(f"  topics: {done}/{len(pending)} new utterances "
                  f"({rate * 60:.0f}/min, ~{left / 60:.0f} min left)", flush=True)

    for seg in todo:
        seg["topics"] = cache.get(key_of(seg["text"]), [])

    for seg in segments:
        seg.setdefault("topics", [])

    flush()
    if not quiet:
        labelled = sum(1 for s in todo if s.get("topics"))
        print(f"  topics: {len(todo)} utterances considered, "
              f"{len(pending)} newly labelled, "
              f"{len(todo) - len(pending)} already cached, "
              f"{labelled} carry at least one topic")
    return cache


# ── one day, written once ───────────────────────────────────────────
#
# A day is the unit. It is built from that day's events, written once, and
# never rewritten. Everything downstream reads days and merges them, so the
# store grows by appending rather than by rebuilding, and an answer that cited
# a day last month cites the same evidence today.

DAYS_DIR = os.path.join(STORE_DIR, "days")
INDEX_PATH = os.path.join(STORE_DIR, "index.json")
# 3 adds the L1.5 relational dimensions to dyad_series and
# person_series. A version 2 day has none of them, so mixing the two
# silently would make a relationship look flat before the change.
# 4 keeps each interpreted exchange as a node of its own -- its types, its six
# dimensions, its complaints, its evidence line and the utterances behind it --
# and adds the complaint counts to both series. A version 3 day answers "how
# tense was Tuesday" but cannot show the argument, so tension_moments,
# warm_moments and complaints return nothing for it.
# The incident layer (incidents.py) does NOT raise this. It is added to a day,
# or left off it, without changing anything a version 4 reader relies on, and
# each day records for itself whether it was read for incidents. Raising the
# number would declare forty days stale that are stale in no other respect.
STORE_VERSION = 4

# Levels 2 and 3 merge events across gaps of up to ten minutes, so an episode
# beginning at 23:55 continues past midnight. Each day is therefore built with
# a few hours of preceding context, and every episode is attributed to the date
# it STARTS on. The overlap is computed and discarded except where it completes
# an episode that belongs to this day.
LOOKBACK_HOURS = 3


def day_path(date: str) -> str:
    return os.path.join(DAYS_DIR, f"{date}.json")


def existing_days() -> list:
    if not os.path.isdir(DAYS_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(DAYS_DIR) if f.endswith(".json"))


def build_day(date: str, model: str, host: str, batch: int,
              quiet: bool = False, semantic=None, incidents=None) -> dict:
    """Everything the graph knows about one calendar day."""
    start = (datetime.strptime(date, "%Y-%m-%d")
             - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    end = date + " 23:59:59"
    events = load_events_between(start, end)
    # Only this day's events are described. The earlier ones exist purely so
    # an episode straddling midnight is assembled correctly.
    own = [e for e in events if str(e.get("Timestamp") or "")[:10] == date]
    if not own:
        return {}

    bundle = run_pipeline(events, interpreter=semantic)
    segments = [s for s in extract_segments(events) if s["day"] == date]
    if not quiet:
        print(f"  {date}: {len(own)} events, {len(segments)} utterances")
    label_topics(segments, model, host, quiet=True, batch=batch)

    nodes, edges = {}, []

    def add(node_id, kind, **attrs):
        nodes.setdefault(node_id, {"id": node_id, "kind": kind, **attrs})
        return node_id

    add("day::" + date, "day", date=date)
    for seg in segments:
        if seg["speaker"] not in RESIDENTS:
            continue
        sid = add(seg["id"], "segment", text=seg["text"], day=date,
                  room=seg["room"], timestamp=seg["timestamp"],
                  event_id=seg["event_id"], audio_file=seg["audio_file"],
                  speaker=seg["speaker"], words=seg["words"],
                  emotion=seg.get("emotion"),
                  emotion_confidence=seg.get("emotion_confidence"))
        add("person::" + seg["speaker"], "person", name=seg["speaker"])
        add("room::" + seg["room"], "room", name=seg["room"])
        edges.append({"src": "person::" + seg["speaker"], "dst": sid, "kind": "spoke"})
        edges.append({"src": sid, "dst": "day::" + date, "kind": "on_day"})
        edges.append({"src": sid, "dst": "room::" + seg["room"], "kind": "in_room"})
        if seg.get("emotion"):
            add("emotion::" + seg["emotion"], "emotion", label=seg["emotion"])
            edges.append({"src": sid, "dst": "emotion::" + seg["emotion"],
                          "kind": "sounded"})
        for topic in seg.get("topics") or []:
            add("topic::" + topic, "topic", label=topic)
            edges.append({"src": sid, "dst": "topic::" + topic, "kind": "mentions"})

    for ep in bundle["level3"]:
        # Attribution by start date is what makes days disjoint: an episode
        # belongs to exactly one day and can never be counted twice.
        if (ep.start or "")[:10] != date:
            continue
        parts = [p for p in (ep.participants or []) if p in RESIDENTS]
        if not parts:
            continue
        eid = add("episode::" + str(ep.episode_id), "episode", type=ep.type,
                  start=ep.start, end=ep.end, room=ep.room, participants=parts,
                  confidence=ep.confidence,
                  source_event_ids=list(ep.source_event_ids or [])[:12])
        edges.append({"src": eid, "dst": "day::" + date, "kind": "on_day"})
        for p in parts:
            add("person::" + p, "person", name=p)
            edges.append({"src": "person::" + p, "dst": eid, "kind": "participated"})

    # Every interpreted exchange, kept whole rather than only averaged. In the
    # day series an argument and an affectionate exchange look alike, so the
    # questions this store exists for -- show me the fight, show me the warm
    # moment, what do they complain about -- had nothing to read. The reading
    # already exists here; this is only what stops it being discarded.
    segments_of_event = defaultdict(list)
    for seg in segments:
        segments_of_event[str(seg["event_id"])].append(seg["id"])
    for ex in bundle["level1_5"]:
        if ex.semantic_status not in ("interpretable", "partially_interpretable"):
            continue
        if (ex.start or "")[:10] != date:          # days stay disjoint
            continue
        parts = [p for p in (ex.participants or []) if p in RESIDENTS]
        xid = add("exchange::" + str(ex.interaction_id), "exchange",
                  start=ex.start, end=ex.end, room=ex.room, participants=parts,
                  types=list(ex.interaction_types or []),
                  dimensions=dict(ex.relational_dimensions or {}),
                  complaints=[dict(c) for c in (ex.complaints or [])],
                  evidence=ex.evidence, confidence=ex.semantic_confidence,
                  status=ex.semantic_status, turn_count=ex.turn_count,
                  word_count=ex.word_count,
                  source_event_ids=[str(i) for i in (ex.source_event_ids or [])][:12])
        edges.append({"src": xid, "dst": "day::" + date, "kind": "on_day"})
        if ex.room:
            add("room::" + ex.room, "room", name=ex.room)
            edges.append({"src": xid, "dst": "room::" + ex.room, "kind": "in_room"})
        for p in parts:
            add("person::" + p, "person", name=p)
            edges.append({"src": "person::" + p, "dst": xid, "kind": "participated"})
        # The utterances it was read from, so every claim still resolves to
        # words, an event id and a file on disk.
        for event_id in (ex.source_event_ids or []):
            for sid in segments_of_event.get(str(event_id), []):
                edges.append({"src": xid, "dst": sid, "kind": "evidenced_by"})

    level4 = next((d for d in bundle["level4"] if d.get("date") == date), {}) or {}
    dyad_series, person_series = {}, {}
    for pair, m in (level4.get("pair_dynamics") or {}).items():
        dyad_series[pair] = {
            "conversation_episode_count": m.get("conversation_episode_count", 0),
            "interaction_event_count": m.get("interaction_event_count", 0),
            "copresence_candidate_count": m.get("copresence_candidate_count", 0),
            "shared_activity_candidate_count": m.get("shared_activity_candidate_count", 0),
            "turn_balance": (m.get("turn_balance_summary") or {}).get("mean"),
            "evidence_strength": m.get("evidence_strength"),
            # How they interacted, not merely that they did. Everything above
            # counts an argument and an affectionate exchange identically, so a
            # question about a relationship over months had nothing to read.
            **_semantic_fields(m.get("semantic_summary")),
        }
    for person, m in (level4.get("individual_patterns") or {}).items():
        person_series[person] = {
            "speech_episode_count": m.get("speech_episode_count", 0),
            "presence_evidence_count": m.get("presence_evidence_count", 0),
            "rooms": m.get("recognized_rooms") or {},
            **_semantic_fields(m.get("semantic_summary")),
        }
    for seg in segments:
        if seg["speaker"] not in RESIDENTS:
            continue
        row = person_series.setdefault(seg["speaker"], {})
        row["segment_count"] = row.get("segment_count", 0) + 1
        row["word_count"] = row.get("word_count", 0) + seg["words"]

    day = {
        "date": date,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "store_version": STORE_VERSION,
        "event_count": len(own),
        "nodes": list(nodes.values()),
        "edges": edges,
        "dyad_series": dyad_series,
        "person_series": person_series,
        "group_dynamics": level4.get("group_dynamics") or {},
        "uncertainties": level4.get("uncertainties") or [],
        "data_quality": level4.get("data_quality") or {},
    }
    # Read last and from the day as it will be stored, so a day built now and
    # a day given the layer later go through exactly the same code.
    if incidents:
        read_incidents(day, incidents, quiet)
    return day


def read_incidents(day: dict, incidents: dict, quiet: bool = False) -> dict:
    """Add the incident layer to a built day. `incidents` names the reader."""
    from .incidents import add_incident_layer
    return add_incident_layer(
        day, incidents["reader"], memory=incidents.get("memory", ""),
        model=incidents.get("model", ""), workers=incidents.get("workers", 1),
        quiet=quiet)


def load_day(date: str) -> dict | None:
    try:
        with open(day_path(date), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def write_day(day: dict) -> str:
    os.makedirs(DAYS_DIR, exist_ok=True)
    path = day_path(day["date"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(day, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)          # a day appears whole or not at all
    return path


def write_index() -> dict:
    """The manifest: who lives here, and which days have been written."""
    days = existing_days()
    people, events = set(), 0
    for d in days:
        try:
            with open(day_path(d), encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            continue
        events += payload.get("event_count", 0)
        people.update(n["name"] for n in payload["nodes"] if n["kind"] == "person")
    roles = load_roles()
    index = {
        "store_version": STORE_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "people": sorted(people),
        "roles": {k: v for k, v in roles.items() if k in people},
        "event_count": events,
    }
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)
    return index


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", default=None, help="one date, YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=None,
                    help="the last N days, ending today")
    ap.add_argument("--catch-up", action="store_true",
                    help="every day the database has that the store does not")
    ap.add_argument("--since", default=None,
                    help="earliest date to consider with --catch-up")
    ap.add_argument("--rebuild", action="store_true",
                    help="rewrite days that already exist. They are immutable "
                         "by default, so this is the only way to change one")
    ap.add_argument("--model", default=TOPIC_MODEL)
    ap.add_argument("--semantic-model",
                    help="model for L1.5 relational coding "
                         "(e.g. gpt-4o-mini). Omitted, the level still "
                         "runs and records that nothing interpreted it.")
    ap.add_argument("--incident-model",
                    help="model that reads each day for incidents -- "
                         "quarrels, complaints, worries -- with their start "
                         "and end. Defaults to --semantic-model; 'none' "
                         "leaves the layer off.")
    ap.add_argument("--add-incidents", action="store_true",
                    help="give days that are ALREADY written an incident "
                         "layer, or replace the one they have, and change "
                         "nothing else in them")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--batch", type=int, default=TOPIC_BATCH)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.day:
        wanted = [args.day]
    elif args.days:
        today = datetime.now()
        wanted = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
                  for i in range(args.days - 1, -1, -1)]
    elif args.catch_up:
        wanted = days_with_events(args.since)
    else:
        ap.error("give --day, --days N, or --catch-up")

    semantic = (make_semantic_interpreter(args.semantic_model, args.host)
                if args.semantic_model else None)
    if semantic:
        print(f'L1.5 relational coding via {args.semantic_model}')

    incident_model = args.incident_model or args.semantic_model
    incidents = None
    if incident_model and incident_model.lower() != "none":
        from .incidents import load_household_memory, reader_memory
        # Not everything a resident wrote: see READER_ENTRIES for why.
        memory = reader_memory()
        filled = len(load_household_memory().splitlines())
        incidents = {"reader": make_incident_reader(incident_model, args.host),
                     "model": incident_model, "memory": memory,
                     "workers": incident_workers(incident_model)}
        print(f"incidents read via {incident_model}"
              + (f" with the household memory: {len(memory.splitlines())} of "
                 f"its {filled} filled lines, who each resident is and where "
                 f"each works" if memory else
                 " (no household memory for the reader)"))

    have = set(existing_days())

    if args.add_incidents:
        if not incidents:
            ap.error("--add-incidents needs --incident-model")
        todo = [d for d in wanted if d in have]
        print(f"days requested: {len(wanted)}   written and so readable: "
              f"{len(todo)}")
        for date in todo:
            day = load_day(date)
            if not day:
                print(f"  {date}: could not be read, skipped")
                continue
            read_incidents(day, incidents, quiet=False)
            write_day(day)
        write_index()
        return 0

    todo = [d for d in wanted if args.rebuild or d not in have]
    print(f"days requested: {len(wanted)}   already written: "
          f"{len(wanted) - len(todo)}   to build: {len(todo)}")

    built = 0
    for date in todo:
        day = build_day(date, args.model, args.host, args.batch,
                        args.quiet, semantic=semantic, incidents=incidents)
        if not day:
            if not args.quiet:
                print(f"  {date}: no events, skipped")
            continue
        write_day(day)
        built += 1

    index = write_index()
    print(f"\n  built {built} day(s); store holds {len(index['days'])}: "
          f"{index['first_day']} .. {index['last_day']}")
    print(f"  people: {', '.join(index['people']) or '(none)'}")
    print(f"  {index['event_count']} events across all days")
    print(f"  store: {STORE_DIR}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
