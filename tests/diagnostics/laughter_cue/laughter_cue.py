"""Does a CLAP laughter cue change how L1.5 reads a discussion?

    python tests/diagnostics/laughter_cue/laughter_cue.py 2026-09-16 20:16:42 20:17:42 [runs] [--hedged]

runs 0 finds the cues only, with no GPT calls. Otherwise each room's
discussion in the window is read `runs` times as L1.5 reads it today (A) and,
where CLAP hears laughter or shouting, with those cues placed in the stream
and a section explaining them (C). Nothing in the pipeline changes.

Why windows: the CLAP processor keeps a random 10 s crop of a longer clip
(truncation="rand_trunc"), so a stored clip embedding can miss a 2 s laugh
entirely. Each clip of the discussion is scored here in 3 s windows (1.5 s
hop, about 0.4 s of CPU each). A window is a cue when it sounds more like the
event than like talk and beats every window of an ordinary-speech sample from
the same sentinel that day (12 random clips, cached per day in results/; the
sample's maximum moves by about 0.1 from one sample to another), and it is
placed in a discussion within the same margin L1.5 uses for sounds.

First case, 16-09-2026 20:16:42-20:17:42, a playful first try at cooking:
today's L1.5 read tension and connection, joy 0.00, in 5 runs of 5. CLAP found
the laugh on three sentinels at 20:17:16-20:17:22 (the laugh at 14:05:36,
#55502, was the positive control). With the --hedged wording joy rose to 0.50
in every run but tension stayed at 0.50 and no lamp changed. The default
wording follows the household's principle instead: laughter indicates a
joyful conversation even when people seem to argue. What it most needs is a
real argument without laughter, where a false cue would turn it joyful.
"""
import json
import os
import random
import sqlite3
import sys
import time
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
warnings.filterwarnings("ignore")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
RESULTS = os.path.join(HERE, "results")
for path in (ROOT, os.path.join(ROOT, "conversation_day"), os.path.join(ROOT, "ast_dash", "llm_report")):
    sys.path.insert(0, path)
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np                   # noqa: E402

import app as A                      # noqa: E402  the conversation viewer: events, prompt, lamp thresholds
import derive_home_dynamics as H     # noqa: E402
import timeline as T                 # noqa: E402

MODEL_ID = "laion/larger_clap_music_and_speech"
AUDIO = os.path.join(ROOT, "assets", "audio_mp3")
WINDOW_S, HOP_S, SR, MERGE_S = 3.0, 1.5, 48000, 3.0
BASELINE_CLIPS = 12
EVENTS = {
    "laughter": ["the sound of people laughing", "a man laughing", "a child laughing", "laughter"],
    "shouting": ["a man shouting angrily", "people yelling at each other", "a loud shout", "shouting"],
}
TALK = ["the sound of a man speaking", "people talking", "speech"]
DIMENSIONS = ("tension", "withdrawal", "connection", "inclusion", "warmth", "joy")
PRICE_IN, PRICE_OUT = 0.15 / 1e6, 0.60 / 1e6          # gpt-4o-mini, $ per token

VOICE = """## Voice cues

Some entries in the stream are `voice_cue`: an audio model compared a few
seconds of the recording with laughter and with shouting, and the match was
stronger than anything in this room's ordinary speech. A cue does not say who
laughed or shouted.

Laughter can reverse what the words seem to say. When there is laughter in an
exchange, read the exchange as joyful even if the people seem to argue: harsh
words, complaints and swearing around the laughter are most likely teasing.
Score joy and warmth for it, and keep tension only for a grievance that still
reads as real with the laughter taken into account. This goes further than
the rule on humour in the words: joking words alone do not make an exchange
joyful, heard laughter does. The exception is laughter plainly aimed at
someone who objects or is hurt, and the words must show it.

A shouting cue supports tension only when the words carry it. A cue never
licenses a reading of words you cannot understand.

"""
# The wording of the first run (17-09-2026): the words decide, laughter only tips them.
VOICE_HEDGED = """## Voice cues

Some entries in the stream are `voice_cue`: an audio model compared a few
seconds of the recording with laughter and with shouting, and the match was
stronger than anything in this room's ordinary speech. A cue is a hint, not a
fact. It does not say who laughed or shouted, or why.

Read a cue with the words around it. Laughter in an exchange points to a light
or playful tone at that moment, and harsh-sounding words right around it are
then more likely teasing than hostility, unless the words show a grievance
aimed at someone. Laughter can also mock; the words decide which. A shouting
cue supports tension only when the words carry it. A cue never licenses a
reading of words you cannot understand.

"""


