"""Continuous microphone capture that feeds fixed-size PCM frames to a callback."""

from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

import sounddevice as sd

from .constants import CHANNELS, DTYPE, FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE

FrameCallback = Callable[[bytes], None]


class MicSource:
    """
    Owns a sounddevice InputStream and delivers FRAME_BYTES-sized PCM frames.

    Started once and left running (matches legacy BoxStreamer behaviour) so
    Push-to-Talk press/release has no mic open/close latency.
    """

    def __init__(self, on_frame: Optional[FrameCallback] = None):
        self._on_frame = on_frame
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()
        self._running = False

    def set_callback(self, on_frame: Optional[FrameCallback]) -> None:
        self._on_frame = on_frame

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            leftover = b""

            def audio_callback(indata, frames, time_info, status):
                nonlocal leftover
                if status:
                    print(f"[MIC] {status}", file=sys.stderr)
                raw = leftover + indata.tobytes()
                cb = self._on_frame
                while len(raw) >= FRAME_BYTES:
                    frame, raw = raw[:FRAME_BYTES], raw[FRAME_BYTES:]
                    if cb is not None:
                        cb(frame)
                leftover = raw

            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=FRAME_SAMPLES,
                callback=audio_callback,
            )
            self._stream.start()
            self._running = True
            print("[MIC] InputStream started")

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                if self._stream is not None:
                    self._stream.stop()
                    self._stream.close()
            except Exception as e:
                print(f"[MIC] stop error: {e}", file=sys.stderr)
            self._stream = None
            self._running = False
            print("[MIC] InputStream stopped")
