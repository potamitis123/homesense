"""
Shared audio / MQTT constants for the Push-to-Talk and live-audio transports.

These values match the existing BOX-3 chunked WAV play protocol and must not
change without a coordinated firmware update.
"""

# ── PCM capture ──────────────────────────────────────────────────────
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
SAMPLE_WIDTH = 2  # bytes per sample (int16)

FRAME_MS = 30
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)  # 480
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH  # 960

TAIL_PAD_MS = 250

# ── MQTT chunked play protocol (server → Sentinel loudspeaker) ───────
MQTT_CHUNK_SIZE = 4096
MQTT_QOS = 1
MQTT_KEEPALIVE = 60
INTER_CHUNK_DELAY_S = 0.08
POST_INFO_DELAY_S = 0.20
POST_CHUNKS_DELAY_S = 0.50

# Peak normalisation used by production PTT (see audio_details legacy)
PTT_TARGET_PEAK = 1.50


# ── Topic helpers (play protocol — do not rename) ────────────────────

def topic_play_info(device_id: str) -> str:
    return f"commands/{device_id}/play/info"


def topic_play_chunk(device_id: str, n: int) -> str:
    return f"commands/{device_id}/play/chunk/{n}"


def topic_play_complete(device_id: str) -> str:
    return f"commands/{device_id}/play/complete"


def topic_live_control(device_id: str) -> str:
    """
    Single control topic for Live Audio Mode start/stop.

    The firmware (audiosense_mqtt.c) subscribes to exactly one control
    topic per device and dispatches on payload content (looking for the
    substrings "start_live_audio" / "stop_live_audio" in the message
    body) rather than on a per-command topic. There is no
    commands/<id>/start_live_audio or .../stop_live_audio subscription
    on the device side, so both directions must publish here.
    """
    return f"commands/{device_id}/control"


def topic_start_live_audio(device_id: str) -> str:
    """MQTT command that switches a Sentinel into Live Audio Mode."""
    return topic_live_control(device_id)


def topic_stop_live_audio(device_id: str) -> str:
    """MQTT command that restores Event Mode on a Sentinel."""
    return topic_live_control(device_id)


# ── MQTT real-time downlink (server -> Sentinel loudspeaker) ─────────
# Same wire shape as the play/* protocol above, but sent frame-by-frame
# as audio is captured instead of buffered into one WAV. No control
# command is required to arm this path -- the firmware is subscribed to
# commands/<id>/stream/# from boot.

def topic_stream_info(device_id: str) -> str:
    return f"commands/{device_id}/stream/info"


def topic_stream_chunk(device_id: str, n: int) -> str:
    return f"commands/{device_id}/stream/chunk/{n}"


def topic_stream_complete(device_id: str) -> str:
    return f"commands/{device_id}/stream/complete"


# ── Live Audio Mode uplink (Sentinel mic -> server, Opus) ────────────
# Published by the device itself once start_live_audio has been
# accepted; see live_audio.h on the firmware side for the binary bundle
# layout carried on the "opus" topic.

def topic_live_info(device_id: str) -> str:
    return f"audio/recordings/{device_id}/live/info"


def topic_live_opus(device_id: str) -> str:
    return f"audio/recordings/{device_id}/live/opus"


def topic_live_complete(device_id: str) -> str:
    return f"audio/recordings/{device_id}/live/complete"


def topic_live_state(device_id: str) -> str:
    """Retained {"live": true/false} flag published by the Sentinel."""
    return f"devices/{device_id}/live"