class Clap:
    """CLAP on CPU with four threads, as the app runs it, scoring short windows."""

    def __init__(self):
        import torch
        from transformers import ClapModel, ClapProcessor
        torch.set_num_threads(4)
        self.torch = torch
        self.model = ClapModel.from_pretrained(MODEL_ID, local_files_only=True).eval()
        self.processor = ClapProcessor.from_pretrained(MODEL_ID, local_files_only=True)
        self.talk = self._text(TALK)
        self.events = {name: self._text(texts) for name, texts in EVENTS.items()}

    def _text(self, texts):
        with self.torch.no_grad():
            feats = self.model.get_text_features(**self.processor(text=texts, return_tensors="pt", padding=True))
        feats = feats / feats.norm(dim=-1, keepdim=True)
        mean = feats.mean(0)
        return (mean / mean.norm()).numpy()

    def windows(self, path):
        """([window start, s], {event: score per window}); score = similarity to the event minus to talk."""
        import librosa
        y, _sr = librosa.load(path, sr=SR, mono=True)
        if not len(y):
            return [], {name: np.zeros(0) for name in EVENTS}
        size, hop = int(WINDOW_S * SR), int(HOP_S * SR)
        starts = list(range(0, max(1, len(y) - size + 1), hop))
        if len(y) > size and starts[-1] + size < len(y):
            starts.append(len(y) - size)
        chunks = [y[s:s + size] for s in starts]
        feats = []
        for i in range(0, len(chunks), 8):
            with self.torch.no_grad():
                out = self.model.get_audio_features(**self.processor(
                    audios=chunks[i:i + 8], sampling_rate=SR, return_tensors="pt"))
            feats.append((out / out.norm(dim=-1, keepdim=True)).numpy())
        emb = np.concatenate(feats)
        return [s / SR for s in starts], {name: emb @ vec - emb @ self.talk for name, vec in self.events.items()}


