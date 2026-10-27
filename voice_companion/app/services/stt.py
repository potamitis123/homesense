"""Speech to text.

Two configurations of one model, for two different jobs:

  * live   -- greedy decoding, lowest latency, drives the conversation
  * report -- beam search, run offline afterwards over the saved audio

Both go through the same STTProvider interface.

Hallucination guarding is not optional here. Measured on real sentinel
recordings, Whisper large-v3 transcribed silent clips as
'Υπότιτλοι AUTHORWAVE' -- subtitle credits it invented from noise. In a
companion conversation that becomes a sentence the person never said,
which the agent then answers. Silence must resolve to empty text, so
every result passes three gates: no_speech_prob, a minimum duration, and
a blocklist of known hallucination artefacts.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

# Phrases Whisper emits when given silence or noise. Matched case- and
# punctuation-insensitively against the whole transcript.
HALLUCINATION_PATTERNS = [
    r"υπ[οό]τιτλοι",                  # "subtitles ..." (Greek)
    r"subtitles? by",
    r"amara\.org",
    r"thanks? for watching",
    r"authorwave",
    r"www\.[a-z0-9.\-]+",
    r"^\W*$",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in HALLUCINATION_PATTERNS]


def looks_repetitive(text: str) -> bool:
    """A decoder loop rather than a sentence.

    Greedy decoding on near-silence collapses into repeating one phrase
    until it runs out of tokens -- observed live as "I'm sorry."
    twenty-nine times, at a no_speech_prob that the relaxed long-clip
    threshold would now let through. This is the guard that makes
    relaxing that threshold safe, and it is a shape no real utterance
    has: nobody says the same short phrase four times running.
    """
    parts = [p.strip().lower() for p in re.split(r"[.!?]+", text) if p.strip()]
    if len(parts) < REPEAT_LIMIT:
        return False
    for phrase in set(parts):
        if len(phrase.split()) <= 5 and parts.count(phrase) >= REPEAT_LIMIT:
            return True
    return False

# Whisper's own estimate that a clip contains no speech. Above this the
# transcript is discarded however plausible it reads.
#
# Two thresholds, because the risk is not the same at both ends. Silence
# and room tone arrive in short bursts, and that is where the invented
# subtitle credits come from, so short clips are judged strictly. A clip
# of several seconds that produced an ordinary sentence is almost always
# somebody talking quietly at the far side of a room -- measured at 0.71
# on "I don't remember I think", which was real, and thrown away. For an
# elderly person who does not project, strict-everywhere means unheard.
NO_SPEECH_PROB_MAX = 0.6
NO_SPEECH_PROB_MAX_LONG = 0.9
# Above this many seconds a clip is judged by the relaxed threshold.
LONG_AUDIO_SECONDS = 1.5
MIN_AUDIO_SECONDS = 0.35
# A phrase repeated this many times is a decoder loop, not speech: real
# speech does not say "I'm sorry." twenty-nine times. Catching this is
# what lets the threshold above be relaxed safely.
REPEAT_LIMIT = 4


@dataclass
class TranscriptionResult:
    text: str
    language: str | None = None
    confidence: float | None = None
    duration_seconds: float = 0.0
    is_empty: bool = False
    rejected_reason: str | None = None
    segments: list[dict] = field(default_factory=list)

    @classmethod
    def empty(cls, reason: str, duration: float = 0.0) -> "TranscriptionResult":
        return cls(text="", is_empty=True, rejected_reason=reason,
                   duration_seconds=duration)


class STTProvider(Protocol):
    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        ...


def looks_hallucinated(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return True
    return any(p.search(cleaned) for p in _COMPILED)


class FasterWhisperSTT:
    """faster-whisper, shared by the live and report configurations."""

    def __init__(self, model_name: str, language: str, beam_size: int,
                 sample_rate: int = 16000, device: str = "auto",
                 min_free_vram_mb: int = 3600, cpu_threads: int = 0):
        from faster_whisper import WhisperModel

        # faster-whisper takes None to mean "detect it".
        language = (language or "auto").strip().lower()
        self.language = None if language in ("auto", "", "detect") else language
        self.beam_size = beam_size
        self.sample_rate = sample_rate

        device = (device or "auto").strip().lower()
        if device == "auto":
            device = self._choose_device(min_free_vram_mb)

        self.device = device
        compute = "float16" if device == "cuda" else "int8"
        threads = cpu_threads or (os.cpu_count() or 4)

        log.info("loading whisper %s on %s (%s)%s", model_name, device,
                 compute, "" if device == "cuda" else f" x{threads} threads")
        try:
            self._model = WhisperModel(model_name, device=device,
                                       compute_type=compute,
                                       cpu_threads=threads)
        except Exception:
            if device == "cpu":
                raise
            # Out of memory, no driver, a card that vanished: none of
            # these should stop the conversation happening at all.
            log.exception("whisper failed to load on the GPU; falling back "
                          "to the CPU with the same model")
            self.device = "cpu"
            self._model = WhisperModel(model_name, device="cpu",
                                       compute_type="int8",
                                       cpu_threads=threads)

        if self.device == "cpu":
            log.warning("speech recognition is on the CPU: replies will be "
                        "slower, but they will arrive at a predictable "
                        "speed rather than stalling")

    @staticmethod
    def _choose_device(min_free_vram_mb: int) -> str:
        """GPU only if the model genuinely fits in what is free right now.

        `torch.cuda.is_available()` answers a different question -- it is
        true whenever a driver exists, including when another process is
        holding almost the whole card. Loading on that answer is what
        produced 22-second transcriptions of half-second clips.
        """
        try:
            import torch
            if not torch.cuda.is_available():
                log.info("no CUDA device; speech recognition on the CPU")
                return "cpu"
            free_b, total_b = torch.cuda.mem_get_info()
        except Exception:
            log.warning("could not inspect the GPU; using the CPU",
                        exc_info=True)
            return "cpu"

        free_mb, total_mb = free_b // (1 << 20), total_b // (1 << 20)
        if free_mb >= min_free_vram_mb:
            log.info("GPU has %d MiB free of %d MiB (need %d): using CUDA",
                     free_mb, total_mb, min_free_vram_mb)
            return "cuda"

        log.warning("GPU has only %d MiB free of %d MiB and this model needs "
                    "about %d: staying off it and running the same model on "
                    "the CPU. Something else is holding the card -- freeing "
                    "it will make replies faster.",
                    free_mb, total_mb, min_free_vram_mb)
        return "cpu"

    def _pcm_to_float(self, audio: bytes):
        import numpy as np
        return np.frombuffer(audio, dtype=np.int16).astype("float32") / 32768.0

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        duration = len(audio) / (self.sample_rate * 2)
        if duration < MIN_AUDIO_SECONDS:
            return TranscriptionResult.empty("too_short", duration)

        samples = self._pcm_to_float(audio)
        try:
            # vad_filter deliberately off: endpointing already ran upstream,
            # and faster-whisper raises ValueError("max() arg is an empty
            # sequence") when its own VAD removes everything.
            segments, info = self._model.transcribe(
                samples, language=self.language, beam_size=self.beam_size,
                vad_filter=False, condition_on_previous_text=False,
                # One decode, not six.
                #
                # faster-whisper's default temperature list is a fallback
                # ladder: when a decode fails its log-probability and
                # compression thresholds it retries, hotter, up to six
                # times. Silence fails those thresholds every time, so
                # the clips that are worth the least cost the most --
                # measured at 10 s to reject 0.9 s of room tone on an
                # idle GPU, while real speech of the same length took
                # two. In a spoken conversation that reads as the
                # companion having frozen.
                #
                # Giving up after one attempt is the right trade here:
                # the fallback exists to rescue hard audio, and what it
                # actually rescues on this input is a hallucination that
                # the guards below then throw away.
                temperature=0.0)
            segments = list(segments)
        except ValueError as exc:
            log.warning("whisper rejected the buffer: %s", exc)
            return TranscriptionResult.empty("decoder_empty", duration)
        except Exception as exc:
            log.exception("whisper failed")
            return TranscriptionResult.empty(f"error:{type(exc).__name__}", duration)

        if not segments:
            return TranscriptionResult.empty("no_segments", duration)

        no_speech = sum(getattr(s, "no_speech_prob", 0.0) for s in segments) / len(segments)
        text = " ".join(s.text for s in segments).strip()

        ceiling = (NO_SPEECH_PROB_MAX_LONG if duration >= LONG_AUDIO_SECONDS
                   else NO_SPEECH_PROB_MAX)
        if no_speech > ceiling:
            log.info("discarded transcript (no_speech_prob=%.2f > %.2f for "
                     "%.2fs): %r", no_speech, ceiling, duration, text)
            return TranscriptionResult.empty("no_speech", duration)
        if looks_repetitive(text):
            log.info("discarded looping transcript: %r", text[:80])
            return TranscriptionResult.empty("repetition", duration)
        if looks_hallucinated(text):
            log.info("discarded hallucinated transcript: %r", text)
            return TranscriptionResult.empty("hallucination", duration)

        return TranscriptionResult(
            text=text,
            language=getattr(info, "language", self.language),
            confidence=1.0 - no_speech,
            duration_seconds=duration,
            segments=[{"start": s.start, "end": s.end, "text": s.text}
                      for s in segments],
        )


class DummySTT:
    """Returns a canned line; lets the graph run with no model loaded."""

    def __init__(self, replies: list[str] | None = None):
        self._replies = replies or ["Yes, I am doing all right today."]
        self._i = 0

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        text = self._replies[self._i % len(self._replies)]
        self._i += 1
        return TranscriptionResult(text=text, language="en", confidence=1.0,
                                   duration_seconds=2.0)
