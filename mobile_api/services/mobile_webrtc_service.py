"""
WebRTC video-call signaling for the mobile API.

Reuses the existing relay directly: SignalingBridge
(ast_src/webrtc/signaling.py) is the same class the desktop dashboard's
Visuals tab signals through. No MQTT topics, session bookkeeping or
message shapes are reimplemented here; this module only puts an
authenticated, mobile-shaped face on that bridge.

Why not just point the app at the existing Flask endpoints in
ast_src/webrtc/routes.py: those live in the Dash process and are
unauthenticated (they are reachable only from an already-authenticated
dashboard page). The mobile app talks to mobile_api over the network and
everything it calls sits behind require_auth, so the same relay is
exposed here instead of widening the Dash surface. get_bridge() is a
per-process singleton, so this creates mobile_api's own bridge -- the
same arrangement mobile_audio_service uses for StreamingAudioTransport,
and for the same reason (separate OS process, see its docstring).

Only the P4 sentinels can take a call: they are the units running
mqtt_signaling.c, and they are always the callee -- the app must be the
caller and send the offer (see assets/webrtc_intercom.js, which
implements the identical contract for the browser).
"""

from __future__ import annotations

from typing import Any, Dict, List

from ast_src.webrtc.signaling import get_bridge

from mobile_api.logging_setup import get_logger, log_extra

logger = get_logger()


def open_session(device_id: str) -> Dict[str, Any]:
    bridge = get_bridge()
    session_id = bridge.open_session(device_id)
    logger.info(
        f"webrtc session {session_id[:8]} opened -> device={device_id}",
        extra=log_extra("MOBILE_WEBRTC"),
    )
    return {
        "session_id": session_id,
        "device_id": device_id,
        "ice_servers": bridge.ice_servers(),
        "video": bridge.video_profile(),
    }


def send(session_id: str, message: Dict[str, Any]) -> None:
    get_bridge().send(session_id, message)


def poll(session_id: str, timeout: float) -> List[Dict[str, Any]]:
    return get_bridge().poll(session_id, timeout)


def close_session(session_id: str) -> None:
    get_bridge().close_session(session_id)
    logger.info(
        f"webrtc session {session_id[:8]} closed",
        extra=log_extra("MOBILE_WEBRTC"),
    )
