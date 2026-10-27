"""
PushToTalkController — UI-facing façade over AudioTransport.

Default hold-to-talk path (streaming)::

    press   → start_live_audio + open stream + forward mic frames
    release → stop_live_audio  (+ close stream)

Fallback path (legacy MQTT chunked WAV play)::

    press   → arm utterance buffer
    release → encode WAV → commands/{id}/play/{info,chunk,complete}

Fallback is used when ``ptt_mode="mqtt_play"``, or automatically when
streaming fails to start and ``fallback_to_mqtt_play`` is True.
"""

from __future__ import annotations

import threading
from typing import List, Optional, Sequence

from .base import (
    AudioCodec,
    AudioFrame,
    AudioSessionConfig,
    AudioTransport,
    SessionMode,
    TransportDirection,
)
from .mic_source import MicSource
from .pcm_utils import PttSegmenter

# Preferred hold-to-talk behaviour
MODE_STREAMING = "streaming"
MODE_MQTT_PLAY = "mqtt_play"


class PushToTalkController:
    """
    Hold-to-talk orchestration.  The Dash layer only calls press() / release().
    """

    def __init__(
        self,
        streaming_transport: Optional[AudioTransport] = None,
        fallback_transport: Optional[AudioTransport] = None,
        mic: Optional[MicSource] = None,
        auto_start_mic: bool = True,
        ptt_mode: str = MODE_STREAMING,
        fallback_to_mqtt_play: bool = True,
        # Backward-compat alias for a single transport (forces mqtt_play)
        transport: Optional[AudioTransport] = None,
    ):
        if transport is not None and streaming_transport is None:
            streaming_transport = transport
            fallback_transport = fallback_transport or transport
            ptt_mode = MODE_MQTT_PLAY
            fallback_to_mqtt_play = False

        if streaming_transport is None:
            raise ValueError("streaming_transport is required")

        self._streaming = streaming_transport
        self._fallback = fallback_transport
        self._preferred_mode = (
            MODE_MQTT_PLAY if ptt_mode == MODE_MQTT_PLAY else MODE_STREAMING
        )
        self._fallback_enabled = bool(fallback_to_mqtt_play and fallback_transport)

        self._seg = PttSegmenter(chunk_interval_s=0.0)
        self._mic = mic or MicSource()
        self._mic.set_callback(self._on_mic_frame)
        self._device_ids: List[str] = []
        self._session_id: Optional[str] = None
        self._active_mode: Optional[str] = None  # mode used for current hold
        self._live_streaming = False  # forward mic frames while held
        self._started = False
        self._lock = threading.Lock()
        self._last_status = "idle"
        self._frames_forwarded = 0
        # An open channel outlives a single press. The session (and its
        # uplink subscription) stays up between presses, so the operator
        # keeps hearing the room while NOT talking -- which is the whole
        # point of an intercom, and the opposite of what press-to-open did.
        self._channel_open = False
        self._inbound_sink = None

        if auto_start_mic:
            self.start()

    # ── channel (open across many presses) ───────────────────────────

    def set_inbound_sink(self, sink) -> None:
        """Sink to silence while transmitting (normally the SpeakerSink)."""
        self._inbound_sink = sink

    @property
    def channel_open(self) -> bool:
        return self._channel_open

    def open_channel(self, device_ids: Sequence[str]) -> str:
        """Open a persistent duplex channel and start listening.

        Returns the mode actually established.
        """
        if not self._started:
            self.start()
        self.set_targets(device_ids)
        if not self._device_ids:
            raise ValueError("No target devices selected")

        with self._lock:
            if self._channel_open:
                return self._active_mode or MODE_STREAMING
            mode = self._try_start_streaming()
            if mode is None:
                raise RuntimeError("Could not open the live audio channel")
            # The session is up, but nothing is being transmitted yet:
            # listening is the default state of an open channel.
            self._live_streaming = False
            self._seg.release()
            self._channel_open = True
            self._active_mode = None
            self._unmute_inbound()
            self._last_status = f"listening to {', '.join(self._device_ids)}"
            print(f"[PTT] channel OPEN -> {self._device_ids}", flush=True)
            return mode

    def close_channel(self) -> None:
        with self._lock:
            if not self._channel_open:
                return
            self._channel_open = False
            self._live_streaming = False
            self._active_mode = None
            self._seg.release()
            self._unmute_inbound()
            if self._session_id is not None:
                try:
                    self._streaming.stop_session(self._session_id)
                except Exception as e:
                    print(f"[PTT] closing channel: {e}", flush=True)
            self._session_id = None
            self._last_status = "idle"
            print("[PTT] channel CLOSED", flush=True)

    def _mute_inbound(self) -> None:
        if self._inbound_sink is not None and hasattr(self._inbound_sink, "mute"):
            self._inbound_sink.mute()

    def _unmute_inbound(self) -> None:
        if self._inbound_sink is not None and hasattr(self._inbound_sink, "unmute"):
            self._inbound_sink.unmute()

    # ── public API used by the UI ────────────────────────────────────

    @property
    def transport(self) -> AudioTransport:
        """Active transport for the current (or preferred) mode."""
        if self._active_mode == MODE_MQTT_PLAY and self._fallback is not None:
            return self._fallback
        return self._streaming

    @property
    def preferred_mode(self) -> str:
        return self._preferred_mode

    @property
    def active_mode(self) -> Optional[str]:
        return self._active_mode

    @property
    def last_status(self) -> str:
        return self._last_status

    @property
    def device_ids(self) -> List[str]:
        return list(self._device_ids)

    def set_mode(self, mode: str) -> None:
        """Switch preferred mode: ``streaming`` or ``mqtt_play``."""
        if mode not in (MODE_STREAMING, MODE_MQTT_PLAY):
            raise ValueError(f"Unknown PTT mode: {mode}")
        self._preferred_mode = mode
        print(f"[PTT] preferred mode -> {mode}")

    def start(self) -> None:
        """Connect transports + start mic (idempotent)."""
        if self._started:
            return
        self._streaming.connect()
        if self._fallback is not None and self._fallback is not self._streaming:
            self._fallback.connect()
        self._mic.start()
        self._started = True

    def stop(self) -> None:
        with self._lock:
            if self._session_id is not None and self._active_mode is not None:
                self._stop_active_session()
        self._mic.stop()
        self._streaming.disconnect()
        if self._fallback is not None and self._fallback is not self._streaming:
            self._fallback.disconnect()
        self._started = False

    def set_targets(self, device_ids: Sequence[str]) -> None:
        ids = list(device_ids)
        if sorted(self._device_ids) != sorted(ids):
            print(f"[PTT] Switching target devices to: {ids}")
        self._device_ids = ids
        for t in (self._streaming, self._fallback):
            if t is not None and hasattr(t, "device_ids"):
                t.device_ids = ids

    def press(self, device_ids: Optional[Sequence[str]] = None) -> str:
        """
        Begin hold-to-talk.

        Returns the mode actually used: ``streaming`` or ``mqtt_play``.
        """
        if not self._started:
            self.start()
        if device_ids is not None:
            self.set_targets(device_ids)
        if not self._device_ids:
            raise ValueError("No target devices selected for Push-to-Talk")

        with self._lock:
            if self._active_mode is not None:
                # Nested press — ignore
                return self._active_mode

            self._frames_forwarded = 0

            if self._channel_open:
                # Channel already up: talking is just un-gating the mic and
                # silencing what comes back, so we do not hear our own voice
                # returning from the Sentinel's loudspeaker.
                self._mute_inbound()
                self._live_streaming = True
                self._seg.press()
                self._active_mode = MODE_STREAMING
                self._last_status = f"talking -> {', '.join(self._device_ids)}"
                return MODE_STREAMING

            if self._preferred_mode == MODE_STREAMING:
                mode = self._try_start_streaming()
                if mode is None and self._fallback_enabled:
                    print("[PTT] streaming failed — falling back to MQTT play")
                    mode = self._start_mqtt_play()
                elif mode is None:
                    raise RuntimeError(
                        "Failed to start live audio stream and MQTT play fallback is disabled"
                    )
            else:
                mode = self._start_mqtt_play()

            self._active_mode = mode
            self._last_status = (
                f"streaming -> {', '.join(self._device_ids)}"
                if mode == MODE_STREAMING
                else f"mqtt_play recording -> {', '.join(self._device_ids)}"
            )
            return mode

    def release(self) -> bool:
        """
        End hold-to-talk.

        Streaming: publishes stop_live_audio and closes the session.
        MQTT play: sends the buffered utterance via the legacy play protocol.

        Returns True if work was performed (stream closed or utterance queued).
        """
        with self._lock:
            mode = self._active_mode
            if mode is None:
                self._last_status = "idle"
                return False

            if self._channel_open:
                # Stop transmitting, keep the channel and start listening
                # again. The session deliberately stays open.
                self._live_streaming = False
                self._seg.release()
                self._active_mode = None
                self._unmute_inbound()
                self._last_status = f"listening to {', '.join(self._device_ids)}"
                return True

            did_work = False
            try:
                if mode == MODE_STREAMING:
                    did_work = self._release_streaming()
                else:
                    did_work = self._release_mqtt_play()
            finally:
                self._live_streaming = False
                self._session_id = None
                self._active_mode = None

            if mode == MODE_STREAMING:
                self._last_status = (
                    f"stream closed ({self._frames_forwarded} frames) -> "
                    f"{', '.join(self._device_ids)}"
                )
            else:
                self._last_status = (
                    f"sent via MQTT play -> {', '.join(self._device_ids)}"
                    if did_work
                    else f"no audio -> {', '.join(self._device_ids)}"
                )
            return did_work

    # ── streaming path ───────────────────────────────────────────────

    def _try_start_streaming(self) -> Optional[str]:
        config = AudioSessionConfig(
            device_ids=list(self._device_ids),
            direction=TransportDirection.DUPLEX,
            mode=SessionMode.CONTINUOUS,
            codec=AudioCodec.OPUS,
        )
        try:
            if not self._streaming_ready():
                self._streaming.connect()
            if not self._streaming_ready():
                print("[PTT] streaming control plane not connected", flush=True)
                return None
            self._session_id = self._streaming.start_session(config)
            self._live_streaming = True
            # Also arm segmenter so a silent buffer exists if we ever need it
            self._seg.press()
            print(
                f"[PTT] LIVE stream open session={self._session_id} "
                f"devices={self._device_ids}",
                flush=True,
            )
            return MODE_STREAMING
        except Exception as e:
            print(f"[PTT] start_live_audio failed: {e}", flush=True)
            self._live_streaming = False
            self._session_id = None
            return None

    def _release_streaming(self) -> bool:
        sid = self._session_id
        # Discard any buffered PCM — streaming path does not use play protocol
        self._seg.release()
        self._live_streaming = False
        if sid is not None:
            self._streaming.stop_session(sid)
            print(
                f"[PTT] LIVE stream closed session={sid} "
                f"frames_fwd={self._frames_forwarded}",
                flush=True,
            )
            return True
        return False

    def _streaming_ready(self) -> bool:
        connected = getattr(self._streaming, "_connected", None)
        if connected is not None and hasattr(connected, "is_set"):
            return bool(connected.is_set())
        # Transports without an Event still attempt start_session
        return True

    # ── legacy MQTT play path ────────────────────────────────────────

    def _start_mqtt_play(self) -> str:
        if self._fallback is None:
            raise RuntimeError("MQTT play fallback transport is not configured")
        config = AudioSessionConfig(
            device_ids=list(self._device_ids),
            direction=TransportDirection.TX,
            mode=SessionMode.PTT,
            codec=AudioCodec.PCM_WAV,
        )
        self._session_id = self._fallback.start_session(config)
        self._live_streaming = False
        self._seg.press()
        print(
            f"[PTT] MQTT play session={self._session_id} devices={self._device_ids}",
            flush=True,
        )
        return MODE_MQTT_PLAY

    def _release_mqtt_play(self) -> bool:
        pcm = self._seg.release()
        sent = False
        if pcm and self._fallback is not None:
            frame = AudioFrame(
                pcm=pcm,
                is_last=True,
                codec=AudioCodec.PCM_RAW,
                metadata={"source": "PTT"},
            )
            sent = bool(self._fallback.send_audio_frame(frame, self._session_id))
        if self._session_id is not None and self._fallback is not None:
            self._fallback.stop_session(self._session_id)
        return sent

    def _stop_active_session(self) -> None:
        if self._active_mode == MODE_STREAMING and self._session_id:
            self._streaming.stop_session(self._session_id)
        elif self._active_mode == MODE_MQTT_PLAY and self._session_id and self._fallback:
            self._fallback.stop_session(self._session_id)
        self._live_streaming = False
        self._session_id = None
        self._active_mode = None

    # ── mic → transport ──────────────────────────────────────────────

    def _on_mic_frame(self, frame: bytes) -> None:
        if self._live_streaming and self._session_id is not None:
            ok = self._streaming.send_audio_frame(
                AudioFrame(
                    pcm=frame,
                    is_last=False,
                    codec=AudioCodec.PCM_RAW,
                    metadata={"source": "PTT_LIVE"},
                ),
                self._session_id,
            )
            if ok:
                self._frames_forwarded += 1
            # Keep segmenter fed so release() can discard cleanly
            self._seg.push(frame)
            return

        # Legacy / buffering path
        live_chunk = self._seg.push(frame)
        if live_chunk is not None and self._fallback is not None:
            self._fallback.send_audio_frame(
                AudioFrame(pcm=live_chunk, is_last=False, metadata={"source": "PTT"})
            )
