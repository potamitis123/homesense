"""
Modular audio transport layer for Sentinel Home Server.

Public API
----------
* AudioTransport / AudioFrame / AudioSessionConfig — abstraction
* MQTTAudioTransport — existing Push-to-Talk MQTT chunked WAV play
* StreamingAudioTransport — Live Audio Mode placeholder (UDP/WebRTC/Opus later)
* PushToTalkController — UI façade (press / release)
* AudioSessionManager — owns sessions and routes inbound audio to sinks

See README.md in this package for extension points.
"""

from .base import (
    AudioCodec,
    AudioFrame,
    AudioSessionConfig,
    AudioTransport,
    SessionMode,
    TransportCapabilities,
    TransportDirection,
)
from .mqtt_transport import MQTTAudioTransport
from .ptt_controller import MODE_MQTT_PLAY, MODE_STREAMING, PushToTalkController
from .session_manager import AudioSessionManager
from .streaming_transport import StreamingAudioTransport

__all__ = [
    "AudioCodec",
    "AudioFrame",
    "AudioSessionConfig",
    "AudioTransport",
    "SessionMode",
    "TransportCapabilities",
    "TransportDirection",
    "MQTTAudioTransport",
    "StreamingAudioTransport",
    "PushToTalkController",
    "AudioSessionManager",
    "MODE_STREAMING",
    "MODE_MQTT_PLAY",
]
