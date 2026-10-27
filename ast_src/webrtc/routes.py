"""HTTP face of the WebRTC signaling bridge.

The browser cannot reach the broker itself (see signaling.py), so it talks to
these four endpoints instead. Everything is JSON; nothing here understands SDP,
it only moves opaque messages in and out of ``SignalingBridge``.

Inbound messages are long-polled rather than pushed. Server-sent events would
be tidier, but they pin a worker thread open for the life of the page, and this
app is served by the Werkzeug development server. A 20 second poll costs three
requests a minute on an idle call and releases the thread in between.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from ast_src.webrtc.signaling import get_bridge

log = logging.getLogger(__name__)

webrtc_bp = Blueprint("webrtc_signaling", __name__, url_prefix="/webrtc/api")


@webrtc_bp.post("/session")
def open_session():
    """Register a browser peer against one sentinel."""
    body = request.get_json(silent=True) or {}
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    bridge = get_bridge()
    try:
        session_id = bridge.open_session(device_id)
    except Exception as exc:
        log.exception("Could not open a WebRTC session for %s", device_id)
        return jsonify({"error": str(exc)}), 503

    return jsonify({
        "session_id": session_id,
        "device_id": device_id,
        "ice_servers": bridge.ice_servers(),
        "video": bridge.video_profile(),
    })


@webrtc_bp.post("/signal")
def signal():
    """Forward one offer, candidate or command to the sentinel."""
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id") or ""
    message = body.get("message")
    if not session_id or not isinstance(message, dict):
        return jsonify({"error": "session_id and message are required"}), 400

    try:
        get_bridge().send(session_id, message)
    except KeyError:
        return jsonify({"error": "unknown session"}), 404
    except Exception as exc:
        log.exception("Could not publish a signaling message")
        return jsonify({"error": str(exc)}), 503
    return jsonify({"ok": True})


@webrtc_bp.get("/poll")
def poll():
    """Block until the sentinel answers, or the timeout expires."""
    session_id = request.args.get("session_id", "")
    try:
        timeout = float(request.args.get("timeout", "20"))
    except ValueError:
        timeout = 20.0

    try:
        messages = get_bridge().poll(session_id, timeout)
    except KeyError:
        return jsonify({"error": "unknown session"}), 404
    return jsonify({"messages": messages})


@webrtc_bp.post("/close")
def close_session():
    """Hang up and drop the session."""
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id") or ""
    get_bridge().close_session(session_id)
    return jsonify({"ok": True})


def register(server) -> None:
    """Attach the blueprint to a Flask app, ignoring a repeat registration."""
    if "webrtc_signaling" in server.blueprints:
        return
    server.register_blueprint(webrtc_bp)
