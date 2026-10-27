"""
WebRTC signaling gateway tests.

The SignalingBridge is faked throughout: these tests never open an MQTT
connection or need a P4 sentinel present.
"""

from __future__ import annotations

import pytest

from mobile_api.services import mobile_device_service, mobile_webrtc_service


class _FakeBridge:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.closed: list[str] = []
        self.inbox: list[dict] = []
        self.opened: list[str] = []

    def open_session(self, device_id):
        self.opened.append(device_id)
        return "fakesession0123456789"

    def ice_servers(self):
        return [{"urls": "stun:stun.l.google.com:19302"}]

    def video_profile(self):
        return {"width": 720, "height": 1280, "fps": 12, "quality": 0.4}

    def send(self, session_id, message):
        if session_id != "fakesession0123456789":
            raise KeyError(session_id)
        self.sent.append((session_id, message))

    def poll(self, session_id, timeout):
        if session_id != "fakesession0123456789":
            raise KeyError(session_id)
        return list(self.inbox)

    def close_session(self, session_id):
        self.closed.append(session_id)


@pytest.fixture
def fake_bridge(monkeypatch):
    bridge = _FakeBridge()
    monkeypatch.setattr(mobile_webrtc_service, "get_bridge", lambda: bridge)
    return bridge


@pytest.fixture(autouse=True)
def video_capable_device(monkeypatch):
    """living_room stands in for a P4 (the only units that can take a call)."""
    monkeypatch.setattr(mobile_device_service, "load_device_capabilities", lambda: {
        "kitchen": {"duplex": True, "video": False},
        "living_room": {"duplex": True, "video": True},
    })


def test_session_requires_auth(client, fake_bridge):
    resp = client.post("/api/v1/webrtc/session", json={"device_id": "living_room"})
    assert resp.status_code == 401


def test_session_unknown_device(client, auth_headers, fake_bridge):
    resp = client.post("/api/v1/webrtc/session", json={"device_id": "nope"}, headers=auth_headers)
    assert resp.status_code == 404


def test_session_rejects_non_video_device(client, auth_headers, fake_bridge):
    """A box3 has no signaling firmware -- calling it would ring forever."""
    resp = client.post("/api/v1/webrtc/session", json={"device_id": "kitchen"}, headers=auth_headers)
    assert resp.status_code == 400
    assert fake_bridge.opened == []


def test_session_opens_for_video_device(client, auth_headers, fake_bridge):
    resp = client.post("/api/v1/webrtc/session", json={"device_id": "living_room"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == "fakesession0123456789"
    assert body["device_id"] == "living_room"
    assert body["ice_servers"] == [{"urls": "stun:stun.l.google.com:19302"}]
    # Geometry has to match the firmware's settings.h, so it comes from
    # the server rather than being guessed by the app.
    assert body["video"] == {"width": 720, "height": 1280, "fps": 12, "quality": 0.4}
    assert fake_bridge.opened == ["living_room"]


def test_signal_is_relayed_verbatim(client, auth_headers, fake_bridge):
    offer = {"type": "offer", "sdp": "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n"}
    resp = client.post(
        "/api/v1/webrtc/signal",
        json={"session_id": "fakesession0123456789", "message": offer},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert fake_bridge.sent == [("fakesession0123456789", offer)]


def test_signal_unknown_session(client, auth_headers, fake_bridge):
    resp = client.post(
        "/api/v1/webrtc/signal",
        json={"session_id": "bogus", "message": {"type": "bye"}},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_poll_returns_pending_messages(client, auth_headers, fake_bridge):
    fake_bridge.inbox = [{"type": "answer", "sdp": "v=0"}, {"type": "candidate", "candidate": "a=x"}]
    resp = client.get(
        "/api/v1/webrtc/poll",
        params={"session_id": "fakesession0123456789", "timeout": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["messages"] == fake_bridge.inbox


def test_poll_unknown_session(client, auth_headers, fake_bridge):
    resp = client.get(
        "/api/v1/webrtc/poll",
        params={"session_id": "bogus", "timeout": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_close_hangs_up(client, auth_headers, fake_bridge):
    resp = client.post(
        "/api/v1/webrtc/close",
        json={"session_id": "fakesession0123456789"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert fake_bridge.closed == ["fakesession0123456789"]
