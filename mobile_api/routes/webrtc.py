"""
WebRTC video-call signaling gateway.

    POST /api/v1/webrtc/session  -> register the app as caller for one P4
    POST /api/v1/webrtc/signal   -> forward an offer / candidate / command
    GET  /api/v1/webrtc/poll     -> long-poll the sentinel's replies
    POST /api/v1/webrtc/close    -> hang up

Mirrors ast_src/webrtc/routes.py (the browser's unauthenticated
equivalent) endpoint for endpoint, so the app and the dashboard speak the
identical protocol to the same relay -- only the auth and the URL prefix
differ. Media never touches this process; see mobile_webrtc_service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from mobile_api.logging_setup import get_logger, log_extra
from mobile_api.models.schemas import (
    WebrtcCloseRequest,
    WebrtcPollResponse,
    WebrtcSessionCreateRequest,
    WebrtcSessionCreateResponse,
    WebrtcSignalRequest,
)
from mobile_api.security.authentication import require_auth
from mobile_api.services import mobile_device_service, mobile_webrtc_service

logger = get_logger()

router = APIRouter(prefix="/api/v1/webrtc", tags=["webrtc"])

# The bridge itself caps this (MAX_POLL_TIMEOUT_S); kept here so an app
# bug cannot pin a worker thread for longer than the relay allows.
_MAX_POLL_TIMEOUT = 20.0


@router.post("/session", response_model=WebrtcSessionCreateResponse)
def create_webrtc_session(
    payload: WebrtcSessionCreateRequest,
    _username: str = Depends(require_auth),
):
    devices = {d["device_id"]: d for d in mobile_device_service.get_devices()}
    device = devices.get(payload.device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown device '{payload.device_id}'",
        )
    # Only the P4 units run the signaling firmware; calling anything else
    # would ring forever waiting for an answer that cannot come.
    if not device.get("capabilities", {}).get("video"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Device '{payload.device_id}' does not support video calls",
        )

    try:
        return mobile_webrtc_service.open_session(payload.device_id)
    except Exception as e:
        logger.error(
            f"could not open webrtc session for {payload.device_id}: {e}",
            extra=log_extra("MOBILE_WEBRTC"),
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@router.post("/signal")
def signal(payload: WebrtcSignalRequest, _username: str = Depends(require_auth)):
    try:
        mobile_webrtc_service.send(payload.session_id, payload.message)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown session")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    return {"ok": True}


@router.get("/poll", response_model=WebrtcPollResponse)
def poll(
    session_id: str = Query(...),
    timeout: float = Query(20.0),
    _username: str = Depends(require_auth),
):
    # Deliberately a sync def: the bridge's poll() blocks on a queue, so
    # FastAPI runs it in its threadpool instead of stalling the event loop
    # (the same reason the audio route keeps its session calls sync).
    try:
        messages = mobile_webrtc_service.poll(session_id, min(timeout, _MAX_POLL_TIMEOUT))
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown session")
    return WebrtcPollResponse(messages=messages)


@router.post("/close")
def close(payload: WebrtcCloseRequest, _username: str = Depends(require_auth)):
    mobile_webrtc_service.close_session(payload.session_id)
    return {"ok": True}
