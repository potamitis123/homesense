#!/usr/bin/env python3
"""Component annotation and scoring for the Sentinel perception stack.

Standalone. Reads ast_audio.db read-only, writes nothing back to it, and
touches no part of the running application. Everything it produces lives in
tests/diagnostics/annotation_store/.

    python tests/diagnostics/annotate_components.py sample --n 10
    python tests/diagnostics/annotate_components.py annotate
    python tests/diagnostics/annotate_components.py stats

WHY A SNAPSHOT

The live database is still receiving events, and housekeeper.py is armed to
delete rows older than DATA_RETENTION_DAYS and zip their audio away. An
evaluation set that mutates underneath you is not an evaluation set, so
`sample` copies both the rows and the audio into this tool's own store, once,
and every later command reads only from there.

BLIND WHERE IT MATTERS

Annotation runs in two stages per clip:

    stage 1   audio only. Speaker and emotion are answered with the model's
              output hidden, because those are one-click judgements and
              showing the prediction first would measure agreement rather
              than accuracy.
    stage 2   the model's output is revealed. The AST labels are judged
              (that task *is* judging the output, so nothing to hide) and the
              transcript is corrected from a pre-filled box, because typing
              Greek from scratch is the only genuinely slow part.

Stage 1 is committed before stage 2 is rendered, so a stage-1 answer cannot
be revised once the prediction is on screen.

SKIPPING

Either stage offers Skip, for a clip that cannot honestly be judged -- most
often speech too unintelligible to write a reference transcript for. A skipped
clip leaves the queue and is excluded from every metric: it is not a zero-error
clip, not a wrong answer, and not part of any denominator. Counts of skips are
reported alongside the results so the exclusions stay visible.

SCOPE

Speaker truth is one dominant speaker per clip over three residents. Clips
carrying a Speaker_N label are excluded at sampling time: those come from an
older diarisation path and are not what the current pipeline does.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import textwrap
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "annotation_store")
STORE_DB = os.path.join(STORE_DIR, "annotations.db")
AUDIO_DIR = os.path.join(STORE_DIR, "audio")
SOURCE_DB = os.path.join(PROJECT_ROOT, "ast_audio_db", "ast_audio.db")

# Windows consoles default to cp1252 and this prints Greek.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # pragma: no cover
    pass


# ── what the pipeline says, parsed ──────────────────────────────────

# Kept identical to ast_src/main.py. The gate metric is only meaningful if it
# is computed with the same label set the application actually gates on.
SPEECH_LABELS = {
    "speech", "male speech", "female speech", "child speech",
    "conversation", "narration", "monologue", "babbling", "shouting",
    "yelling", "whispering", "singing", "rapping",
}

RESIDENTS = ["Ilyas", "Voula", "Rafael"]
# What the annotator may answer. OTHER covers a visitor, the television or a
# radio -- a voice that is nobody in the closed set. NO_SPEECH is not a
# speaker answer at all and drops the clip out of the speaker matrix.
# MULTIPLE says two or more residents share the clip, with the set itself in
# annotations.true_speakers -- see the presence metric in compute_stats.
SPEAKER_CHOICES = RESIDENTS + ["Other", "Unclear", "Multiple", "NoSpeech"]
# What the pipeline may answer. "Unknown" is a real outcome, distinct from
# naming the wrong person, and has to appear in the matrix as its own column.
SPEAKER_PREDICTIONS = RESIDENTS + ["Unknown"]

EMOTIONS = ["neutral", "happy", "angry", "sad"]
EMOTION_CHOICES = EMOTIONS + ["unclear"]
EMOTION_CODES = {
    "HAP": "happy", "HAPPY": "happy", "SAD": "sad",
    "ANG": "angry", "ANGRY": "angry", "NEU": "neutral", "NEUTRAL": "neutral",
    "FEA": "fearful", "DIS": "disgust", "SUR": "surprised",
}

AST_VERDICTS = ["acceptable", "partly", "wrong"]

# Tolerates the stray spaces the ASR writer emits: "[ 00: 00. 00 - 00: 29. 98 ]"
SEGMENT_RE = re.compile(
    r"\[\s*(?P<start>[\d:.\s]+?)\s*-\s*(?P<end>[\d:.\s]+?)\s*\]\s*"
    r"(?P<speaker>[^:\[\]]*?)\s*:\s*(?P<text>.*)")

# The same marker, matched anywhere rather than anchored to a line start. The
# pipeline joins its segments with newlines, but its repetition cleaner then
# rejoins every token with spaces, so a multi-turn transcript reaches this
# tool as a single line and a line-anchored match saw only the first turn.
SEGMENT_HEAD_RE = re.compile(
    r"\[\s*(?P<start>[\d:.\s]+?)\s*-\s*(?P<end>[\d:.\s]+?)\s*\]\s*"
    r"(?P<speaker>[^:\[\]]{0,32}?)\s*:\s*")
LEGACY_SPEAKER_RE = re.compile(r"\bSpeaker[_ ]?\d+\b", re.I)


def parse_clock(text: str) -> float | None:
    """'00: 01. 23' -> 83.0 seconds."""
    cleaned = re.sub(r"\s+", "", text or "")
    if not cleaned:
        return None
    try:
        if ":" in cleaned:
            mm, ss = cleaned.split(":", 1)
            return int(mm) * 60 + float(ss)
        return float(cleaned)
    except ValueError:
        return None


def parse_transcript(transcription: str) -> tuple[str, str, list]:
    """(dominant speaker, spoken text, segments) from one transcript blob.

    Dominant is by total speech duration rather than segment count: one long
    turn is more of the clip than three interjections, and the clip carries a
    single speaker label.
    """
    blob = transcription or ""
    heads = list(SEGMENT_HEAD_RE.finditer(blob))
    segments = []
    for i, m in enumerate(heads):
        # A turn's text runs from the end of its own marker to the start of
        # the next one, so turns survive whether they are newline-separated
        # or collapsed onto one line.
        stop = heads[i + 1].start() if i + 1 < len(heads) else len(blob)
        start = parse_clock(m.group("start"))
        end = parse_clock(m.group("end"))
        duration = (end - start) if (start is not None and end is not None
                                     and end > start) else 0.0
        segments.append({
            "speaker": (m.group("speaker") or "").strip() or "Unknown",
            "text": blob[m.end():stop].strip(),
            "seconds": duration,
        })

    if not segments:
        return "Unknown", "", []

    by_speaker = defaultdict(float)
    for s in segments:
        # A zero-length segment still counts a little, or a clip whose clocks
        # failed to parse would have no dominant speaker at all.
        by_speaker[s["speaker"]] += s["seconds"] + 0.001
    dominant = max(by_speaker, key=by_speaker.get)

    text = " ".join(s["text"] for s in segments if s["text"]).strip()
    return dominant, text, segments


def parse_emotion(value: str) -> tuple[str | None, float | None]:
    """'NEU (0.61)' -> ('neutral', 0.61)."""
    if not value or value.strip() in ("None", ""):
        return None, None
    m = re.match(r"\s*([A-Za-z]+)\s*(?:\(\s*([\d.]+)\s*\))?", value)
    if not m:
        return None, None
    label = EMOTION_CODES.get(m.group(1).upper())
    conf = float(m.group(2)) if m.group(2) else None
    return label, conf


def predicted_speech(labels: list) -> bool:
    """The gate exactly as ast_src/main.py applies it."""
    return any((lab or "").lower() in SPEECH_LABELS for lab in labels)


# ── the store ───────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id              INTEGER PRIMARY KEY,   -- ast_table.id, so a clip is traceable
    date            TEXT,
    device          TEXT,
    room            TEXT,
    file_name       TEXT,
    audio_rel       TEXT,                  -- copy inside this store
    rms             REAL,
    snr             REAL,                  -- dB, NULL on clips predating it
    sound1 TEXT, conf1 REAL,
    sound2 TEXT, conf2 REAL,
    sound3 TEXT, conf3 REAL,
    transcription   TEXT,                  -- raw, as the pipeline wrote it
    emotion_raw     TEXT,
    pred_speaker    TEXT,
    pred_text       TEXT,
    pred_emotion    TEXT,
    pred_emotion_conf REAL,
    pred_speech     INTEGER,
    stratum         TEXT
);

CREATE TABLE IF NOT EXISTS annotations (
    clip_id       INTEGER PRIMARY KEY REFERENCES clips(id),
    true_speech   INTEGER,
    true_speaker  TEXT,
    true_emotion  TEXT,
    ast1 TEXT, ast2 TEXT, ast3 TEXT,
    -- Set only when true_speaker is "Multiple": the residents the annotator
    -- can hear, comma-joined. A clip with two people talking has no single
    -- right answer to score a one-label prediction against, so it leaves the
    -- confusion matrix and is scored on presence instead.
    true_speakers TEXT,
    true_text     TEXT,
    notes         TEXT,
    stage1_at     TEXT,
    stage2_at     TEXT,
    -- A clip the annotator could not judge (unintelligible speech, a recording
    -- that is not worth a reference transcript). The row exists so the clip is
    -- not served again, and every scoring query excludes it: a skip must not
    -- become a silent zero-error or a forced wrong answer anywhere.
    skipped       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def store_init() -> sqlite3.Connection:
    """Create the store if it is not there yet. Called once per command."""
    os.makedirs(STORE_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    conn = sqlite3.connect(STORE_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Stores created before skipping existed have the rest of the table right
    # but not this column; CREATE TABLE IF NOT EXISTS will not add it.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(annotations)")}
    if "skipped" not in cols:
        conn.execute("ALTER TABLE annotations ADD COLUMN"
                     " skipped INTEGER NOT NULL DEFAULT 0")
    if "true_speakers" not in cols:
        conn.execute("ALTER TABLE annotations ADD COLUMN true_speakers TEXT")
    clip_cols = {r["name"] for r in conn.execute("PRAGMA table_info(clips)")}
    if "snr" not in clip_cols:
        conn.execute("ALTER TABLE clips ADD COLUMN snr REAL")
    conn.commit()
    return conn


def store_connect() -> sqlite3.Connection:
    """A plain connection, with no schema work.

    Deliberately separate from store_init: executescript takes a write lock,
    and running it on every web request made several concurrent requests (the
    page, its audio, the favicon) contend for that lock on every page load.
    """
    conn = sqlite3.connect(STORE_DB, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def source_connect() -> sqlite3.Connection:
    if not os.path.exists(SOURCE_DB):
        sys.exit(f"Source database not found: {SOURCE_DB}")
    conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def find_audio(file_name: str) -> str | None:
    """Locate a clip's audio, trying both container extensions."""
    if not file_name:
        return None
    base = os.path.basename(file_name)
    candidates = [base]
    stem, ext = os.path.splitext(base)
    for alt in (".wav", ".mp3", ".flac", ".ogg"):
        if alt != ext.lower():
            candidates.append(stem + alt)
    for folder in ("audio_mp3", "audios", "audiosuser"):
        for name in candidates:
            path = os.path.join(PROJECT_ROOT, "assets", folder, name)
            if os.path.exists(path):
                return path
    return None


# ── sampling ────────────────────────────────────────────────────────

def cmd_sample(args):
    """Draw a stratified sample and freeze it into this tool's own store."""
    # Timestamps are stored as "YYYY-MM-DD HH:MM:SS", which sorts correctly as
    # text, so a plain string comparison is an exact datetime window. A bare
    # date on --until means the whole of that day; a date with a time is taken
    # literally, which is what a "same hour tomorrow" window needs.
    since = args.since.strip()
    until = args.until.strip()
    if ":" not in until:
        until += " 23:59:59"

    src = source_connect()
    # snr arrived after this tool did, so it is selected only when the source
    # database actually has it; an older database still samples cleanly.
    source_cols = {r[1] for r in src.execute("PRAGMA table_info(ast_table)")}
    snr_select = "snr" if "snr" in source_cols else "NULL AS snr"
    rows = src.execute(
        "SELECT id, date, device_name, room, file_name, rms, "
        f"       {snr_select}, "
        "       sound1, conf_sound1, sound2, conf_sound2, sound3, conf_sound3, "
        "       transcription, emotion "
        "FROM ast_table WHERE date >= ? AND date <= ? ORDER BY date",
        (since, until)).fetchall()
    print(f"window: {since}  ->  {until}")
    print(f"source rows in window: {len(rows)}")

    speech_pool, other_pool = [], []
    skipped_legacy = skipped_audio = 0

    for r in rows:
        transcription = r["transcription"] or ""
        # Speaker_N is the old diarisation path, not what the pipeline does
        # now; scoring it would measure a version that no longer exists.
        if LEGACY_SPEAKER_RE.search(transcription):
            skipped_legacy += 1
            continue
        audio = find_audio(r["file_name"])
        if not audio:
            skipped_audio += 1
            continue

        labels = [r["sound1"], r["sound2"], r["sound3"]]
        dominant, text, _ = parse_transcript(transcription)
        emo, emo_conf = parse_emotion(r["emotion"])
        entry = {
            "row": r, "audio": audio, "labels": labels,
            "pred_speaker": dominant, "pred_text": text,
            "pred_emotion": emo, "pred_emotion_conf": emo_conf,
            "pred_speech": predicted_speech(labels),
        }
        # Stratify on what the pipeline believes, not on truth (which is what
        # we are here to establish). A sample drawn only from clips the system
        # thinks contain speech can never measure the gate's false negatives,
        # so both sides are represented deliberately.
        (speech_pool if entry["pred_speech"] else other_pool).append(entry)

    print(f"  usable: {len(speech_pool)} predicted-speech, "
          f"{len(other_pool)} predicted-non-speech")
    print(f"  skipped: {skipped_legacy} legacy Speaker_N, "
          f"{skipped_audio} missing audio")

    rng = random.Random(args.seed)
    n_speech = min(len(speech_pool), max(1, round(args.n * args.speech_fraction)))
    n_other = min(len(other_pool), args.n - n_speech)
    # If one pool is short, spend the remainder on the other rather than
    # returning a smaller sample than asked for.
    n_speech = min(len(speech_pool), args.n - n_other)

    picked = (rng.sample(speech_pool, n_speech) + rng.sample(other_pool, n_other))
    rng.shuffle(picked)

    conn = store_init()
    existing = {r[0] for r in conn.execute("SELECT id FROM clips")}
    added = 0
    for e in picked:
        r = e["row"]
        if r["id"] in existing:
            continue
        ext = os.path.splitext(e["audio"])[1]
        rel = f"{r['id']}{ext}"
        shutil.copyfile(e["audio"], os.path.join(AUDIO_DIR, rel))
        conn.execute(
            "INSERT INTO clips (id, date, device, room, file_name, audio_rel, rms,"
            " snr, sound1, conf1, sound2, conf2, sound3, conf3, transcription,"
            " emotion_raw, pred_speaker, pred_text, pred_emotion,"
            " pred_emotion_conf, pred_speech, stratum)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r["date"], r["device_name"], r["room"], r["file_name"], rel,
             r["rms"], r["snr"],
             r["sound1"], r["conf_sound1"], r["sound2"], r["conf_sound2"],
             r["sound3"], r["conf_sound3"], r["transcription"], r["emotion"],
             e["pred_speaker"], e["pred_text"], e["pred_emotion"],
             e["pred_emotion_conf"], int(e["pred_speech"]),
             "speech" if e["pred_speech"] else "non-speech"))
        added += 1

    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                 ("last_sample", json.dumps({
                     "at": datetime.now().isoformat(timespec="seconds"),
                     "since": since, "until": until,
                     "seed": args.seed, "n_requested": args.n,
                     "n_added": added,
                     "speech_fraction": args.speech_fraction})))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    print(f"\nadded {added} clip(s); store now holds {total}")
    print(f"store: {STORE_DB}")
    print("\nnext:  python tests/diagnostics/annotate_components.py annotate")


