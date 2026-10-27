"""PCM helpers and Push-to-Talk utterance segmentation."""

from __future__ import annotations

import io
import threading
from typing import List, Optional

import numpy as np
from pydub import AudioSegment

from .constants import (
    CHANNELS,
    PTT_TARGET_PEAK,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    TAIL_PAD_MS,
)


def normalize_ptt(pcm: np.ndarray, target_peak: float = PTT_TARGET_PEAK) -> np.ndarray:
    peak = np.max(np.abs(pcm.astype(np.float32)))
    if peak < 50:
        return pcm
    gain = (target_peak * 32767.0) / peak
    return np.clip(pcm.astype(np.float32) * gain, -32768, 32767).astype(np.int16)


def utterance_to_wav(pcm_bytes: bytes) -> bytes:
    """Normalise, pad, and encode PCM int16 bytes as a WAV file (production PTT)."""
    pcm = np.frombuffer(pcm_bytes, dtype=np.int16).copy()
    normed = normalize_ptt(pcm)
    tail = np.zeros(int(SAMPLE_RATE * TAIL_PAD_MS / 1000), dtype=np.int16)
    padded = np.concatenate([normed, tail])
    audio = AudioSegment(
        data=padded.tobytes(),
        sample_width=SAMPLE_WIDTH,
        frame_rate=SAMPLE_RATE,
        channels=CHANNELS,
    )
    buf = io.BytesIO()
    audio.export(buf, format="wav")
    return buf.getvalue()


class PttSegmenter:
    """
    Arms on press(), accumulates mic frames, returns a PCM utterance on release().

    Optional streaming mode (chunk_interval_s > 0) emits mid-hold chunks — unused
    by production PTT but preserved for experiments.
    """

    def __init__(self, chunk_interval_s: float = 0.0):
        self._streaming = chunk_interval_s > 0.0
        self._bytes_per_chunk = int(SAMPLE_RATE * SAMPLE_WIDTH * chunk_interval_s)
        self._recording = False
        self._accumulator: List[bytes] = []
        self._accumulated_bytes = 0
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def press(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._recording = True
            self._accumulator = []
            self._accumulated_bytes = 0
        print("[PTT] recording started")

    def release(self) -> Optional[bytes]:
        with self._lock:
            if not self._recording:
                return None
            self._recording = False
            if not self._accumulator:
                return None
            utterance = b"".join(self._accumulator)
            self._accumulator = []
            self._accumulated_bytes = 0
        print(f"[PTT] utterance release ({len(utterance) // 1024} kB PCM)")
        return utterance

    def push(self, frame: bytes) -> Optional[bytes]:
        chunk = None
        with self._lock:
            if not self._recording:
                return None
            self._accumulator.append(frame)
            self._accumulated_bytes += len(frame)
            if self._streaming and self._accumulated_bytes >= self._bytes_per_chunk:
                chunk = b"".join(self._accumulator)
                self._accumulator = []
                self._accumulated_bytes = 0
        return chunk
