"""
Push-to-Talk / duplex audio session management for the mobile API.

Reuses the existing duplex audio streaming implementation directly --
StreamingAudioTransport (ast_src/audio_transport/streaming_transport.py)
is the same class PushToTalkController drives for the desktop dashboard's
PTT. No MQTT topics, wire formats, or protocol logic are reimplemented
here; this module only does session bookkeeping and moves bytes between
a WebSocket and that transport, mirroring exactly what
PushToTalkController._on_mic_frame / _try_start_streaming /
_release_streaming already do for the desktop mic.

Why a fresh StreamingAudioTransport per session rather than reusing the
Dash process's AudioSessionManager singleton: AudioSessionManager lives
inside the Dash process. mobile_api is a separate OS process (required by
the ASGI/WSGI split -- see mobile_api/README.md) and cannot call into a
live object in a different process. Instantiating the same class here
reuses the real implementation; it does not reimplement it.

Known v1 limitation: a simultaneous desktop-PTT-press and a mobile
session on the same device are not arbitrated against each other. Not
solved in this pass -- acceptable for a personal/family deployment.

Opus decode happens here, server-side, before forwarding to the phone:
reuses the exact decoder class already proven for desktop playback
(_OpusDecoderLL in ast_src/audio_transport/speaker_sink.py, a small
ctypes wrapper around pyogg's raw libopus bindings) rather than requiring
a native/JNI Opus dependency in the Android client. Trades ~10x more
WebSocket bandwidth during a call (raw PCM16 vs Opus) for a much simpler,
more reliable mobile client -- reasonable for short PTT conversations.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from ast_src.audio_transport.base import (
    AudioCodec,
    AudioFrame,
    AudioSessionConfig,
    SessionMode,
    TransportDirection,
)
from ast_src.audio_transport.constants import FRAME_BYTES
from ast_src.audio_transport.session_manager import _default_ptt_broker_kwargs
from ast_src.audio_transport.streaming_transport import StreamingAudioTransport
from ast_src.audio_transport.speaker_sink import _OPUS_DECODE_AVAILABLE, _OpusDecoderLL

from mobile_api.logging_setup import get_logger, log_extra

logger = get_logger()

# Callback invoked with decoded PCM16 bytes received from the Sentinel, to
# be forwarded to the phone over its WebSocket. Set by the route layer.
PcmSinkCallback = Callable[[bytes], None]

_warned_missing_opus = False


@dataclass
class _AudioSession:
    session_id: str
    device_id: str
    transport: StreamingAudioTransport
    transport_session_id: str
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    leftover: bytes = b""
    ws_sink: Optional[PcmSinkCallback] = None
    opus_decoder: Optional[_OpusDecoderLL] = None


class AudioSessionService:
    def __init__(self, timeout_minutes: int = 5):
        self._timeout_seconds = timeout_minutes * 60
        self._sessions: dict[str, _AudioSession] = {}
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────

    def create_session(self, device_id: str) -> str:
        transport = StreamingAudioTransport(**_default_ptt_broker_kwargs())
        transport.connect()
        if not transport._connected.wait(timeout=10):
            transport.disconnect()
            raise RuntimeError(f"Could not reach MQTT broker for device '{device_id}'")

        config = AudioSessionConfig(
            device_ids=[device_id],
            direction=TransportDirection.DUPLEX,
            mode=SessionMode.CONTINUOUS,
            codec=AudioCodec.OPUS,
        )
        transport_session_id = transport.start_session(config)

        session_id = uuid.uuid4().hex[:12]
        session = _AudioSession(
            session_id=session_id,
            device_id=device_id,
            transport=transport,
            transport_session_id=transport_session_id,
        )
        transport.set_receive_callback(self._make_receive_callback(session_id))

        with self._lock:
            self._sessions[session_id] = session

        logger.info(f"session {session_id} opened -> device={device_id}", extra=log_extra("MOBILE_AUDIO"))
        return session_id

    def attach_ws_sink(self, session_id: str, sink: PcmSinkCallback) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.ws_sink = sink
            return True

    def feed_mic_bytes(self, session_id: str, data: bytes) -> bool:
        """Chunk incoming raw PCM into FRAME_BYTES pieces and forward each
        one, mirroring mic_source.MicSource's leftover-buffering pattern
        and ptt_controller.PushToTalkController._on_mic_frame's send."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.last_activity = time.time()
            raw = session.leftover + data
            transport = session.transport
            transport_session_id = session.transport_session_id
            frames = []
            while len(raw) >= FRAME_BYTES:
                frames.append(raw[:FRAME_BYTES])
                raw = raw[FRAME_BYTES:]
            session.leftover = raw

        for frame in frames:
            transport.send_audio_frame(
                AudioFrame(pcm=frame, is_last=False, codec=AudioCodec.PCM_RAW),
                transport_session_id,
            )
        return True

    def stop_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            session.transport.stop_session(session.transport_session_id)
            session.transport.disconnect()
        except Exception as e:
            logger.error(f"error stopping session {session_id}: {e}", extra=log_extra("MOBILE_AUDIO"))
        if session.opus_decoder is not None:
            session.opus_decoder.close()
        logger.info(f"session {session_id} closed", extra=log_extra("MOBILE_AUDIO"))

    def sweep_idle_sessions(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                sid for sid, s in self._sessions.items()
                if now - s.last_activity > self._timeout_seconds
            ]
        for sid in stale:
            logger.info(f"session {sid} idle-timeout, closing", extra=log_extra("MOBILE_AUDIO"))
            self.stop_session(sid)

    def session_exists(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    # ── internals ────────────────────────────────────────────────────

    def _make_receive_callback(self, session_id: str):
        def _on_receive(_transport_session_id: str, frame: AudioFrame) -> None:
            global _warned_missing_opus
            if not frame.pcm:
                return
            if not _OPUS_DECODE_AVAILABLE:
                if not _warned_missing_opus:
                    _warned_missing_opus = True
                    logger.error(
                        "pyogg opus decode unavailable -- Sentinel audio cannot be forwarded to mobile clients",
                        extra=log_extra("MOBILE_AUDIO"),
                    )
                return

            with self._lock:
                session = self._sessions.get(session_id)
                if session is None:
                    return
                session.last_activity = time.time()
                if session.opus_decoder is None:
                    try:
                        session.opus_decoder = _OpusDecoderLL(frame.sample_rate, frame.channels)
                    except Exception as e:
                        logger.error(f"session {session_id} could not create opus decoder: {e}", extra=log_extra("MOBILE_AUDIO"))
                        return
                decoder = session.opus_decoder
                sink = session.ws_sink

            try:
                pcm = decoder.decode(bytes(frame.pcm))
            except Exception as e:
                logger.error(f"session {session_id} opus decode failed: {e}", extra=log_extra("MOBILE_AUDIO"))
                return

            if sink is not None and pcm:
                try:
                    sink(pcm)
                except Exception as e:
                    logger.error(f"session {session_id} ws sink error: {e}", extra=log_extra("MOBILE_WS"))
        return _on_receive


# Populated by mobile_api.main at startup from MobileApiConfig.
audio_session_service: Optional[AudioSessionService] = None
