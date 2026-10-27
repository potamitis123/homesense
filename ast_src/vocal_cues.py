"""Vocal events the words do not carry: laughter, shouting, crying, singing.

WHY THIS EXISTS

The relational reading is made from a transcript, and three of its dimensions
have their evidence in tone rather than in words. Measured against a listener on
99 exchanges, joy was read 0.80 scale points low, warmth 0.34 low, and
withdrawal never fired at all; every one of the listener's twenty-one
withdrawal judgements described manner -- "answers but reluctantly", "talks
briefly like he is bored" -- and not one quoted a verbal refusal.

The obvious channel cannot supply it. Over 21,578 clips of 14-19 September the
AudioSet tagger returned a laughter label thirteen times, never as its first
label. So laughter, which the household holds to be the cue that can reverse
the meaning of a heated exchange, reaches the interpreter essentially never.

WHAT THIS IS NOT

Not an affect classifier. The four-class affect label already in the pipeline
scored Cohen's kappa of -0.02 against a listener, and when it travelled with
every turn the interpreter leaned on it: told "happy" for 65 of 68 lines of a
boy venting about a bus driver, it scored joy 0.75 three times running, against
0.5, 0.5 and 0.3 without. This detects specific acoustic EVENTS and reports
them where they happened, as sounds in the stream, which is the form the
interpreter already knows how to weigh in sequence.

HOW IT SCORES

CLAP embeds a 3-second window and each event's prompts, and the score is the
window's similarity to the event MINUS its similarity to ordinary talk. The
contrast matters: without it, any clear speech scores highly against every
prompt. A score at or below zero is never a cue however the day went.

Absolute scores drift between sentinels -- different microphones, different
rooms, different distances -- so each device carries its own floor, taken from
the highest score its ordinary speech reaches over a sample of its own clips.
A cue is an event whose window clears that floor by a margin.

COST

About 0.4 s of CPU per window, so this runs over the exchanges the interpreter
is about to read -- roughly 110 a day -- and not over every clip the sentinels
deliver. At a median exchange that is a few seconds each, minutes a day.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta

SR = 48000
WINDOW_S, HOP_S = 3.0, 1.5
# Two cues of the same kind within this many seconds are one event, reported at
# its strongest window. A laugh is not six laughs because it lasted nine
# seconds.
MERGE_S = 3.0
MODEL_ID = "laion/larger_clap_music_and_speech"

# Several wordings per event, averaged. One phrasing is a lottery; the mean of
# a few is stable, and this is how the offline harness was calibrated.
EVENTS = {
    "laughter": ["the sound of people laughing", "a man laughing",
                 "a child laughing", "laughter"],
    "shouting": ["a man shouting angrily", "people yelling at each other",
                 "a loud shout", "shouting"],
    # Not one of the three asked for, and included for a measured reason: a
    # resident singing alone was read as a two-person exchange during the
    # coding study. Singing is the commonest way one voice looks like a
    # conversation.
    "singing":  ["a person singing", "someone humming a tune", "singing"],
}
TALK = ["the sound of a man speaking", "people talking", "speech"]

# What a VocalCues() detects unless told otherwise.
DEFAULT_EVENTS = ("laughter", "shouting", "singing")

# `crying` is deliberately absent. The household reports that a CLAP search
# for laughter is reliably correct while crying "returns very low
# correlation", and a trial here fired crying on two exchanges in which
# nobody cried. Crying is also rare in this household, which makes a weak
# detector worse rather than better: against a low base rate almost every
# firing would be a false one, and a false distress signal in a family report
# is the fabrication-shaped error this system most needs to avoid.

# How far above a device's ordinary-speech ceiling a window must sit.
#
# Provisional. At 0.02 an exchange the listener described as "faking but in a
# convincing way (no laughs)" cleared the laughter floor by 0.002, while a
# genuine one cleared it by 0.16. Three labelled cases are not a calibration,
# and this number should be fitted properly against a labelled set before any
# figure derived from these cues is reported.
DEFAULT_MARGIN = 0.05
# Clips sampled per device to establish that ceiling.
BASELINE_CLIPS = 12

log = logging.getLogger(__name__)


class VocalCues:
    """CLAP on CPU, scoring short windows of a clip against the event prompts."""

    def __init__(self, audio_dir: str, margin: float = DEFAULT_MARGIN,
                 events: dict | None = None):
        self.audio_dir = audio_dir
        self.margin = margin
        self.events_text = events or {k: v for k, v in EVENTS.items()
                                      if k in DEFAULT_EVENTS}
        self._model = None
        self._floors: dict[str, dict[str, float]] = {}

    # -- lazily loaded, because a report that never asks for a cue should not
    # -- pay to load CLAP at all.
    def _load(self):
        if self._model is not None:
            return
        import numpy as np
        import torch
        from transformers import ClapModel, ClapProcessor
        torch.set_num_threads(4)
        self.np, self.torch = np, torch
        self._model = ClapModel.from_pretrained(MODEL_ID).eval()
        self._processor = ClapProcessor.from_pretrained(MODEL_ID)
        self._talk = self._text(TALK)
        self._events = {name: self._text(texts)
                        for name, texts in self.events_text.items()}
        log.info("Vocal cue detector ready (%s)", ", ".join(self._events))

    def _text(self, texts):
        with self.torch.no_grad():
            feats = self._model.get_text_features(
                **self._processor(text=texts, return_tensors="pt", padding=True))
        feats = feats / feats.norm(dim=-1, keepdim=True)
        mean = feats.mean(0)
        return (mean / mean.norm()).numpy()

    def windows(self, path: str):
        """([window start in seconds], {event: score per window})."""
        self._load()
        import librosa
        try:
            y, _ = librosa.load(path, sr=SR, mono=True)
        except Exception as exc:                              # noqa: BLE001
            log.warning("Vocal cues: could not read %s (%s)", path, exc)
            return [], {}
        if not len(y):
            return [], {}
        size, hop = int(WINDOW_S * SR), int(HOP_S * SR)
        starts = list(range(0, max(1, len(y) - size + 1), hop))
        if len(y) > size and starts[-1] + size < len(y):
            starts.append(len(y) - size)
        chunks = [y[s:s + size] for s in starts]
        feats = []
        for i in range(0, len(chunks), 8):
            with self.torch.no_grad():
                out = self._model.get_audio_features(**self._processor(
                    audios=chunks[i:i + 8], sampling_rate=SR, return_tensors="pt"))
            feats.append((out / out.norm(dim=-1, keepdim=True)).numpy())
        emb = self.np.concatenate(feats)
        return ([s / SR for s in starts],
                {name: emb @ vec - emb @ self._talk
                 for name, vec in self._events.items()})

    def floor_for(self, device: str, sample_paths: list[str]) -> dict[str, float]:
        """The ceiling this device's ORDINARY speech reaches, per event.

        Computed once per device and kept. Without it a quiet microphone's
        normal conversation outscores a loud one's genuine shout, and the cue
        would say more about the room than about the people in it.
        """
        if device in self._floors:
            return self._floors[device]
        best: dict[str, float] = {name: 0.0 for name in self.events_text}
        for path in sample_paths[:BASELINE_CLIPS]:
            _offsets, scores = self.windows(path)
            for name, values in scores.items():
                if len(values):
                    best[name] = max(best[name], float(values.max()))
        self._floors[device] = best
        log.info("Vocal cue floors for %s: %s", device,
                 {k: round(v, 3) for k, v in best.items()})
        return best

    def for_clip(self, path: str, started_at, device: str = "?",
                 floors: dict | None = None) -> list[dict]:
        """[{time, label, confidence}] for one clip, in the shape sounds use.

        `time` is the middle of the window that fired, so a cue can be placed
        in sequence against the turns around it -- which is the whole reason
        the interpreter is given sounds with times rather than a list.
        """
        offsets, scores = self.windows(path)
        if not offsets:
            return []
        floors = floors or self._floors.get(device) or {}
        found: list[dict] = []
        for name, values in scores.items():
            floor = max(0.0, float(floors.get(name, 0.0))) + self.margin
            hits = [(offsets[i], float(v)) for i, v in enumerate(values)
                    if v > floor]
            for offset, score in hits:
                when = started_at + timedelta(seconds=offset + WINDOW_S / 2)
                # Merge with the previous cue of the same kind if it is close,
                # keeping whichever window scored higher.
                same = [f for f in found if f["label"] == name
                        and abs((f["time"] - when).total_seconds()) <= MERGE_S]
                if same:
                    if score > same[0]["confidence"]:
                        same[0].update(time=when, confidence=score)
                    continue
                found.append({"time": when, "label": name,
                              "confidence": round(score, 3)})
        return sorted(found, key=lambda f: f["time"])

    def for_clips(self, clips) -> list[dict]:
        """clips: [(device, audio path, started_at)] -> all their cues, in time order."""
        out: list[dict] = []
        for device, path, started_at in clips:
            if not path or not os.path.exists(path):
                continue
            out.extend(self.for_clip(path, started_at, device,
                                     self._floors.get(device)))
        return sorted(out, key=lambda f: f["time"])