# ── metrics ─────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


# "[ 00: 02. 56 - 00: 04. 98 ] Voula:" -- one diarisation marker, anywhere in
# the string rather than only at the start of a line.
SEGMENT_MARKER_RE = re.compile(
    r"\[\s*[\d:.\s]+-\s*[\d:.\s]+\]\s*[^:\[\]]{0,32}:\s*")


def strip_segment_markers(text: str) -> str:
    """Remove the diarisation prefixes so only spoken words are scored.

    parse_transcript() splits on newlines, but the pipeline's repetition
    cleaner rejoins its segments with spaces, so a multi-turn transcript
    arrives as one line and only the first marker is recognised. The rest
    stay embedded in pred_text -- and, because the annotation form is
    pre-filled from pred_text, in true_text as well.

    Identical markers on both sides always match, so they were scored as
    correct words. On the 29-30 Aug run that was 196 of 654 reference
    "words": WER read 0.266 instead of 0.376 and CER 0.212 instead of 0.256.
    A speaker name and a timestamp are not speech and must not flatter the
    transcription.
    """
    return re.sub(r"\s+", " ", SEGMENT_MARKER_RE.sub(" ", text or "")).strip()


def normalise(text: str) -> str:
    """Lowercase, strip Greek tonos and punctuation, collapse whitespace.

    The headline WER is computed on this. Raw WER is reported beside it, so a
    reader can see how much of the error is orthographic rather than lexical
    -- in Greek that difference is large and hiding it would flatter the
    model.
    """
    lowered = (text or "").lower()
    decomposed = unicodedata.normalize("NFD", lowered)
    stripped = "".join(c for c in decomposed
                       if unicodedata.category(c) != "Mn")
    stripped = unicodedata.normalize("NFC", stripped)
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", stripped)).strip()


def edit_ops(ref: list, hyp: list) -> tuple[int, int, int]:
    """Levenshtein alignment returning (substitutions, deletions, insertions).

    S/D/I are kept apart rather than summed because they diagnose different
    faults: insertions are Whisper hallucinating into silence, deletions are
    speech the gate or the decoder dropped.
    """
    n, m = len(ref), len(hyp)
    # cost matrix plus a backtrace of which operation was cheapest
    d = [[0] * (m + 1) for _ in range(n + 1)]
    bt = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        d[i][0] = i
        bt[i][0] = "D"
    for j in range(1, m + 1):
        d[0][j] = j
        bt[0][j] = "I"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                d[i][j] = d[i - 1][j - 1]
                bt[i][j] = "="
                continue
            sub, dele, ins = d[i - 1][j - 1] + 1, d[i - 1][j] + 1, d[i][j - 1] + 1
            best = min(sub, dele, ins)
            d[i][j] = best
            bt[i][j] = "S" if best == sub else ("D" if best == dele else "I")

    s = dl = ins = 0
    i, j = n, m
    while i > 0 or j > 0:
        op = bt[i][j]
        if op == "=":
            i, j = i - 1, j - 1
        elif op == "S":
            s += 1; i, j = i - 1, j - 1
        elif op == "D":
            dl += 1; i -= 1
        else:
            ins += 1; j -= 1
    return s, dl, ins


def error_rate(pairs: list, unit: str = "word") -> dict:
    """Corpus-level and per-clip error rate over (reference, hypothesis)."""
    tot_s = tot_d = tot_i = tot_n = 0
    per_clip = []
    for ref, hyp in pairs:
        r = ref.split() if unit == "word" else list(ref.replace(" ", ""))
        h = hyp.split() if unit == "word" else list(hyp.replace(" ", ""))
        s, dl, ins = edit_ops(r, h)
        tot_s += s; tot_d += dl; tot_i += ins; tot_n += len(r)
        if r:
            per_clip.append((s + dl + ins) / len(r))
    return {
        "rate": (tot_s + tot_d + tot_i) / tot_n if tot_n else None,
        "substitutions": tot_s, "deletions": tot_d, "insertions": tot_i,
        "reference_units": tot_n, "n_clips": len(per_clip),
        "per_clip": per_clip,
    }


def bootstrap_ci(pairs: list, unit: str = "word", iterations: int = 2000,
                 seed: int = 0) -> tuple[float, float] | None:
    """Percentile CI by resampling CLIPS, not words.

    Resampling words would treat every word as independent and give an
    interval far too tight; the clip is the sampling unit, so it is the unit
    that must be resampled.

    Each clip's (errors, reference length) is computed ONCE and the resampling
    then only re-sums those pairs. Aligning inside the loop instead meant 2000
    full Levenshtein passes over the same text -- twelve seconds on three
    clips, and minutes on a realistic sample.
    """
    if len(pairs) < 2:
        return None
    counted = []
    for ref, hyp in pairs:
        r = ref.split() if unit == "word" else list(ref.replace(" ", ""))
        h = hyp.split() if unit == "word" else list(hyp.replace(" ", ""))
        s, d, i = edit_ops(r, h)
        counted.append((s + d + i, len(r)))

    rng = random.Random(seed)
    n = len(counted)
    rates = []
    for _ in range(iterations):
        errors = length = 0
        for _ in range(n):
            e, ln = counted[rng.randrange(n)]
            errors += e
            length += ln
        if length:
            rates.append(errors / length)
    if not rates:
        return None
    rates.sort()
    return (rates[int(0.025 * len(rates))], rates[int(0.975 * len(rates)) - 1])


def confusion(pairs: list, true_classes: list, pred_classes: list) -> dict:
    """Counts keyed [true][pred], plus per-class precision/recall/F1."""
    matrix = {t: {p: 0 for p in pred_classes} for t in true_classes}
    for t, p in pairs:
        if t in matrix and p in matrix[t]:
            matrix[t][p] += 1

    per_class = {}
    for c in true_classes:
        tp = matrix[c].get(c, 0)
        fn = sum(v for k, v in matrix[c].items() if k != c)
        fp = sum(matrix[t].get(c, 0) for t in true_classes if t != c)
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision and recall else (0.0 if (tp + fp + fn) else None))
        per_class[c] = {"tp": tp, "fp": fp, "fn": fn, "support": tp + fn,
                        "precision": precision, "recall": recall, "f1": f1}

    scored = [m["f1"] for m in per_class.values() if m["f1"] is not None]
    recalls = [m["recall"] for m in per_class.values() if m["recall"] is not None]
    correct = sum(matrix[c].get(c, 0) for c in true_classes)
    total = sum(sum(r.values()) for r in matrix.values())
    return {
        "matrix": matrix, "per_class": per_class,
        "n": total,
        "accuracy": correct / total if total else None,
        "macro_f1": sum(scored) / len(scored) if scored else None,
        "balanced_accuracy": sum(recalls) / len(recalls) if recalls else None,
    }


def binary_scores(pairs: list) -> dict:
    """(true, predicted) booleans -> precision / recall / F1 for the gate."""
    tp = sum(1 for t, p in pairs if t and p)
    fp = sum(1 for t, p in pairs if not t and p)
    fn = sum(1 for t, p in pairs if t and not p)
    tn = sum(1 for t, p in pairs if not t and not p)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall else None)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "n": tp + fp + fn + tn,
            "precision": precision, "recall": recall, "f1": f1,
            "accuracy": (tp + tn) / (tp + fp + fn + tn) if pairs else None}


def ast_recall_at_k(records: list) -> dict:
    """correct@1/2/3 from the annotator's per-label verdicts.

    Two readings of the same judgements. Strict counts only 'acceptable';
    lenient also counts 'partly', which is the honest way to handle a label
    that is in the right family but not the right leaf (Speech vs
    Conversation). Reporting both removes the argument about where that line
    should sit.
    """
    out = {}
    for name, accepted in (("strict", {"acceptable"}),
                           ("lenient", {"acceptable", "partly"})):
        hits = {1: 0, 2: 0, 3: 0}
        n = 0
        for verdicts in records:
            if not any(v for v in verdicts):
                continue
            n += 1
            for k in (1, 2, 3):
                if any(v in accepted for v in verdicts[:k] if v):
                    hits[k] += 1
        out[name] = {"n": n,
                     **{f"correct@{k}": (hits[k] / n if n else None)
                        for k in (1, 2, 3)},
                     **{f"hits@{k}": hits[k] for k in (1, 2, 3)}}
    return out


# ── scoring ─────────────────────────────────────────────────────────

def collect(conn) -> list:
    """Every judged clip. Skipped clips are annotated but not judged, so they
    are excluded here and therefore from every metric downstream."""
    return conn.execute(
        "SELECT c.*, a.true_speech, a.true_speaker, a.true_speakers,"
        "       a.true_emotion,"
        "       a.ast1, a.ast2, a.ast3, a.true_text, a.notes, a.stage2_at"
        " FROM clips c JOIN annotations a ON a.clip_id = c.id"
        " WHERE a.stage2_at IS NOT NULL AND a.skipped = 0"
        " ORDER BY c.date").fetchall()


