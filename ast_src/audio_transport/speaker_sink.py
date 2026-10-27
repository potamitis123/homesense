"""
SpeakerSink — plays inbound live-audio frames on the local machine's
default (or configured) output device.

This is what AudioSessionManager registers as the "speakers" sink. Without
it, frames unpacked by StreamingAudioTransport's live/opus handling reach
_emit_received() and stop there — routed to zero sinks, so nothing plays.
That's the actual gap: RX was working (subscribe + unpack were correct),
there was just nothing listening on the other end of register_sink().

Two dependencies, both chosen because they ship prebuilt native binaries on
Windows/macOS/Linux via pip and need no separate SDK install:

  * pyogg        – ctypes wrapper around libopus, bundles the DLL/so/dylib
  * sounddevice  – ctypes wrapper around PortAudio, bundles the DLL/so/dylib

    pip install pyogg sounddevice

NOTE on pyogg: the streaming ``pyogg.OpusDecoder`` class documented on
PyOgg's GitHub is NOT part of the published PyPI wheel — only file-based
``OpusFile``/``OpusFileStream`` and the raw ctypes bindings under
``pyogg.opus`` (``opus_decoder_create`` / ``opus_decode`` /
``opus_decoder_destroy``) are actually importable from ``pip install
pyogg``. This module drives those raw bindings directly via the small
_OpusDecoderLL wrapper below instead of depending on the class that isn't
there.

If a dependency is missing or the native decode symbols aren't available,
the sink logs one clear warning (not one per frame) and drops audio
instead of raising out of the receive callback.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Dict, Optional, Tuple

from .base import AudioCodec, AudioFrame

try:
    import sounddevice as sd
except Exception as _e:  # pragma: no cover - environment dependent
    sd = None
    _SD_IMPORT_ERROR: Optional[Exception] = _e
else:
    _SD_IMPORT_ERROR = None

try:
    from pyogg import opus as _opus
except Exception as _e:  # pragma: no cover - environment dependent
    _opus = None
    _OPUS_IMPORT_ERROR: Optional[Exception] = _e
else:
    _OPUS_IMPORT_ERROR = None

# The raw decode symbols only exist in pyogg.opus when it found both
# libopus AND libopusfile at import time (see pyogg/opus.py) -- on the
# Windows wheel both are bundled together so this is normally just True,
# but check explicitly rather than assume.
_OPUS_DECODE_AVAILABLE = bool(
    _opus is not None
    and getattr(_opus, "PYOGG_OPUS_AVAIL", False)
    and hasattr(_opus, "opus_decoder_create")
    and hasattr(_opus, "opus_decode")
    and hasattr(_opus, "opus_decoder_destroy")
)
if _opus is not None and not _OPUS_DECODE_AVAILABLE and _OPUS_IMPORT_ERROR is None:
    _OPUS_IMPORT_ERROR = RuntimeError(
        "pyogg.opus imported but decode symbols are missing "
        "(libopus and/or libopusfile not found by pyogg's loader)"
    )


class _OpusDecoderLL:
    """
    Minimal wrapper around pyogg's low-level ctypes Opus bindings, giving
    them the decode(bytes) -> bytes shape the rest of this file expects.

    One instance holds one native libopus decoder handle. That handle
    carries packet-loss-concealment state across calls, so it must be
    reused across frames of the same stream and explicitly destroyed
    (close()) when the stream ends -- ctypes will not do that for us.
    """

    # Largest Opus frame is 120 ms; size the output buffer for that
    # regardless of the 20 ms frames the firmware actually sends (see
    # live_audio.h), so a future frame-duration change can't overflow it.
    _MAX_FRAME_MS = 120

    def __init__(self, sample_rate: int, channels: int):
        self._channels = channels
        self._max_samples = int(sample_rate * self._MAX_FRAME_MS / 1000)

        error = ctypes.c_int(0)
        self._dec = _opus.opus_decoder_create(sample_rate, channels, ctypes.byref(error))
        if not self._dec or error.value != 0:
            raise RuntimeError(f"opus_decoder_create failed (err={error.value})")

    def decode(self, opus_bytes: bytes) -> bytes:
        data_buf = (ctypes.c_ubyte * len(opus_bytes)).from_buffer_copy(opus_bytes)
        pcm_buf = (ctypes.c_int16 * (self._max_samples * self._channels))()

        n_samples = _opus.opus_decode(
            self._dec, data_buf, len(opus_bytes), pcm_buf, self._max_samples, 0
        )
        if n_samples < 0:
            raise RuntimeError(f"opus_decode failed (err={n_samples})")

        n_bytes = n_samples * self._channels * ctypes.sizeof(ctypes.c_int16)
        return ctypes.string_at(pcm_buf, n_bytes)

    def close(self) -> None:
        if self._dec:
            _opus.opus_decoder_destroy(self._dec)
            self._dec = None

    def __del__(self) -> None:  # best-effort native cleanup
        try:
            self.close()
        except Exception:
            pass


class SpeakerSink:
    """
    Callable sink: ``speaker_sink(session_id, frame, sink_name)``.

    * PCM_RAW / PCM_WAV frames are written straight through.
    * OPUS frames are decoded first via _OpusDecoderLL. libopus decoder
      state (packet-loss concealment history) is per-stream, so one
      decoder is kept per session_id rather than one global decoder.
    * One PortAudio output stream is kept per (sample_rate, channels)
      combination actually seen — in practice just one, since the
      firmware only ever sends 16 kHz mono.
    """

    def __init__(self, device: Optional[object] = None):
        """
        device: PortAudio output device index or name substring, or None
        for the system default output (what "the laptop's speakers"
        normally resolves to). Pass a specific device if the wrong output
        is being selected, e.g. an idle Bluetooth headset.
        """
        self._device = device
        self._lock = threading.Lock()
        self._decoders: Dict[str, _OpusDecoderLL] = {}
        self._decoder_shape: Dict[str, Tuple[int, int]] = {}
        self._streams: Dict[Tuple[int, int], "sd.RawOutputStream"] = {}
        self._warned_missing = False
        # Set while the operator is transmitting. Inbound audio is dropped
        # rather than played, so their own voice coming back out of the
        # Sentinel's loudspeaker is not fed to their headphones as an echo.
        self._muted = threading.Event()

    def mute(self) -> None:
        """Stop playing inbound audio (called while transmitting)."""
        self._muted.set()

    def unmute(self) -> None:
        self._muted.clear()

    @property
    def is_muted(self) -> bool:
        return self._muted.is_set()

    def __call__(self, session_id: str, frame: AudioFrame, sink_name: str) -> None:
        if self._muted.is_set():
            return
        if sd is None or not _OPUS_DECODE_AVAILABLE:
            self._warn_missing_deps()
            return

        pcm = self._to_pcm(session_id, frame)
        if not pcm:
            return

        stream = self._stream_for(frame.sample_rate, frame.channels)
        if stream is None:
            return
        try:
            stream.write(pcm)
        except Exception as e:
            print(f"[SpeakerSink] playback write failed: {e}", file=sys.stderr)

    # ── decode ────────────────────────────────────────────────────────

    def _to_pcm(self, session_id: str, frame: AudioFrame) -> Optional[bytes]:
        if frame.codec in (AudioCodec.PCM_RAW, AudioCodec.PCM_WAV):
            return bytes(frame.pcm)

        if frame.codec != AudioCodec.OPUS:
            print(f"[SpeakerSink] no decoder for codec={frame.codec.value}, dropped", file=sys.stderr)
            return None

        decoder = self._decoder_for(session_id, frame.sample_rate, frame.channels)
        if decoder is None:
            return None
        try:
            return decoder.decode(bytes(frame.pcm))
        except Exception as e:
            print(f"[SpeakerSink] opus decode failed (session={session_id}): {e}", file=sys.stderr)
            return None

    def _decoder_for(self, session_id: str, sample_rate: int, channels: int) -> Optional[_OpusDecoderLL]:
        shape = (sample_rate, channels)
        with self._lock:
            if session_id not in self._decoders:
                # Only one live RX session is realistically active at a
                # time in this app. PushToTalkController calls
                # start_session()/stop_session() directly on the
                # transport (see ptt_controller.py), bypassing
                # AudioSessionManager's own session bookkeeping, so there
                # is no reliable explicit "session ended" signal reaching
                # this sink for that path. Rather than leak one native
                # decoder per PTT press, close and drop everything but
                # the session that just showed up -- self-bounding
                # instead of depending on a close_session() call that
                # may never come.
                for stale in self._decoders.values():
                    stale.close()
                self._decoders.clear()
                self._decoder_shape.clear()
            if self._decoder_shape.get(session_id) != shape:
                try:
                    decoder = _OpusDecoderLL(sample_rate, channels)
                except Exception as e:
                    print(f"[SpeakerSink] could not create opus decoder: {e}", file=sys.stderr)
                    return None
                self._decoders[session_id] = decoder
                self._decoder_shape[session_id] = shape
            return self._decoders[session_id]

    # ── playback ─────────────────────────────────────────────────────

    def _stream_for(self, sample_rate: int, channels: int):
        key = (sample_rate, channels)
        with self._lock:
            stream = self._streams.get(key)
            if stream is not None:
                return stream
            try:
                stream = sd.RawOutputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="int16",
                    device=self._device,
                )
                stream.start()
            except Exception as e:
                print(
                    f"[SpeakerSink] could not open output device "
                    f"(sr={sample_rate} ch={channels} device={self._device!r}): {e}",
                    file=sys.stderr,
                )
                return None
            self._streams[key] = stream
            print(
                f"[SpeakerSink] opened output stream sr={sample_rate} ch={channels} "
                f"device={self._device if self._device is not None else 'default'}"
            )
            return stream

    # ── lifecycle ────────────────────────────────────────────────────

    def close_session(self, session_id: str) -> None:
        """Destroy and drop the per-session Opus decoder once a session ends."""
        with self._lock:
            decoder = self._decoders.pop(session_id, None)
            self._decoder_shape.pop(session_id, None)
        if decoder is not None:
            decoder.close()

    def close(self) -> None:
        """Stop and release all output streams and native decoders (call on app shutdown)."""
        with self._lock:
            for stream in self._streams.values():
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            self._streams.clear()
            for decoder in self._decoders.values():
                decoder.close()
            self._decoders.clear()
            self._decoder_shape.clear()

    def _warn_missing_deps(self) -> None:
        if self._warned_missing:
            return
        self._warned_missing = True
        missing = []
        if sd is None:
            missing.append(f"sounddevice ({_SD_IMPORT_ERROR})")
        if not _OPUS_DECODE_AVAILABLE:
            missing.append(f"pyogg opus decode ({_OPUS_IMPORT_ERROR})")
        print(
            "[SpeakerSink] cannot play received audio, missing dependency: "
            + "; ".join(missing)
            + " -- run: pip install sounddevice pyogg",
            file=sys.stderr,
        )
