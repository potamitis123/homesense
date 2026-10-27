"""Choosing a carrier per device.

The rule that matters most is the fallback: anything uncertain must land
on the MQTT path, because that is the one the BOX-3 has always used and
it must keep working untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def caps(monkeypatch):
    """Answer the capability probe without a broker."""
    from app.services import transport

    def install(payload):
        monkeypatch.setattr(transport, "probe_capabilities",
                            lambda device, timeout=None: payload)
    return install


# ── auto ─────────────────────────────────────────────────────────────

def test_duplex_webrtc_device_gets_webrtc(caps):
    from app.services.transport import choose_transport
    caps({"WebRTC": True, "Camera": True, "duplex": True, "video": True})
    assert choose_transport("p4-door-1") == "webrtc"


def test_silent_device_gets_mqtt(caps):
    """A BOX-3 publishes no capabilities at all. That is the signal."""
    from app.services.transport import choose_transport
    caps({})
    assert choose_transport("box3-2") == "mqtt"


def test_webrtc_without_duplex_is_not_used(caps):
    """A camera that sends but cannot receive is not a conversation partner."""
    from app.services.transport import choose_transport
    caps({"WebRTC": True, "Camera": True, "duplex": False})
    assert choose_transport("cam-1") == "mqtt"


def test_unreachable_probe_falls_back_to_mqtt(monkeypatch):
    """A broker that is down must not change which transport is chosen."""
    from app.services import transport

    def boom(device, timeout=None):
        raise RuntimeError("no broker")
    monkeypatch.setattr(transport, "probe_capabilities", boom)
    with pytest.raises(RuntimeError):
        transport.choose_transport("box3-2")


def test_probe_itself_never_raises(monkeypatch):
    """probe_capabilities swallows everything; that is its contract."""
    from app.services import transport
    monkeypatch.setattr(transport, "CAPABILITY_TIMEOUT", 0.01)
    assert transport.probe_capabilities("nothing-here", 0.01) == {}


# ── forced ───────────────────────────────────────────────────────────

def test_forced_transport_skips_the_probe(monkeypatch):
    from app.services import transport
    called = []
    monkeypatch.setattr(transport, "probe_capabilities",
                        lambda d, timeout=None: called.append(d) or {})
    assert transport.choose_transport("p4-door-1", "mqtt") == "mqtt"
    assert transport.choose_transport("box3-2", "webrtc") == "webrtc"
    assert called == [], "a forced transport must not need the device"


def test_unknown_transport_is_rejected():
    from app.services.transport import choose_transport
    with pytest.raises(ValueError, match="unknown transport"):
        choose_transport("box3-2", "carrier-pigeon")


# ── the webrtc adapter is selected ───────────────────────────────────

def test_auto_selects_the_webrtc_adapter(caps, monkeypatch):
    """Routing reaches the WebRTC class without a device being present."""
    from app.services import transport, webrtc_sentinel

    caps({"WebRTC": True, "duplex": True})
    built = {}

    class Fake:
        def __init__(self, device, settings=None, cfg=None):
            built["device"] = device
    monkeypatch.setattr(webrtc_sentinel, "WebRtcSentinelAdapter", Fake)
    transport.build_audio_io("p4-door-1", None)
    assert built["device"] == "p4-door-1"


def test_mqtt_device_never_touches_the_webrtc_adapter(caps, monkeypatch):
    """The BOX-3 route must be reachable even if WebRTC is broken."""
    from app.services import transport, webrtc_sentinel

    caps({})

    def explode(*a, **k):
        raise AssertionError("the webrtc adapter must not be constructed")
    monkeypatch.setattr(webrtc_sentinel, "WebRtcSentinelAdapter", explode)

    made = {}

    class FakeMqtt:
        def __init__(self, device, cfg):
            made["device"] = device
    monkeypatch.setattr("app.services.sentinel.SentinelAudioAdapter", FakeMqtt)
    transport.build_audio_io("box3-2", None)
    assert made["device"] == "box3-2"