def count_skipped(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM annotations WHERE skipped = 1").fetchone()[0]


def compute_stats(rows: list, n_skipped: int = 0) -> dict:
    report = {"n_annotated": len(rows),
              "n_skipped": n_skipped,
              "generated_at": datetime.now().isoformat(timespec="seconds")}
    if not rows:
        return report

    # ASR. Only clips the annotator confirmed contain speech and for which a
    # reference was typed; a clip with no speech has no word error rate.
    wer_pairs_norm, wer_pairs_raw = [], []
    for r in rows:
        if not r["true_speech"]:
            continue
        # Speaker labels and turn timings are diarisation output, not words
        # anyone said. Scored on both sides they match for free and understate
        # the error -- see strip_segment_markers.
        ref = strip_segment_markers(r["true_text"])
        hyp = strip_segment_markers(r["pred_text"])
        if not ref:
            continue
        wer_pairs_raw.append((ref, hyp))
        wer_pairs_norm.append((normalise(ref), normalise(hyp)))

    if wer_pairs_norm:
        wer = error_rate(wer_pairs_norm, "word")
        per = sorted(wer["per_clip"])
        report["asr"] = {
            "wer_normalised": wer["rate"],
            "wer_raw": error_rate(wer_pairs_raw, "word")["rate"],
            "cer_normalised": error_rate(wer_pairs_norm, "char")["rate"],
            "substitutions": wer["substitutions"],
            "deletions": wer["deletions"],
            "insertions": wer["insertions"],
            "reference_words": wer["reference_units"],
            "n_clips": wer["n_clips"],
            "wer_median": per[len(per) // 2] if per else None,
            "wer_iqr": ([per[len(per) // 4], per[(3 * len(per)) // 4]]
                        if len(per) >= 4 else None),
            "wer_ci95": bootstrap_ci(wer_pairs_norm, "word"),
        }

    # Speaker. Scored over the residents only: "Other" and "Unclear" are real
    # annotator answers but they are not classes the closed set can predict,
    # so they are reported separately instead of inflating the matrix.
    spk_pairs = [(r["true_speaker"], r["pred_speaker"] if r["pred_speaker"]
                  in SPEAKER_PREDICTIONS else "Unknown")
                 for r in rows
                 if r["true_speech"] and r["true_speaker"] in RESIDENTS]
    if spk_pairs:
        report["speaker"] = confusion(spk_pairs, RESIDENTS, SPEAKER_PREDICTIONS)
    # Presence. A clip with two residents in it has no single right answer, so
    # it cannot sit in the matrix above -- but the prediction is not therefore
    # unscoreable: naming someone who is genuinely in the clip is still worth
    # counting. Single-speaker clips join in as a set of one, so the two kinds
    # of clip are measured the same way and the figure covers the whole set.
    presence, multi = [], []
    for r in rows:
        if not r["true_speech"]:
            continue
        if r["true_speaker"] in RESIDENTS:
            truth = {r["true_speaker"]}
        elif r["true_speaker"] == "Multiple":
            truth = {s.strip() for s in (r["true_speakers"] or "").split(",")
                     if s.strip()}
            if len(truth) < 2:
                continue        # an incomplete answer, not a set
        else:
            continue
        hit = r["pred_speaker"] in truth
        presence.append(hit)
        if r["true_speaker"] == "Multiple":
            multi.append((hit, r["pred_speaker"], sorted(truth)))
    if presence:
        report["speaker_presence"] = {
            "n": len(presence),
            "hits": sum(presence),
            "accuracy": sum(presence) / len(presence),
            "multi_n": len(multi),
            "multi_hits": sum(1 for h, _, _ in multi if h),
            "multi_accuracy": ((sum(1 for h, _, _ in multi if h) / len(multi))
                               if multi else None),
            # Who it named on a shared clip when that person was not in it.
            "multi_missed_as": dict(Counter(
                p for h, p, _ in multi if not h)),
        }
    report["speaker_offset"] = {
        "true_other": sum(1 for r in rows if r["true_speaker"] == "Other"),
        "true_unclear": sum(1 for r in rows if r["true_speaker"] == "Unclear"),
        "true_multiple": sum(1 for r in rows
                             if r["true_speaker"] == "Multiple"),
        # What the closed set did with a voice that was nobody it knows. Every
        # one of these is a forced misattribution by design, and it is the
        # number that shows the cost of having no reject option.
        "other_predicted_as": dict(Counter(
            r["pred_speaker"] for r in rows if r["true_speaker"] == "Other")),
    }

    # Emotion. 'unclear' is excluded from the matrix rather than folded into a
    # class, and reported as its own count.
    emo_pairs = [(r["true_emotion"], r["pred_emotion"]) for r in rows
                 if r["true_emotion"] in EMOTIONS and r["pred_emotion"] in EMOTIONS]
    if emo_pairs:
        report["emotion"] = confusion(emo_pairs, EMOTIONS, EMOTIONS)
    report["emotion_offset"] = {
        "true_unclear": sum(1 for r in rows if r["true_emotion"] == "unclear"),
        "no_prediction": sum(1 for r in rows if not r["pred_emotion"]),
    }

    # AST and the speech gate.
    report["ast"] = ast_recall_at_k([(r["ast1"], r["ast2"], r["ast3"])
                                     for r in rows])
    report["speech_gate"] = binary_scores(
        [(bool(r["true_speech"]), bool(r["pred_speech"])) for r in rows])
    return report


# ── presentation ────────────────────────────────────────────────────

def pct(value, digits=1):
    return "  --  " if value is None else f"{100 * value:.{digits}f}%"


def print_matrix(title, conf_result, true_classes, pred_classes):
    print(f"\n{title}")
    matrix = conf_result["matrix"]
    # Each cell renders as "count share diagonal-mark", which is exactly ten
    # characters; the header column must match or the columns drift apart.
    width = max(10, max(len(c) for c in pred_classes) + 2)
    header = "  true \\ pred ".ljust(16) + "".join(c.rjust(width) for c in pred_classes)
    print("  " + "-" * (len(header) + 8))
    print(header + "     total")
    for t in true_classes:
        row = matrix[t]
        total = sum(row.values())
        cells = ""
        for p in pred_classes:
            n = row[p]
            share = f"{100 * n / total:.0f}%" if total else "-"
            mark = "*" if t == p else " "
            cells += f"{n:>4} {share:>4}{mark}".rjust(width)
        print(f"  {t:<14}" + cells + f"{total:>10}")
    print()
    print(f"  {'class':<10}{'prec':>8}{'recall':>9}{'F1':>8}{'n':>6}")
    for c in true_classes:
        m = conf_result["per_class"][c]
        print(f"  {c:<10}{pct(m['precision']):>8}{pct(m['recall']):>9}"
              f"{pct(m['f1']):>8}{m['support']:>6}")
    print(f"\n  accuracy {pct(conf_result['accuracy'])}   "
          f"macro-F1 {pct(conf_result['macro_f1'])}   "
          f"balanced acc {pct(conf_result['balanced_accuracy'])}   "
          f"n={conf_result['n']}")


def print_report(report: dict):
    n = report["n_annotated"]
    skipped = report.get("n_skipped") or 0
    print("\n" + "=" * 74)
    print(f"  COMPONENT EVALUATION      {n} annotated clip(s)"
          + (f", {skipped} skipped" if skipped else ""))
    print("=" * 74)
    if not n:
        print("\n  Nothing annotated yet.")
        return

    if "asr" in report:
        a = report["asr"]
        ci = a["wer_ci95"]
        print(f"\nASR  ({a['n_clips']} clips, {a['reference_words']} reference words)")
        print(f"  WER normalised   {pct(a['wer_normalised'])}"
              + (f"   95% CI [{pct(ci[0])}, {pct(ci[1])}]" if ci else ""))
        print(f"  WER raw          {pct(a['wer_raw'])}")
        print(f"  CER normalised   {pct(a['cer_normalised'])}")
        print(f"  per-clip median  {pct(a['wer_median'])}"
              + (f"   IQR [{pct(a['wer_iqr'][0])}, {pct(a['wer_iqr'][1])}]"
                 if a["wer_iqr"] else ""))
        print(f"  errors           {a['substitutions']} sub / "
              f"{a['deletions']} del / {a['insertions']} ins")
    else:
        print("\nASR  no clips with confirmed speech and a reference transcript yet")

    if "speaker" in report:
        print_matrix("SPEAKER  (dominant speaker per clip)", report["speaker"],
                     RESIDENTS, SPEAKER_PREDICTIONS)
    else:
        print("\nSPEAKER  no clips with a resident as the true speaker yet")
    off = report["speaker_offset"]
    if off["true_other"] or off["true_unclear"] or off["true_multiple"]:
        print(f"  outside the closed set: {off['true_other']} not-a-resident, "
              f"{off['true_unclear']} unclear, "
              f"{off['true_multiple']} with two or more residents")
        if off["other_predicted_as"]:
            named = ", ".join(f"{k}={v}" for k, v in off["other_predicted_as"].items())
            print(f"  non-residents were named as: {named}")
    if "speaker_presence" in report:
        pr = report["speaker_presence"]
        print(f"\n  named someone actually in the clip: {pr['hits']}/{pr['n']}"
              f"  ({pr['accuracy']:.1%})")
        if pr["multi_n"]:
            print(f"    of which shared clips: {pr['multi_hits']}/{pr['multi_n']}"
                  f"  ({pr['multi_accuracy']:.1%})")
            if pr["multi_missed_as"]:
                named = ", ".join(f"{k}={v}"
                                  for k, v in pr["multi_missed_as"].items())
                print(f"    shared clips missed as: {named}")

    if "emotion" in report:
        print_matrix("EMOTION", report["emotion"], EMOTIONS, EMOTIONS)
    else:
        print("\nEMOTION  no clips with a rateable emotion and a prediction yet")
    eo = report["emotion_offset"]
    print(f"  excluded: {eo['true_unclear']} unclear, "
          f"{eo['no_prediction']} without a prediction")

    ast = report["ast"]
    print(f"\nAST  (n={ast['strict']['n']})")
    print(f"  {'':<10}{'@1':>9}{'@2':>9}{'@3':>9}")
    for mode in ("strict", "lenient"):
        m = ast[mode]
        print(f"  {mode:<10}" + "".join(pct(m[f'correct@{k}']).rjust(9)
                                        for k in (1, 2, 3)))
    print("  strict = 'acceptable' only; lenient also counts 'partly'")

    g = report["speech_gate"]
    print(f"\nSPEECH GATE  (n={g['n']})")
    print(f"  precision {pct(g['precision'])}   recall {pct(g['recall'])}   "
          f"F1 {pct(g['f1'])}   accuracy {pct(g['accuracy'])}")
    print(f"  tp={g['tp']}  fp={g['fp']}  fn={g['fn']}  tn={g['tn']}")
    print(f"  fn are clips with speech the pipeline never transcribed\n")


# ── the same report, rendered for the browser ───────────────────────

def _cell(n: int, total: int, diagonal: bool) -> str:
    share = (n / total) if total else 0.0
    # Shade by row share so the diagonal reads at a glance; the diagonal is
    # tinted green and everything off it red, because on a confusion matrix
    # the two mean opposite things.
    hue = "126,216,143" if diagonal else "227,120,120"
    bg = f"rgba({hue},{0.06 + 0.5 * share:.2f})" if n else "transparent"
    pctxt = f"{100 * share:.0f}%" if total else "-"
    return (f'<td style="background:{bg}"><b>{n}</b>'
            f'<span class="sh">{pctxt}</span></td>')


def matrix_html(title: str, conf: dict, true_classes: list,
                pred_classes: list) -> str:
    m = conf["matrix"]
    head = "".join(f"<th>{p}</th>" for p in pred_classes)
    rows = ""
    for t in true_classes:
        total = sum(m[t].values())
        cells = "".join(_cell(m[t][p], total, t == p) for p in pred_classes)
        rows += f"<tr><th class='rh'>{t}</th>{cells}<td class='tot'>{total}</td></tr>"

    per = ""
    for c in true_classes:
        s = conf["per_class"][c]
        per += (f"<tr><td>{c}</td><td>{pct(s['precision'])}</td>"
                f"<td>{pct(s['recall'])}</td><td>{pct(s['f1'])}</td>"
                f"<td>{s['support']}</td></tr>")

    return f"""
    <h3>{title}</h3>
    <table class="cm">
      <tr><th class="corner">true \\ predicted</th>{head}<th>total</th></tr>
      {rows}
    </table>
    <table class="pc">
      <tr><th>class</th><th>precision</th><th>recall</th><th>F1</th><th>n</th></tr>
      {per}
    </table>
    <p class="sum">accuracy <b>{pct(conf['accuracy'])}</b> &nbsp;·&nbsp;
       macro-F1 <b>{pct(conf['macro_f1'])}</b> &nbsp;·&nbsp;
       balanced accuracy <b>{pct(conf['balanced_accuracy'])}</b>
       &nbsp;·&nbsp; n={conf['n']}</p>"""


def report_html(report: dict, total_clips: int) -> str:
    n = report["n_annotated"]
    if not n:
        return "<div class='card'><h2>No annotations yet</h2></div>"

    skipped = report.get("n_skipped") or 0
    out = [f"<div class='card'><h2>Results</h2>"
           f"<p class='sum'>{n} of {total_clips} clip(s) annotated"
           + (f" &nbsp;·&nbsp; {skipped} skipped, excluded from everything below"
              if skipped else "")
           + "</p>"]

    if n < 10:
        out.append("<p class='caution'>Too few clips for these numbers to mean "
                   "anything &mdash; this is a check that the pipeline runs, "
                   "not a measurement.</p>")

    a = report.get("asr")
    if a:
        ci = a["wer_ci95"]
        ci_txt = f" &nbsp;95% CI [{pct(ci[0])}, {pct(ci[1])}]" if ci else ""
        out.append(f"""
        <h3>ASR &mdash; {a['n_clips']} clip(s), {a['reference_words']} reference words</h3>
        <table class="pc">
          <tr><th>metric</th><th>value</th></tr>
          <tr><td>WER (normalised)</td><td><b>{pct(a['wer_normalised'])}</b>{ci_txt}</td></tr>
          <tr><td>WER (raw)</td><td>{pct(a['wer_raw'])}</td></tr>
          <tr><td>CER (normalised)</td><td>{pct(a['cer_normalised'])}</td></tr>
          <tr><td>per-clip median</td><td>{pct(a['wer_median'])}</td></tr>
          <tr><td>substitutions / deletions / insertions</td>
              <td>{a['substitutions']} / {a['deletions']} / {a['insertions']}</td></tr>
        </table>""")
    else:
        out.append("<h3>ASR</h3><p class='sum'>no clip yet with confirmed "
                   "speech and a reference transcript</p>")

    if "speaker" in report:
        out.append(matrix_html("Speaker &mdash; dominant speaker per clip",
                               report["speaker"], RESIDENTS, SPEAKER_PREDICTIONS))
    else:
        out.append("<h3>Speaker</h3><p class='sum'>no clip yet whose true "
                   "speaker is one of the three residents</p>")

    off = report["speaker_offset"]
    if off["true_other"] or off["true_unclear"] or off["true_multiple"]:
        named = ", ".join(f"{k} ({v})" for k, v in off["other_predicted_as"].items())
        out.append(f"<p class='sum'>outside the closed set: "
                   f"{off['true_other']} not a resident, {off['true_unclear']} unclear,"
                   f" {off['true_multiple']} with two or more residents"
                   + (f" &mdash; non-residents named by the pipeline as {named}"
                      if named else "")
                   + "</p>")
    if "speaker_presence" in report:
        pr = report["speaker_presence"]
        extra = ""
        if pr["multi_n"]:
            extra = (f" On the {pr['multi_n']} clip(s) with two or more "
                     f"residents, where no single label can be right, it named "
                     f"one of the voices present {pr['multi_hits']} time(s) "
                     f"({pr['multi_accuracy']:.1%}).")
        out.append(f"<p class='sum'>Presence &mdash; the pipeline named someone "
                   f"genuinely in the clip on {pr['hits']} of {pr['n']} clip(s) "
                   f"({pr['accuracy']:.1%}).{extra}</p>")

    if "emotion" in report:
        out.append(matrix_html("Emotion", report["emotion"], EMOTIONS, EMOTIONS))
    else:
        out.append("<h3>Emotion</h3><p class='sum'>no clip yet with both a "
                   "rateable emotion and a prediction</p>")

    ast = report["ast"]
    out.append(f"""
    <h3>AST sound labels &mdash; n={ast['strict']['n']}</h3>
    <table class="pc">
      <tr><th></th><th>correct@1</th><th>correct@2</th><th>correct@3</th></tr>
      <tr><td>strict</td>""" + "".join(
        f"<td>{pct(ast['strict'][f'correct@{k}'])}</td>" for k in (1, 2, 3)) + """</tr>
      <tr><td>lenient</td>""" + "".join(
        f"<td>{pct(ast['lenient'][f'correct@{k}'])}</td>" for k in (1, 2, 3)) + """</tr>
    </table>
    <p class="sum">strict counts only &ldquo;ok&rdquo;; lenient also counts
       &ldquo;partly&rdquo;</p>""")

    g = report["speech_gate"]
    out.append(f"""
    <h3>Speech gate &mdash; n={g['n']}</h3>
    <table class="pc">
      <tr><th>precision</th><th>recall</th><th>F1</th><th>tp</th><th>fp</th>
          <th>fn</th><th>tn</th></tr>
      <tr><td>{pct(g['precision'])}</td><td>{pct(g['recall'])}</td>
          <td>{pct(g['f1'])}</td><td>{g['tp']}</td><td>{g['fp']}</td>
          <td>{g['fn']}</td><td>{g['tn']}</td></tr>
    </table>
    <p class="sum">false negatives are clips with speech the pipeline never
       transcribed; false positives are clips it transcribed with none</p>
    </div>""")
    return "\n".join(out)


# ── publication figures ─────────────────────────────────────────────
#
# Every figure has to stand on its own: a reader who sees only the image must
# know what was measured, on how much data, and what the colours mean. So each
# one carries its own title, axis labels, a labelled colour bar, the sample
# size, and a footnote saying what the classes are. Nothing is left to a
# caption written elsewhere.

FIG_DIR = os.path.join(STORE_DIR, "figures")

# Muted sequential blue: perceptually ordered, prints legibly in greyscale,
# and readable for the common colour-vision deficiencies. Deliberately not a
# red/green diagonal, which fails both tests.
CMAP = "Blues"


def _setup_matplotlib(dpi: int):
    import matplotlib
    matplotlib.use("Agg")            # no display, and safe inside Flask
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.dpi": dpi,
        "savefig.dpi": dpi,
        "savefig.bbox": "tight",
        "savefig.facecolor": "white",
        "font.family": "DejaVu Sans",   # carries Greek, ships with matplotlib
        "font.size": 9,
        "axes.titlesize": 11,
        "axes.labelsize": 9.5,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
    })
    return plt


def _fmt(v, digits=1):
    return "n/a" if v is None else f"{100 * v:.{digits}f}%"


def _confusion_figure(plt, conf, true_classes, pred_classes, title,
                      true_label, pred_label, footnote, path):
    import numpy as np

    counts = np.array([[conf["matrix"][t][p] for p in pred_classes]
                       for t in true_classes], dtype=float)
    support = counts.sum(axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        shares = np.where(support > 0, counts / support, np.nan)

    fig = plt.figure(figsize=(7.2, 7.6))
    # Generous hspace: the heatmap carries an x-label and the lower panel
    # carries a title, and those two collide at anything tighter.
    grid = fig.add_gridspec(2, 1, height_ratios=[3.0, 1.0], hspace=0.55,
                            top=0.93, bottom=0.13)
    ax = fig.add_subplot(grid[0])

    masked = np.ma.masked_invalid(shares)
    cmap = plt.get_cmap(CMAP).copy()
    cmap.set_bad("#eceff1")          # a class with no examples at all
    im = ax.imshow(masked, cmap=cmap, vmin=0.0, vmax=1.0, aspect="auto")

    ax.set_xticks(range(len(pred_classes)))
    ax.set_yticks(range(len(true_classes)))
    ax.set_xticklabels(pred_classes)
    ax.set_yticklabels([f"{t}\n(n={int(support[i][0])})"
                        for i, t in enumerate(true_classes)])
    ax.set_xlabel(pred_label, labelpad=8)
    ax.set_ylabel(true_label, labelpad=8)
    ax.set_title(title, pad=12, fontweight="bold")

    # Thin white separators read better than a box on a dense matrix.
    ax.set_xticks(np.arange(-0.5, len(pred_classes), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(true_classes), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=1.4)
    ax.tick_params(which="minor", length=0)

    for i in range(len(true_classes)):
        for j in range(len(pred_classes)):
            if support[i][0] == 0:
                ax.text(j, i, "–", ha="center", va="center", color="#78909c")
                continue
            share = shares[i][j]
            # Flip the label to white once the cell is dark enough that black
            # would stop being legible.
            colour = "white" if share > 0.55 else "#102027"
            ax.text(j, i, f"{int(counts[i][j])}\n{100 * share:.0f}%",
                    ha="center", va="center", color=colour, fontsize=9,
                    linespacing=1.25)

    cbar = fig.colorbar(im, ax=ax, fraction=0.045, pad=0.03)
    cbar.set_label("Share of the true class (each row sums to 100%)",
                   rotation=270, labelpad=16, fontsize=8.5)
    cbar.ax.tick_params(labelsize=8)

    # Per-class scores directly beneath, so the figure answers "how good is
    # it for each person" without a second exhibit.
    tab_ax = fig.add_subplot(grid[1])
    tab_ax.axis("off")
    rows = [[c,
             _fmt(conf["per_class"][c]["precision"]),
             _fmt(conf["per_class"][c]["recall"]),
             _fmt(conf["per_class"][c]["f1"]),
             str(conf["per_class"][c]["support"])] for c in true_classes]
    # bbox pins the table to its axes exactly. Without it the table sizes
    # itself and spills over whatever is drawn beneath.
    table = tab_ax.table(
        cellText=rows,
        colLabels=["Class", "Precision", "Recall", "F1", "Clips (n)"],
        cellLoc="center", loc="center", bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(8.5)
    for (r, _c), cell in table.get_celld().items():
        cell.set_edgecolor("#b0bec5")
        if r == 0:
            cell.set_facecolor("#eceff1")
            cell.set_text_props(fontweight="bold")

    # The headline numbers sit between the matrix and the per-class table, so
    # the figure reads matrix -> summary -> detail -> footnote.
    overall = (f"Overall accuracy {_fmt(conf['accuracy'])}   |   "
               f"macro-F1 {_fmt(conf['macro_f1'])}   |   "
               f"balanced accuracy {_fmt(conf['balanced_accuracy'])}   |   "
               f"N = {conf['n']} clips")
    tab_ax.set_title(overall, fontsize=9, fontweight="bold", pad=10)
    fig.text(0.5, 0.015, footnote, ha="center", va="bottom", fontsize=8,
             color="#455a64", wrap=True)
    fig.savefig(path)
    plt.close(fig)


def _bar_figure(plt, path, title, groups, series, ylabel, footnote,
                value_fmt="{:.1f}%"):
    """Grouped bars with the value printed on every bar."""
    import numpy as np
    x = np.arange(len(groups))
    width = 0.8 / max(1, len(series))
    colours = ["#1f6f8b", "#7fb3c8", "#c9dde5"]

    fig, ax = plt.subplots(figsize=(6.6, 4.3))
    for k, (name, values) in enumerate(series):
        offset = (k - (len(series) - 1) / 2) * width
        vals = [0 if v is None else 100 * v for v in values]
        bars = ax.bar(x + offset, vals, width, label=name,
                      color=colours[k % len(colours)], edgecolor="#37474f",
                      linewidth=0.6)
        for b, v, raw in zip(bars, vals, values):
            ax.text(b.get_x() + b.get_width() / 2, v + 1.5,
                    "n/a" if raw is None else value_fmt.format(v),
                    ha="center", fontsize=8.5)

    ax.set_xticks(x)
    ax.set_xticklabels(groups)
    ax.set_ylabel(ylabel)
    ax.set_ylim(0, 108)
    ax.set_title(title, fontweight="bold", pad=12)
    ax.grid(axis="y", color="#cfd8dc", linewidth=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    if len(series) > 1:
        ax.legend(frameon=False, loc="upper left", fontsize=8.5)
    fig.text(0.5, -0.02, footnote, ha="center", fontsize=8, color="#455a64")
    fig.savefig(path)
    plt.close(fig)


def _table_figure(plt, path, title, col_labels, rows, footnote,
                  col_widths=None):
    height = 1.9 + 0.32 * len(rows)
    fig, ax = plt.subplots(figsize=(7.6, height))
    fig.subplots_adjust(top=0.80, bottom=0.14, left=0.04, right=0.97)
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=col_labels, cellLoc="left",
                     loc="center", colWidths=col_widths, bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor("#b0bec5")
        if r == 0:
            cell.set_facecolor("#eceff1")
            cell.set_text_props(fontweight="bold")
        elif c == 0:
            cell.set_text_props(fontweight="bold")
    ax.set_title(title, fontweight="bold", pad=18)
    fig.text(0.5, 0.01, footnote, ha="center", fontsize=8, color="#455a64")
    fig.savefig(path)
    plt.close(fig)


def make_figures(report: dict, dpi: int = 300) -> list:
    """Write every figure as a PNG. Returns the paths written."""
    if not report.get("n_annotated"):
        return []
    plt = _setup_matplotlib(dpi)
    os.makedirs(FIG_DIR, exist_ok=True)
    written = []
    n = report["n_annotated"]
    provenance = (f"Home Sentinel component evaluation · {n} annotated clips · "
                  f"{report.get('generated_at', '')}")

    # 1. Speaker
    if "speaker" in report:
        off = report["speaker_offset"]
        extra = ""
        if off["other_predicted_as"]:
            named = ", ".join(f"{k} ({v})" for k, v in off["other_predicted_as"].items())
            extra = (f"  Separately, {off['true_other']} clip(s) of a non-resident "
                     f"voice were labelled by the system as: {named}.")
        path = os.path.join(FIG_DIR, "fig1_speaker_confusion.png")
        _confusion_figure(
            plt, report["speaker"], RESIDENTS, SPEAKER_PREDICTIONS,
            "Speaker identification: ECAPA-TDNN closed-set matching",
            "True speaker (listener annotation)",
            "Speaker assigned by the system",
            "Cells give clip counts with the row percentage beneath. "
            "'Unknown' is the system declining to name anyone, which is a "
            "different outcome from naming the wrong person." + extra + "\n"
            + provenance, path)
        written.append(path)

    # 2. Emotion
    if "emotion" in report:
        eo = report["emotion_offset"]
        path = os.path.join(FIG_DIR, "fig2_emotion_confusion.png")
        _confusion_figure(
            plt, report["emotion"], EMOTIONS, EMOTIONS,
            "Speech emotion: wav2vec2 dimensional model, mapped to four classes",
            "True impression (listener annotation)",
            "Class predicted by the system",
            f"Cells give clip counts with the row percentage beneath. "
            f"Excluded: {eo['true_unclear']} clip(s) the listener could not "
            f"rate and {eo['no_prediction']} with no model output.\n"
            + provenance, path)
        written.append(path)

    # 3. Speech gate
    g = report["speech_gate"]
    gate_conf = {
        "matrix": {"Speech": {"Speech": g["tp"], "No speech": g["fn"]},
                   "No speech": {"Speech": g["fp"], "No speech": g["tn"]}},
        "per_class": {
            "Speech": {"precision": g["precision"], "recall": g["recall"],
                       "f1": g["f1"], "support": g["tp"] + g["fn"]},
            "No speech": {
                "precision": (g["tn"] / (g["tn"] + g["fn"])
                              if (g["tn"] + g["fn"]) else None),
                "recall": (g["tn"] / (g["tn"] + g["fp"])
                           if (g["tn"] + g["fp"]) else None),
                "f1": None, "support": g["fp"] + g["tn"]}},
        "accuracy": g["accuracy"], "macro_f1": None,
        "balanced_accuracy": None, "n": g["n"]}
    path = os.path.join(FIG_DIR, "fig3_speech_gate.png")
    _confusion_figure(
        plt, gate_conf, ["Speech", "No speech"], ["Speech", "No speech"],
        "Speech gate: does the clip reach transcription?",
        "True content (listener annotation)",
        "Decision taken by the AST label gate",
        "The gate decides whether a clip is passed to speech recognition. "
        "A false negative is speech that was never transcribed; a false "
        "positive is a clip transcribed with nothing said in it.\n" + provenance,
        path)
    written.append(path)

    # 4. AST top-k
    ast = report["ast"]
    path = os.path.join(FIG_DIR, "fig4_ast_topk.png")
    _bar_figure(
        plt, path,
        "Sound classification: is a usable label within the top K?",
        ["Top 1", "Top 2", "Top 3"],
        [("Strict (label judged fully correct)",
          [ast["strict"][f"correct@{k}"] for k in (1, 2, 3)]),
         ("Lenient (partly correct also counted)",
          [ast["lenient"][f"correct@{k}"] for k in (1, 2, 3)])],
        "Clips with an acceptable label (%)",
        f"The AST model returns three ranked AudioSet labels per clip; a "
        f"listener judged each as correct, partly correct or wrong. "
        f"N = {ast['strict']['n']} clips.\n" + provenance)
    written.append(path)

    # 5. ASR
    a = report.get("asr")
    if a:
        path = os.path.join(FIG_DIR, "fig5_asr_error.png")
        ci = a["wer_ci95"]
        ci_txt = (f"  95% CI [{_fmt(ci[0])}, {_fmt(ci[1])}]" if ci else "")
        _bar_figure(
            plt, path,
            "Speech recognition error: Whisper large-v3 (Greek)",
            ["WER\n(normalised)", "WER\n(raw)", "CER\n(normalised)"],
            [("Error rate", [a["wer_normalised"], a["wer_raw"],
                             a["cer_normalised"]])],
            "Error rate (%)",
            f"Normalised text is lower-cased with diacritics and punctuation "
            f"removed. Errors: {a['substitutions']} substitutions, "
            f"{a['deletions']} deletions, {a['insertions']} insertions over "
            f"{a['reference_words']} reference words in {a['n_clips']} clips."
            f"{ci_txt}\n" + provenance)
        written.append(path)

    # 6. Headline table
    # Labels are kept short enough to fit their column: a matplotlib table
    # clips overflowing text rather than wrapping it, so a long label is
    # silently truncated in the PNG.
    rows = []
    if a:
        rows.append(["Speech recognition, WER", _fmt(a["wer_normalised"]),
                     f"{a['n_clips']}", "Whisper large-v3"])
        rows.append(["Speech recognition, CER", _fmt(a["cer_normalised"]),
                     f"{a['n_clips']}", ""])
    if "speaker" in report:
        sp = report["speaker"]
        rows.append(["Speaker ID, accuracy", _fmt(sp["accuracy"]),
                     f"{sp['n']}", "ECAPA-TDNN"])
        rows.append(["Speaker ID, macro-F1", _fmt(sp["macro_f1"]),
                     f"{sp['n']}", "3 enrolled residents"])
    if "emotion" in report:
        e = report["emotion"]
        rows.append(["Emotion, balanced accuracy", _fmt(e["balanced_accuracy"]),
                     f"{e['n']}", "wav2vec2 dimensional"])
        rows.append(["Emotion, macro-F1", _fmt(e["macro_f1"]),
                     f"{e['n']}", "4 classes"])
    rows.append(["Sound labelling, correct@1", _fmt(ast["strict"]["correct@1"]),
                 f"{ast['strict']['n']}", "AST / AudioSet"])
    rows.append(["Sound labelling, correct@3", _fmt(ast["strict"]["correct@3"]),
                 f"{ast['strict']['n']}", "strict judgement"])
    rows.append(["Speech gate, F1", _fmt(g["f1"]), f"{g['n']}",
                 "AST label set"])
    path = os.path.join(FIG_DIR, "table1_summary.png")
    _table_figure(
        plt, path, "Component-level performance of the perception pipeline",
        ["Component and metric", "Value", "Clips", "Model"], rows,
        "Each row is scored against blind listener annotation of the same audio "
        "clips: speaker and emotion were answered before the system's output was "
        "shown. Word and character error rates use lower-cased text with "
        "diacritics and punctuation removed.\n" + provenance,
        col_widths=[0.40, 0.13, 0.11, 0.36])
    written.append(path)
    return written


def cmd_figures(args):
    conn = store_init()
    fig_rows = collect(conn)
    report = compute_stats(fig_rows, count_skipped(conn))
    conn.close()
    if not report.get("n_annotated"):
        sys.exit("Nothing annotated yet.")
    paths = make_figures(report, dpi=args.dpi)
    print(f"\n  {len(paths)} figure(s) at {args.dpi} dpi:")
    for p in paths:
        print(f"    {os.path.basename(p)}")
    print(f"  in {FIG_DIR}")
    # Redrawing the figures without refreshing the commentary beside them is
    # how the two come to describe different runs.
    print(f"  commentary: {write_commentary(fig_rows, report)}\n")


def _write_results(report: dict) -> str:
    """Keep results.json current wherever the report is produced."""
    out = os.path.join(STORE_DIR, "results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False, default=float)
    return out


# ── automated commentary ────────────────────────────────────────────

# The hand-written companion. Never overwritten: the judgement in it -- what the
# results mean for the report pipeline, whether the application is feasible --
# cannot be rebuilt from the numbers, so an automatic writer must not touch it.
COMMENTARY_AUTO = "figure_commentary_auto.txt"
COMMENTARY_HAND = "figure_commentary.txt"

# Below this many clips a per-class rate is an anecdote and is reported as a
# count instead, so that "0% recall on n=1" never reads as a measured failure.
MIN_CELL = 5


def _pct(x, places=1):
    return "n/a" if x is None else f"{100 * x:.{places}f}%"


def _headline_metrics(report: dict) -> list[tuple]:
    """(component, metric name, value) for the ranking table.

    The values are not strictly comparable -- they measure different tasks --
    so the table is ordered but the file says so rather than implying a
    common scale.
    """
    out = []
    ast = report.get("ast", {}).get("strict") or {}
    if ast.get("n"):
        out.append(("sound labelling", "correct@1", ast.get("correct@1"), ast["n"]))
    gate = report.get("speech_gate") or {}
    if gate.get("n"):
        out.append(("speech gate", "F1", gate.get("f1"), gate["n"]))
    asr = report.get("asr") or {}
    if asr.get("n_clips"):
        wer = asr.get("wer_normalised")
        out.append(("speech recognition", "word accuracy (1-WER)",
                    (1 - wer) if wer is not None else None, asr["n_clips"]))
    spk = report.get("speaker") or {}
    if spk.get("n"):
        out.append(("speaker identity", "accuracy", spk.get("accuracy"), spk["n"]))
    emo = report.get("emotion") or {}
    if emo.get("n"):
        out.append(("emotion", "macro-F1", emo.get("macro_f1"), emo["n"]))
    return sorted(out, key=lambda t: (t[2] is None, -(t[2] or 0)))


def _small_cells(report: dict) -> list[str]:
    notes = []
    for key, label in (("speaker", "speaker"), ("emotion", "emotion")):
        for cls, stats in (report.get(key, {}).get("per_class") or {}).items():
            if stats.get("support", 0) < MIN_CELL:
                notes.append(f"{label} class '{cls}': n={stats['support']}")
    return notes


def _config_flags() -> list[tuple]:
    """Settings that change how the measurements above should be read."""
    flags = []
    path = os.path.join(PROJECT_ROOT, "common_files", "parameters.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:                       # noqa: BLE001 - advisory only
        return [("warn", f"could not read parameters.json ({exc.__class__.__name__});"
                         " configuration not checked")]

    sim = (cfg.get("speaker_id") or {}).get("min_similarity")
    if sim is not None and float(sim) <= 0:
        flags.append(("risk", "speaker_id.min_similarity is "
                              f"{sim} -- the speaker abstain is disabled. Every clip "
                              "now gets a name, including the ones this run shows it "
                              "declining to answer. The speaker precision reported "
                              "above was measured WITH the abstain in place and "
                              "does not describe the current configuration."))
    else:
        flags.append(("ok", f"speaker_id.min_similarity = {sim} (abstain active,"
                            " matching the run measured here)"))
    return flags


def derive_commentary(rows: list, report: dict, store_dir: str) -> list[str]:
    """The mechanical read of one evaluation run.

    Everything here is a rule over results.json and the annotation rows. It
    will not notice anything the rules do not test for, which is exactly why
    it is written to its own file rather than over the hand-written one.
    """
    L: list[str] = []
    line = L.append
    rule = lambda ch="-": line(ch * 80)                       # noqa: E731

    n_ann = report.get("n_annotated", 0)
    n_skip = report.get("n_skipped", 0)
    total = n_ann + n_skip

    rule("=")
    line("HOME SENTINEL - AUTOMATED COMPONENT COMMENTARY")
    rule("=")
    line("")
    line(f"Store:      {store_dir}")
    line(f"Basis:      {n_ann} annotated, {n_skip} skipped of {total} sampled"
         + (f" ({n_skip / total:.0%} skip rate)" if total else ""))
    line(f"Generated:  {report.get('generated_at', '-')}")
    line("")
    line("Written by rule, not by judgement, and regenerated on every `stats`")
    line("run. It reports what the numbers say and flags the conditions it was")
    line("taught to look for; it cannot notice anything else.")
    line("")
    line(f"The interpretive companion is {COMMENTARY_HAND} -- written by hand and")
    line("NOT regenerated. If the figures have moved since it was written, treat")
    line("its numbers as stale and this file as current.")
    line("")

    # ── ranking ─────────────────────────────────────────────────────
    rule()
    line("COMPONENT RANKING")
    rule()
    line("")
    ranked = _headline_metrics(report)
    if not ranked:
        line("  nothing scored yet.")
    else:
        line(f"  {'component':<22}{'headline metric':<26}{'value':>10}{'n':>7}")
        for name, metric, value, n in ranked:
            line(f"  {name:<22}{metric:<26}{_pct(value):>10}{n:>7}")
        line("")
        line("  These measure different tasks and do not share a scale; the order")
        line("  shows which component is least able to support a claim, not how")
        line("  much better one is than another.")
        strong, weak = ranked[0], ranked[-1]
        line("")
        line(f"  Strongest: {strong[0]} ({_pct(strong[2])} {strong[1]}).")
        line(f"  Weakest:   {weak[0]} ({_pct(weak[2])} {weak[1]}).")
        line("")
        line("  In the report pipeline, L0-L2 are keyed on sound and L3-L4 are keyed")
        line("  on people. Any component below that is an identity or affect channel")
        line("  caps everything the daily report says about persons and pairs.")
    line("")

    # ── findings ────────────────────────────────────────────────────
    rule()
    line("FINDINGS")
    rule()
    line("")
    flags: list[tuple] = []

    asr = report.get("asr") or {}
    if asr.get("reference_words"):
        ref = asr["reference_words"]
        ins_rate = asr["insertions"] / ref
        errors = asr["substitutions"] + asr["deletions"] + asr["insertions"]
        del_share = asr["deletions"] / errors if errors else 0
        flags.append((
            "ok" if ins_rate < 0.02 else "warn",
            f"ASR inserts {asr['insertions']} word(s) per {ref} reference words "
            f"({_pct(ins_rate)}). "
            + ("It rarely adds words nobody said."
               if ins_rate < 0.02 else
               "It is adding words that were not spoken.")))
        wer, cer = asr.get("wer_normalised"), asr.get("cer_normalised")
        if wer and cer and cer < wer * 0.8:
            flags.append((
                "ok", f"CER ({_pct(cer)}) sits well below WER ({_pct(wer)}), so most "
                      "errors are morphological rather than invented content: the "
                      "gist of an utterance often survives the word errors."))
        if del_share > 0.3:
            flags.append((
                "warn", f"{_pct(del_share)} of ASR errors are deletions "
                        f"({asr['deletions']} words). Speech is being dropped, not "
                        "just misheard, and dropped speech leaves no trace for any "
                        "later stage to recover."))
        iqr = asr.get("wer_iqr")
        if iqr and (iqr[1] - iqr[0]) > 0.6:
            flags.append((
                "warn", f"per-clip WER is bimodal, not centred: median "
                        f"{_pct(asr.get('wer_median'))}, IQR [{_pct(iqr[0])}, "
                        f"{_pct(iqr[1])}]. Clips either transcribe well or fail "
                        "completely, so the aggregate WER describes no actual clip. "
                        "A spread this wide points at recording conditions rather "
                        "than at the model."))
        ci = asr.get("wer_ci95")
        if ci and (ci[1] - ci[0]) > 0.15:
            flags.append((
                "warn", f"the WER interval is wide: 95% CI [{_pct(ci[0])}, "
                        f"{_pct(ci[1])}] on {asr['n_clips']} clip(s). Do not quote "
                        "the point estimate as settled."))

    spk = report.get("speaker") or {}
    if spk.get("n"):
        per = spk.get("per_class") or {}
        big = {c: s for c, s in per.items() if s.get("support", 0) >= MIN_CELL}
        if big:
            prec = sum(s["precision"] for s in big.values()) / len(big)
            rec = sum(s["recall"] for s in big.values()) / len(big)
            if prec - rec > 0.2:
                flags.append((
                    "ok", f"speaker identity is honest when it answers: mean "
                          f"precision {_pct(prec)} against mean recall {_pct(rec)} "
                          "on the well-sampled residents. Its failure mode is "
                          "declining to answer, not naming the wrong person."))
        matrix = spk.get("matrix") or {}
        unknown = sum(r.get("Unknown", 0) for r in matrix.values())
        if unknown:
            # Two very different failures both end as Unknown, and reporting
            # them together sends the reader to the wrong component. A clip
            # with no transcript never reached speaker identification at all --
            # parse_transcript returns "Unknown" for an empty segment list --
            # so it is a recognition miss, not an abstention by the closed set.
            silent = sum(
                1 for r in rows
                if r["true_speech"] and r["true_speaker"] in RESIDENTS
                and (r["pred_speaker"] or "Unknown") == "Unknown"
                and "no speech detected" in (r["transcription"] or "").lower())
            abstained = unknown - silent
            flags.append((
                "warn", f"{unknown} of {spk['n']} speech clip(s) "
                        f"({_pct(unknown / spk['n'])}) produced no named speaker. "
                        f"{silent} of those carry no transcript at all "
                        f"('No Speech Detected'), so they never reached speaker "
                        f"identification -- that is a recognition miss, counted "
                        f"again in the speech-gate figures. Only {abstained} are "
                        f"the closed set declining to name anyone."))
            if silent:
                flags.append((
                    "risk", f"{silent} clip(s) the listener confirmed contain a "
                            f"resident's speech were never transcribed "
                            f"({_pct(silent / spk['n'])} of scored speech). No "
                            "person reaches L3 from them, so they cannot become "
                            "presence, speech or conversation episodes -- and "
                            "because conversation_candidate needs TWO named "
                            "speakers in one recording, a two-person exchange "
                            "with one end missing disappears rather than "
                            "degrading."))
        cross = sum(v for t, r in matrix.items()
                    for p, v in r.items() if p != t and p in RESIDENTS)
        flags.append((
            "ok" if cross <= max(1, spk["n"] * 0.1) else "warn",
            f"{cross} cross-resident confusion(s) in {spk['n']} clip(s): "
            + ("household members are not being mixed up with each other."
               if cross <= max(1, spk["n"] * 0.1) else
               "household members are being mixed up with each other.")))
    off = report.get("speaker_offset") or {}
    forced = off.get("other_predicted_as") or {}
    if off.get("true_other") and forced:
        named = ", ".join(f"{k}={v}" for k, v in forced.items())
        flags.append((
            "warn", f"all {off['true_other']} non-resident voice(s) were still "
                    f"given a resident's name ({named}). The closed set has no "
                    "reject option, so visitors and television are attributed to "
                    "the family."))

    emo = report.get("emotion") or {}
    if emo.get("n"):
        matrix = emo.get("matrix") or {}
        supports = {c: sum(r.values()) for c, r in matrix.items()}
        baseline = (max(supports.values()) / emo["n"]) if supports else None
        if baseline is not None:
            major = max(supports, key=supports.get)
            if emo["accuracy"] <= baseline:
                flags.append((
                    "risk", f"the emotion model scores {_pct(emo['accuracy'])} "
                            f"accuracy against a majority-class baseline of "
                            f"{_pct(baseline)} (answer '{major}' every time, no "
                            "model at all). It is at or below the trivial "
                            "baseline: beyond the majority prior this channel "
                            "carries little information, while still producing a "
                            "well-formed per-person distribution that LOOKS like "
                            "evidence at L4."))
            else:
                flags.append((
                    "ok", f"emotion accuracy {_pct(emo['accuracy'])} exceeds the "
                          f"majority-class baseline {_pct(baseline)} "
                          f"(always '{major}')."))
        if emo.get("macro_f1") is not None and emo["macro_f1"] < 0.5:
            flags.append((
                "warn", f"emotion macro-F1 is {_pct(emo['macro_f1'])}: performance "
                        "is concentrated in the majority class and the minority "
                        "emotions are largely unrecovered."))
    ns_emotion = sum(1 for r in rows if not r["true_speech"] and r["pred_emotion"])
    if ns_emotion:
        flags.append((
            "warn", f"{ns_emotion} clip(s) with no speech were still given an "
                    "emotion label. Mood evidence is being attached to audio that "
                    "nobody spoke in."))

    gate = report.get("speech_gate") or {}
    if gate.get("n"):
        if gate.get("recall") == 1.0:
            flags.append((
                "ok", f"the speech gate dropped no speech at all ({gate['tp']} of "
                      f"{gate['tp'] + gate['fn']} speech clips passed). This is the "
                      "correct asymmetry: admitted noise can be filtered later, a "
                      "conversation never transcribed is unrecoverable."))
        elif gate.get("fn"):
            flags.append((
                "risk", f"the speech gate dropped {gate['fn']} clip(s) that "
                        "contained speech. Those conversations are lost before any "
                        "later stage can see them."))
        if gate.get("fp"):
            leaked = [r for r in rows if not r["true_speech"] and r["pred_speech"]]
            with_text = sum(1 for r in leaked if (r["pred_text"] or "").strip())
            with_name = sum(1 for r in leaked
                            if r["pred_speaker"] and r["pred_speaker"] != "Unknown")
            flags.append((
                "ok" if with_text <= len(leaked) * 0.25 else "warn",
                f"{gate['fp']} non-speech clip(s) passed the gate, but only "
                f"{with_text} produced transcript text and {with_name} received a "
                "speaker name. "
                + ("The hallucination guards behind the gate are doing the real "
                   "filtering, which is the right place for it."
                   if with_text <= len(leaked) * 0.25 else
                   "Content is being invented on clips with nothing said in them.")))

    ast = (report.get("ast") or {}).get("strict") or {}
    if ast.get("n"):
        gain = (ast.get("correct@3") or 0) - (ast.get("correct@1") or 0)
        flags.append((
            "ok" if (ast.get("correct@1") or 0) >= 0.75 else "warn",
            f"sound labelling is correct at rank 1 on {_pct(ast.get('correct@1'))} "
            f"of clips and within the top three on {_pct(ast.get('correct@3'))}. "
            "This is what L1 activity events and all of L2 rest on."))
        if gain < 0.1:
            flags.append((
                "warn", f"the top-3 label list adds only {_pct(gain)} over the top-1 "
                        "label, so when the first label is wrong the others usually "
                        "are too. The extra ranks are not a reliable safety net."))

    order = {"risk": 0, "warn": 1, "ok": 2}
    for level, text in sorted(flags, key=lambda f: order.get(f[0], 3)):
        tag = f"[{level}]".ljust(7)
        wrapped = textwrap.wrap(text, width=72) or [""]
        line(f"  {tag}{wrapped[0]}")
        for extra in wrapped[1:]:
            line(f"  {'':<7}{extra}")
        line("")

    # ── character of the errors ─────────────────────────────────────
    rule()
    line("CHARACTER OF THE ERRORS")
    rule()
    line("")
    line("  Whether the system omits or invents matters more than its accuracy.")
    line("  An under-reporting system is recoverable -- better microphones, more")
    line("  enrolment, more data all push it toward truth. A confabulating one is")
    line("  not, because nothing distinguishes its inventions from its evidence.")
    line("")
    # Counted as distinct clips, on both sides, so that one clip failing in two
    # ways is one failure and neither side is inflated by how finely its
    # channel happens to be measured. The ASR word counts are reported beside
    # them rather than added in: deleted words and unnamed clips are not
    # commensurable, and summing them would let the finer unit decide the
    # verdict.
    omit_ids: dict[str, set] = defaultdict(set)
    fab_ids: dict[str, set] = defaultdict(set)
    unintelligible = set()
    for r in rows:
        speech, pred = r["true_speech"], r["pred_speaker"]
        text = normalise(strip_segment_markers(r["pred_text"] or ""))
        ref = normalise(strip_segment_markers(r["true_text"] or ""))
        if speech:
            if not r["pred_speech"]:
                omit_ids["never transcribed"].add(r["id"])
            if (pred or "Unknown") == "Unknown":
                omit_ids["left unnamed"].add(r["id"])
            # Speech too degraded for the annotator to write down either. The
            # system producing nothing is agreement, not omission: there is no
            # reference to have missed. Scoring it as a miss would penalise the
            # pipeline for the one case where it behaved exactly right.
            if not ref:
                unintelligible.add(r["id"])
            elif len(text.split()) < 3:
                omit_ids["no usable transcript"].add(r["id"])
            if (r["true_speaker"] in RESIDENTS and pred in RESIDENTS
                    and pred != r["true_speaker"]):
                fab_ids["named the wrong resident"].add(r["id"])
            if r["true_speaker"] == "Other" and pred in RESIDENTS:
                fab_ids["non-resident given a resident's name"].add(r["id"])
        else:
            if text:
                fab_ids["transcript on a clip with nothing said"].add(r["id"])
            if r["pred_emotion"]:
                fab_ids["emotion label on non-speech"].add(r["id"])

    def _side(groups: dict) -> tuple:
        every = set().union(*groups.values()) if groups else set()
        parts = "; ".join(f"{len(v)} {k}"
                          for k, v in sorted(groups.items(),
                                             key=lambda kv: -len(kv[1])))
        return len(every), (parts or "none detected")

    n_omit, omit_text = _side(omit_ids)
    n_fab, fab_text = _side(fab_ids)
    for head, n, body in (("Omission-shaped   ", n_omit, omit_text),
                          ("Fabrication-shaped", n_fab, fab_text)):
        wrapped = textwrap.wrap(body, width=54) or [""]
        line(f"  {head} ({n:>3} clip(s)):  {wrapped[0]}")
        for extra in wrapped[1:]:
            line(f"  {'':<24}  {extra}")
    if asr.get("reference_words"):
        line(f"  ASR words:  {asr['deletions']} deleted against "
             f"{asr['insertions']} inserted "
             f"({_pct(asr['insertions'] / asr['reference_words'])} of the "
             "reference) -- dropped speech far outweighs invented speech.")
    if unintelligible:
        line("")
        for w in textwrap.wrap(
                f"Neither side of the count: {len(unintelligible)} clip(s) hold "
                "speech that the annotator could not transcribe either. The "
                "gate was right that someone spoke and the empty transcript "
                "agrees with the listener, so these are correct behaviour on "
                "both counts and are excluded from the error rates above.",
                width=76):
            line(f"  {w}")
    line("")
    if not n_fab:
        verdict = ("No fabrication-shaped errors were detected. Every failure in "
                   "this run is the system reporting less than happened.")
    elif n_fab * 2 < n_omit:
        verdict = (f"Errors are predominantly omissions ({n_omit} clip(s) against "
                   f"{n_fab}). The system under-reports rather than invents, which "
                   "is the recoverable failure mode: better microphones, better "
                   "enrolment and more data all push it toward truth.")
    else:
        verdict = (f"Fabrication-shaped errors are a material share of the total "
                   f"({n_fab} clip(s) against {n_omit} omissions). Content the "
                   "system did not observe is reaching the report, where no later "
                   "stage and no prompt can distinguish it from evidence. This is "
                   "the failure mode to fix first.")
    for w in textwrap.wrap(verdict, width=76):
        line(f"  {w}")
    line("")

    # ── things not safe to quote ────────────────────────────────────
    small = _small_cells(report)
    rule()
    line("NOT SAFE TO QUOTE")
    rule()
    line("")
    if small:
        line(f"  Classes with fewer than {MIN_CELL} annotated clips. Their rates are")
        line("  arithmetic on a handful of clips, not measurements, and a '0.0%'")
        line("  among them means 'never sampled enough', not 'never works':")
        line("")
        for note in small:
            line(f"    - {note}")
    else:
        line(f"  Every scored class has at least {MIN_CELL} clips.")
    line("")

    # ── configuration ───────────────────────────────────────────────
    rule()
    line("CONFIGURATION AFFECTING HOW THIS IS READ")
    rule()
    line("")
    for level, text in _config_flags():
        tag = f"[{level}]".ljust(7)
        wrapped = textwrap.wrap(text, width=72) or [""]
        line(f"  {tag}{wrapped[0]}")
        for extra in wrapped[1:]:
            line(f"  {'':<7}{extra}")
        line("")
    return L


def write_commentary(rows: list, report: dict) -> str:
    text = "\n".join(derive_commentary(rows, report, STORE_DIR)) + "\n"
    out = os.path.join(STORE_DIR, COMMENTARY_AUTO)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out


def cmd_commentary(args):
    conn = store_init()
    rows = collect(conn)
    report = compute_stats(rows, count_skipped(conn))
    conn.close()
    if not rows:
        sys.exit("nothing annotated yet -- no commentary to write")
    out = write_commentary(rows, report)
    print(f"\n  written: {out}")
    if args.show:
        print()
        print(open(out, encoding="utf-8").read())


# ── pruning skipped clips ───────────────────────────────────────────
#
# A store is delivered whole -- database and audio folder together -- so
# anything a skipped clip leaves behind travels with it. A skip means the
# annotator declined to judge the clip, often precisely because it was
# unintelligible or private, and none of it is scored: collect() has always
# filtered `skipped = 0`. Keeping the recording and its transcript afterwards
# distributes content that was never used for anything.
#
# The clip ROW survives as an empty stub rather than being deleted. Two counts
# are computed from it -- `SELECT COUNT(*) FROM clips` is the size of the
# sample, and cmd_stats subtracts from it to report how many clips are still
# unannotated. Deleting the row would silently shrink both.

# Everything a skipped clip is not allowed to carry into a delivered store:
# the audio's own name, what was said, what the models thought was said, and
# the acoustics that identify the recording. `date`, `stratum` and
# `pred_speech` stay -- they describe the sample, not its content.
_PRUNED_CLIP_FIELDS = (
    "device", "room", "file_name", "audio_rel", "rms", "snr",
    "sound1", "conf1", "sound2", "conf2", "sound3", "conf3",
    "transcription", "emotion_raw", "pred_speaker", "pred_text",
    "pred_emotion", "pred_emotion_conf",
)


def prune_one_skipped(conn, clip_id, dry_run=False):
    """Drop one skipped clip's audio and content. Returns bytes freed."""
    row = conn.execute("SELECT audio_rel FROM clips WHERE id = ?",
                       (clip_id,)).fetchone()
    freed = 0
    if row and row["audio_rel"]:
        path = os.path.join(AUDIO_DIR, str(row["audio_rel"]))
        if os.path.exists(path):
            freed = os.path.getsize(path)
            if not dry_run:
                try:
                    os.remove(path)
                except OSError as exc:                    # noqa: PERF203
                    print(f"  could not remove {path}: {exc}", file=sys.stderr)
                    freed = 0
    if not dry_run:
        conn.execute(
            "UPDATE clips SET "
            + ", ".join(f"{name} = NULL" for name in _PRUNED_CLIP_FIELDS)
            + " WHERE id = ?", (clip_id,))
        # The note is the annotator's own words about a clip they declined to
        # judge, which is exactly the kind of comment that should not ship.
        conn.execute("UPDATE annotations SET notes = NULL WHERE clip_id = ?",
                     (clip_id,))
    return freed


def prune_skipped(conn, dry_run=False):
    """Prune every skipped clip in the store. Idempotent."""
    rows = conn.execute(
        "SELECT c.id, c.audio_rel FROM clips c"
        " JOIN annotations a ON a.clip_id = c.id"
        " WHERE a.skipped = 1").fetchall()
    with_audio = [r for r in rows if r["audio_rel"]]
    freed = sum(prune_one_skipped(conn, r["id"], dry_run) for r in with_audio)
    if not dry_run:
        conn.commit()
    return {"skipped_total": len(rows), "pruned": len(with_audio),
            "already_clean": len(rows) - len(with_audio), "bytes": freed}


def cmd_prune(args):
    """Strip skipped clips from a store so it can be delivered whole."""
    conn = store_init()
    result = prune_skipped(conn, dry_run=args.dry_run)
    kept = conn.execute("SELECT COUNT(*) FROM annotations"
                        " WHERE stage2_at IS NOT NULL AND skipped = 0").fetchone()[0]
    conn.close()
    verb = "would remove" if args.dry_run else "removed"
    print(f"\n  skipped clips in store : {result['skipped_total']}")
    print(f"  {verb} audio for      : {result['pruned']}"
          f"   ({result['bytes'] / 1e6:.1f} MB)")
    if result["already_clean"]:
        print(f"  already pruned         : {result['already_clean']}")
    print(f"  judged clips kept      : {kept}")
    files = (len([f for f in os.listdir(AUDIO_DIR)])
             if os.path.isdir(AUDIO_DIR) else 0)
    print(f"  files now in audio/    : {files}")
    if args.dry_run:
        print("\n  dry run -- nothing written. Re-run without --dry-run.\n")
    else:
        print("\n  metrics are unaffected: only skipped clips were touched,"
              " and they were never scored.\n")


def cmd_reparse(args):
    """Re-derive pred_speaker and pred_text from the stored raw transcripts.

    Both are frozen into the store at sample time, so a store sampled before
    parse_transcript could see past the first turn keeps the old, first-turn
    answers. Nothing the annotator typed is touched -- only the pipeline's own
    side of the comparison is recomputed, from the transcript text the store
    already holds, so this changes no ground truth.
    """
    conn = store_init()
    rows = conn.execute("SELECT id, transcription, pred_speaker, pred_text"
                        " FROM clips").fetchall()
    changed = []
    for r in rows:
        dominant, text, _ = parse_transcript(r["transcription"] or "")
        if dominant == (r["pred_speaker"] or "") and text == (r["pred_text"] or ""):
            continue
        changed.append((r["id"], r["pred_speaker"], dominant))
        if not args.dry_run:
            conn.execute("UPDATE clips SET pred_speaker = ?, pred_text = ?"
                         " WHERE id = ?", (dominant, text, r["id"]))
    if not args.dry_run:
        conn.commit()

    speaker_moves = [c for c in changed if c[1] != c[2]]
    verb = "would change" if args.dry_run else "changed"
    print(f"\n  {len(rows)} clip(s) in the store")
    print(f"  {verb}: {len(changed)} row(s), of which "
          f"{len(speaker_moves)} change the predicted speaker")
    for cid, old, new in speaker_moves:
        print(f"    clip {cid}: {old!r} -> {new!r}")
    if args.dry_run:
        print("\n  dry run -- nothing written. Re-run without --dry-run.\n")
    else:
        print("\n  re-run `stats` to rescore.\n")
    conn.close()


def cmd_stats(args):
    conn = store_init()
    rows = collect(conn)
    skipped = count_skipped(conn)
    report = compute_stats(rows, skipped)
    print_report(report)
    print(f"  written: {_write_results(report)}")
    print(f"  written: {write_commentary(rows, report)}")

    total = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    remaining = total - report["n_annotated"] - skipped
    if remaining > 0:
        print(f"  {remaining} clip(s) still unannotated\n")


# ── annotation UI ───────────────────────────────────────────────────

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Component annotation</title>
<style>
 :root{--bg:#0f1417;--card:#182126;--line:#2a373d;--ink:#e6eef1;--mute:#8fa3ab;
       --accent:#4fc3ce;--warn:#e3a250;--ok:#7bd88f}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);
      font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
 .wrap{max-width:820px;margin:0 auto;padding:24px 20px 80px}
 h1{font-size:17px;margin:0 0 4px;font-weight:600}
 .sub{color:var(--mute);font-size:13px;margin-bottom:20px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:6px;
       padding:18px 20px;margin-bottom:16px}
 .meta{color:var(--mute);font-size:12px;font-family:ui-monospace,Consolas,monospace}
 audio{width:100%;margin:12px 0 4px}
 h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;
    color:var(--accent);margin:0 0 10px;font-weight:600}
 .q{margin:16px 0 6px;font-size:14px}
 .opts{display:flex;flex-wrap:wrap;gap:8px}
 button.opt{background:#22303722;border:1px solid var(--line);color:var(--ink);
   padding:8px 14px;border-radius:5px;cursor:pointer;font-size:14px}
 button.opt:hover{border-color:var(--accent)}
 button.opt.sel{background:var(--accent);color:#0f1417;border-color:var(--accent);
   font-weight:600}
 button.opt kbd{opacity:.55;font-size:11px;margin-left:6px}
 .go{background:var(--accent);color:#0f1417;border:0;padding:11px 22px;
     border-radius:5px;font-size:15px;font-weight:600;cursor:pointer}
 .go:disabled{opacity:.35;cursor:not-allowed}
 .skip{background:transparent;color:var(--mute);border:1px solid var(--line);
   padding:11px 18px;border-radius:5px;font-size:14px;cursor:pointer;
   margin-left:10px}
 .skip:hover{border-color:var(--warn);color:var(--warn)}
 .actions{margin-top:20px;display:flex;align-items:center;flex-wrap:wrap}
 textarea{width:100%;min-height:92px;background:#0f1417;color:var(--ink);
   border:1px solid var(--line);border-radius:5px;padding:10px;font-size:15px;
   font-family:inherit;line-height:1.5}
 .pred{font-family:ui-monospace,Consolas,monospace;font-size:13px;
   background:#0f1417;border:1px solid var(--line);border-radius:5px;
   padding:10px 12px;margin:6px 0 2px;white-space:pre-wrap}
 .lab{display:flex;align-items:center;gap:10px;margin:8px 0}
 .lab .name{flex:1;font-family:ui-monospace,Consolas,monospace;font-size:13px}
 .hid{display:none}
 .bar{color:var(--mute);font-size:12px;margin-bottom:14px;
      display:flex;justify-content:space-between;align-items:center}
 .bar a{color:var(--accent);text-decoration:none}
 .bar a:hover{text-decoration:underline}
 .done{text-align:center;padding:40px 20px}
 .warn{color:var(--warn);font-size:12px;margin-top:8px}
 h3{font-size:14px;margin:22px 0 8px;font-weight:600}
 table{border-collapse:collapse;margin:6px 0 4px;font-size:13px}
 table.cm td,table.cm th{border:1px solid var(--line);padding:6px 10px;
   text-align:center;min-width:74px}
 table.cm th.corner,table.cm th.rh{text-align:left;color:var(--mute);
   font-weight:500;min-width:110px}
 table.cm td.tot{color:var(--mute)}
 .sh{display:block;font-size:11px;color:var(--mute)}
 table.pc td,table.pc th{border:1px solid var(--line);padding:5px 12px;
   text-align:left}
 table.pc th{color:var(--mute);font-weight:500}
 .sum{color:var(--mute);font-size:12px;margin:6px 0 0}
 .caution{color:var(--warn);font-size:13px;margin:10px 0 0}
</style></head><body><div class="wrap">
<h1>Component annotation</h1>
<div class="sub">Stage 1 is answered blind. The pipeline's output appears only after you commit it.</div>
__BODY__
</div>
<script>__SCRIPT__</script></body></html>"""

DONE_BODY = """<div class="card done">
  <h2>All clips annotated</h2>
  <p>{n} clip(s) complete.{skipped_note}</p>
</div>
{results}
<div class="card"><p class="meta">Also written to annotation_store/results.json by:<br>
python tests/diagnostics/annotate_components.py stats</p></div>"""

CLIP_BODY = """
<div class="bar"><span>clip {position} of {total} &nbsp;·&nbsp; {done} done{skipped_note}</span><a href="/results">results so far &rarr;</a></div>

<div class="card">
  <div class="meta">id {id}{snr} &nbsp;·&nbsp; {date} &nbsp;·&nbsp; {device} &nbsp;·&nbsp; {room}</div>
  <audio id="player" controls autoplay src="/audio/{audio_rel}"></audio>
  <div class="meta">space = play/pause &nbsp;·&nbsp; r = replay</div>
</div>

<form method="post" action="/save">
<input type="hidden" name="clip_id" value="{id}">

<div class="card" id="stage1">
  <h2>Stage 1 — listen only</h2>

  <div class="q">Is anyone speaking?</div>
  <div class="opts" data-group="true_speech">
    <button type="button" class="opt" data-val="1">Yes<kbd>y</kbd></button>
    <button type="button" class="opt" data-val="0">No<kbd>n</kbd></button>
  </div>

  <!-- Speaker and emotion both apply to speech only: the pipeline runs the
       speaker embedding and the emotion model on VAD-filtered speech, so on a
       clip with no speech there is nothing for either to have got right. -->
  <div id="speech-only" class="hid">
    <div class="q">Who speaks most in this clip?</div>
    <div class="opts" data-group="true_speaker">
      <button type="button" class="opt" data-val="Ilyas">Ilyas<kbd>1</kbd></button>
      <button type="button" class="opt" data-val="Voula">Voula<kbd>2</kbd></button>
      <button type="button" class="opt" data-val="Rafael">Rafael<kbd>3</kbd></button>
      <button type="button" class="opt" data-val="Other">Not a resident<kbd>4</kbd></button>
      <button type="button" class="opt" data-val="Unclear">Can't tell<kbd>5</kbd></button>
      <button type="button" class="opt" data-val="Multiple">Two or more<kbd>6</kbd></button>
    </div>

    <!-- Only reachable through "Two or more", and it is a multi-select: the
         answer is the set of voices in the clip, not a ranking of them. -->
    <div id="multi-block" class="hid">
      <div class="q">Which of them can you hear?</div>
      <div class="opts multi" data-group="true_speakers">
        <button type="button" class="opt" data-val="Ilyas">Ilyas</button>
        <button type="button" class="opt" data-val="Voula">Voula</button>
        <button type="button" class="opt" data-val="Rafael">Rafael</button>
      </div>
      <div class="meta">Pick every resident you hear &mdash; at least two.
        The clip leaves the one-vs-one speaker matrix and is scored on whether
        the pipeline named someone who is actually there.</div>
    </div>

    <div class="q">How does the speech sound?</div>
    <div class="opts" data-group="true_emotion">
      <button type="button" class="opt" data-val="neutral">Neutral<kbd>q</kbd></button>
      <button type="button" class="opt" data-val="happy">Happy<kbd>w</kbd></button>
      <button type="button" class="opt" data-val="angry">Angry<kbd>e</kbd></button>
      <button type="button" class="opt" data-val="sad">Sad<kbd>a</kbd></button>
      <button type="button" class="opt" data-val="unclear">Can't tell<kbd>s</kbd></button>
    </div>
  </div>

  <input type="hidden" name="true_speech" id="f_true_speech">
  <input type="hidden" name="true_speaker" id="f_true_speaker">
  <input type="hidden" name="true_speakers" id="f_true_speakers">
  <input type="hidden" name="true_emotion" id="f_true_emotion">

  <div class="actions">
    <button type="button" class="go" id="reveal" disabled>Commit and reveal</button>
    <button type="submit" class="skip" formaction="/skip" formnovalidate
            name="skip_stage" value="1">Skip this clip</button>
  </div>
</div>

<div class="card hid" id="stage2">
  <h2>Stage 2 — the pipeline's answer</h2>

  <div class="q">Sound labels — is each one acceptable for this clip?</div>
  {label_rows}

  <div id="transcript-block">
    <div class="q">Transcript — correct it where it is wrong</div>
    <div class="meta">predicted speaker: {pred_speaker} &nbsp;·&nbsp; predicted emotion: {pred_emotion}</div>
    <textarea name="true_text" id="true_text">{pred_text}</textarea>
    <div class="warn" id="emptywarn"></div>
  </div>

  <!-- Shown instead of the transcript on a clip with no speech. Text here is
       the pipeline transcribing something that was never spoken, which the
       speech-gate figures already count as a false positive. -->
  <div id="nospeech-note" class="hid">
    <div class="q">No speech in this clip — nothing to transcribe</div>
    <div class="meta">the pipeline wrote: {pred_text_display}</div>
  </div>

  <div class="q">Notes (optional)</div>
  <textarea name="notes" style="min-height:48px"></textarea>

  <div class="actions">
    <button type="submit" class="go" id="savebtn">Save and next</button>
    <button type="submit" class="skip" formaction="/skip" formnovalidate
            name="skip_stage" value="2">Skip this clip</button>
  </div>
  <div class="meta" style="margin-top:8px">Skipping drops the clip from the
    evaluation entirely &mdash; nothing you answered for it is scored.</div>
</div>
</form>
"""

LABEL_ROW = """
<div class="lab">
  <div class="name">{rank}. {label} <span style="color:#8fa3ab">{conf}</span></div>
  <div class="opts" data-group="ast{rank}">
    <button type="button" class="opt" data-val="acceptable">ok</button>
    <button type="button" class="opt" data-val="partly">partly</button>
    <button type="button" class="opt" data-val="wrong">wrong</button>
  </div>
</div>
<input type="hidden" name="ast{rank}" id="f_ast{rank}">
"""

SCRIPT = """
const sel = {};
function choose(group, val, btn){
  sel[group] = val;
  btn.parentNode.querySelectorAll('.opt').forEach(b => b.classList.remove('sel'));
  btn.classList.add('sel');
  const f = document.getElementById('f_' + group);
  if (f) f.value = val;
  if (group === 'true_speech') {
    // Speaker and emotion are speech-only questions, so on "no speech" they
    // disappear rather than being answered by default. Emotion is cleared as
    // well as hidden: a non-speech clip must not land in the emotion matrix,
    // not even as "can't tell".
    const speech = val === '1';
    document.getElementById('speech-only').className = speech ? '' : 'hid';
    if (!speech) {
      sel['true_speaker'] = 'NoSpeech';
      document.getElementById('f_true_speaker').value = 'NoSpeech';
      delete sel['true_emotion'];
      document.getElementById('f_true_emotion').value = '';
      document.querySelectorAll('.opts[data-group="true_emotion"] .opt')
              .forEach(b => b.classList.remove('sel'));
      clearMulti();
    } else if (sel['true_speaker'] === 'NoSpeech') {
      delete sel['true_speaker'];
      document.getElementById('f_true_speaker').value = '';
    }
  }
  if (group === 'true_speaker') {
    // The set of voices is asked for only when the answer is "two or more",
    // and is dropped the moment a single speaker is named instead.
    const many = val === 'Multiple';
    document.getElementById('multi-block').className = many ? '' : 'hid';
    if (!many) clearMulti();
  }
  check();
}
function clearMulti(){
  delete sel['true_speakers'];
  document.getElementById('f_true_speakers').value = '';
  document.getElementById('multi-block').className = 'hid';
  document.querySelectorAll('.opts.multi .opt').forEach(b =>
          b.classList.remove('sel'));
}
function toggleMulti(grp, btn){
  btn.classList.toggle('sel');
  const picked = Array.from(grp.querySelectorAll('.opt.sel'))
                      .map(b => b.dataset.val);
  sel['true_speakers'] = picked.join(',');
  document.getElementById('f_true_speakers').value = picked.join(',');
  check();
}
function check(){
  // With no speech the only required answer is the speech question itself.
  const need = sel['true_speech'] === '1'
    ? ['true_speech', 'true_speaker', 'true_emotion']
    : ['true_speech'];
  let ok = need.every(k => sel[k]);
  // "Two or more" is not an answer on its own -- one name in the set would
  // have been a single-speaker answer, so the button stays disabled until
  // there are at least two.
  if (ok && sel['true_speaker'] === 'Multiple')
    ok = (sel['true_speakers'] || '').split(',').filter(Boolean).length >= 2;
  const b = document.getElementById('reveal');
  if (b) b.disabled = !ok;
}
document.querySelectorAll('.opts').forEach(g => {
  const group = g.dataset.group;
  if (g.classList.contains('multi')) {
    g.querySelectorAll('.opt').forEach(b =>
      b.addEventListener('click', () => toggleMulti(g, b)));
    return;
  }
  g.querySelectorAll('.opt').forEach(b =>
    b.addEventListener('click', () => choose(group, b.dataset.val, b)));
});
const reveal = document.getElementById('reveal');
if (reveal) reveal.addEventListener('click', () => {
  document.getElementById('stage1').style.opacity = .55;
  document.getElementById('stage1').style.pointerEvents = 'none';
  document.getElementById('stage2').className = 'card';
  // Nothing was spoken, so there is no reference transcript to give. The
  // sound labels are still judged -- that is the whole point of sampling
  // non-speech clips.
  const speech = sel['true_speech'] === '1';
  document.getElementById('transcript-block').className = speech ? '' : 'hid';
  document.getElementById('nospeech-note').className = speech ? 'hid' : '';
  if (!speech) document.getElementById('true_text').value = '';
  document.getElementById('stage2').scrollIntoView({behavior:'smooth'});
});
const KEYS = {y:['true_speech','1'], n:['true_speech','0'],
  1:['true_speaker','Ilyas'], 2:['true_speaker','Voula'], 3:['true_speaker','Rafael'],
  4:['true_speaker','Other'], 5:['true_speaker','Unclear'],
  // 6 opens the set question; the residents in it are picked by clicking,
  // since 1-3 already mean "this one speaker" on the row above.
  6:['true_speaker','Multiple'],
  q:['true_emotion','neutral'], w:['true_emotion','happy'],
  e:['true_emotion','angry'], a:['true_emotion','sad'], s:['true_emotion','unclear']};
document.addEventListener('keydown', ev => {
  if (ev.target.tagName === 'TEXTAREA') return;
  const p = document.getElementById('player');
  if (ev.key === ' ') { ev.preventDefault(); p.paused ? p.play() : p.pause(); return; }
  if (ev.key === 'r') { p.currentTime = 0; p.play(); return; }
  const hit = KEYS[ev.key];
  if (!hit) return;
  const grp = document.querySelector(`.opts[data-group="${hit[0]}"]`);
  if (!grp || grp.closest('.hid')) return;
  const btn = grp.querySelector(`.opt[data-val="${hit[1]}"]`);
  if (btn) btn.click();
});
"""


def cmd_annotate(args):
    try:
        from flask import Flask, request, redirect, send_from_directory
    except ImportError:
        sys.exit("Flask is required (it ships with Dash): pip install flask")

    conn = store_init()
    total = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    if not total:
        sys.exit("Store is empty. Run `sample` first.")
    conn.close()

    # Windows lets a second process bind a port that is already listening
    # instead of refusing it. Both then accept connections, requests land on
    # whichever socket wins, and the browser hangs on the half of them that
    # reach the instance nobody is watching. Refuse to be the second one.
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    already_up = probe.connect_ex(("127.0.0.1", args.port)) == 0
    probe.close()
    if already_up:
        sys.exit(
            f"Port {args.port} is already serving.\n"
            f"  Another copy of this tool is probably still running -- use it at\n"
            f"  http://127.0.0.1:{args.port}, or stop it and start again.\n"
            f"  To run a second one anyway: --port {args.port + 1}")

    app = Flask(__name__)

    def next_clip(db):
        return db.execute(
            "SELECT c.* FROM clips c"
            " LEFT JOIN annotations a ON a.clip_id = c.id"
            " WHERE a.stage2_at IS NULL ORDER BY c.date LIMIT 1").fetchone()

    @app.route("/")
    def index():
        db = store_connect()
        # A skipped clip is out of the queue but is not a completed judgement,
        # so it is counted apart from `done` everywhere it is shown.
        done = db.execute("SELECT COUNT(*) FROM annotations"
                          " WHERE stage2_at IS NOT NULL"
                          " AND skipped = 0").fetchone()[0]
        skipped = count_skipped(db)
        total = db.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
        clip = next_clip(db)
        if clip is None:
            # Everything is annotated: show the metrics here rather than
            # sending the annotator to a terminal for them.
            done_rows = collect(db)
            report = compute_stats(done_rows, skipped)
            db.close()
            _write_results(report)
            # The run is over, so produce the publication figures now rather
            # than making the annotator remember a second command. A plotting
            # failure must not cost them the report they just earned.
            try:
                made = make_figures(report, dpi=args.dpi)
                log_note = (f"<p class='meta'>{len(made)} figure(s) written to "
                            f"annotation_store/figures/ at {args.dpi} dpi</p>")
            except Exception as exc:
                log_note = (f"<p class='warn'>Figures could not be written: "
                            f"{exc}</p>")
            try:
                log_note += (f"<p class='meta'>commentary written to "
                             f"{os.path.basename(write_commentary(done_rows, report))}"
                             f"</p>")
            except Exception as exc:
                log_note += (f"<p class='warn'>Commentary could not be written: "
                             f"{exc}</p>")
            body = DONE_BODY.format(
                n=done,
                skipped_note=f" {skipped} skipped." if skipped else "",
                results=report_html(report, total) + log_note)
            return PAGE.replace("__BODY__", body).replace("__SCRIPT__", "")
        db.close()

        rows = ""
        for rank in (1, 2, 3):
            label = clip[f"sound{rank}"]
            conf = clip[f"conf{rank}"]
            if not label or label == "None":
                continue
            rows += LABEL_ROW.format(rank=rank, label=label,
                                     conf=f"{conf:.2f}" if conf else "")
        # SNR is shown beside the id because it is the best single predictor of
        # whether a clip will be worth annotating: the transcription failures
        # cluster at the low end, so seeing it makes a skip an informed one.
        snr_value = clip["snr"] if "snr" in clip.keys() else None
        snr_text = (f" &nbsp;·&nbsp; SNR {snr_value:.1f} dB"
                    if snr_value is not None else "")
        body = CLIP_BODY.format(
            position=done + skipped + 1, total=total, done=done,
            skipped_note=(f" &nbsp;·&nbsp; {skipped} skipped" if skipped else ""),
            id=clip["id"], snr=snr_text,
            date=clip["date"], device=clip["device"] or "?",
            room=clip["room"] or "?", audio_rel=clip["audio_rel"],
            label_rows=rows or "<div class='meta'>no labels recorded</div>",
            pred_speaker=clip["pred_speaker"] or "-",
            pred_emotion=clip["pred_emotion"] or "-",
            pred_text=(clip["pred_text"] or ""),
            pred_text_display=(clip["pred_text"] or "").strip() or "(nothing)")
        return PAGE.replace("__BODY__", body).replace("__SCRIPT__", SCRIPT)

    @app.route("/audio/<path:name>")
    def audio(name):
        return send_from_directory(AUDIO_DIR, name)

    @app.route("/results")
    def results():
        db = store_connect()
        total = db.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
        live_rows = collect(db)
        report = compute_stats(live_rows, count_skipped(db))
        db.close()
        _write_results(report)
        # Kept in step with results.json, so the commentary never describes an
        # older run than the figures beside it.
        if live_rows:
            write_commentary(live_rows, report)
        body = ('<div class="bar"><span></span>'
                '<a href="/">&larr; back to annotating</a></div>'
                + report_html(report, total))
        return PAGE.replace("__BODY__", body).replace("__SCRIPT__", "")

    @app.route("/save", methods=["POST"])
    def save():
        f = request.form
        now = datetime.now().isoformat(timespec="seconds")
        db = store_connect()
        # Only kept alongside the "Multiple" answer: a stale set left over from
        # a changed answer would silently score the clip on voices the
        # annotator no longer claims are there.
        many = f.get("true_speaker") == "Multiple"
        db.execute(
            "INSERT OR REPLACE INTO annotations (clip_id, true_speech,"
            " true_speaker, true_speakers, true_emotion, ast1, ast2, ast3,"
            " true_text, notes, stage1_at, stage2_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(f["clip_id"]), int(f.get("true_speech") or 0),
             f.get("true_speaker") or None,
             (f.get("true_speakers") or None) if many else None,
             f.get("true_emotion") or None,
             f.get("ast1") or None, f.get("ast2") or None, f.get("ast3") or None,
             (f.get("true_text") or "").strip(), (f.get("notes") or "").strip(),
             now, now))
        db.commit()
        db.close()
        return redirect("/")

    @app.route("/skip", methods=["POST"])
    def skip():
        # The row is written with skipped = 1 and nothing else: whatever was
        # answered before the skip is deliberately discarded rather than kept
        # as a half-judgement. stage2_at is set only so next_clip() moves past
        # this clip -- collect() filters it out of the scoring either way.
        f = request.form
        now = datetime.now().isoformat(timespec="seconds")
        db = store_connect()
        db.execute(
            "INSERT OR REPLACE INTO annotations (clip_id, true_speech,"
            " true_speaker, true_emotion, ast1, ast2, ast3, true_text, notes,"
            " stage1_at, stage2_at, skipped)"
            " VALUES (?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,?,?,1)",
            (int(f["clip_id"]), (f.get("notes") or "").strip(), now, now))
        db.commit()
        # Dropped straight away rather than swept up later. A skipped clip is
        # never revisited -- next_clip() moves past it on stage2_at -- so the
        # recording and its transcript have no further use, and a store stays
        # deliverable without anyone remembering to clean it.
        prune_one_skipped(db, int(f["clip_id"]))
        db.commit()
        db.close()
        return redirect("/")

    print(f"\n  annotation UI:  http://127.0.0.1:{args.port}")
    print(f"  store:          {STORE_DB}")
    print("  ctrl-c to stop\n")
    app.run(host="127.0.0.1", port=args.port, debug=False)


# ── cli ─────────────────────────────────────────────────────────────

def use_store(name: str):
    """Point every path at a named store.

    One store is one dataset. Keeping a pilot and a real collection apart
    matters: sampling twice into the same store would silently mix ten
    shakedown clips into the run being reported.
    """
    global STORE_DIR, STORE_DB, AUDIO_DIR, FIG_DIR
    base = os.path.dirname(os.path.abspath(__file__))
    STORE_DIR = name if os.path.isabs(name) else os.path.join(base, name)
    STORE_DB = os.path.join(STORE_DIR, "annotations.db")
    AUDIO_DIR = os.path.join(STORE_DIR, "audio")
    FIG_DIR = os.path.join(STORE_DIR, "figures")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", default="annotation_store",
                   help="which dataset to work on (default: annotation_store). "
                        "Use a fresh name per collection, e.g. --store run_oct")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sample", help="freeze a stratified sample into the store")
    s.add_argument("--n", type=int, default=10)
    s.add_argument("--since", default="2026-08-17",
                   help='start of the window: "YYYY-MM-DD" or '
                        '"YYYY-MM-DD HH:MM:SS"')
    s.add_argument("--until", default="2026-08-23",
                   help='end of the window. A bare date means the whole of '
                        'that day; add a time for an exact cut-off, e.g. '
                        '"2026-08-27 14:10:00"')
    s.add_argument("--speech-fraction", type=float, default=0.85,
                   help="share of the sample drawn from predicted-speech clips "
                        "(default 0.85; the remainder is predicted non-speech, "
                        "which is what measures the speech gate's false positives)")
    s.add_argument("--seed", type=int, default=20260825)
    s.set_defaults(func=cmd_sample)

    a = sub.add_parser("annotate", help="serve the annotation UI")
    a.add_argument("--port", type=int, default=8061)
    a.add_argument("--dpi", type=int, default=300,
                   help="resolution of the figures written when the last "
                        "clip is annotated")
    a.set_defaults(func=cmd_annotate)

    c = sub.add_parser("commentary",
                       help=f"(re)write {COMMENTARY_AUTO} from the current scores")
    c.add_argument("--show", action="store_true",
                   help="also print it to the terminal")
    c.set_defaults(func=cmd_commentary)

    pr = sub.add_parser("prune", help="remove audio and content for skipped "
                                      "clips, so the store can be delivered whole")
    pr.add_argument("--dry-run", action="store_true",
                    help="report what would be removed without removing it")
    pr.set_defaults(func=cmd_prune)

    r = sub.add_parser("reparse", help="re-derive the pipeline's speaker and "
                                       "text from the stored transcripts")
    r.add_argument("--dry-run", action="store_true",
                   help="report what would change without writing")
    r.set_defaults(func=cmd_reparse)

    f = sub.add_parser("figures", help="(re)write the publication figures")
    f.add_argument("--dpi", type=int, default=300)
    f.set_defaults(func=cmd_figures)

    t = sub.add_parser("stats", help="score everything annotated so far")
    t.set_defaults(func=cmd_stats)

    args = p.parse_args()
    use_store(args.store)
    args.func(args)


if __name__ == "__main__":
    main()