def results_dir():
    """results/, kept out of git: what it holds are transcripts of the household."""
    os.makedirs(RESULTS, exist_ok=True)
    ignore = os.path.join(RESULTS, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as fh:
            fh.write("*\n")
    return RESULTS


def audio_path(file_name):
    path = os.path.join(AUDIO, os.path.basename(file_name or ""))
    return path if file_name and os.path.exists(path) else None


def baseline(clap, day, devices, scene_ids):
    """{sentinel: {"ids", "laughter", "shouting"}}: the highest window score in a sample of its ordinary speech.

    Cached per day. A sentinel is sampled again when the cached sample holds a
    clip of the discussion under test, which would hide its own laugh.
    """
    path = os.path.join(results_dir(), f"baseline_{day}.json")
    known = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            known = json.load(fh)
    stale = sorted(d for d in devices if d not in known or set(known[d]["ids"]) & scene_ids)
    if not stale:
        return known
    conn = sqlite3.connect(f"file:{A._db_path()}?mode=ro", uri=True)
    try:
        for device in stale:
            pool = [row for row in conn.execute(
                "SELECT id, file_name FROM ast_table WHERE device_name = ? AND date LIKE ? "
                "AND transcription IS NOT NULL AND transcription NOT LIKE 'No Speech%' ORDER BY id",
                (device, f"{day}%")) if row[0] not in scene_ids and audio_path(row[1])]
            sample = random.Random(f"{day}{device}").sample(pool, min(BASELINE_CLIPS, len(pool)))
            best = {name: None for name in EVENTS}
            for _id, file_name in sample:
                _offsets, scores = clap.windows(audio_path(file_name))
                for name, values in scores.items():
                    if len(values) and (best[name] is None or values.max() > best[name]):
                        best[name] = float(values.max())
            known[device] = {"ids": [row[0] for row in sample], **best}
    finally:
        conn.close()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(known, fh, indent=1)
    return known


def window_for(level0, cfg, number, discussion):
    """The window L1.5 reads for a discussion, as the viewer builds it (timeline.read_discussions)."""
    turns = discussion["turns"]
    window = {"cluster_index": number, "start": discussion["start"], "end": discussion["end"],
              "room": discussion["room"], "turns": turns, "context_before": [], "context_after": [],
              "named_speakers": sorted(discussion["residents"]),
              "distinct_speakers": sorted({t.speaker for t in turns if t.speaker}),
              "word_count": sum(H._turn_words(t) for t in turns),
              "source_event_ids": sorted({t.event_id for t in turns}, key=str)}
    window["sounds"] = H.nearby_sounds(level0, window, cfg)
    return window


def cues_for(clap, window, clips, floors, margin):
    """[(time, event, score)]: windows above their sentinel's baseline, near ones merged into the best.

    A score at or below zero is never a cue, whatever the sample: it sounds
    more like talk than like the event, and a sentinel whose ordinary speech
    happened to score low would otherwise turn plain talk into shouting.
    """
    found = []
    for clip in clips:
        path = audio_path(clip["file_name"])
        if path is None:
            continue
        offsets, scores = clap.windows(path)
        stamp = datetime.fromisoformat(clip["date"])
        for name, values in scores.items():
            floor = (floors.get(clip["device"]) or {}).get(name)
            for offset, value in zip(offsets, values):
                when = stamp + timedelta(seconds=offset + WINDOW_S / 2)
                if floor is not None and value > max(floor, 0.0) and \
                        window["start"] - margin <= when <= window["end"] + margin:
                    found.append((when, name, float(value)))
    merged = []
    for when, name, value in sorted(found):
        near = next((i for i in range(len(merged) - 1, -1, -1) if merged[i][1] == name
                     and abs((when - merged[i][0]).total_seconds()) <= MERGE_S), None)
        if near is None:
            merged.append((when, name, value))
        elif value > merged[near][2]:
            merged[near] = (when, name, value)
    return merged


def with_cues(payload, cues):
    stream = [(entry["time"], 0, entry) for entry in payload["exchange"]]
    stream += [(f"{when:%H:%M:%S}", 1, {"time": f"{when:%H:%M:%S}", "voice_cue": name})
               for when, name, _value in cues]
    return {**payload, "exchange": [entry for _t, _o, entry in sorted(stream, key=lambda s: (s[0], s[1]))]}


def ask(client, prompt):
    delay = 5.0
    for attempt in range(6):
        try:
            response = client.chat.completions.create(
                model=A.OPENAI_MODEL, messages=[{"role": "user", "content": prompt}], stream=False)
            return response.choices[0].message.content, response.usage
        except Exception as exc:                                     # noqa: BLE001
            if ("429" in str(exc) or "rate limit" in str(exc).lower()) and attempt < 5:
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            raise


def lit(name, score):
    return score is not None and score >= (A.AMBER if name in ("tension", "withdrawal") else A.GREEN)


def report(records):
    for record in records:
        dims = record.relational_dimensions
        scores = " ".join(f"{name[:4]} {dims[name]:.2f}{'*' if lit(name, dims[name]) else ' '}"
                          if dims.get(name) is not None else f"{name[:4]}  –   " for name in DIMENSIONS)
        complaints = ", ".join(f"{c['by']}→{c['about']}" for c in record.complaints)
        print(f"    {record.semantic_status[:7]:<7} conf {record.semantic_confidence or 0:.2f} | {scores} | "
              f"{', '.join(record.interaction_types)}" + (f" | complaints {complaints}" if complaints else ""))
        print(f"            {record.evidence}")
    lamps = Counter(name for r in records for name in DIMENSIONS if lit(name, r.relational_dimensions.get(name)))
    print(f"    lamps lit over {len(records)} runs: "
          + (", ".join(f"{name} {lamps[name]}/{len(records)}" for name in DIMENSIONS if lamps[name]) or "none"))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 3:
        sys.exit(__doc__)
    hedged = "--hedged" in sys.argv
    day, start_clock, end_clock = args[:3]
    runs = int(args[3]) if len(args) > 3 else 5
    start = datetime.fromisoformat(f"{day} {start_clock}")
    end = datetime.fromisoformat(f"{day} {end_clock}")

    cfg = H.load_config(A.CONFIG)
    events = A.load_events(start, end)
    level0 = H.derive_level0(events, cfg)
    discussions, left_out = T.anchor(level0, cfg, T.SILENCE_MINUTES)
    with open(A.PROMPT, encoding="utf-8") as fh:
        prompt = fh.read()
    marker = "The exchange follows."
    if prompt.count(marker) != 1:
        sys.exit(f"The prompt no longer ends with one “{marker}”: place the voice cue section by hand.")
    cue_prompt = prompt.replace(marker, (VOICE_HEDGED if hedged else VOICE) + marker)
    margin = timedelta(seconds=float(cfg["thresholds"].get("semantic_sound_margin_seconds", 15)))
    by_id = {e["ID"]: e for e in events}
    windows = [window_for(level0, cfg, number, d) for number, d in enumerate(discussions)]
    scene_ids = {i for w in windows for i in w["source_event_ids"]}
    print(f"{len(events)} recordings {start:%d-%m-%Y %H:%M:%S}–{end:%H:%M:%S} · {len(discussions)} discussions · "
          f"{len(left_out)} lone stretches left out · {runs} runs per version · "
          f"{'hedged' if hedged else 'laughter-led'} wording")

    began = time.monotonic()
    clap = Clap()
    floors = baseline(clap, day, {by_id[i]["Device"] for i in scene_ids if i in by_id}, scene_ids)
    for device in sorted(floors):
        if any(by_id[i]["Device"] == device for i in scene_ids if i in by_id):
            print(f"  baseline {device:<10} {len(floors[device]['ids'])} clips · " + " · ".join(
                f"{name} max {floors[device][name]:+.2f}" if floors[device][name] is not None else f"{name} –"
                for name in EVENTS))
    print()

    client = None
    if runs:
        from openai import OpenAI
        from misc.secrets import openai_api_key
        client = OpenAI(api_key=openai_api_key())
    spent, saved = [0, 0], []
    for discussion, window in zip(discussions, windows):
        clips = [{"date": by_id[i]["Timestamp"], "device": by_id[i]["Device"], "file_name": by_id[i]["file_name"]}
                 for i in window["source_event_ids"] if i in by_id]
        cues = cues_for(clap, window, clips, floors, margin)
        print(f"=== {discussion['start']:%H:%M:%S}–{discussion['end']:%H:%M:%S} {discussion['room']} "
              f"({discussion['sentinel']}) · {', '.join(sorted(discussion['residents']))} · cues: "
              + (", ".join(f"{name} {when:%H:%M:%S} ({value:+.2f})" for when, name, value in cues) or "none"))
        saved.append({"room": discussion["room"], "start": discussion["start"], "end": discussion["end"],
                      "cues": cues, "readings": []})
        if not runs:
            continue
        payload = H.window_payload(window)
        versions = [("A today", prompt, payload)]
        if cues:
            versions.append(("C laughter cue", cue_prompt, with_cues(payload, cues)))
        for label, text, sent in versions:
            full = f"{text}\n\n{json.dumps(sent, ensure_ascii=False, indent=2)}"
            with ThreadPoolExecutor(max_workers=4) as pool:
                answers = list(pool.map(lambda _run: ask(client, full), range(runs)))
            records = []
            for reply_text, usage in answers:
                spent[0] += usage.prompt_tokens
                spent[1] += usage.completion_tokens
                reply = A.parse_reply(reply_text)
                records.append(H.normalise_semantic_reply(reply, window, cfg))
                saved[-1]["readings"].append({"version": label, "reply": reply, "payload": sent})
            print(f"  {label}")
            report(records)
        if not cues:
            print("  C: no cue, the same input as A")
        print()

    name = f"laughter_cue_{start:%Y%m%d_%H%M%S}-{end:%H%M%S}{'_hedged' if hedged else ''}.json"
    out = os.path.join(results_dir(), name)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"window": [start, end], "wording": "hedged" if hedged else "laughter-led",
                   "baseline": floors, "discussions": saved}, fh, ensure_ascii=False, indent=1, default=str)
    print(f"CLAP {time.monotonic() - began:.0f} s on CPU · {sum(len(d['readings']) for d in saved)} calls · "
          f"${spent[0] * PRICE_IN + spent[1] * PRICE_OUT:.3f} · saved to {out}")
    print("* = lamp lit (tension/withdrawal >= 0.40, the others >= 0.55)")


if __name__ == "__main__":
    main()
