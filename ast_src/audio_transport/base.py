"""
AudioTransport abstraction — transport-agnostic audio session interface.

UI and business logic talk only to PushToTalkController / AudioSessionManager.
Concrete transports (MQTT chunked WAV, future UDP/WebRTC/Opus) implement this
interface so they can be swapped without touching Dash callbacks.
"""

from __future__ import annotations

import abc
import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .constants import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH


class TransportDirection(str, enum.Enum):
    TX = "tx"
    RX = "rx"
    DUPLEX = "duplex"


class SessionMode(str, enum.Enum):
    """How audio is segmented for the transport."""

    PTT = "ptt"  # buffer until release, then send utterance
    CONTINUOUS = "continuous"  # live streaming frames


class AudioCodec(str, enum.Enum):
    """Logical codec identifiers. Wire formats are transport-specific."""

    PCM_WAV = "pcm_wav"  # existing BOX-3 play protocol
    PCM_RAW = "pcm_raw"  # raw int16 frames
    OPUS = "opus"  # future
    G711_ULAW = "g711_ulaw"  # future / WebRTC intercom


@dataclass
class TransportCapabilities:
    """What a concrete transport can do — used for negotiation later."""

    name: str
    directions: Sequence[TransportDirection] = (TransportDirection.TX,)
    codecs: Sequence[AudioCodec] = (AudioCodec.PCM_WAV,)
    modes: Sequence[SessionMode] = (SessionMode.PTT,)
    supports_bitrate_selection: bool = False
    supports_negotiation: bool = False
    duplex: bool = False


@dataclass
class AudioSessionConfig:
    """
    Session parameters shared across transports.

    Designed so future continuous / duplex / codec selection can be expressed
    here without changing the UI layer.
    """

    device_ids: List[str]
    direction: TransportDirection = TransportDirection.TX
    mode: SessionMode = SessionMode.PTT
    codec: AudioCodec = AudioCodec.PCM_WAV
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    sample_width: int = SAMPLE_WIDTH
    bitrate: Optional[int] = None  # bits/s when codec supports it
    metadata: Dict = field(default_factory=dict)

    def copy_with(self, **kwargs) -> "AudioSessionConfig":
        data = {
            "device_ids": list(self.device_ids),
            "direction": self.direction,
            "mode": self.mode,
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_width": self.sample_width,
            "bitrate": self.bitrate,
            "metadata": dict(self.metadata),
        }
        data.update(kwargs)
        return AudioSessionConfig(**data)


@dataclass
class AudioFrame:
    """One unit of audio handed to / from a transport."""

    pcm: bytes
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    sample_width: int = SAMPLE_WIDTH
    timestamp: Optional[float] = None
    sequence: Optional[int] = None
    is_last: bool = False  # end-of-utterance for PTT buffering transports
    codec: AudioCodec = AudioCodec.PCM_RAW
    metadata: Dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp is None:
            self.timestamp = time.time()


# Callback for duplex / inbound frames: (session_id, frame) -> None
ReceiveCallback = Callable[[str, AudioFrame], None]


class AudioTransport(abc.ABC):
    """
    Abstract audio transport.

    Lifecycle
    ---------
    1. connect()            – optional long-lived broker/socket setup
    2. start_session(cfg)   – open a logical session to one or more devices
    3. send_audio_frame(...)– push PCM (or encoded) frames
    4. stop_session()       – tear down the logical session
    5. disconnect()         – release long-lived resources

    Implementations must keep the existing Sentinel MQTT play protocol
    behaviour bit-compatible when representing Push-to-Talk.
    """

    def __init__(self) -> None:
        self._receive_callback: Optional[ReceiveCallback] = None
        self._active_session_id: Optional[str] = None
        self._config: Optional[AudioSessionConfig] = None

    # ── identity / capability ────────────────────────────────────────

    @property
    @abc.abstractmethod
    def name(self) -> str:
        ...

    @property
    @abc.abstractmethod
    def capabilities(self) -> TransportCapabilities:
        ...

    @property
    def active_session_id(self) -> Optional[str]:
        return self._active_session_id

    @property
    def config(self) -> Optional[AudioSessionConfig]:
        return self._config

    # ── connection (long-lived) ──────────────────────────────────────

    def connect(self) -> None:
        """Establish long-lived resources (MQTT client, sockets, etc.)."""

    def disconnect(self) -> None:
        """Release long-lived resources."""

    # ── session lifecycle ────────────────────────────────────────────

    @abc.abstractmethod
    def start_session(self, config: AudioSessionConfig) -> str:
        """
        Begin a transmit / receive / duplex session.

        Returns a session_id that subsequent send_audio_frame / stop_session
        calls may reference.
        """

    @abc.abstractmethod
    def stop_session(self, session_id: Optional[str] = None) -> None:
        """End the session and restore peer devices to their idle mode if needed."""

    @abc.abstractmethod
    def send_audio_frame(
        self,
        frame: AudioFrame,
        session_id: Optional[str] = None,
    ) -> bool:
        """
        Deliver one audio frame on the active (or specified) session.

        For PTT / MQTT chunked play, callers typically send one frame with
        ``is_last=True`` containing a full utterance of PCM.
        For continuous streaming, callers send many frames while the session
        is open.
        """

    # ── duplex / negotiation hooks (future-ready) ────────────────────

    def set_receive_callback(self, callback: Optional[ReceiveCallback]) -> None:
        """Register a handler for inbound frames (duplex / live listen)."""
        self._receive_callback = callback

    def negotiate(self, preferred: AudioSessionConfig) -> AudioSessionConfig:
        """
        Optionally negotiate codec / bitrate / direction with the peer.

        Default: return preferred unchanged. Streaming transports should
        override this when real capability exchange is implemented.
        """
        return preferred

    # ── helpers for subclasses ───────────────────────────────────────

    def _new_session_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _emit_received(self, session_id: str, frame: AudioFrame) -> None:
        cb = self._receive_callback
        if cb is not None:
            cb(session_id, frame)
