"""HTTP face of the sentinels' MJPEG uplink.

Three endpoints, mounted on the same Flask app Dash runs on (see
``ast_dash/ast_app.py``), alongside the WebRTC signaling blueprint:

    POST /video/api/ingest/<device_id>   one JPEG, body is the raw image
    GET  /video/api/stream/<device_id>   multipart/x-mixed-replace MJPEG
    GET  /video/api/frame/<device_id>    the newest JPEG, once
    GET  /video/api/devices              who is streaming, and how fast

The uplink is one POST per frame rather than a single long upload. At the
2-5 fps this feed runs at, the request overhead is negligible next to the
JPEG, and it buys the properties that matter on a device on the far side of
a domestic Wi-Fi link: every frame is independently framed by its own
Content-Length, a dropped connection costs exactly one frame, and the server
holds no per-device connection state to get wedged. It also keeps the
sentinel's uplink entirely separate from the WebRTC path in
``ast_src/webrtc``, so a video call can be negotiated, run and torn down
without this stream noticing.

Frames are read straight out of ``request.get_data()`` and handed to the
store; nothing is decoded, re-encoded or written to disk.
"""

from __future__ import annotations

import logging

from flask import Blueprint, Response, jsonify, request

from ast_src.video_stream import ingest_server
from ast_src.video_stream.store import get_store

log = logging.getLogger(__name__)

video_bp = Blueprint("video_stream", __name__, url_prefix="/video/api")

# Boundary for multipart/x-mixed-replace. Any token works as long as the
# header and the parts agree on it.
_BOUNDARY = "astframe"

# How long a viewer waits for a new frame before resending the last one as a
# keepalive. Short enough that a closed tab is noticed within seconds.
_KEEPALIVE_S = 2.0


@video_bp.post("/ingest/<device_id>")
def ingest(device_id: str):
    """Accept one JPEG frame from a sentinel."""
    store = get_store()

    length = request.content_length or 0
    if length > store.max_frame_bytes:
        log.warning("Video uplink: %s sent %d bytes, over the %d limit",
                    device_id, length, store.max_frame_bytes)
        return jsonify({"error": "frame too large"}), 413

    data = request.get_data(cache=False)
    if not data:
        return jsonify({"error": "empty body"}), 400
    # SOI marker. Cheap, and it keeps a misconfigured poster from filling the
    # panel with something no browser will render.
    if not data.startswith(b"\xff\xd8"):
        return jsonify({"error": "body is not a JPEG"}), 415

    store.put(device_id, data)
    # No body: the sentinel only reads the status line, and an empty response
    # keeps its kept-alive socket clean between frames.
    return "", 204


@video_bp.get("/frame/<device_id>")
def frame(device_id: str):
    """The newest frame, as a plain JPEG."""
    latest = get_store().latest(device_id)
    if latest is None:
        return jsonify({"error": "no frame yet"}), 404
    return Response(latest.jpeg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@video_bp.get("/stream/<device_id>")
def stream(device_id: str):
    """Continuous MJPEG, for an <img> tag or any MJPEG client."""
    store = get_store()

    def frames():
        last_seq = 0
        idle = 0.0
        while True:
            item = store.wait_for(device_id, last_seq, _KEEPALIVE_S)
            if item is None:
                idle += _KEEPALIVE_S
                # Nothing new. Resending the last frame keeps the connection
                # warm and, more usefully, fails the write when the viewer
                # has gone -- which is the only way this generator learns to
                # stop. A device that has never streamed has nothing to
                # resend, so that case is bounded by a timeout instead.
                item = store.latest(device_id)
                if item is None:
                    if idle >= store.viewer_idle_timeout:
                        log.info("Video stream: no frames from %s, dropping "
                                 "the viewer", device_id)
                        return
                    continue
            else:
                idle = 0.0
                last_seq = item.seq

            yield (b"--" + _BOUNDARY.encode() + b"\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(item.jpeg)).encode() +
                   b"\r\n\r\n" + item.jpeg + b"\r\n")

    return Response(
        frames(),
        mimetype=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            # Harmless without a proxy, and stops nginx buffering the stream
            # into uselessness if one is ever put in front of the app.
            "X-Accel-Buffering": "no",
        },
    )


@video_bp.get("/clip/<device_id>")
def clip(device_id: str):
    """The last clip a sentinel sent over MQTT, replayed on a loop as MJPEG.

    A clip lasts a few seconds, so by the time anyone looks the live frames
    are gone. This plays what was received again, at the rate it was shot,
    with a pause between rounds so the start can be told from the end.
    """
    from ast_src.video_stream.clip_receiver import get_receiver
    import time as _time

    last = get_receiver().last_clip(device_id)
    if last is None:
        return jsonify({"error": "no clip yet"}), 404

    def frames():
        delay = 1.0 / max(last.fps, 0.2)
        while True:
            for jpeg in last.frames:
                yield (b"--" + _BOUNDARY.encode() + b"\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(jpeg)).encode() +
                       b"\r\n\r\n" + jpeg + b"\r\n")
                _time.sleep(delay)
            _time.sleep(1.0)

    return Response(
        frames(),
        mimetype=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "Pragma": "no-cache", "X-Accel-Buffering": "no"})


@video_bp.get("/devices")
def devices():
    """Which sentinels are streaming, and how well."""
    return jsonify({"devices": get_store().devices()})


def register(server) -> None:
    """Attach the blueprint to a Flask app and start the ingest listener.

    Both, because the two are one feature: the listener takes the frames and
    the blueprint serves them. The listener is best-effort — see
    ingest_server.start() — and the POST endpoint above works without it.
    """
    if "video_stream" in server.blueprints:
        return
    server.register_blueprint(video_bp)
    ingest_server.start()
    # Clips sent over MQTT feed the same store and the same panel.
    from ast_src.video_stream.clip_receiver import get_receiver
    get_receiver().start()
