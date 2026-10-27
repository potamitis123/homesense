"""
AudioSessionManager — owns live audio sessions and routes inbound streams.

Responsibilities
----------------
* Construct and hold AudioTransport instances (MQTT PTT, Streaming, …)
* Expose a PushToTalkController to the UI (streaming hold-to-talk by default)
* Register output sinks (Android, desktop speakers, recorder, AI, …)
* Route frames received on duplex / live-listen sessions to those sinks

The UI never sees MQTT, UDP, or WebRTC — only the controller / manager API.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Dict, List, Optional, Sequence

from .base import AudioFrame, AudioSessionConfig, AudioTransport, SessionMode
from .mqtt_transport import MQTTAudioTransport
from .ptt_controller import MODE_MQTT_PLAY, MODE_STREAMING, PushToTalkController
from .speaker_sink import SpeakerSink
from .streaming_transport import StreamingAudioTransport

# Sink callback: (session_id, frame, sink_name) -> None
AudioSink = Callable[[str, AudioFrame, str], None]

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _default_ptt_broker_kwargs() -> dict:
    """
    Broker settings used by production PTT / live audio.

    Kept identical to the former GLOBAL_STREAMER hard-coded values so existing
    Sentinels keep working without firmware or ops changes.
    """
    from misc.secrets import mqtt_password

    return {
        "broker": "audiosense.hmu.gr",
        "port": 8883,
        "username": "hlias",
        "password": mqtt_password(),
        "cafile": os.path.join(_PROJECT_ROOT, "certs/certs/ca.crt"),
        "certfile": os.path.join(_PROJECT_ROOT, "certs/certs/hlias.crt"),
        "keyfile": os.path.join(_PROJECT_ROOT, "certs/certs/hlias.key"),
    }


def _load_ptt_settings() -> dict:
    """Read optional audio_transport section from parameters.json."""
    defaults = {
        "ptt_mode": MODE_STREAMING,
        "fallback_to_mqtt_play": True,
    }
    try:
        from misc.load_parameters import parameters

        params = parameters.get_parameters() or {}
        section = params.get("audio_transport") or {}
        mode = section.get("ptt_mode", defaults["ptt_mode"])
        if mode not in (MODE_STREAMING, MODE_MQTT_PLAY):
            mode = MODE_STREAMING
        defaults["ptt_mode"] = mode
        defaults["fallback_to_mqtt_play"] = bool(
            section.get("fallback_to_mqtt_play", True)
        )
    except Exception as e:
        print(f"[AudioSessionManager] using default PTT settings ({e})")
    return defaults


def _load_speaker_device() -> Optional[object]:
    """
    Optional PortAudio output device (index or name substring) from
    parameters.json's audio_transport.speaker_device. None (the default)
    means "system default output device".
    """
    try:
        from misc.load_parameters import parameters

        params = parameters.get_parameters() or {}
        section = params.get("audio_transport") or {}
        return section.get("speaker_device")
    except Exception:
        return None


class AudioSessionManager:
    """
    Process-wide owner of live audio sessions (singleton).

    Typical use from Dash::

        mgr = AudioSessionManager.get_instance()
        ptt = mgr.get_ptt_controller()
        ptt.press(device_ids)   # start_live_audio + stream
        ptt.release()           # stop_live_audio
    """

    _instance: Optional["AudioSessionManager"] = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        broker_kwargs: Optional[dict] = None,
        ptt_mode: Optional[str] = None,
        fallback_to_mqtt_play: Optional[bool] = None,
        auto_register_speaker_sink: bool = True,
        speaker_device: Optional[object] = None,
    ):
        self._broker_kwargs = broker_kwargs or _default_ptt_broker_kwargs()
        settings = _load_ptt_settings()
        self._ptt_mode = ptt_mode or settings["ptt_mode"]
        self._fallback_to_mqtt_play = (
            settings["fallback_to_mqtt_play"]
            if fallback_to_mqtt_play is None
            else fallback_to_mqtt_play
        )
        self._lock = threading.Lock()

        self._mqtt_transport = MQTTAudioTransport(**self._broker_kwargs)
        self._streaming_transport = StreamingAudioTransport(**self._broker_kwargs)

        self._transports: Dict[str, AudioTransport] = {
            self._mqtt_transport.name: self._mqtt_transport,
            self._streaming_transport.name: self._streaming_transport,
        }

        # session_id -> transport name
        self._sessions: Dict[str, str] = {}
        # sink_name -> callback
        self._sinks: Dict[str, AudioSink] = {}

        self._mqtt_transport.set_receive_callback(self._on_transport_receive)
        self._streaming_transport.set_receive_callback(self._on_transport_receive)

        self._speaker_sink: Optional[SpeakerSink] = None
        if auto_register_speaker_sink:
            device = speaker_device if speaker_device is not None else _load_speaker_device()
            self._speaker_sink = SpeakerSink(device=device)
            self.register_sink("speakers", self._speaker_sink)

        self._ptt = PushToTalkController(
            streaming_transport=self._streaming_transport,
            fallback_transport=self._mqtt_transport,
            auto_start_mic=True,
            ptt_mode=self._ptt_mode,
            fallback_to_mqtt_play=self._fallback_to_mqtt_play,
        )
        # The controller silences this sink while the operator transmits,
        # so their own voice returning from the Sentinel is not played back.
        if self._speaker_sink is not None:
            self._ptt.set_inbound_sink(self._speaker_sink)

        print(
            f"[AudioSessionManager] PTT mode={self._ptt_mode} "
            f"fallback_mqtt_play={self._fallback_to_mqtt_play}"
        )

    # ── singleton ────────────────────────────────────────────────────

    @classmethod
    def get_instance(cls, broker_kwargs: Optional[dict] = None) -> "AudioSessionManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(broker_kwargs=broker_kwargs)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Test helper — tear down the singleton."""
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance.shutdown()
                except Exception:
                    pass
                cls._instance = None

    # ── UI entry points ──────────────────────────────────────────────

    def get_ptt_controller(self) -> PushToTalkController:
        return self._ptt

    def get_transport(self, name: str) -> AudioTransport:
        try:
            return self._transports[name]
        except KeyError as exc:
            raise KeyError(
                f"Unknown transport '{name}'. Available: {list(self._transports)}"
            ) from exc

    # ── session factory ──────────────────────────────────────────────

    def start_session(
        self,
        transport_name: str,
        config: AudioSessionConfig,
    ) -> str:
        """
        Open a session on the named transport and track it for routing.

        Examples
        --------
        * transport_name="mqtt_ptt"   -> classic Push-to-Talk play protocol
        * transport_name="streaming"  -> Live Audio Mode (start_live_audio)
        """
        transport = self.get_transport(transport_name)
        session_id = transport.start_session(config)
        with self._lock:
            self._sessions[session_id] = transport_name
        return session_id

    def stop_session(self, session_id: str) -> None:
        with self._lock:
            transport_name = self._sessions.pop(session_id, None)
        if transport_name is None:
            return
        self.get_transport(transport_name).stop_session(session_id)
        if self._speaker_sink is not None:
            self._speaker_sink.close_session(session_id)

    def start_live_audio(
        self,
        device_ids: Sequence[str],
        config: Optional[AudioSessionConfig] = None,
    ) -> str:
        """
        Convenience: put Sentinels into Live Audio Mode via StreamingAudioTransport.

        Media streaming is not implemented yet; mode-switch MQTT commands are.
        """
        if config is None:
            config = AudioSessionConfig(
                device_ids=list(device_ids),
                mode=SessionMode.CONTINUOUS,
            )
        else:
            config = config.copy_with(
                device_ids=list(device_ids),
                mode=SessionMode.CONTINUOUS,
            )
        return self.start_session("streaming", config)

    def stop_live_audio(self, session_id: str) -> None:
        """Exit Live Audio Mode for a previously started streaming session."""
        self.stop_session(session_id)

    # ── output routing ───────────────────────────────────────────────

    def register_sink(self, name: str, sink: AudioSink) -> None:
        """
        Register an output destination for inbound live audio.

        Intended sinks: ``android``, ``speakers``, ``recorder``, ``ai``, …
        """
        with self._lock:
            self._sinks[name] = sink
        print(f"[AudioSessionManager] registered sink '{name}'")

    def unregister_sink(self, name: str) -> None:
        with self._lock:
            self._sinks.pop(name, None)

    def list_sinks(self) -> List[str]:
        with self._lock:
            return list(self._sinks.keys())

    def list_sessions(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._sessions)

    def _on_transport_receive(self, session_id: str, frame: AudioFrame) -> None:
        with self._lock:
            sinks = list(self._sinks.items())
        if not sinks:
            return
        for name, sink in sinks:
            try:
                sink(session_id, frame, name)
            except Exception as e:
                print(f"[AudioSessionManager] sink '{name}' error: {e}")

    # ── lifecycle ────────────────────────────────────────────────────

    def shutdown(self) -> None:
        self._ptt.stop()
        for name, transport in list(self._transports.items()):
            try:
                if transport.active_session_id:
                    transport.stop_session(transport.active_session_id)
                transport.disconnect()
            except Exception as e:
                print(f"[AudioSessionManager] shutdown {name}: {e}")
        if self._speaker_sink is not None:
            self._speaker_sink.close()
        with self._lock:
            self._sessions.clear()
