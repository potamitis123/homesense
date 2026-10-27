"""Choosing how the companion reaches a device.

The conversation does not care what carries the audio. Everything above
`SentinelAudioIO` deals in "give me the next utterance" and "say this",
and this module is the only place that knows which implementation of
that a given device needs.

Two carriers exist, and they are not variations of one design -- they
disagree about who owns turn-taking:

  mqtt    The BOX-3 path. The device decides when to record, using its
          own sound trigger, and delimits the utterance itself with
          `stream/complete`. Endpointing is the firmware's.

  webrtc  The P4 path. A continuous duplex stream with no utterance
          boundaries in it at all, so endpointing becomes ours.

That difference is why this is a choice rather than a setting. Adding a
device means adding an adapter here; it does not mean touching the
graph, the nodes, or the other adapter.
"""

from __future__ import annotations

import json
import logging
import sys

from ..config import SENTINEL_ROOT

# The Sentinel checkout supplies the broker credentials and the MQTT
# transport. sentinel.py does this too; both are import-order dependent
# and neither may assume the other ran first.
if str(SENTINEL_ROOT) not in sys.path:
    sys.path.insert(0, str(SENTINEL_ROOT))

log = logging.getLogger(__name__)

# What the device publishes about itself, retained, at boot.
CAPABILITY_TOPIC = "devices/{device}/capabilities"
CAPABILITY_TIMEOUT = 3.0

MQTT = "mqtt"
WEBRTC = "webrtc"
AUTO = "auto"


def probe_capabilities(device_id: str, timeout: float = CAPABILITY_TIMEOUT) -> dict:
    """Read a device's retained capability announcement.

    Returns {} when the device says nothing, which is the normal answer
    for a BOX-3: that firmware predates the capability topic, and its
    silence is what identifies it.

    Never raises. A broker that is unreachable is a problem for the
    session that follows, and it will report it far better than a
    capability probe can.
    """
    import ssl
    import threading

    try:
        import paho.mqtt.client as mqtt_client

        from ast_src.audio_transport.session_manager import (
            _default_ptt_broker_kwargs,
        )
    except Exception:
        log.debug("capability probe unavailable", exc_info=True)
        return {}

    topic = CAPABILITY_TOPIC.format(device=device_id)
    got: dict = {}
    done = threading.Event()

    def on_connect(client, _u, _f, _rc):
        client.subscribe(topic, qos=0)

    def on_message(_c, _u, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if isinstance(payload, dict):
                got.update(payload)
        except Exception:
            log.debug("unparsable capabilities from %s: %r", device_id,
                      msg.payload[:120])
        done.set()

    client = None
    try:
        kw = _default_ptt_broker_kwargs()
        client = mqtt_client.Client(client_id=f"companion-probe-{device_id}")
        client.username_pw_set(kw.get("username", ""), kw.get("password", ""))
        if kw.get("cafile"):
            client.tls_set(ca_certs=kw["cafile"], certfile=kw.get("certfile"),
                           keyfile=kw.get("keyfile"), cert_reqs=ssl.CERT_REQUIRED)
            client.tls_insecure_set(True)
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(kw["broker"], kw["port"], 30)
        client.loop_start()
        done.wait(timeout)
    except Exception:
        log.debug("capability probe failed for %s", device_id, exc_info=True)
    finally:
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    if got:
        log.info("%s announces %s", device_id,
                 ", ".join(sorted(k for k, v in got.items() if v)) or "nothing")
    else:
        log.info("%s announced no capabilities (expected for a BOX-3)", device_id)
    return got


def choose_transport(device_id: str, requested: str = AUTO,
                     timeout: float = CAPABILITY_TIMEOUT) -> str:
    """Which carrier to use for this device.

    An explicit request is obeyed without asking the device anything --
    that is the point of the flag, and it is what makes a device
    reachable when its announcement is wrong or missing.
    """
    requested = (requested or AUTO).strip().lower()
    if requested in (MQTT, WEBRTC):
        log.info("transport %s (forced)", requested)
        return requested
    if requested != AUTO:
        raise ValueError(f"unknown transport {requested!r}: "
                         f"expected {AUTO}, {MQTT} or {WEBRTC}")

    caps = probe_capabilities(device_id, timeout)

    # WebRTC only when the device says it can, AND says it is duplex.
    # A camera that can send but not receive is not something to try to
    # hold a conversation with.
    if caps.get("WebRTC") and caps.get("duplex"):
        log.info("transport webrtc (announced by %s)", device_id)
        return WEBRTC

    # Everything else is the MQTT path, including every case where the
    # probe learned nothing. The BOX-3 route is the proven one, so it is
    # what an uncertain answer falls back to.
    if caps:
        log.info("transport mqtt (%s does not announce duplex WebRTC)", device_id)
    else:
        log.info("transport mqtt (default)")
    return MQTT


def build_audio_io(device_id: str, settings, *, transport: str = AUTO,
                   utterance_config=None):
    """Construct the adapter for this device. Returns a SentinelAudioIO.

    The MQTT adapter is usable the moment it returns. The WebRTC one
    still has to place a call, which is asynchronous -- use
    `open_audio_io` unless you intend to drive that yourself.
    """
    kind = choose_transport(device_id, transport)

    if kind == MQTT:
        from .sentinel import SentinelAudioAdapter, UtteranceConfig
        return SentinelAudioAdapter(device_id, utterance_config or UtteranceConfig())

    from .webrtc_sentinel import WebRtcSentinelAdapter
    return WebRtcSentinelAdapter(device_id, settings, utterance_config)


async def open_audio_io(device_id: str, settings, *, transport: str = AUTO,
                        utterance_config=None):
    """Build the adapter and make it ready to carry a conversation.

    Hides the difference between a transport that is connected as soon
    as it is constructed and one that has to ring a device first. Callers
    get something they can immediately receive on either way.
    """
    audio = build_audio_io(device_id, settings, transport=transport,
                           utterance_config=utterance_config)
    connect = getattr(audio, "connect", None)
    if connect is not None:
        await connect()
    return audio
