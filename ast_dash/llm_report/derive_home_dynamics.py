#!/usr/bin/env python3
"""
derive_home_dynamics.py — Home Sentinel event pipeline, Levels 0-4.

Turns raw dashboard audio events into a progressively more meaningful
hierarchy, so that an LLM asked to reason about household life is handed
structured evidence rather than a heap of AudioSet labels.

    RAW  ->  L0 canonical observations
         ->  L1 semantic events
         ->  L1.5 semantic interaction (what the speech meant between people)
         ->  L2 activity episodes
         ->  L3 person / social episodes
         ->  L4 daily + longitudinal dynamics

Design notes
------------
Each level is a pure function of the level below it, so any one level can be
replaced without touching the others. Records share a generic shape --
type / confidence / evidence / source_ids / start / end / room / people /
uncertainty -- which is what lets a future vision or door-sensor modality
join at Level 1 and flow upward unchanged.

Level 1 is a registry of *extractors*. Each reads Level 0 and emits semantic
events in a common shape; nothing above Level 1 knows which modality
produced what. Adding vision means appending an extractor.

Two rules the whole file obeys:

  * Absence of an event is not silence. The sentinels are event-triggered,
    so gaps are reported as `no_observation_interval` and never as quiet.
  * Weak evidence stays weak. Uncertain inferences keep a `_candidate`
    suffix, carry their evidence, and are counted in `uncertainties`.

No network calls and no database. Levels 0-1 and 2-4 are deterministic and
reproducible. Level 1.5 is the one interpretive stage: it needs a language
model to read meaning, so the model is passed IN as a callable rather than
constructed here. Given no interpreter the level still runs, marking every
candidate exchange "not_attempted" -- so the rest of the pipeline never
depends on a model being reachable, and a missing interpretation stays visible
as missing rather than reading as an absence of warmth.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Imports and constants
# ---------------------------------------------------------------------------

import argparse
import difflib
import hashlib
import json
import math
import re
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

PIPELINE_VERSION = "1.0"

# Dashboard timestamps look like "2026-08-18 13:31:56"; ISO is emitted.
TS_INPUT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")

# The emotion model emits three-letter codes with a confidence in brackets,
# e.g. "SAD (0.45)". The literal string "None" means no emotion was produced.
EMOTION_CODES = {
    "HAP": "happy", "HAPPY": "happy",
    "SAD": "sad",
    "ANG": "angry", "ANGRY": "angry",
    "NEU": "neutral", "NEUTRAL": "neutral",
    "FEA": "fearful", "DIS": "disgust", "SUR": "surprised",
}

# Diarisation is written into the transcription string rather than a field:
#   "[ 00: 00. 00 - 00: 29. 98 ] Ilyas: some words"
# The clock groups tolerate the stray spaces the ASR writer emits.
DIARIZATION_SEGMENT_RE = re.compile(
    r"\[\s*(?P<start>[\d:.\s]+?)\s*-\s*(?P<end>[\d:.\s]+?)\s*\]\s*"
    r"(?P<speaker>[^:\[\]]*?)\s*:",
)

# Values that mean "nothing here" across several differently-typed fields.
NULLISH = {"", "none", "null", "nan", "no speech detected", "unknown"}

# Speaker label the diarizer uses when it cannot attribute a segment.
UNKNOWN_SPEAKER = "Unknown"


# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------

# AudioSet label -> semantic types. Deliberately data, not logic: extend this
# freely without touching any level's code.
SEMANTIC_MAPPINGS: dict[str, list[str]] = {
    # water / bathroom
    "Water": ["water_use"],
    "Water tap, faucet": ["water_use", "faucet_use"],
    "Sink (filling or washing)": ["water_use", "faucet_use"],
    "Bathtub (filling or washing)": ["water_use", "personal_care_candidate"],
    "Shower": ["water_use", "personal_care_candidate"],
    "Toilet flush": ["bathroom_use_candidate"],
    # speech / voice
    "Speech": ["speech_activity"],
    "Male speech, man speaking": ["speech_activity"],
    "Female speech, woman speaking": ["speech_activity"],
    "Child speech, kid speaking": ["speech_activity"],
    "Conversation": ["speech_activity", "conversation_candidate"],
    "Narration, monologue": ["speech_activity"],
    "Telephone": ["telephone_activity"],
    "Telephone bell ringing": ["telephone_activity"],
    "Whispering": ["speech_activity"],
    "Shout": ["speech_activity", "raised_voice_candidate"],
    "Yell": ["speech_activity", "raised_voice_candidate"],
    # media
    "Music": ["media_activity"],
    "Television": ["media_activity", "tv_activity"],
    "Radio": ["media_activity"],
    "Musical instrument": ["media_activity"],
    "Singing": ["media_activity", "human_vocal_sound"],
    # objects / furniture
    "Drawer open or close": ["object_access"],
    "Cupboard open or close": ["object_access"],
    "Door": ["entry_exit_candidate"],
    "Slam": ["entry_exit_candidate"],
    "Knock": ["entry_interaction_candidate"],
    "Doorbell": ["entry_interaction_candidate"],
    # human vocal / body
    "Cough": ["human_vocal_sound", "cough"],
    "Throat clearing": ["human_vocal_sound"],
    "Sneeze": ["human_vocal_sound"],
    "Breathing": ["human_presence_candidate"],
    "Snort": ["human_vocal_sound", "human_presence_candidate"],
    "Snoring": ["human_presence_candidate", "probable_sleep_candidate"],
    "Sniff": ["human_vocal_sound"],
    "Footsteps": ["human_presence_candidate", "movement_candidate"],
    "Walk, footsteps": ["human_presence_candidate", "movement_candidate"],
    "Laughter": ["laughter", "positive_social_candidate"],
    "Giggle": ["laughter", "positive_social_candidate"],
    "Crying, sobbing": ["distress_vocalisation_candidate"],
    "Applause": ["social_activity_candidate"],
    # kitchen / food
    "Dishes, pots, and pans": ["food_activity_candidate"],
    "Cutlery, silverware": ["food_activity_candidate"],
    "Boiling": ["cooking_candidate"],
    "Frying (food)": ["cooking_candidate"],
    "Microwave oven": ["cooking_candidate"],
    "Chopping (food)": ["cooking_candidate"],
    "Chink, clink": ["object_handling_candidate"],
    "Washing dishes": ["cleaning_activity", "food_cleanup_candidate"],
    # cleaning / appliances
    "Vacuum cleaner": ["cleaning_activity"],
    "Washing machine": ["appliance_activity"],
    "Blender": ["cooking_candidate", "appliance_activity"],
    # ambience -- deliberately mapped to nothing meaningful
    "Inside, small room": [],
    "Inside, large room or hall": [],
    "Silence": [],
}

# How long a gap may be before a new episode starts, per semantic family.
DEFAULT_EPISODE_GAPS: dict[str, int] = {
    "water_use": 300,
    "speech_activity": 120,
    "media_activity": 600,
    "tv_activity": 600,
    "object_access": 180,
    "cooking_candidate": 600,
    "food_activity_candidate": 600,
    "cleaning_activity": 600,
    "human_presence_candidate": 300,
    "default": 300,
}

# Composite rules: when the semantic types present in a candidate episode
# satisfy a rule, the episode is relabelled and its confidence nudged.
# Evaluated in order; the first match wins. `rooms` of None means any room.
COMPOSITE_ACTIVITY_RULES: list[dict[str, Any]] = [
    {
        "activity": "personal_care_candidate",
        "requires_any": [{"water_use"}, {"personal_care_candidate"}],
        "supports": {"human_presence_candidate", "faucet_use", "human_vocal_sound"},
        "rooms": {"Bathroom", "WC"},
        "boost": 0.15,
    },
    {
        "activity": "bathroom_use_candidate",
        "requires_any": [{"bathroom_use_candidate"}],
        "supports": {"water_use", "human_presence_candidate"},
        "rooms": {"Bathroom", "WC"},
        "boost": 0.10,
    },
    {
        "activity": "food_preparation_candidate",
        "requires_any": [{"cooking_candidate"}, {"food_activity_candidate"}],
        "supports": {"object_access", "object_handling_candidate", "water_use"},
        "rooms": None,
        "boost": 0.10,
    },
    {
        "activity": "media_use",
        "requires_any": [{"tv_activity"}, {"media_activity"}],
        "supports": {"speech_activity"},
        "rooms": None,
        "boost": 0.05,
    },
    {
        "activity": "cleaning_activity",
        "requires_any": [{"cleaning_activity"}],
        "supports": {"water_use", "object_access"},
        "rooms": None,
        "boost": 0.05,
    },
]

DEFAULT_CONFIG: dict[str, Any] = {
    "household_members": {
        "Ilyas": {"role": "adult", "sex": "male"},
        "Voula": {"role": "adult", "sex": "female"},
        "Rafael": {"role": "child", "sex": "male"},
    },
    "thresholds": {
        "min_semantic_confidence": 0.05,
        "min_speaker_confidence": 0.60,
        "conversation_gap_seconds": 120,
        "copresence_gap_seconds": 300,
        "min_baseline_days": 7,
        # Level 1.5. The word floor is the intelligibility gate: below it an
        # exchange is too thin to carry meaning, and asking a model to read one
        # anyway is how confident nonsense enters the record.
        "semantic_min_words": 8,
        # A reply is itself evidence, so a genuine back-and-forth clears a
        # lower bar than one person talking: "Έρχεσαι να φάμε;" / "Άσε με
        # ήσυχο." is six words and completely unambiguous, while six words
        # from a single turn usually is not.
        "semantic_min_words_exchange": 4,
        "semantic_min_turns_for_exchange": 2,
        "semantic_min_named_speakers": 0,
        "semantic_context_turns": 3,
        "semantic_low_word_warning": 15,
        # Non-speech sounds shown alongside an exchange: how far either
        # side to look, how confident a label must be, and how many to
        # show before the room's noise drowns the conversation.
        "semantic_sound_margin_seconds": 15,
        "semantic_sound_min_probability": 0.15,
        "semantic_sound_max": 6,
        "no_observation_gap_seconds": 1800,
        "corroboration_bonus": 0.05,
        "max_semantic_confidence": 0.95,
        # One utterance heard by several sentinels is kept once -- see
        # merge_cross_sentinel_turns. False puts every copy back as a turn.
        "cross_sentinel_merge": True,
        "cross_sentinel_slack_seconds": 3.0,
        "cross_sentinel_min_containment": 0.7,
    },
    "episode_gaps": dict(DEFAULT_EPISODE_GAPS),
    # device -> room, used only when an event carries no room of its own.
    "device_room_map": {},
    "semantic_mappings": {},  # merged over SEMANTIC_MAPPINGS
}


def load_config(path: str | None) -> dict[str, Any]:
    """Internal defaults, optionally overridden by a JSON file (deep-merged)."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if not path:
        return cfg
    try:
        user = _read_json(Path(path))
    except Exception as exc:
        print(f"WARNING: could not read config {path}: {exc}", file=sys.stderr)
        return cfg
    for key, value in (user or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def effective_semantic_mappings(cfg: dict[str, Any]) -> dict[str, list[str]]:
    mappings = dict(SEMANTIC_MAPPINGS)
    mappings.update(cfg.get("semantic_mappings") or {})
    return mappings


# ---------------------------------------------------------------------------
# 3. Input loading
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    """Read JSON tolerating a UTF-8 BOM, which the dashboard export writes."""
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def load_raw_events(path: str) -> list[dict[str, Any]]:
    """Accept either a bare list of events or an object wrapping one."""
    data = _read_json(Path(path))
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        for key in ("events", "data", "records", "audio_events", "rows"):
            if isinstance(data.get(key), list):
                return [e for e in data[key] if isinstance(e, dict)]
        # Fall back to the first list of dicts we find.
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [e for e in value if isinstance(e, dict)]
    raise ValueError("Input JSON contains no recognisable list of events")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _is_nullish(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower()
    # "no speech detected (vad|asr|ast)" all mean the same thing here: no
    # words were captured. The suffix is for a human reading the grid.
    return text in NULLISH or text.startswith("no speech detected")


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def parse_timestamp(value: Any) -> datetime | None:
    """Parse to a timezone-naive datetime; the source carries no offset."""
    if _is_nullish(value):
        return None
    text = str(value).strip()
    for fmt in TS_INPUT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:  # last resort: ISO with optional offset, which we then drop
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_emotion(value: Any) -> dict[str, Any] | None:
    """'SAD (0.45) [a=0.31 d=0.42 v=0.28]' -> label, confidence and the raw axes.

    The bracketed part arrived later than the label and is absent from every
    row written before 2026-09-25, so it is optional and its absence is not an
    error. The label is what the four-class channel reports and it scores at
    chance against a listener; the three axes are the regression it was
    derived from, kept so that channel can be re-examined without re-running
    anything.
    """
    if _is_nullish(value):
        return None
    text = str(value).strip()
    match = re.match(r"^\s*([A-Za-z_]+)\s*(?:\(\s*([\d.]+)\s*\))?", text)
    if not match:
        return None
    code = match.group(1).upper()
    label = EMOTION_CODES.get(code, code.lower())
    out: dict[str, Any] = {"label": label, "confidence": _to_float(match.group(2))}
    axes = re.search(r"\[\s*a=(-?[\d.]+)\s+d=(-?[\d.]+)\s+v=(-?[\d.]+)\s*\]", text)
    if axes:
        out["arousal"] = _to_float(axes.group(1))
        out["dominance"] = _to_float(axes.group(2))
        out["valence"] = _to_float(axes.group(3))
    return out


def _parse_clock(text: str) -> float | None:
    """'00: 00. 29. 98' style clocks -> seconds. Tolerates ASR whitespace."""
    cleaned = re.sub(r"\s+", "", str(text))
    if not cleaned:
        return None
    # Split on ':' for h/m/s, allowing a fractional final component.
    parts = cleaned.split(":")
    try:
        values = [float(p) for p in parts if p != ""]
    except ValueError:
        return None
    if not values:
        return None
    seconds = 0.0
    for value in values:
        seconds = seconds * 60.0 + value
    return seconds


def parse_speech_segments(transcription: Any) -> list[dict[str, Any]]:
    """Pull diarised segments out of the transcription string.

    The pipeline writes speaker identity into the text rather than into a
    field, e.g. "[ 00: 00. 00 - 00: 29. 98 ] Ilyas: words". Several segments
    may appear in one transcription, which is real turn-taking evidence.
    Returns [] when there is nothing usable.
    """
    if _is_nullish(transcription):
        return []
    text = str(transcription)
    matches = list(DIARIZATION_SEGMENT_RE.finditer(text))
    if not matches:
        # Speech with words but no diarisation markers.
        stripped = text.strip()
        return [{
            "speaker": None, "speaker_confidence": None,
            "offset_start": None, "offset_end": None, "text": stripped,
        }] if stripped else []

    segments: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        words = text[body_start:body_end].strip()
        raw_speaker = (match.group("speaker") or "").strip()
        speaker = None if _is_nullish(raw_speaker) else raw_speaker
        segments.append({
            "speaker": speaker,                      # None when 'Unknown'
            "speaker_raw": raw_speaker or None,
            # Diarisation carries no per-segment probability, so confidence is
            # left unset rather than invented.
            "speaker_confidence": None,
            "offset_start": _parse_clock(match.group("start")),
            "offset_end": _parse_clock(match.group("end")),
            "text": words or None,
        })
    return segments


def _stable_id(prefix: str, day: str, index: int) -> str:
    """Deterministic, human-readable, stable for a given ordered input."""
    return f"{prefix}_{day}_{index:04d}"


def _round(value: float | None, places: int = 3) -> float | None:
    return None if value is None else round(float(value), places)


# ---------------------------------------------------------------------------
# 4. Level 0 — canonical observations
# ---------------------------------------------------------------------------

@dataclass
class Level0Observation:
    event_id: Any
    timestamp: str | None
    room: str | None
    device: str | None
    rms: float | None
    audio_labels: list[dict[str, Any]] = field(default_factory=list)
    speech: dict[str, Any] = field(default_factory=dict)
    emotion: dict[str, Any] | None = None
    source_file: str | None = None
    raw_labels_discarded: list[str] = field(default_factory=list)
    room_source: str = "event"


def derive_level0(raw_events: list[dict[str, Any]], cfg: dict[str, Any]) -> list[Level0Observation]:
    """Normalise the dashboard export without adding any interpretation."""
    device_rooms = cfg.get("device_room_map") or {}
    observations: list[Level0Observation] = []

    for raw in raw_events:
        timestamp = parse_timestamp(raw.get("Timestamp") or raw.get("timestamp") or raw.get("date"))

        labels: list[dict[str, Any]] = []
        discarded: list[str] = []
        for slot in (1, 2, 3):
            label = raw.get(f"Class {slot}", raw.get(f"class_{slot}"))
            prob = _to_float(raw.get(f"Prob {slot}", raw.get(f"prob_{slot}")), 0.0) or 0.0
            if _is_nullish(label):
                if label is not None:
                    discarded.append(str(label))
                continue
            labels.append({"label": str(label).strip(), "probability": _round(prob)})

        device = raw.get("Device") or raw.get("device") or raw.get("device_name")
        room = raw.get("room") or raw.get("Room")
        room_source = "event"
        if _is_nullish(room):
            room = device_rooms.get(str(device)) if device else None
            room_source = "device_map" if room else "unknown"
            if _is_nullish(room):
                room = None
                room_source = "unknown"

        transcription = raw.get("transcription")
        segments = parse_speech_segments(transcription)
        has_words = any(seg.get("text") for seg in segments)
        # A transcription that exists at all is evidence of detected speech,
        # even when the recogniser produced no words for it.
        has_speech = bool(segments) or not _is_nullish(transcription)

        # An explicit speaker field is preferred when present; the current
        # export has none, so diarisation is the only source.
        explicit_speaker = raw.get("speaker")
        explicit_conf = _to_float(raw.get("speaker_confidence"))
        if not _is_nullish(explicit_speaker):
            for seg in segments or [{}]:
                seg.setdefault("speaker", str(explicit_speaker))
                seg.setdefault("speaker_confidence", explicit_conf)
            if not segments:
                segments = [{
                    "speaker": str(explicit_speaker), "speaker_raw": str(explicit_speaker),
                    "speaker_confidence": explicit_conf,
                    "offset_start": None, "offset_end": None, "text": None,
                }]

        speakers = sorted({s["speaker"] for s in segments if s.get("speaker")})

        observations.append(Level0Observation(
            event_id=raw.get("ID", raw.get("id", raw.get("event_id"))),
            timestamp=_iso(timestamp),
            room=room,
            device=str(device) if device else None,
            rms=_round(_to_float(raw.get("rms"))),
            audio_labels=labels,
            speech={
                "transcription": None if _is_nullish(transcription) else str(transcription),
                "has_speech": has_speech,
                "has_words": has_words,
                "segments": segments,
                "speakers": speakers,
            },
            emotion=parse_emotion(raw.get("emotion")),
            source_file=None if _is_nullish(raw.get("file_name")) else str(raw.get("file_name")),
            raw_labels_discarded=discarded,
            room_source=room_source,
        ))

    # Chronological, with unparseable timestamps last but retained.
    observations.sort(key=lambda o: (o.timestamp is None, o.timestamp or "", str(o.event_id)))
    return observations


# ---------------------------------------------------------------------------
# 5. Level 1 — semantic events
# ---------------------------------------------------------------------------

@dataclass
class Level1Record:
    source_event_id: Any
    timestamp: str | None
    room: str | None
    semantic_events: list[dict[str, Any]] = field(default_factory=list)


def _combine_confidence(probabilities: list[float], cfg: dict[str, Any]) -> float:
    """Strongest evidence wins; corroboration adds a small deterministic bonus.

    Deliberately not a probabilistic fusion: the inputs are not independent
    and pretending otherwise would manufacture confidence.
    """
    if not probabilities:
        return 0.0
    thresholds = cfg["thresholds"]
    bonus = thresholds.get("corroboration_bonus", 0.05)
    ceiling = thresholds.get("max_semantic_confidence", 0.95)
    return round(min(max(probabilities) + bonus * (len(probabilities) - 1), ceiling), 3)


# --- extractor registry ----------------------------------------------------
# Each extractor turns one Level 0 observation into semantic events. New
# modalities (vision, door sensors) join here without touching Levels 2-4.

Extractor = Callable[[Level0Observation, dict[str, Any]], list[dict[str, Any]]]


def audio_label_extractor(obs: Level0Observation, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """AudioSet labels -> semantic types via the mapping table."""
    mappings = effective_semantic_mappings(cfg)
    minimum = cfg["thresholds"]["min_semantic_confidence"]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmapped: list[dict[str, Any]] = []
    for item in obs.audio_labels:
        types = mappings.get(item["label"])
        if types is None:
            unmapped.append(item)
            continue
        for semantic_type in types:  # empty list = intentionally ignored
            grouped[semantic_type].append(item)

    events: list[dict[str, Any]] = []
    for semantic_type, evidence in sorted(grouped.items()):
        probabilities = [e["probability"] or 0.0 for e in evidence]
        confidence = _combine_confidence(probabilities, cfg)
        if confidence < minimum:
            continue
        events.append({
            "type": semantic_type,
            "confidence": confidence,
            "evidence": evidence,
            "modality": "audio_labels",
        })

    # Labels we have no mapping for are surfaced, not silently dropped.
    for item in unmapped:
        if (item["probability"] or 0.0) >= minimum:
            events.append({
                "type": "unknown_activity",
                "confidence": item["probability"],
                "evidence": [item],
                "modality": "audio_labels",
                "uncertainty": [f"unmapped label: {item['label']}"],
            })
    return events


def diarization_extractor(obs: Level0Observation, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Speech/speaker evidence from the transcription, independent of labels.

    The AudioSet head often scores Speech very low on recordings that clearly
    contain diarised words, so speech evidence is derived here rather than
    depending on the label path.
    """
    segments = obs.speech.get("segments") or []
    if not segments:
        return []

    events: list[dict[str, Any]] = [{
        "type": "speech_activity",
        # Diarisation output is strong evidence that speech occurred.
        "confidence": 0.90 if obs.speech.get("has_words") else 0.70,
        "evidence": [{"label": "diarization_segment", "probability": None,
                      "segments": len(segments)}],
        "modality": "diarization",
    }]

    named = sorted({s["speaker"] for s in segments if s.get("speaker")})
    for speaker in named:
        speaker_segments = [s for s in segments if s.get("speaker") == speaker]
        events.append({
            "type": "identified_speaker",
            "confidence": max(
                (s.get("speaker_confidence") or 0.80) for s in speaker_segments),
            "person": speaker,
            "evidence": [{"label": "diarization_speaker", "speaker": speaker,
                          "segments": len(speaker_segments)}],
            "modality": "diarization",
        })

    # Two or more distinct known speakers inside one recording is the
    # strongest single piece of turn-taking evidence available.
    if len(named) >= 2:
        events.append({
            "type": "conversation_candidate",
            "confidence": 0.75,
            "evidence": [{"label": "multiple_speakers_in_one_recording",
                          "speakers": named}],
            "modality": "diarization",
        })
    return events


def emotion_extractor(obs: Level0Observation, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Emotion is carried forward as evidence only, never as a conclusion."""
    if not obs.emotion or not obs.emotion.get("label"):
        return []
    return [{
        "type": "emotion_evidence",
        "confidence": obs.emotion.get("confidence") or 0.0,
        "emotion": obs.emotion["label"],
        "evidence": [{"label": f"emotion:{obs.emotion['label']}",
                      "probability": obs.emotion.get("confidence")}],
        "modality": "emotion_model",
    }]


EXTRACTORS: list[Extractor] = [
    audio_label_extractor,
    diarization_extractor,
    emotion_extractor,
]


def derive_level1(level0: list[Level0Observation], cfg: dict[str, Any]) -> list[Level1Record]:
    records: list[Level1Record] = []
    for obs in level0:
        semantic: list[dict[str, Any]] = []
        for extractor in EXTRACTORS:
            try:
                semantic.extend(extractor(obs, cfg))
            except Exception as exc:  # one bad event must not stop the run
                print(f"WARNING: extractor {extractor.__name__} failed on "
                      f"event {obs.event_id}: {exc}", file=sys.stderr)
        records.append(Level1Record(
            source_event_id=obs.event_id,
            timestamp=obs.timestamp,
            room=obs.room,
            semantic_events=semantic,
        ))
    return records


# ---------------------------------------------------------------------------
# 5.5 Level 1.5 — semantic interaction
#
# L1 says a semantic event occurred. L1.5 says what a spoken exchange MEANT
# between people, which L1 cannot: "I love you" and "you idiot" are both
# `speech_activity` + `conversation_candidate` + two `identified_speaker`s, and
# the distinction between them is otherwise lost before L4 ever sees it.
#
# The central rule of this level is that engagement and relational quality are
# separate axes. An argument is a strong interaction: high connection AND high
# tension is a valid, expected output, not a contradiction to be smoothed away.
#
# Interpretation needs a language model, and this module is deterministic by
# design. So the model is INJECTED: derive_level1_5 takes an `interpreter`
# callable and, given none, still emits one record per candidate exchange with
# status "not_attempted" and null dimensions. Missing interpretation therefore
# stays visible as missing rather than reading as an absence of warmth.
# ---------------------------------------------------------------------------

# `inclusion` was the sixth and was dropped on 2026-09-22. Measured against a
# listener on 99 exchanges it was not carrying its own weight from either
# side: the coder's inclusion correlated 0.38 with their own connection and
# 0.07 with joy -- they were scoring reciprocity, which is connection -- while
# the interpreter's correlated 0.43 with warmth and 0.45 with joy, making it a
# second warmth score. A dimension two careful readers read two different ways,
# each of them a duplicate of a dimension already here, measures nothing.
# `inclusion` survives as an INTERACTION TYPE, where it names an observable
# act rather than a quantity.
# `connection` was dropped on 2026-09-25 and rebuilt at Level 4 as a daily
# reciprocity rate. Per exchange it could not vary: an exchange reaches this
# level because two residents spoke in it, and measured against a listener on
# 91 exchanges it scored weighted kappa -0.09 while being lit on 98% of them.
# `inclusion` went earlier, for a different reason -- see the note below.
RELATIONAL_DIMENSIONS = ("warmth", "joy", "withdrawal", "tension")

# The score at or above which a dimension counts as PRESENT in an exchange, for
# the `present` count in every dimension summary. Measured against a listener on
# 90 exchanges: at 0.40 the interpreter called an exchange warm with 71%
# sensitivity and 80% precision. One floor for all five, because the evidence is
# about where the interpreter's scale sits rather than about any one dimension.
SEMANTIC_PRESENT_FLOOR = 0.40

# "not_attempted" is ours, not the model's: no interpreter ran at all. It is
# kept distinct from "uninterpretable", which is a judgement the model made.
SEMANTIC_STATUSES = ("interpretable", "partially_interpretable",
                     "uninterpretable", "not_attempted")
SEMANTIC_USABLE = ("interpretable", "partially_interpretable")

# Controlled vocabulary. An open field would drift into a new label per
# exchange and never aggregate; anything outside this set is mapped to "other".
INTERACTION_TYPES = frozenset({
    "affection", "greeting", "farewell", "gratitude", "support", "reassurance",
    "praise", "humor", "shared_enjoyment", "invitation", "inclusion",
    "coordination", "request", "information_sharing", "neutral_conversation",
    "agreement", "disagreement", "complaint", "criticism", "insult",
    "hostility", "threat", "rejection", "withdrawal", "repair", "apology",
    "comfort", "other", "uninterpretable",
})
# Where a complaint points. A complaint is tension whoever it is about, but a
# grievance against the person in front of you is a different thing from a
# child venting about a bus driver to a parent who advises him, and a family
# reading has to tell the two apart.
COMPLAINT_TARGETS = ("someone_present", "family_absent", "outsider", "circumstance")
# Types that count as a repair move when they follow tension in one episode.
REPAIR_TYPES = frozenset({"repair", "apology", "reassurance", "comfort"})

# A label implies a floor under the dimension it names.
#
# Measured on the 40 labelled dialogues: the interpreter chose the type
# `rejection` correctly on every rejection scenario, described it correctly in
# its evidence -- "Ilyas rejected Voula's requests to talk" -- and then scored
# the withdrawal dimension 0.0 on four of five. The closed vocabulary is the
# reliable half of the reply and the continuous score is not, so where a type
# names a dimension outright the score is not allowed below the floor.
#
# Deliberately limited to near-tautological pairs. `humor` does not imply joy
# and `coordination` does not imply inclusion: those are inferences, and
# encoding an inference here would manufacture evidence rather than repair a
# contradiction.
TYPE_DIMENSION_FLOOR: dict[str, dict[str, float]] = {
    "rejection": {"withdrawal": 0.50},
    "withdrawal": {"withdrawal": 0.50},
    "insult": {"tension": 0.60},
    "hostility": {"tension": 0.60},
    "threat": {"tension": 0.70},
    "affection": {"warmth": 0.60},
}


@dataclass
class SemanticInteraction:
    """One interpreted exchange. Evidence about meaning, never a diagnosis."""
    interaction_id: str
    start: str | None
    end: str | None
    room: str | None
    participants: list[str] = field(default_factory=list)
    target: str | None = None
    semantic_status: str = "not_attempted"
    semantic_confidence: float | None = None
    interaction_types: list[str] = field(default_factory=list)
    # Each complaint: {"by": speaker, "about": one of COMPLAINT_TARGETS,
    # "who": the resident it names, or None}.
    complaints: list[dict[str, Any]] = field(default_factory=list)
    # None means "no evidence either way" and 0.0 means "evidence of absence".
    # Collapsing the two would turn every unintelligible clip into a cold one.
    relational_dimensions: dict[str, float | None] = field(default_factory=dict)
    evidence: str | None = None
    reason: str | None = None
    source_event_ids: list[Any] = field(default_factory=list)
    turn_count: int = 0
    word_count: int = 0
    context_hash: str = ""
    uncertainty: list[str] = field(default_factory=list)


def _null_dimensions() -> dict[str, None]:
    return {name: None for name in RELATIONAL_DIMENSIONS}


def cluster_speech_turns(level0: list[Level0Observation],
                         cfg: dict[str, Any]) -> list[list[_SpeechTurn]]:
    """Group turns into exchanges: same room, within the conversation gap.

    Extracted so L1.5 and L3 share ONE definition of what an exchange is. It
    depends only on Level 0, which is what lets L1.5 sit below L2 without
    reaching upward for its window.

    Same room is still required, and deliberately. When several sentinels
    hear one conversation it splits by room -- 38 of the 40 takes recorded on
    2026-09-10 did -- and joining rooms looks like the fix. It was measured
    and left out: on ordinary days 4,904 pairs of exchanges in different
    rooms fell within the conversation gap and only 114 shared a line, so
    joining on time would glue a television in one room to a conversation in
    another, while joining only where a line is shared rebuilt 3 takes of
    40. What is done instead is to keep each utterance once, before any of
    this (merge_cross_sentinel_turns).

    A turn joins the most recent cluster IN ITS OWN ROOM, not simply the last
    cluster made. Comparing against the last cluster meant that one turn from
    another room ended the conversation: on 2026-09-15 a five-second turn
    labelled Entrance at 18:36:18 cut a German lesson in Bedroom_2 into two
    exchanges two seconds apart, and over 15-19 September 81 pairs of
    consecutive same-room exchanges began within five seconds of the previous
    one ending.

    The cost of that was not cosmetic. It left a median exchange of 22 words,
    and exchanges that short are the ones the interpreter cannot read: measured
    against a listener on 99 exchanges, agreement on tension was kappa 0.16 on
    the shortest third and 0.43 on exchanges of 57 words or more. Letting
    another room's turn pass through raises the median to 50 words and takes
    the share above 57 words from 13% to 46%, without touching the gap.
    """
    gap = cfg["thresholds"]["conversation_gap_seconds"]
    clusters: list[list[_SpeechTurn]] = []
    for turn in _collect_turns(level0, cfg):
        placed = False
        for cluster in reversed(clusters):
            if cluster[-1].room != turn.room:
                continue
            # The most recent cluster in this room, and the only one this turn
            # could belong to; if it is too old, the turn starts a new one.
            if (turn.start - cluster[-1].start).total_seconds() <= gap:
                cluster.append(turn)
                placed = True
            break
        if not placed:
            clusters.append([turn])
    return clusters


def _turn_words(turn: _SpeechTurn) -> int:
    return len((turn.text or "").split())


# Labels that say nothing an interpreter can use next to speech: the speech
# classes themselves, room ambience, and silence. Everything else is a real
# event in the room and is shown.
_NON_INFORMATIVE_SOUNDS = {
    "speech", "conversation", "silence", "none",
    "male speech, man speaking", "female speech, woman speaking",
    "child speech, kid speaking", "narration, monologue", "whispering",
    "inside, small room", "inside, large room or hall",
}


def nearby_sounds(level0: list[Level0Observation], window: dict[str, Any],
                  cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Non-speech events in the same room, around an exchange.

    A glass breaking a second before "go ahead, smash everything" is what makes
    that sentence legible; without it the words read as a joke. The sounds are
    returned with their times so they can be placed in sequence rather than
    listed, because a crash BEFORE a remark and one AFTER it mean different
    things.
    """
    thresholds = cfg["thresholds"]
    margin = timedelta(seconds=float(thresholds.get("semantic_sound_margin_seconds", 15)))
    floor = float(thresholds.get("semantic_sound_min_probability", 0.15))
    limit = int(thresholds.get("semantic_sound_max", 6))

    low, high = window["start"] - margin, window["end"] + margin
    found: list[dict[str, Any]] = []
    for obs in level0:
        when = parse_timestamp(obs.timestamp)
        if when is None or obs.room != window["room"]:
            continue
        if not (low <= when <= high):
            continue
        for label in obs.audio_labels:
            name = (label.get("label") or "").strip()
            if name.lower() in _NON_INFORMATIVE_SOUNDS:
                continue
            if (label.get("probability") or 0.0) < floor:
                continue
            found.append({"time": when, "label": name,
                          "confidence": label.get("probability")})
    # Strongest first when trimming, then back into time order: a cap that cut
    # chronologically would drop the loudest event for an earlier faint one.
    found.sort(key=lambda s: -(s["confidence"] or 0))
    found = found[:limit]
    found.sort(key=lambda s: s["time"])
    return found


def _named_speakers(cluster: list[_SpeechTurn], members: set[str],
                    min_share: float) -> tuple[list[str], list[str]]:
    """(named, quiet): who this exchange may be attributed to, and who it may not.

    Taking the set of everyone diarisation named anywhere in the cluster let a
    single misattributed clip put a person in the room for a whole
    conversation. Measured against a listener on 94 exchanges, that union
    matched who was actually present on 32% of them, and named Rafael in
    conversations he was not in more than half the times it named him.

    A resident must hold a SHARE of the exchange's turns rather than a count of
    them, because a count punishes short exchanges and lets long ones through:
    a single turn is 100% of a one-turn window and 2% of a fifty-turn one, and
    it is only the second that is evidence of nothing. Measured on the same 94,
    per-speaker precision and exact-set match against the listener:

        rule                     exact   precision   recall   Rafael
        union (previous)           32%         69%      98%      43%
        at least 2 turns           38%         72%      92%      47%
        >= 10% of the turns        56%         79%      97%      57%
        >= 15% of the turns        59%         81%      92%      61%
        >= 20% of the turns        60%         86%      88%      70%

    0.15 is the default rather than 0.20 because the three share-based rows are
    within noise of each other at n=94 and the threshold was chosen on the same
    exchanges it is scored on; the middle of a flat region survives a new
    sample better than its argmax. Set semantic_min_speaker_turn_share to move
    it.

    Nobody is silently discarded: a resident who spoke but fell under the bar
    is returned as `quiet`, so an exchange can still show that they were heard
    without letting a pair claim be built on them.
    """
    counts = Counter(t.speaker for t in cluster
                     if t.speaker and t.speaker in members)
    total = sum(counts.values())
    if not total:
        return [], []
    named = sorted(s for s, n in counts.items() if n / total >= min_share)
    quiet = sorted(s for s in counts if s not in named)
    return named, quiet


def build_semantic_windows(level0: list[Level0Observation],
                           cfg: dict[str, Any],
                           cues: dict[str, list] | None = None
                           ) -> list[dict[str, Any]]:
    """Exchanges worth interpreting, each with its conversational context.

    The gate is deliberately about intelligibility rather than about who spoke:
    a single person's clear sentence is interpretable evidence, while a
    two-speaker exchange of three garbled words is not. Speaker identity
    governs whether PAIR-level evidence may be drawn, which is a separate
    decision made downstream.
    """
    thresholds = cfg["thresholds"]
    min_words = int(thresholds.get("semantic_min_words", 8))
    min_words_exchange = int(thresholds.get("semantic_min_words_exchange", 4))
    min_turns_exchange = int(thresholds.get("semantic_min_turns_for_exchange", 2))
    min_named = int(thresholds.get("semantic_min_named_speakers", 0))
    context_turns = int(thresholds.get("semantic_context_turns", 3))
    min_share = float(thresholds.get("semantic_min_speaker_turn_share", 0.15))
    members = set(cfg.get("household_members") or {})

    windows: list[dict[str, Any]] = []
    clusters = cluster_speech_turns(level0, cfg)
    for index, cluster in enumerate(clusters):
        spoken = [t for t in cluster if (t.text or "").strip()]
        words = sum(_turn_words(t) for t in spoken)
        named, quiet = _named_speakers(cluster, members, min_share)
        # Either a substantial single stretch of speech, or a real exchange:
        # two or more turns, from two or more voices, clearing the lower bar.
        voices = {t.speaker for t in spoken if t.speaker}
        is_exchange = (len(spoken) >= min_turns_exchange and len(voices) >= 2
                       and words >= min_words_exchange)
        if not (words >= min_words or is_exchange):
            continue
        if len(named) < min_named:
            continue

        # Neighbouring clusters as context only. They are shown to the model so
        # a reply or a follow-up can be read, but they are never attributed to
        # this exchange and never counted in its evidence.
        before = clusters[index - 1][-context_turns:] if index else []
        after = clusters[index + 1][:context_turns] if index + 1 < len(clusters) else []

        start = cluster[0].start
        ends = [t.end for t in cluster if t.end] + [t.start for t in cluster]
        windows.append({
            "cluster_index": index,
            "start": start,
            "end": max(ends),
            "room": cluster[0].room,
            "turns": cluster,
            "context_before": [t for t in before if (t.text or "").strip()],
            "context_after": [t for t in after if (t.text or "").strip()],
            "named_speakers": named,
            # Residents who spoke too little of this exchange to be attributed
            # it. Kept so the fact they were heard is not lost with the claim.
            "quiet_speakers": quiet,
            "distinct_speakers": sorted({t.speaker for t in cluster if t.speaker}),
            "word_count": words,
            "source_event_ids": sorted({t.event_id for t in cluster}, key=str),
        })
    # Attached after the windows exist so the lookup runs once per surviving
    # exchange rather than once per candidate cluster.
    for window in windows:
        window["sounds"] = nearby_sounds(level0, window, cfg)

    # Vocal events -- laughter, shouting, crying, singing -- join the same
    # stream. They are INJECTED rather than computed here: detecting them needs
    # CLAP, torch and the audio files, and this module is imported by graphrag
    # and by the CLI precisely because it depends on none of those. `cues` is
    # whatever ast_src/vocal_cues.py produced, keyed by source event id.
    if cues:
        for window in windows:
            extra = []
            for event_id in window["source_event_ids"]:
                extra.extend(cues.get(str(event_id)) or ())
            if extra:
                merged = list(window["sounds"]) + list(extra)
                merged.sort(key=lambda s: s["time"])
                window["sounds"] = merged
    return windows


def _turn_payload(turn: _SpeechTurn,
                  named: set[str] | None = None) -> dict[str, Any]:
    """One turn as the interpreter sees it. No audio, no file paths, no emotion label.

    The audio emotion classifier's label used to travel with every turn as
    context the model might weigh. The prompt called it weak evidence and the
    interpreter leaned on it anyway: on 14-09-2026 the classifier said "happy"
    for 65 of the 68 lines of a discussion in which a boy vented about a bus
    driver, and asked three times with the labels the interpreter scored joy
    0.75 each time; without them, 0.5, 0.5 and 0.3.

    `named` is the set of residents this exchange may be attributed to, from
    _named_speakers. A resident who did not clear that bar is shown as
    "unidentified" here too. Filtering the window's named_speakers while still
    printing the name on the line would say two different things about who was
    there, and the model believes the line: with named_speakers filtered and
    the lines left alone, its `participants` still matched the listener on only
    43% of exchanges against the 59% the filtered list itself reached. Someone
    spoke; which resident it was is what is not established.
    """
    speaker = turn.speaker
    if named is not None and speaker not in named:
        speaker = None
    return {
        "time": turn.start.strftime("%H:%M:%S"),
        "speaker": speaker or "unidentified",
        "text": (turn.text or "").strip(),
    }


def repeated_lines(turns, speaker_of=lambda t: t.speaker, text_of=lambda t: t.text,
                   time_of=lambda t: t.start, window_seconds=30.0, min_repeats=3,
                   max_words=4):
    """Short lines one speaker "says" again and again within seconds.

    Speech recognition loops on noise and silence -- "Ευχαριστούμε!" once a
    second for twenty seconds -- and each repeat arrives as a turn of its own,
    so a loop read as speech looks like a flood of thanks. A line of at most
    `max_words` words that one speaker repeats at least `min_repeats` times,
    each within `window_seconds` of the one before, is taken for a loop. The
    same short thing said twice is not.

    Returns ({id of each loop's first turn: how many times it came},
    {ids of the repeats}). The getters let callers pass turns of any shape.
    """
    groups: dict[tuple[Any, str], list[Any]] = defaultdict(list)
    for turn in turns:
        text = re.sub(r"[\W_]+", " ", (text_of(turn) or "").casefold()).strip()
        if text and len(text.split()) <= max_words:
            groups[(speaker_of(turn), text)].append(turn)
    first: dict[int, int] = {}
    repeats: set[int] = set()
    for group in groups.values():
        group.sort(key=time_of)
        run = [group[0]]
        for turn in group[1:] + [None]:
            if turn is not None and \
                    (time_of(turn) - time_of(run[-1])).total_seconds() <= window_seconds:
                run.append(turn)
                continue
            if len(run) >= min_repeats:
                first[id(run[0])] = len(run)
                repeats.update(id(t) for t in run[1:])
            if turn is not None:
                run = [turn]
    return first, repeats


def _turn_payloads(turns: list[_SpeechTurn], named: set[str] | None = None
                   ) -> list[tuple[datetime, dict[str, Any]]]:
    """(start, turn as the interpreter sees it), each transcription loop given once.

    The kept line says how many times it came, so the interpreter is told a
    loop happened rather than left to count greetings.
    """
    first, repeats = repeated_lines(turns)
    entries = []
    for turn in turns:
        if id(turn) in repeats:
            continue
        entry = _turn_payload(turn, named)
        if id(turn) in first:
            entry["repeated"] = first[id(turn)]
        entries.append((turn.start, entry))
    return entries


def window_payload(window: dict[str, Any]) -> dict[str, Any]:
    """The interpreter's input. Deterministic, so it can be hashed and cached.

    Speech and the room's other sounds are merged into ONE time-ordered stream.
    Two lists would lose the ordering, and ordering is most of the information:
    a crash before a remark provokes it, the same crash after it is a reaction.
    """
    named = set(window.get("named_speakers") or ())
    stream: list[tuple[datetime, dict[str, Any]]] = _turn_payloads(
        window["turns"], named or None)
    for sound in window.get("sounds") or []:
        stream.append((sound["time"], {
            "time": sound["time"].strftime("%H:%M:%S"),
            "sound": sound["label"],
            "label_confidence": sound["confidence"],
        }))
    stream.sort(key=lambda item: item[0])
    return {
        "room": window["room"],
        "start": _iso(window["start"]),
        "known_speakers_present": window["named_speakers"],
        "context_before": [entry for _, entry in _turn_payloads(window["context_before"])],
        "exchange": [entry for _, entry in stream],
        "context_after": [entry for _, entry in _turn_payloads(window["context_after"])],
    }


def window_hash(payload: dict[str, Any]) -> str:
    """Stable key for caching, so re-running a day costs no repeat calls."""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _coerce_score(value: Any) -> float | None:
    """A dimension score, or None. Anything unparseable becomes None, never 0."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:                                  # NaN
        return None
    return round(max(0.0, min(1.0, number)), 3)


def normalise_semantic_reply(reply: Any, window: dict[str, Any],
                             cfg: dict[str, Any]) -> SemanticInteraction:
    """Coerce an interpreter reply into a record the pipeline can trust.

    Everything is validated rather than believed: an interpreter that invents a
    speaker, an unknown interaction type or a confident reading of nothing gets
    corrected here, once, instead of at every point that later consumes it.
    """
    members = set(cfg.get("household_members") or {})
    base = SemanticInteraction(
        interaction_id=_stable_id("sem", window["start"].strftime("%Y%m%d"),
                                  window["cluster_index"] + 1),
        start=_iso(window["start"]), end=_iso(window["end"]),
        room=window["room"],
        source_event_ids=list(window["source_event_ids"]),
        turn_count=len(window["turns"]),
        word_count=window["word_count"],
        context_hash=window.get("context_hash", ""),
        relational_dimensions=_null_dimensions(),
        # Known from diarisation whatever the interpreter did, so the record
        # still says who was talking even when nothing read the exchange.
        participants=list(window["named_speakers"]),
    )
    if not isinstance(reply, dict):
        base.semantic_status = "uninterpretable"
        base.reason = "interpreter returned no usable object"
        base.interaction_types = ["uninterpretable"]
        return base

    status = str(reply.get("semantic_status", "") or "").strip().lower()
    if status not in SEMANTIC_STATUSES:
        status = "uninterpretable"
    base.semantic_status = status
    base.semantic_confidence = _coerce_score(reply.get("semantic_confidence"))
    base.evidence = (str(reply.get("evidence") or "").strip() or None)
    base.reason = (str(reply.get("reason") or "").strip() or None)

    raw_types = reply.get("interaction_types") or reply.get("interaction_type") or []
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    types = []
    for item in raw_types:
        name = str(item or "").strip().lower().replace(" ", "_")
        if name in INTERACTION_TYPES and name not in types:
            types.append(name)
        elif name and "other" not in types:
            types.append("other")
    base.interaction_types = types or ["other"]

    # Speakers are never taken from the reply. Diarisation is the only source
    # of identity in this system, so an invented name cannot enter here.
    observed = set(window["distinct_speakers"])
    claimed = reply.get("participants") or []
    if isinstance(claimed, str):
        claimed = [claimed]
    base.participants = sorted({str(p) for p in claimed if str(p) in observed})
    if not base.participants:
        base.participants = list(window["named_speakers"])
    target = str(reply.get("target") or "").strip()
    base.target = target if target in observed and target in members else None

    # Complaints are validated like speakers: the one complaining must have
    # been heard, and a named person must be a resident -- one taking part,
    # when the complaint is said to be about someone present.
    if status in SEMANTIC_USABLE and isinstance(reply.get("complaints"), list):
        for item in reply["complaints"]:
            if not isinstance(item, dict):
                continue
            by = str(item.get("by") or "").strip()
            about = str(item.get("about") or "").strip().lower()
            who = str(item.get("who") or "").strip()
            if by not in observed or about not in COMPLAINT_TARGETS:
                continue
            if about == "someone_present":
                who = who if who in observed and who in members and who != by else None
            elif about == "family_absent":
                who = who if who in members and who != by else None
            else:
                who = None
            entry = {"by": by, "about": about, "who": who}
            if entry not in base.complaints:
                base.complaints.append(entry)
        if base.complaints and "complaint" not in base.interaction_types:
            base.interaction_types.append("complaint")
            base.uncertainty.append("complaint label added to match the complaints listed")

    if status in SEMANTIC_USABLE:
        dims = reply.get("relational_dimensions") or {}
        if isinstance(dims, dict):
            base.relational_dimensions = {
                name: _coerce_score(dims.get(name)) for name in RELATIONAL_DIMENSIONS}
        # Reconcile the two halves of the reply where they contradict. Only
        # ever raises a score, never lowers one, and always says it did.
        for name in base.interaction_types:
            for dimension, floor in (TYPE_DIMENSION_FLOOR.get(name) or {}).items():
                current = base.relational_dimensions.get(dimension)
                if current is not None and current < floor:
                    base.relational_dimensions[dimension] = floor
                    base.uncertainty.append(
                        f"{dimension} raised to {floor} to match the "
                        f"'{name}' label, which the interpreter scored at "
                        f"{current}")
                elif current is None:
                    base.relational_dimensions[dimension] = floor
                    base.uncertainty.append(
                        f"{dimension} set to {floor} from the '{name}' label; "
                        f"the interpreter scored none")
        if all(v is None for v in base.relational_dimensions.values()):
            # Claimed to be interpretable but scored nothing: treat the status
            # as the unreliable half rather than keeping an empty judgement.
            base.semantic_status = "uninterpretable"
            base.complaints = []
            base.reason = base.reason or "no relational dimensions returned"
            base.uncertainty.append("interpreter reported meaning but scored none")
    else:
        # Never any numbers on an abstention -- that is the whole point of it.
        base.relational_dimensions = _null_dimensions()

    if len(base.participants) < 2:
        base.uncertainty.append(
            "fewer than two identified speakers; not pair-level evidence")
    if window["word_count"] < int(
            cfg["thresholds"].get("semantic_low_word_warning", 15)):
        base.uncertainty.append("short exchange; meaning rests on few words")
    return base


def derive_level1_5(level0: list[Level0Observation], cfg: dict[str, Any],
                    interpreter: Callable[[dict[str, Any]], Any] | None = None,
                    cache: dict[str, Any] | None = None,
                    cues: dict[str, list] | None = None
                    ) -> list[SemanticInteraction]:
    """Interpret each candidate exchange, or record that nothing interpreted it.

    `interpreter` takes the window payload and returns a decoded JSON object.
    Given none -- no model configured, no network, a CLI run -- every window is
    still emitted with status "not_attempted" and null dimensions, so the
    absence of interpretation is auditable instead of silently absent.
    """
    results: list[SemanticInteraction] = []
    for window in build_semantic_windows(level0, cfg, cues):
        payload = window_payload(window)
        window["context_hash"] = window_hash(payload)

        if interpreter is None:
            record = normalise_semantic_reply(None, window, cfg)
            record.semantic_status = "not_attempted"
            record.interaction_types = []
            record.reason = "no semantic interpreter configured"
            record.relational_dimensions = _null_dimensions()
            results.append(record)
            continue

        if cache is not None and window["context_hash"] in cache:
            reply = cache[window["context_hash"]]
        else:
            try:
                reply = interpreter(payload)
            except Exception as exc:                      # noqa: BLE001
                # One failed exchange must not cost the run, and must not be
                # recorded as a calm one.
                print(f"WARNING: semantic interpreter failed on "
                      f"{window['source_event_ids']}: {exc}", file=sys.stderr)
                reply = None
            if cache is not None and reply is not None:
                cache[window["context_hash"]] = reply
        results.append(normalise_semantic_reply(reply, window, cfg))
    return results


def summarise_semantics(interactions: list[SemanticInteraction]) -> dict[str, Any] | None:
    """Aggregate interpretations for one episode WITHOUT flattening them.

    A mean alone cannot tell an argument that was repaired from a lukewarm
    conversation, so the ordered sequence is kept beside the averages and the
    tension-then-repair shape is reported explicitly.
    """
    if not interactions:
        return None
    usable = [i for i in interactions if i.semantic_status in SEMANTIC_USABLE]
    summary: dict[str, Any] = {
        # Carried so a rollup can tell two episodes describing the SAME
        # exchange apart from two different exchanges: one cluster produces a
        # speech_episode and a conversation episode, and both hold this
        # summary. Counting it twice would double every evidence total.
        "interaction_ids": [i.interaction_id for i in interactions],
        "interpreted_count": len(usable),
        "uninterpretable_count": sum(
            1 for i in interactions if i.semantic_status == "uninterpretable"),
        "not_attempted_count": sum(
            1 for i in interactions if i.semantic_status == "not_attempted"),
        "interaction_types": sorted({t for i in usable for t in i.interaction_types}),
        "note": "Semantic evidence about observed speech; not a psychological "
                "assessment of anyone.",
    }
    if not usable:
        summary["dimensions"] = {name: None for name in RELATIONAL_DIMENSIONS}
        return summary

    dimensions: dict[str, Any] = {}
    for name in RELATIONAL_DIMENSIONS:
        scored = [(i.relational_dimensions.get(name), i.semantic_confidence or 0.5)
                  for i in usable if i.relational_dimensions.get(name) is not None]
        if not scored:
            dimensions[name] = None
            continue
        weight = sum(w for _, w in scored)
        dimensions[name] = {
            "mean": round(sum(v * w for v, w in scored) / weight, 3) if weight else None,
            # The peak matters on its own: one sharp exchange in a calm hour is
            # a real event, and a mean over the hour would erase it.
            "max": round(max(v for v, _ in scored), 3),
            "n": len(scored),
            # How many exchanges actually showed the thing. A mean answers "how
            # warm was the average exchange", which is not a question anyone
            # asks: it averages an evening together with asking who moved the
            # charger, and the answer is always lukewarm. Measured on 2026-09-22
            # the interpreter's warmth had a median of 0.50 and a mean of 0.39,
            # so no week could light a lamp set at 0.55 however warm it was.
            # Counting is what tracks a listener -- see SEMANTIC_PRESENT in
            # ast_dash/pages/llm_report.py.
            "present": sum(1 for v, _ in scored if v >= SEMANTIC_PRESENT_FLOOR),
        }
    summary["dimensions"] = dimensions

    sequence = [{
        "start": i.start,
        "types": i.interaction_types,
        "tension": i.relational_dimensions.get("tension"),
        "warmth": i.relational_dimensions.get("warmth"),
        "confidence": i.semantic_confidence,
    } for i in usable]
    summary["sequence"] = sequence

    # Complaints counted by where they point. A household's recurring
    # grievances are the thing a long-run reading is asked about, and the mean
    # tension above cannot tell "he complained about his brother" from "he
    # complained about the bus" -- which is the whole distinction the
    # complaints field exists to record.
    lodged = [c for i in usable for c in i.complaints]
    if lodged:
        summary["complaints"] = {
            "count": len(lodged),
            "by_target": dict(Counter(c.get("about") for c in lodged)),
            "by_person": dict(Counter(c.get("by") for c in lodged)),
            "about_person": dict(Counter(c["who"] for c in lodged if c.get("who"))),
        }

    # Repair: a tension peak followed later by a repair move or clear warmth.
    # Reported as a shape, never as a verdict about the relationship.
    high = [n for n, step in enumerate(sequence)
            if (step["tension"] or 0) >= 0.6]
    if high:
        first = high[0]
        after = sequence[first + 1:]
        repaired = any(
            set(step["types"]) & REPAIR_TYPES or (step["warmth"] or 0) >= 0.6
            for step in after)
        summary["tension_peak"] = {
            "at": sequence[first]["start"],
            "tension": sequence[first]["tension"],
            "followed_by_repair": repaired if after else None,
            "note": "Repair does not cancel the earlier tension; both are kept.",
        }
    return summary


# ---------------------------------------------------------------------------
# 6. Level 2 — activity episodes
# ---------------------------------------------------------------------------

@dataclass
class Level2Episode:
    episode_id: str
    start: str | None
    end: str | None
    duration_seconds: float
    room: str | None
    activity: str
    confidence: float
    evidence: list[dict[str, Any]] = field(default_factory=list)
    source_event_ids: list[Any] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    semantic_types: list[str] = field(default_factory=list)


# Types that describe *how* something was observed rather than *what*
# happened; they support episodes but never define one on their own.
SUPPORTING_TYPES = {
    "human_presence_candidate", "human_vocal_sound", "emotion_evidence",
    "identified_speaker", "object_handling_candidate", "faucet_use",
    "movement_candidate", "unknown_activity",
}


def _gap_for(activity: str, cfg: dict[str, Any]) -> int:
    gaps = cfg.get("episode_gaps") or DEFAULT_EPISODE_GAPS
    return int(gaps.get(activity, gaps.get("default", 300)))


def derive_level2(level1: list[Level1Record], cfg: dict[str, Any]) -> list[Level2Episode]:
    """Merge semantic events that are close in time, room and meaning.

    Grouping key is (room, primary semantic type). Supporting evidence
    recorded in the same room within the same window is folded into whichever
    episode it overlaps, so 'breathing then water' becomes one episode.
    """
    # Flatten to (timestamp, room, event, source_id), dropping undated rows.
    flat: list[tuple[datetime, str | None, dict[str, Any], Any]] = []
    for record in level1:
        ts = parse_timestamp(record.timestamp)
        if ts is None:
            continue
        for semantic in record.semantic_events:
            flat.append((ts, record.room, semantic, record.source_event_id))
    flat.sort(key=lambda item: (item[0], str(item[3])))

    # --- pass 1: cluster defining types into raw episodes ------------------
    clusters: dict[tuple[str | None, str], list[list[tuple]]] = defaultdict(list)
    for ts, room, semantic, source_id in flat:
        stype = semantic["type"]
        if stype in SUPPORTING_TYPES:
            continue
        key = (room, stype)
        gap = _gap_for(stype, cfg)
        bucket = clusters[key]
        if bucket and (ts - bucket[-1][-1][0]).total_seconds() <= gap:
            bucket[-1].append((ts, room, semantic, source_id))
        else:
            bucket.append([(ts, room, semantic, source_id)])

    # --- pass 2: build episodes, attaching supporting evidence -------------
    episodes: list[Level2Episode] = []
    counters: dict[str, int] = defaultdict(int)

    for (room, stype), buckets in clusters.items():
        for members in buckets:
            start = members[0][0]
            end = members[-1][0]
            gap = _gap_for(stype, cfg)
            window_start, window_end = start - timedelta(seconds=gap), end + timedelta(seconds=gap)

            supporting = [
                (ts, sem, sid) for ts, r, sem, sid in flat
                if sem["type"] in SUPPORTING_TYPES
                and r == room and window_start <= ts <= window_end
            ]

            present_types = {m[2]["type"] for m in members} | {s[1]["type"] for s in supporting}
            confidences = [m[2].get("confidence") or 0.0 for m in members]
            base_confidence = max(confidences) if confidences else 0.0

            activity = stype
            boost = 0.0
            for rule in COMPOSITE_ACTIVITY_RULES:
                if rule["rooms"] is not None and room not in rule["rooms"]:
                    continue
                if not any(required & present_types for required in rule["requires_any"]):
                    continue
                activity = rule["activity"]
                supported = len(rule["supports"] & present_types)
                boost = rule["boost"] * (1 if supported == 0 else 1.5)
                break

            confidence = round(min(base_confidence + boost, 0.95), 3)

            people = sorted({
                sem.get("person") for _, sem, _ in supporting
                if sem.get("type") == "identified_speaker" and sem.get("person")
            })

            source_ids: list[Any] = []
            for member in members:
                if member[3] not in source_ids:
                    source_ids.append(member[3])
            for _, _, sid in supporting:
                if sid not in source_ids:
                    source_ids.append(sid)

            uncertainty: list[str] = []
            if room is None:
                uncertainty.append("room unknown for these events")
            if base_confidence < 0.30:
                uncertainty.append("derived from low-confidence audio labels")
            if len(members) == 1 and not supporting:
                uncertainty.append("single isolated observation")

            day = start.strftime("%Y%m%d")
            counters[day] += 1
            episode_id = _stable_id("act", day, counters[day])

            evidence = [{
                "timestamp": _iso(ts), "type": sem["type"],
                "confidence": sem.get("confidence"),
                "labels": [e.get("label") for e in sem.get("evidence", [])],
                "role": "defining",
            } for ts, _, sem, _ in members]
            evidence += [{
                "timestamp": _iso(ts), "type": sem["type"],
                "confidence": sem.get("confidence"),
                "labels": [e.get("label") for e in sem.get("evidence", [])],
                "role": "supporting",
            } for ts, sem, _ in supporting]

            episodes.append(Level2Episode(
                episode_id=episode_id,
                start=_iso(start), end=_iso(end),
                duration_seconds=(end - start).total_seconds(),
                room=room, activity=activity, confidence=confidence,
                evidence=evidence, source_event_ids=source_ids,
                people=people, uncertainty=uncertainty,
                semantic_types=sorted(present_types),
            ))

    episodes = _merge_duplicate_episodes(episodes, cfg)

    episodes.sort(key=lambda e: (e.start or "", e.episode_id))
    # Renumber so ids ascend in chronological order.
    renumbered: dict[str, int] = defaultdict(int)
    for episode in episodes:
        day = (episode.start or "")[:10].replace("-", "") or "00000000"
        renumbered[day] += 1
        episode.episode_id = _stable_id("act", day, renumbered[day])
    return episodes


def _merge_duplicate_episodes(episodes: list[Level2Episode],
                              cfg: dict[str, Any]) -> list[Level2Episode]:
    """Fold episodes that describe the same activity in the same room and time.

    Several defining types can collapse onto one composite activity -- both
    `media_activity` and `tv_activity` become `media_use`, both `water_use`
    and `personal_care_candidate` become `personal_care_candidate` -- which
    would otherwise emit one episode per contributing type and double-count
    the same real-world event. Since these counts feed Level 4 and the
    longitudinal baseline, the duplication has to be resolved here.
    """
    grouped: dict[tuple[str | None, str], list[Level2Episode]] = defaultdict(list)
    for episode in episodes:
        grouped[(episode.room, episode.activity)].append(episode)

    merged: list[Level2Episode] = []
    for (room, activity), group in grouped.items():
        group.sort(key=lambda e: e.start or "")
        gap = _gap_for(activity, cfg)
        current: Level2Episode | None = None
        for episode in group:
            start = parse_timestamp(episode.start)
            if current is None:
                current = episode
                continue
            current_end = parse_timestamp(current.end)
            if start and current_end and (start - current_end).total_seconds() <= gap:
                # Same activity, same room, overlapping or adjacent: one event.
                new_end = max(current_end, parse_timestamp(episode.end) or current_end)
                current.end = _iso(new_end)
                current.duration_seconds = (
                    new_end - (parse_timestamp(current.start) or new_end)).total_seconds()
                current.confidence = max(current.confidence, episode.confidence)
                current.evidence.extend(episode.evidence)
                for source_id in episode.source_event_ids:
                    if source_id not in current.source_event_ids:
                        current.source_event_ids.append(source_id)
                current.people = sorted(set(current.people) | set(episode.people))
                current.semantic_types = sorted(
                    set(current.semantic_types) | set(episode.semantic_types))
                current.uncertainty = sorted(set(current.uncertainty) | set(episode.uncertainty))
            else:
                merged.append(current)
                current = episode
        if current is not None:
            merged.append(current)
    return merged


def derive_observation_gaps(level0: list[Level0Observation], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Report gaps as unobserved, never as silence.

    The sentinels are event-triggered: nothing arriving means nothing was
    loud enough to trigger a recording, which is not the same as quiet. These
    intervals are emitted so a later LLM can see where evidence is missing.
    """
    threshold = cfg["thresholds"].get("no_observation_gap_seconds", 1800)
    stamped = [parse_timestamp(o.timestamp) for o in level0]
    stamped = sorted(t for t in stamped if t)
    gaps: list[dict[str, Any]] = []
    for earlier, later in zip(stamped, stamped[1:]):
        seconds = (later - earlier).total_seconds()
        if seconds >= threshold:
            gaps.append({
                "type": "no_observation_interval",
                "start": _iso(earlier), "end": _iso(later),
                "duration_seconds": seconds,
                "note": ("No triggered recordings in this interval. The system is "
                         "event-driven, so this is absence of evidence, not evidence "
                         "of silence."),
            })
    return gaps


# ---------------------------------------------------------------------------
# 7. Level 3 — person and social episodes
# ---------------------------------------------------------------------------

@dataclass
class Level3Episode:
    episode_id: str
    type: str
    start: str | None
    end: str | None
    room: str | None
    participants: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    source_event_ids: list[Any] = field(default_factory=list)
    source_episode_ids: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    uncertainty: list[str] = field(default_factory=list)


@dataclass
class _SpeechTurn:
    """One diarised segment, placed on the absolute clock."""
    event_id: Any
    room: str | None
    speaker: str | None
    start: datetime
    end: datetime | None
    text: str | None
    emotion: dict[str, Any] | None
    # Which sentinel heard it, so that one utterance heard by two sentinels
    # can be recognised as one. See merge_cross_sentinel_turns.
    device: str | None = None


# Speaker values that mean "not identified" rather than naming a resident.
_UNNAMED_SPEAKERS = {None, "", "Unknown", "unidentified"}


def _flat_text(text: str | None) -> str:
    """Lowercase, accents and punctuation removed, spaces collapsed."""
    lowered = unicodedata.normalize("NFD", (text or "").lower())
    stripped = "".join(c for c in lowered if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", stripped)).strip()


def _contained(short: str, long: str) -> float:
    """How much of `short` appears, in order, inside `long` (0-1)."""
    if not short:
        return 0.0
    blocks = difflib.SequenceMatcher(None, short, long,
                                     autojunk=False).get_matching_blocks()
    return sum(block.size for block in blocks) / len(short)


def merge_cross_sentinel_turns(
        turns: list[_SpeechTurn], cfg: dict[str, Any],
) -> tuple[list[_SpeechTurn], list[tuple[_SpeechTurn, _SpeechTurn]]]:
    """One utterance heard by several sentinels, kept once.

    Every sentinel within earshot records the same sentence, and each copy
    used to become a turn of its own. On ordinary days a quarter of the
    exchanges read at L1.5 shared a line with another room's exchange; in
    the 40 takes recorded on 2026-09-10 three sentinels heard every line and
    82 copies came back. A copy is not a second utterance. Counted, it
    doubles turn totals and speaker counts; read, it hands the interpreter
    the same sentence twice.

    Two turns are one utterance when they come from different sentinels,
    overlap in time give or take a few seconds, name the same resident (or
    one names nobody), and the shorter sits inside the longer. Containment
    rather than equality, because sentinels cut speech differently: one
    heard "Πλάκα κάνεις. Διάβασες μόνος σου..." where another heard only the
    second sentence. Very short turns ("Ναι.") must match almost exactly,
    since a two-letter word is contained in nearly anything.

    The speaker condition is not optional. Without it, scored against the
    script of those 40 takes, the rule swallowed seven real lines -- every
    one a short reply, like Rafael's "Ένα λεπτό ακόμα!" absorbed into
    Ilyas's line moments earlier on another sentinel. With it, none: every
    line that any copy matched was still matched.

    Of each group the copy with the most words is kept, since a sentinel
    that heard a line well decodes more of it -- which also leaves the line
    in the room of the sentinel that heard it best. Rooms are otherwise
    untouched: see cluster_speech_turns for why they are not joined.

    Returns (kept, pairs), each pair being (dropped, the copy kept).
    """
    thresholds = cfg.get("thresholds", {})
    slack = float(thresholds.get("cross_sentinel_slack_seconds", 3.0))
    floor = float(thresholds.get("cross_sentinel_min_containment", 0.7))

    order = sorted(turns, key=lambda t: (-len(_flat_text(t.text).split()), t.start))
    kept: list[_SpeechTurn] = []
    pairs: list[tuple[_SpeechTurn, _SpeechTurn]] = []
    for turn in order:
        text = _flat_text(turn.text)
        match = None
        if text and turn.device:
            turn_end = turn.end or turn.start
            for other in kept:
                if not other.device or other.device == turn.device:
                    continue
                if (turn.speaker not in _UNNAMED_SPEAKERS
                        and other.speaker not in _UNNAMED_SPEAKERS
                        and turn.speaker != other.speaker):
                    continue
                if ((turn.start - (other.end or other.start)).total_seconds() > slack
                        or (other.start - turn_end).total_seconds() > slack):
                    continue
                other_text = _flat_text(other.text)
                if len(text) < 8:
                    same = difflib.SequenceMatcher(None, text, other_text).ratio() >= 0.8
                else:
                    same = _contained(text, other_text) >= floor
                if same:
                    match = other
                    break
        if match is None:
            kept.append(turn)
        else:
            pairs.append((turn, match))
    kept.sort(key=lambda t: (t.start, str(t.event_id)))
    return kept, pairs


def _collect_turns(level0: list[Level0Observation],
                   cfg: dict[str, Any] | None = None,
                   merge: bool = True) -> list[_SpeechTurn]:
    """Absolute-time speech turns from diarisation offsets.

    The offsets are relative to the start of each recording, so adding them to
    the event timestamp gives real turn timing -- which means turn counts and
    response gaps are measured, not guessed.

    Given a config, an utterance several sentinels heard is kept once
    (merge_cross_sentinel_turns) unless the config switches that off. Both
    L1.5 and L3 read turns through here, so they agree on it. merge=False
    returns every copy, for tools that need to see them.
    """
    turns: list[_SpeechTurn] = []
    for obs in level0:
        base = parse_timestamp(obs.timestamp)
        if base is None:
            continue
        for segment in obs.speech.get("segments") or []:
            offset_start = segment.get("offset_start") or 0.0
            offset_end = segment.get("offset_end")
            start = base + timedelta(seconds=offset_start)
            end = base + timedelta(seconds=offset_end) if offset_end is not None else None
            turns.append(_SpeechTurn(
                event_id=obs.event_id, room=obs.room, speaker=segment.get("speaker"),
                start=start, end=end, text=segment.get("text"), emotion=obs.emotion,
                device=obs.device,
            ))
    turns.sort(key=lambda t: (t.start, str(t.event_id)))
    if (cfg is not None and merge
            and cfg.get("thresholds", {}).get("cross_sentinel_merge", True)):
        turns, _pairs = merge_cross_sentinel_turns(turns, cfg)
    return turns


def _aggregate_emotions(emotions: Iterable[dict[str, Any] | None]) -> dict[str, Any]:
    """Confidence-weighted distribution. Descriptive only -- never a diagnosis."""
    weights: dict[str, float] = defaultdict(float)
    count = 0
    for emotion in emotions:
        if not emotion or not emotion.get("label"):
            continue
        weights[emotion["label"]] += float(emotion.get("confidence") or 0.5)
        count += 1
    if not weights:
        return {"dominant": None, "distribution": {}, "evidence_count": 0}
    total = sum(weights.values())
    distribution = {k: round(v / total, 3) for k, v in sorted(weights.items(), key=lambda kv: -kv[1])}
    return {
        "dominant": next(iter(distribution)),
        "distribution": distribution,
        "evidence_count": count,
        "note": "Observed emotion-model output only; not a psychological assessment.",
    }


def _semantics_for_span(level1_5: list[SemanticInteraction] | None,
                        start: datetime, end: datetime,
                        room: str | None) -> list[SemanticInteraction]:
    """L1.5 records whose exchange falls inside this episode, in time order.

    Matched on time and room rather than on identity, because L1.5 windows and
    L3 clusters are built from the same shared clustering and therefore line
    up; the containment test is what keeps that an assumption the code checks
    rather than one it relies on.
    """
    if not level1_5:
        return []
    inside = []
    for item in level1_5:
        when = parse_timestamp(item.start)
        if when is None or item.room != room:
            continue
        if start <= when <= end:
            inside.append(item)
    return sorted(inside, key=lambda i: i.start or "")


def derive_level3(level0: list[Level0Observation],
                  level2: list[Level2Episode],
                  cfg: dict[str, Any],
                  level1_5: list[SemanticInteraction] | None = None) -> list[Level3Episode]:
    """Who was involved, and -- when L1.5 ran -- how the interaction went.

    `level1_5` is optional and additive. Every existing episode type, rule and
    threshold is unchanged: what a reciprocal conversation IS still means two
    or more known speakers in one cluster. Semantic evidence only rides along
    with it.
    """
    members = set(cfg.get("household_members") or {})
    gap = cfg["thresholds"]["conversation_gap_seconds"]
    copresence_gap = cfg["thresholds"]["copresence_gap_seconds"]
    min_speaker_conf = cfg["thresholds"]["min_speaker_confidence"]

    turns = _collect_turns(level0, cfg)
    episodes: list[Level3Episode] = []
    counters: dict[str, int] = defaultdict(int)

    def new_id(prefix: str, when: datetime) -> str:
        day = when.strftime("%Y%m%d")
        counters[day] += 1
        return _stable_id(prefix, day, counters[day])

    # --- person presence evidence -----------------------------------------
    for obs in level0:
        base = parse_timestamp(obs.timestamp)
        if base is None:
            continue
        for speaker in obs.speech.get("speakers") or []:
            known = speaker in members
            episodes.append(Level3Episode(
                episode_id=new_id("pres", base),
                type="person_presence_evidence",
                start=_iso(base), end=_iso(base), room=obs.room,
                participants=[speaker],
                confidence=0.80 if known else 0.50,
                evidence=[{"basis": "speaker_recognition", "speaker": speaker,
                           "room": obs.room}],
                source_event_ids=[obs.event_id],
                details={"known_household_member": known},
                uncertainty=([] if known else ["speaker not in configured household"])
                + ["evidence of presence at this moment only; continuous presence not implied"],
            ))

    # --- speech episodes ---------------------------------------------------
    clusters: list[list[_SpeechTurn]] = []
    for turn in turns:
        if clusters and (turn.start - clusters[-1][-1].start).total_seconds() <= gap \
                and turn.room == clusters[-1][-1].room:
            clusters[-1].append(turn)
        else:
            clusters.append([turn])

    for cluster in clusters:
        start = cluster[0].start
        ends = [t.end for t in cluster if t.end] + [t.start for t in cluster]
        end = max(ends)
        named = [t.speaker for t in cluster if t.speaker]
        distinct = sorted(set(named))
        known = sorted(set(distinct) & members)
        counts = Counter(named)

        # Speaker changes measured on the ordered turn sequence.
        sequence = [t.speaker for t in cluster if t.speaker]
        changes = sum(1 for a, b in zip(sequence, sequence[1:]) if a != b)

        transcripts = [t.text for t in cluster if t.text]
        emotions = _aggregate_emotions(t.emotion for t in cluster)

        uncertainty: list[str] = []
        if not distinct:
            uncertainty.append("no speaker could be attributed to these segments")
        if len(distinct) == 1:
            uncertainty.append(
                "one identified speaker only; a second party may be present but "
                "unrecognised, or the speech may be directed at a phone or television")

        base_type = "speech_episode"
        confidence = 0.70 if known else 0.50
        # What this exchange meant, when L1.5 interpreted it. Kept beside the
        # emotion summary rather than replacing it: they are different kinds of
        # evidence and the emotion model is the weaker one.
        semantics = summarise_semantics(
            _semantics_for_span(level1_5, start, end, cluster[0].room))
        details = {
            "turn_count": len(cluster),
            "speaker_turn_counts": dict(counts),
            "speaker_changes": changes,
            "transcript_excerpts": transcripts[:10],
            "emotion_summary": emotions,
        }
        if semantics:
            details["semantic_interaction"] = semantics
        episodes.append(Level3Episode(
            episode_id=new_id("spk", start),
            type=base_type,
            start=_iso(start), end=_iso(end), room=cluster[0].room,
            participants=known or distinct,
            confidence=confidence,
            evidence=[{"turns": len(cluster), "identified_turns": len(named)}],
            source_event_ids=sorted({t.event_id for t in cluster}, key=str),
            details=details,
            uncertainty=uncertainty,
        ))

        # --- conversation / one-sided speech -------------------------------
        if len(known) >= 2:
            per_person = {p: counts.get(p, 0) for p in known}
            values = sorted(per_person.values())
            balance = round(values[0] / values[-1], 3) if values[-1] else 0.0

            # Response gaps between consecutive turns by different speakers.
            response_gaps = [
                round((b.start - (a.end or a.start)).total_seconds(), 2)
                for a, b in zip(cluster, cluster[1:])
                if a.speaker and b.speaker and a.speaker != b.speaker
            ]
            episodes.append(Level3Episode(
                episode_id=new_id("soc", start),
                type="reciprocal_conversation_candidate",
                start=_iso(start), end=_iso(end), room=cluster[0].room,
                participants=known,
                confidence=round(min(0.60 + 0.05 * changes, 0.90), 3),
                evidence=[{"basis": "multiple identified speakers alternating",
                           "speaker_changes": changes}],
                source_event_ids=sorted({t.event_id for t in cluster}, key=str),
                details={
                    "estimated_turns": per_person,
                    "turn_balance": balance,
                    "speaker_changes": changes,
                    "response_gaps_seconds": response_gaps,
                    "emotion_summary": emotions,
                    # The distinction this whole level previously could not
                    # make: that these two people interacted says nothing about
                    # how. Present only when L1.5 read the exchange.
                    **({"semantic_interaction": semantics} if semantics else {}),
                },
                uncertainty=["turn counts derive from diarisation segments, not from "
                             "continuous voice-activity timing"],
            ))
        elif len(known) == 1:
            episodes.append(Level3Episode(
                episode_id=new_id("soc", start),
                type="single_person_speech_episode",
                start=_iso(start), end=_iso(end), room=cluster[0].room,
                participants=known,
                confidence=0.60,
                evidence=[{"basis": "one identified speaker, no second party identified"}],
                source_event_ids=sorted({t.event_id for t in cluster}, key=str),
                details={"turn_count": len(cluster), "emotion_summary": emotions,
                         **({"semantic_interaction": semantics} if semantics else {})},
                uncertainty=["does not imply the person was alone"],
            ))

    # --- co-presence -------------------------------------------------------
    presence = [e for e in episodes if e.type == "person_presence_evidence"
                and e.details.get("known_household_member")]
    by_room: dict[str | None, list[Level3Episode]] = defaultdict(list)
    for item in presence:
        by_room[item.room].append(item)

    # Pairs are collected into windows first, so a run of alternating speech
    # yields one co-presence episode rather than one per pair of records.
    windows: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = defaultdict(list)
    for room, items in by_room.items():
        if room is None:
            continue  # co-presence without a room is not evidence
        items.sort(key=lambda e: e.start or "")
        for first, second in ((a, b) for i, a in enumerate(items) for b in items[i + 1:]):
            person_a = first.participants[0]
            person_b = second.participants[0]
            if person_a == person_b:
                continue
            t_a = parse_timestamp(first.start)
            t_b = parse_timestamp(second.start)
            if not t_a or not t_b or abs((t_b - t_a).total_seconds()) > copresence_gap:
                continue
            pair = tuple(sorted({person_a, person_b}))
            windows[(pair, room)].append({
                "start": min(t_a, t_b), "end": max(t_a, t_b),
                "sources": set(first.source_event_ids) | set(second.source_event_ids),
            })

    for (pair, room), spans in windows.items():
        spans.sort(key=lambda s: s["start"])
        merged_spans: list[dict[str, Any]] = []
        for span in spans:
            if merged_spans and (span["start"] - merged_spans[-1]["end"]).total_seconds() <= copresence_gap:
                previous = merged_spans[-1]
                previous["end"] = max(previous["end"], span["end"])
                previous["sources"] |= span["sources"]
            else:
                merged_spans.append(dict(span))
        for span in merged_spans:
            episodes.append(Level3Episode(
                episode_id=new_id("cop", span["start"]),
                type="same_room_copresence_candidate",
                start=_iso(span["start"]), end=_iso(span["end"]), room=room,
                participants=list(pair), confidence=0.65,
                evidence=[{"basis": f"{p} speaker evidence in {room}"} for p in pair],
                source_event_ids=sorted(span["sources"], key=str),
                details={"span_seconds": (span["end"] - span["start"]).total_seconds()},
                uncertainty=["inferred from speech evidence close in time and room; "
                             "not direct observation of both people together"],
            ))

    # --- shared activities -------------------------------------------------
    for episode in level2:
        if episode.activity not in {"media_use", "tv_activity", "media_activity"}:
            continue
        start = parse_timestamp(episode.start)
        end = parse_timestamp(episode.end) or start
        if not start:
            continue
        window_people = sorted({
            p for e in presence
            for p in e.participants
            if e.room == episode.room
            and (ts := parse_timestamp(e.start)) is not None
            and start - timedelta(seconds=copresence_gap) <= ts <= end + timedelta(seconds=copresence_gap)
        })
        if len(window_people) >= 2:
            etype, confidence = "shared_media_candidate", 0.65
            uncertainty = ["people identified near this media activity; joint attention not observed"]
        elif window_people:
            etype, confidence = "media_use_with_unidentified_viewers", 0.50
            uncertainty = ["only one person identified; others may be present but unrecognised"]
        else:
            etype, confidence = "media_use", 0.50
            uncertainty = ["no person identified; audience unknown"]
        episodes.append(Level3Episode(
            episode_id=new_id("shr", start),
            type=etype, start=episode.start, end=episode.end, room=episode.room,
            participants=window_people, confidence=confidence,
            evidence=[{"basis": "level2 media episode", "episode_id": episode.episode_id}],
            source_event_ids=list(episode.source_event_ids),
            source_episode_ids=[episode.episode_id],
            details={}, uncertainty=uncertainty,
        ))

    episodes.sort(key=lambda e: (e.start or "", e.episode_id))
    return episodes


# ---------------------------------------------------------------------------
# 8. Level 4 — daily dynamics
# ---------------------------------------------------------------------------

def _day_of(iso_ts: str | None) -> str | None:
    return iso_ts[:10] if iso_ts else None


def _semantic_rollup(episodes: list[Level3Episode]) -> dict[str, Any] | None:
    """Roll episode-level semantics up to a day or a pair.

    Averages the per-episode means, weighted by how many exchanges each rests
    on, so an episode carrying one interpretation does not outweigh one
    carrying ten. Keeps `max` unweighted: a single sharp exchange is a real
    event and must survive aggregation intact.
    """
    # One exchange reaches here through several episodes -- a speech_episode
    # and a conversation episode describe the same turns -- so summaries are
    # deduplicated on the interactions they cover before anything is counted.
    carrying, seen = [], set()
    for episode in episodes:
        summary = (episode.details or {}).get("semantic_interaction")
        if not summary:
            continue
        key = tuple(summary.get("interaction_ids") or ())
        if key and key in seen:
            continue
        seen.add(key)
        carrying.append(summary)
    if not carrying:
        return None
    interpreted = sum(s.get("interpreted_count", 0) for s in carrying)
    rollup: dict[str, Any] = {
        "episodes_with_semantics": len(carrying),
        "interpreted_exchanges": interpreted,
        "uninterpretable_exchanges": sum(
            s.get("uninterpretable_count", 0) for s in carrying),
        "not_attempted_exchanges": sum(
            s.get("not_attempted_count", 0) for s in carrying),
        "interaction_types": dict(sorted(Counter(
            t for s in carrying for t in s.get("interaction_types") or []).items())),
        "note": "Semantic evidence about observed speech; not a psychological "
                "assessment of anyone.",
    }
    # Complaints travel up with their direction intact. Summed rather than
    # averaged: "they complained about each other four times today" is the
    # figure a long record is read for, and a mean over exchanges would hide it.
    lodged = [s.get("complaints") or {} for s in carrying]
    if any(lodged):
        def merged(field: str) -> dict[str, int]:
            total: Counter = Counter()
            for one in lodged:
                total.update(one.get(field) or {})
            return dict(sorted(total.items()))

        rollup["complaints"] = {
            "count": sum(one.get("count", 0) for one in lodged),
            "by_target": merged("by_target"),
            "by_person": merged("by_person"),
            "about_person": merged("about_person"),
        }
    dimensions: dict[str, Any] = {}
    for name in RELATIONAL_DIMENSIONS:
        scored = [(s["dimensions"][name], s.get("interpreted_count", 1))
                  for s in carrying
                  if (s.get("dimensions") or {}).get(name)]
        if not scored:
            dimensions[name] = None
            continue
        weight = sum(w for _, w in scored) or 1
        dimensions[name] = {
            "mean": round(sum((d["mean"] or 0) * w for d, w in scored) / weight, 3),
            "max": round(max(d["max"] for d, _ in scored), 3),
            "n": sum(d["n"] for d, _ in scored),
            # Counts add; older summaries written before `present` existed are
            # read as zero rather than crashing the rollup.
            "present": sum(d.get("present", 0) for d, _ in scored),
        }
    rollup["dimensions"] = dimensions

    peaks = [s["tension_peak"] for s in carrying if s.get("tension_peak")]
    if peaks:
        repaired = [p for p in peaks if p.get("followed_by_repair")]
        rollup["tension_peaks"] = {
            "count": len(peaks),
            "followed_by_repair": len(repaired),
            "highest": round(max(p["tension"] or 0 for p in peaks), 3),
            "note": "Repair is recorded alongside the tension it followed, "
                    "never as cancelling it.",
        }
    return rollup


def derive_level4(level0: list[Level0Observation],
                  level2: list[Level2Episode],
                  level3: list[Level3Episode],
                  gaps: list[dict[str, Any]],
                  cfg: dict[str, Any],
                  historical: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    members = list(cfg.get("household_members") or {})
    days = sorted({d for d in (
        [_day_of(o.timestamp) for o in level0]
        + [_day_of(e.start) for e in level2]
        + [_day_of(e.start) for e in level3]) if d})

    summaries: list[dict[str, Any]] = []
    for day in days:
        obs_day = [o for o in level0 if _day_of(o.timestamp) == day]
        l2_day = [e for e in level2 if _day_of(e.start) == day]
        l3_day = [e for e in level3 if _day_of(e.start) == day]
        gaps_day = [g for g in gaps if _day_of(g["start"]) == day]

        # -- household activity --------------------------------------------
        activity_counts = Counter(e.activity for e in l2_day)
        # Reciprocity: of the speech the day evidenced, how much of it was
        # people answering one another rather than one person talking.
        #
        # This replaces `connection` as a relational dimension. Measured
        # against a listener on 91 exchanges, the per-exchange connection score
        # carried no information -- weighted kappa -0.09, lit on 98% of
        # exchanges, and the listener scored it 3 on 84 of 91. It could hardly
        # do otherwise: an exchange reaches the interpreter BECAUSE two
        # residents spoke in it, so asking whether they were engaged measures
        # the selection rule rather than the household. Counted over a day the
        # same idea has somewhere to vary.
        reciprocal = [e for e in l3_day
                      if e.type == "reciprocal_conversation_candidate"]
        alone = [e for e in l3_day if e.type == "single_person_speech_episode"]
        spoken = len(reciprocal) + len(alone)
        household = {
            "activity_counts": dict(sorted(activity_counts.items())),
            "activity_episode_count": len(l2_day),
            "rooms_with_activity": dict(sorted(Counter(
                e.room or "unknown" for e in l2_day).items())),
            "reciprocity": {
                "reciprocal_episodes": len(reciprocal),
                "single_speaker_episodes": len(alone),
                "rate": round(len(reciprocal) / spoken, 3) if spoken else None,
                "participants": dict(sorted(Counter(
                    p for e in reciprocal for p in e.participants).items())),
                "note": ("Share of the day's speech episodes in which people "
                         "answered one another. Episode counts, not minutes: "
                         "the recorder is event-triggered."),
            },
            "note": ("Episode durations span detected evidence only; the system is "
                     "event-triggered and does not monitor continuously."),
        }

        # -- individual patterns -------------------------------------------
        individual: dict[str, Any] = {}
        for person in members:
            presence = [e for e in l3_day if e.type == "person_presence_evidence"
                        and person in e.participants]
            speech = [e for e in l3_day if e.type == "speech_episode" and person in e.participants]
            social = [e for e in l3_day if person in e.participants
                      and e.type in {"reciprocal_conversation_candidate",
                                     "single_person_speech_episode",
                                     "same_room_copresence_candidate",
                                     "shared_media_candidate"}]
            if not (presence or speech or social):
                continue  # only report people the day actually evidences
            emotions = _aggregate_emotions(
                o.emotion for o in obs_day if person in (o.speech.get("speakers") or []))
            individual[person] = {
                "speech_episode_count": len(speech),
                "presence_evidence_count": len(presence),
                "recognized_rooms": dict(sorted(Counter(
                    e.room or "unknown" for e in presence).items())),
                "emotion_summary": emotions,
                "social_participation": dict(sorted(Counter(e.type for e in social).items())),
                **({"semantic_summary": rollup}
                   if (rollup := _semantic_rollup(speech + social)) else {}),
            }

        # -- pair dynamics --------------------------------------------------
        pair_dynamics: dict[str, Any] = {}
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair = sorted([a, b])
                key = "__".join(pair)
                shared = [e for e in l3_day if set(pair) <= set(e.participants)]
                if not shared:
                    continue
                conversations = [e for e in shared if e.type == "reciprocal_conversation_candidate"]
                copresence = [e for e in shared if e.type == "same_room_copresence_candidate"]
                shared_act = [e for e in shared if e.type.startswith("shared_")]
                balances = [e.details.get("turn_balance") for e in conversations
                            if e.details.get("turn_balance") is not None]
                strength = ("moderate" if len(shared) >= 3 else "limited")
                pair_dynamics[key] = {
                    "conversation_episode_count": len(conversations),
                    "reciprocal_conversation_candidate_count": len(conversations),
                    "copresence_candidate_count": len(copresence),
                    "shared_activity_candidate_count": len(shared_act),
                    "interaction_event_count": len(shared),
                    "turn_balance_summary": {
                        "mean": round(statistics.fmean(balances), 3) if balances else None,
                        "samples": len(balances),
                    },
                    "joint_emotion_summary": _aggregate_emotions(
                        o.emotion for o in obs_day
                        if set(pair) & set(o.speech.get("speakers") or [])),
                    # How this pair interacted, not merely that they did. This
                    # is the field that separates an argument from an
                    # affectionate exchange -- everything above counts both the
                    # same way.
                    **({"semantic_summary": pair_semantics}
                       if (pair_semantics := _semantic_rollup(shared)) else {}),
                    "evidence_strength": strength,
                }

        # -- household semantics ---------------------------------------------
        # The day's relational reading across every episode that carries one.
        # This is what the dashboard's lamps consume.
        household_semantics = _semantic_rollup(l3_day)

        # -- group dynamics --------------------------------------------------
        group_events = [e for e in l3_day if len(e.participants) >= 3]
        group = {
            "multi_person_episode_count": len(group_events),
            "largest_group_observed": max((len(e.participants) for e in l3_day), default=0),
        }

        # -- data quality ----------------------------------------------------
        speech_events = [o for o in obs_day if o.speech.get("has_speech")]
        identified = [o for o in speech_events if o.speech.get("speakers")]
        emotion_events = [o for o in obs_day if o.emotion]
        unknown_rooms = [o for o in obs_day if o.room is None]

        if len(obs_day) < 20 or not identified:
            quality = "limited"
        elif len(identified) / max(len(speech_events), 1) > 0.5:
            quality = "moderate"
        else:
            quality = "limited"

        data_quality = {
            "raw_event_count": len(obs_day),
            "rooms_observed": sorted({o.room for o in obs_day if o.room}),
            "events_without_room": len(unknown_rooms),
            "speech_event_count": len(speech_events),
            "identified_speaker_event_count": len(identified),
            "unidentified_speech_event_count": len(speech_events) - len(identified),
            "emotion_event_count": len(emotion_events),
            "continuous_monitoring_known": False,
            "observation_gaps_are_silence": False,
            "no_observation_interval_count": len(gaps_day),
            "overall_evidence_quality": quality,
        }

        # -- uncertainties ----------------------------------------------------
        uncertainties: list[dict[str, str]] = []
        if speech_events and len(identified) / max(len(speech_events), 1) < 0.5:
            pct = 100 * (1 - len(identified) / max(len(speech_events), 1))
            uncertainties.append({
                "type": "sparse_speaker_identification",
                "message": f"{pct:.0f}% of speech events could not be attributed to a "
                           f"known household member.",
            })
        weak = [e for e in l2_day if e.confidence < 0.30]
        if weak:
            uncertainties.append({
                "type": "low_audio_confidence",
                "message": f"{len(weak)} of {len(l2_day)} activity episodes rest on "
                           f"low-confidence audio labels.",
            })
        if unknown_rooms:
            uncertainties.append({
                "type": "missing_room_assignment",
                "message": f"{len(unknown_rooms)} events carry no room, limiting "
                           f"location-based inference.",
            })
        uncertainties.append({
            "type": "event_triggered_coverage",
            "message": "Recording is event-triggered. Intervals without events are "
                       "unobserved, not silent, and must not be read as inactivity.",
        })

        # -- notable patterns (descriptive only) --------------------------------
        notable: list[dict[str, Any]] = []
        if activity_counts:
            top, count = activity_counts.most_common(1)[0]
            notable.append({"type": "most_frequent_activity", "activity": top, "count": count})
        conv = [e for e in l3_day if e.type == "reciprocal_conversation_candidate"]
        if conv:
            notable.append({"type": "reciprocal_conversation_observed",
                            "count": len(conv),
                            "participants": sorted({p for e in conv for p in e.participants})})

        summary = {
            "date": day,
            "household_activity": household,
            "individual_patterns": individual,
            "pair_dynamics": pair_dynamics,
            "group_dynamics": group,
            **({"household_semantics": household_semantics}
               if household_semantics else {}),
            "social_events": [
                {"episode_id": e.episode_id, "type": e.type, "start": e.start,
                 "end": e.end, "room": e.room, "participants": e.participants,
                 "confidence": e.confidence}
                for e in l3_day
                if e.type not in {"person_presence_evidence"}
            ],
            "no_observation_intervals": gaps_day,
            "notable_patterns": notable,
            "uncertainties": uncertainties,
            "data_quality": data_quality,
        }

        baseline = compare_to_baseline(summary, historical, cfg)
        if baseline:
            summary["baseline_comparison"] = baseline
        summaries.append(summary)

    return summaries


# ---------------------------------------------------------------------------
# 9. Historical baseline comparison
# ---------------------------------------------------------------------------

BASELINE_METRICS: dict[str, Callable[[dict[str, Any]], float | None]] = {
    "activity_episode_count":
        lambda s: s.get("household_activity", {}).get("activity_episode_count"),
    "speech_episode_count":
        lambda s: sum(1 for e in s.get("social_events", []) if e["type"] == "speech_episode"),
    "reciprocal_conversation_count":
        lambda s: sum(1 for e in s.get("social_events", [])
                      if e["type"] == "reciprocal_conversation_candidate"),
    "copresence_candidate_count":
        lambda s: sum(1 for e in s.get("social_events", [])
                      if e["type"] == "same_room_copresence_candidate"),
    "shared_activity_count":
        lambda s: sum(1 for e in s.get("social_events", []) if e["type"].startswith("shared_")),
    "media_episode_count":
        lambda s: s.get("household_activity", {}).get("activity_counts", {}).get("media_use", 0),
}


def compare_to_baseline(summary: dict[str, Any],
                        historical: list[dict[str, Any]] | None,
                        cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Compare today's metrics with previous days, descriptively.

    Deliberately reports observations against a baseline, never a judgement:
    'below the observed baseline', not 'the relationship is deteriorating'.
    """
    minimum = cfg["thresholds"]["min_baseline_days"]
    history = [h for h in (historical or []) if h.get("date") != summary.get("date")]
    if len(history) < minimum:
        return {
            "status": "insufficient_history",
            "days_available": len(history),
            "days_required": minimum,
        }

    comparisons: list[dict[str, Any]] = []
    for name, getter in BASELINE_METRICS.items():
        try:
            current = getter(summary)
            past = [v for v in (getter(h) for h in history) if v is not None]
        except Exception:
            continue
        if current is None or len(past) < minimum:
            continue
        mean = statistics.fmean(past)
        stdev = statistics.pstdev(past) if len(past) > 1 else 0.0
        score = round((current - mean) / stdev, 2) if stdev > 0 else 0.0
        if stdev == 0:
            interpretation = "within_usual_range" if current == mean else (
                "above_usual" if current > mean else "below_usual")
        elif score >= 2:
            interpretation = "above_usual"
        elif score <= -2:
            interpretation = "below_usual"
        else:
            interpretation = "within_usual_range"
        comparisons.append({
            "metric": name, "current": current,
            "baseline_mean": round(mean, 2), "baseline_std": round(stdev, 2),
            "change_score": score, "interpretation": interpretation,
            "confidence": "moderate" if len(past) >= minimum * 2 else "limited",
        })

    return {"status": "compared", "days_of_history": len(history), "metrics": comparisons}


# ---------------------------------------------------------------------------
# 10. Validation / debug reporting
# ---------------------------------------------------------------------------

def emit_warnings(level0: list[Level0Observation], level2: list[Level2Episode]) -> None:
    speech = [o for o in level0 if o.speech.get("has_speech")]
    identified = [o for o in speech if o.speech.get("speakers")]
    if speech:
        pct = 100 * (1 - len(identified) / len(speech))
        if pct > 20:
            print(f"WARNING: Speaker field absent in {pct:.0f}% of speech events.",
                  file=sys.stderr)
    no_room = [o for o in level0 if o.room is None]
    if no_room:
        print(f"WARNING: {len(no_room)} of {len(level0)} events have no room assigned; "
              f"location-dependent inference is limited for those.", file=sys.stderr)
    weak = [e for e in level2 if e.confidence < 0.30]
    if weak:
        print(f"WARNING: {len(weak)} of {len(level2)} activity episodes rest on "
              f"low-confidence audio labels.", file=sys.stderr)
    print("WARNING: Continuous monitoring coverage is unknown; gaps will not be "
          "interpreted as silence.", file=sys.stderr)


# ---------------------------------------------------------------------------
# 11. JSON output
# ---------------------------------------------------------------------------

def build_output(level0, level1, level2, level3, level4, gaps, event_count,
                 level1_5=None) -> dict[str, Any]:
    timestamps = sorted(o.timestamp for o in level0 if o.timestamp)
    return {
        "metadata": {
            "pipeline_version": PIPELINE_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "input_event_count": event_count,
            "date_range": {
                "start": timestamps[0] if timestamps else None,
                "end": timestamps[-1] if timestamps else None,
            },
            "notes": [
                "Recording is event-triggered; gaps are unobserved, not silent.",
                "Weak inferences carry a _candidate suffix and keep their evidence.",
            ],
        },
        "level_0_raw_observations": [asdict(o) for o in level0],
        "level_1_semantic_events": [asdict(r) for r in level1],
        "level_1_5_semantic_interactions": [asdict(s) for s in (level1_5 or [])],
        "level_2_activity_episodes": [asdict(e) for e in level2],
        "level_3_social_episodes": [asdict(e) for e in level3],
        "level_4_daily_dynamics": level4,
        "no_observation_intervals": gaps,
    }


def write_output(payload: dict[str, Any], output: str, mode: str) -> list[str]:
    written: list[str] = []
    if mode == "split":
        base = Path(output).with_suffix("")
        parent = base.parent if str(base.parent) != "" else Path(".")
        parent.mkdir(parents=True, exist_ok=True)
        # Named rather than numbered now: L1.5 has no integer index, and
        # renumbering the others would break every reader of an existing split.
        for name, key in [
                ("level0", "level_0_raw_observations"),
                ("level1", "level_1_semantic_events"),
                ("level1_5", "level_1_5_semantic_interactions"),
                ("level2", "level_2_activity_episodes"),
                ("level3", "level_3_social_episodes"),
                ("level4", "level_4_daily_dynamics")]:
            path = parent / f"{name}.json"
            path.write_text(json.dumps(payload[key], ensure_ascii=False, indent=2),
                            encoding="utf-8")
            written.append(str(path))
        meta = parent / "metadata.json"
        meta.write_text(json.dumps(
            {"metadata": payload["metadata"],
             "no_observation_intervals": payload["no_observation_intervals"]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(str(meta))
    else:
        path = Path(output)
        if path.parent and str(path.parent) != "":
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(str(path))
    return written


# ---------------------------------------------------------------------------
# 12. CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derive Levels 0-4 household dynamics from Home Sentinel audio events.")
    parser.add_argument("input", help="Raw dashboard events JSON")
    parser.add_argument("--output", default="home_dynamics.json", help="Output path")
    parser.add_argument("--historical", help="Previous Level 4 summaries JSON")
    parser.add_argument("--config", help="Config JSON overriding built-in defaults")
    parser.add_argument("--output-mode", choices=("full", "split"), default="full")
    parser.add_argument("--debug", action="store_true", help="Print per-level counts")
    return parser


def load_historical(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        data = _read_json(Path(path))
    except Exception as exc:
        print(f"WARNING: could not read historical file {path}: {exc}", file=sys.stderr)
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("level_4_daily_dynamics", "days", "summaries"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
    return []


# ---------------------------------------------------------------------------
# 13. main()
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)

    try:
        raw_events = load_raw_events(args.input)
    except Exception as exc:
        print(f"ERROR: could not load {args.input}: {exc}", file=sys.stderr)
        return 1

    level0 = derive_level0(raw_events, cfg)
    level1 = derive_level1(level0, cfg)
    # The CLI has no model wired to it, so L1.5 runs without an interpreter and
    # reports which exchanges WOULD be interpreted. The dashboard passes a real
    # one; see make_semantic_interpreter in ast_dash/pages/llm_report.py.
    level1_5 = derive_level1_5(level0, cfg, interpreter=None)
    level2 = derive_level2(level1, cfg)
    level3 = derive_level3(level0, level2, cfg, level1_5=level1_5)
    gaps = derive_observation_gaps(level0, cfg)
    historical = load_historical(args.historical)
    level4 = derive_level4(level0, level2, level3, gaps, cfg, historical)

    payload = build_output(level0, level1, level2, level3, level4, gaps,
                           len(raw_events), level1_5=level1_5)
    written = write_output(payload, args.output, args.output_mode)

    if args.debug:
        semantic_count = sum(len(r.semantic_events) for r in level1)
        print(f"Loaded events: {len(raw_events)}")
        print(f"Level 0 observations: {len(level0)}")
        print(f"Level 1 semantic records: {len(level1)} ({semantic_count} semantic events)")
        print(f"Level 2 activity episodes: {len(level2)}")
        print(f"Level 3 social episodes: {len(level3)}")
        print(f"Level 4 daily summaries: {len(level4)}")
        print(f"No-observation intervals: {len(gaps)}")
        emit_warnings(level0, level2)

    for path in written:
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
