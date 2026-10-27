"""
Push-to-Talk / duplex audio gateway.

    POST /api/v1/audio/session       -> create a session against one Sentinel
    WS   /api/v1/audio/ws/{session_id} -> realtime duplex audio

The mobile client never sees MQTT: this route only moves bytes between the
WebSocket and mobile_audio_service.AudioSessionService, which owns the
actual StreamingAudioTransport (the existing duplex implementation).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from mobile_api.logging_setup import get_logger, log_extra
from mobile_api.models.schemas import AudioSessionCreateRequest, AudioSessionCreateResponse
from mobile_api.security import authentication
from mobile_api.security.authentication import require_auth
from mobile_api.services import mobile_audio_service, mobile_device_service

logger = get_logger()

router = APIRouter(prefix="/api/v1/audio", tags=["audio"])


@router.post("/session", response_model=AudioSessionCreateResponse)
def create_audio_session(payload: AudioSessionCreateRequest, _username: str = Depends(require_auth)):
    service = mobile_audio_service.audio_session_service
    if service is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Audio service not initialised")

    known_ids = {d["device_id"] for d in mobile_device_service.get_devices()}
    if payload.device_id not in known_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown device '{payload.device_id}'")

    try:
        session_id = service.create_session(payload.device_id)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    return AudioSessionCreateResponse(session_id=session_id, device_id=payload.device_id, status="starting")


def _authenticate_ws(websocket: WebSocket, token: str | None) -> str | None:
    """Bearer token from the Authorization header, falling back to a
    ?token= query param for WS clients that can't set custom headers."""
    auth_header = websocket.headers.get("authorization")
    candidate = None
    if auth_header and auth_header.lower().startswith("bearer "):
        candidate = auth_header[7:]
    elif token:
        candidate = token
    if candidate is None or authentication.token_store is None:
        return None
    return authentication.token_store.verify_access(candidate)


@router.websocket("/ws/{session_id}")
async def audio_ws(websocket: WebSocket, session_id: str, token: str | None = Query(None)):
    service = mobile_audio_service.audio_session_service
    username = _authenticate_ws(websocket, token)
    if username is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    if service is None or not service.session_exists(session_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    logger.info(f"ws connected session={session_id} user={username}", extra=log_extra("MOBILE_WS"))

    loop = asyncio.get_running_loop()

    def _sink(pcm_bytes: bytes) -> None:
        # Called from the MQTT client's own thread; bridge back onto the
        # event loop that owns this WebSocket. Already-decoded PCM16 (see
        # mobile_audio_service's docstring for why decoding happens
        # server-side rather than in the Android client).
        asyncio.run_coroutine_threadsafe(_safe_send(websocket, pcm_bytes), loop)

    service.attach_ws_sink(session_id, _sink)

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                service.feed_mic_bytes(session_id, data)
                continue
            text = message.get("text")
            if text == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"ws error session={session_id}: {e}", extra=log_extra("MOBILE_WS"))
    finally:
        service.stop_session(session_id)
        logger.info(f"ws closed session={session_id}", extra=log_extra("MOBILE_WS"))


async def _safe_send(websocket: WebSocket, data: bytes) -> None:
    try:
        await websocket.send_bytes(data)
    except Exception:
        pass
