import time

import pytest
from starlette.websockets import WebSocketDisconnect

from ast_src.audio_transport.constants import FRAME_BYTES
from mobile_api.services.mobile_audio_service import AudioSessionService


def test_create_session_known_device(client, auth_headers, fake_transport):
    resp = client.post("/api/v1/audio/session", json={"device_id": "kitchen"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "kitchen"
    assert body["status"] == "starting"
    assert body["session_id"]


def test_create_session_unknown_device(client, auth_headers, fake_transport):
    resp = client.post("/api/v1/audio/session", json={"device_id": "not-a-real-device"}, headers=auth_headers)
    assert resp.status_code == 404


def test_create_session_unauthorized(client, fake_transport):
    resp = client.post("/api/v1/audio/session", json={"device_id": "kitchen"})
    assert resp.status_code == 401


def test_ws_rejected_without_token(client, auth_headers, fake_transport):
    create = client.post("/api/v1/audio/session", json={"device_id": "kitchen"}, headers=auth_headers)
    session_id = create.json()["session_id"]

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/v1/audio/ws/{session_id}"):
            pass


def test_ws_rejected_for_unknown_session(client, auth_headers, fake_transport):
    token = auth_headers["Authorization"].split(" ")[1]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/v1/audio/ws/does-not-exist?token={token}"):
            pass


def test_ws_send_audio_and_clean_disconnect(client, auth_headers, fake_transport):
    create = client.post("/api/v1/audio/session", json={"device_id": "kitchen"}, headers=auth_headers)
    session_id = create.json()["session_id"]
    token = auth_headers["Authorization"].split(" ")[1]

    with client.websocket_connect(f"/api/v1/audio/ws/{session_id}?token={token}") as ws:
        ws.send_bytes(b"\x00\x01" * (FRAME_BYTES // 2))  # exactly one frame

    # After the `with` block exits, the server side has processed the
    # disconnect and run its finally: cleanup.
    transport = fake_transport.instances[-1]
    assert len(transport.sent_frames) == 1
    assert transport.stopped is True


def test_session_idle_timeout_closes_session(fake_transport):
    service = AudioSessionService(timeout_minutes=5)
    session_id = service.create_session("kitchen")
    assert service.session_exists(session_id)

    # Simulate time passing without forcing a real sleep.
    with service._lock:
        service._sessions[session_id].last_activity = time.time() - 10 * 60

    service.sweep_idle_sessions()
    assert not service.session_exists(session_id)
    assert fake_transport.instances[-1].stopped is True
