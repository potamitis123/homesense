"""Local text-to-speech.

Interface first, implementation behind it, exactly as the specification
asks -- so a different local model can be dropped in without the graph
noticing.

Why Piper is the reference implementation: measured against Chatterbox on
identical sentences, Piper synthesises at RTF 0.06-0.09 and yields its
first audio chunk in ~0.10s, where Chatterbox needs 1.4-3.9x realtime and
cannot emit anything until the whole utterance is finished. For a live
conversation the first-chunk latency is what the listener experiences.
Chatterbox remains the better choice for offline work such as the avatar
videos.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterator, Protocol


@dataclass
class AudioResult:
    pcm: bytes            # signed 16-bit little-endian
    sample_rate: int
    channels: int = 1
    sample_width: int = 2

    @property
    def duration_seconds(self) -> float:
        frame = self.sample_width * self.channels
        return len(self.pcm) / (self.sample_rate * frame) if frame else 0.0

    def to_wav_bytes(self) -> bytes:
        buf = BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(self.channels)
            w.setsampwidth(self.sample_width)
            w.setframerate(self.sample_rate)
            w.writeframes(self.pcm)
        return buf.getvalue()


class TTSProvider(Protocol):
    async def synthesize(self, text: str, language: str,
                         voice_id: str | None = None) -> AudioResult:
        ...

    def stream(self, text: str, language: str,
               voice_id: str | None = None) -> Iterator[AudioResult]:
        """Yield audio as it is produced, for lowest time-to-first-sound."""
        ...


class PiperTTS:
    """Local Piper voice. Streams chunk by chunk."""

    def __init__(self, model_path: str | Path, length_scale: float = 1.0):
        from piper import PiperVoice  # imported late: heavy onnxruntime load

        # length_scale stretches every phoneme: >1 is slower. Speaking a
        # little under normal pace matters here -- the listener is elderly,
        # possibly hard of hearing, and there is no "pardon?" button.
        self.length_scale = length_scale
        self.model_path = str(model_path)
        if not Path(self.model_path).exists():
            raise FileNotFoundError(
                f"Piper voice not found: {self.model_path}\n"
                f"Download one with:  python -m piper.download_voices "
                f"en_US-lessac-medium --data-dir voice_companion/data/voices")
        self._voice = PiperVoice.load(self.model_path)
        self.sample_rate = self._voice.config.sample_rate

        from piper import SynthesisConfig
        self._syn = SynthesisConfig(length_scale=length_scale)

    def stream(self, text: str, language: str = "en",
               voice_id: str | None = None) -> Iterator[AudioResult]:
        for chunk in self._voice.synthesize(text, syn_config=self._syn):
            yield AudioResult(pcm=chunk.audio_int16_bytes,
                              sample_rate=self.sample_rate)

    async def synthesize(self, text: str, language: str = "en",
                         voice_id: str | None = None) -> AudioResult:
        pcm = b"".join(c.pcm for c in self.stream(text, language, voice_id))
        return AudioResult(pcm=pcm, sample_rate=self.sample_rate)


class DummyTTS:
    """Silence of a plausible length. For tests and for proving the
    transport without loading a model."""

    def __init__(self, sample_rate: int = 22050):
        self.sample_rate = sample_rate

    def stream(self, text: str, language: str = "en",
               voice_id: str | None = None) -> Iterator[AudioResult]:
        # ~14 characters per second is close to natural speaking rate.
        seconds = max(0.4, len(text) / 14.0)
        yield AudioResult(pcm=b"\x00\x00" * int(self.sample_rate * seconds),
                          sample_rate=self.sample_rate)

    async def synthesize(self, text: str, language: str = "en",
                         voice_id: str | None = None) -> AudioResult:
        return next(iter(self.stream(text, language, voice_id)))


def build_tts(settings) -> TTSProvider:
    if settings.local_tts_provider.lower() == "dummy":
        return DummyTTS()
    return PiperTTS(settings.local_tts_model_path,
                    length_scale=getattr(settings, "tts_length_scale", 1.0))
