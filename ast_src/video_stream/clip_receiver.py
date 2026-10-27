"""Clips a sentinel sends over MQTT: shown in Visuals, and kept on disk.

Some sentinels (the S3 camera boards) do not post a continuous uplink. When
something happens they publish a short clip instead:

    visual/<device>/clip/info        JSON describing the clip (optional fields)
    visual/<device>/clip/frame/<n>   one JPEG per message
    visual/<device>/clip/complete    end of the clip

Each frame is handed to the frame store as it lands, so "What the sentinel
sees" shows the clip while it is arriving exactly as it shows a live uplink.
A clip is over in seconds, though, and the panel would be blank again before
anyone looked. So the last clip of each device is also kept in memory and can
be replayed on a loop (routes.py, /video/api/clip/<device>), and every clip is
written to assets/video/<device>/.

The file is an .mp4 when ffmpeg is on the PATH and an .mjpeg (the JPEGs one
after another, which ffmpeg and VLC both open) when it is not: a missing
encoder must not cost the recording.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import paho.mqtt.client as mqtt

from ast_src.video_stream.store import get_store

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VIDEO_DIR = os.path.join(PROJECT_ROOT, "assets", "video")

TOPIC = "visual/+/clip/#"

# A clip that never says "complete" is closed after this long without a frame,
# and saved with what arrived: the end message is one packet and can be lost.
IDLE_CLOSE_S = 5.0
# Bounds on what one clip may hold in memory.
MAX_FRAMES = 1500
DEFAULT_FPS = 5.0


@dataclass
class Clip:
    device_id: str
    started: float
    info: dict = field(default_factory=dict)
    frames: List[bytes] = field(default_factory=list)
    last_frame_at: float = 0.0
    path: Optional[str] = None
    fps: float = DEFAULT_FPS
    closed: bool = False


def _as_jpeg(payload: bytes) -> Optional[bytes]:
    """The frame as JPEG bytes: sent raw, or base64 by firmware that prefers text."""
    if payload.startswith(b"\xff\xd8"):
        return payload
    try:
        decoded = base64.b64decode(payload, validate=True)
    except Exception:
        return None
    return decoded if decoded.startswith(b"\xff\xd8") else None


class ClipReceiver:
    def __init__(self) -> None:
        self._open: Dict[str, Clip] = {}
        self._last: Dict[str, Clip] = {}
        self._lock = threading.Lock()
        self._client = None

    # ── what the page asks ──────────────────────────────────────────────

    def last_clip(self, device_id: str) -> Optional[Clip]:
        with self._lock:
            return self._last.get(device_id)

    def devices(self) -> List[str]:
        with self._lock:
            return sorted(set(self._last) | set(self._open))

    # ── MQTT ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Connect and subscribe. Best-effort: Visuals works without clips."""
        if self._client is not None:
            return
        try:
            from ast_src.audio_transport.session_manager import _default_ptt_broker_kwargs
            cfg = _default_ptt_broker_kwargs()
            client_id = f"ast-video-clips-{int(time.time())}"
            try:
                client = mqtt.Client(
                    client_id=client_id,
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
            except AttributeError:
                client = mqtt.Client(client_id=client_id)
            if cfg.get("username"):
                client.username_pw_set(cfg["username"], cfg.get("password", ""))
            if cfg.get("cafile"):
                client.tls_set(ca_certs=cfg["cafile"], certfile=cfg.get("certfile"),
                               keyfile=cfg.get("keyfile"),
                               tls_version=ssl.PROTOCOL_TLS_CLIENT)
            elif cfg.get("port") == 8883:
                client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
                client.tls_insecure_set(True)
            client.on_connect = lambda c, *_: c.subscribe(TOPIC, qos=0)
            client.on_message = self._on_message
            client.connect_async(cfg["broker"], int(cfg["port"]), keepalive=60)
            client.loop_start()
            self._client = client
            threading.Thread(target=self._close_idle_clips, daemon=True,
                             name="video-clip-idle").start()
            log.info("Video clips: listening on %s", TOPIC)
        except Exception as exc:
            log.warning("Video clips: receiver not started (%s)", exc)

    def _on_message(self, _client, _userdata, msg) -> None:
        try:
            parts = msg.topic.split("/")
            if len(parts) < 4 or parts[0] != "visual" or parts[2] != "clip":
                return
            device, kind = parts[1], parts[3]
            if kind == "info":
                self._begin(device, msg.payload)
            elif kind == "frame":
                self._frame(device, msg.payload)
            elif kind == "complete":
                self._finish(device)
        except Exception:
            log.exception("Video clips: could not handle %s", msg.topic)

    # ── a clip ──────────────────────────────────────────────────────────

    def _begin(self, device: str, payload: bytes) -> Clip:
        self._finish(device)                 # one that never said "complete"
        try:
            info = json.loads(payload.decode("utf-8")) if payload else {}
        except (ValueError, UnicodeDecodeError):
            info = {}
        clip = Clip(device_id=device, started=time.time(),
                    info=info if isinstance(info, dict) else {})
        with self._lock:
            self._open[device] = clip
        log.info("Video clips: %s started a clip %s", device, clip.info or "")
        return clip

    def _frame(self, device: str, payload: bytes) -> None:
        jpeg = _as_jpeg(payload)
        if jpeg is None:
            log.warning("Video clips: %s sent a frame that is not a JPEG "
                        "(%d bytes)", device, len(payload))
            return
        with self._lock:
            clip = self._open.get(device)
        if clip is None:                     # the info message was missed
            clip = self._begin(device, b"")
        if len(clip.frames) < MAX_FRAMES:
            clip.frames.append(jpeg)
        clip.last_frame_at = time.time()
        get_store().put(device, jpeg)        # live in the Visuals panel

    def _finish(self, device: str) -> None:
        with self._lock:
            clip = self._open.pop(device, None)
        if clip is None or not clip.frames:
            return
        clip.fps = self._fps(clip)
        clip.closed = True
        try:
            clip.path = self._save(clip)
        except Exception:
            log.exception("Video clips: could not save the clip from %s", device)
        with self._lock:
            self._last[device] = clip
        log.info("Video clips: %s clip ended, %d frames at %.1f fps -> %s",
                 device, len(clip.frames), clip.fps, clip.path or "not saved")

    def _close_idle_clips(self) -> None:
        while True:
            time.sleep(1.0)
            now = time.time()
            with self._lock:
                stale = [d for d, c in self._open.items()
                         if now - (c.last_frame_at or c.started) > IDLE_CLOSE_S]
            for device in stale:
                self._finish(device)

    @staticmethod
    def _fps(clip: Clip) -> float:
        """What the device said, or else how fast the frames arrived."""
        for key in ("fps", "frame_rate", "framerate"):
            try:
                value = float(clip.info.get(key))
                if 0.2 <= value <= 60:
                    return value
            except (TypeError, ValueError):
                pass
        span = clip.last_frame_at - clip.started
        if len(clip.frames) > 1 and span > 0.2:
            return max(1.0, min(30.0, len(clip.frames) / span))
        return DEFAULT_FPS

    @staticmethod
    def _save(clip: Clip) -> str:
        folder = os.path.join(VIDEO_DIR, clip.device_id)
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.fromtimestamp(clip.started).strftime("%Y%m%d_%H%M%S")
        base = os.path.join(folder, f"{stamp}_{clip.device_id}_clip")
        mjpeg = base + ".mjpeg"
        with open(mjpeg, "wb") as fh:
            for jpeg in clip.frames:
                fh.write(jpeg)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return mjpeg
        mp4 = base + ".mp4"
        done = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "mjpeg",
             "-framerate", f"{clip.fps:.3f}", "-i", mjpeg,
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-movflags", "+faststart", mp4],
            capture_output=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode == 0 and os.path.getsize(mp4) > 0:
            os.remove(mjpeg)                 # our own intermediate, just written
            return mp4
        log.warning("Video clips: ffmpeg could not make %s (%s); the .mjpeg is kept",
                    mp4, done.stderr.decode("utf-8", "replace").strip()[:200])
        return mjpeg


_receiver: Optional[ClipReceiver] = None
_receiver_lock = threading.Lock()


def get_receiver() -> ClipReceiver:
    global _receiver
    with _receiver_lock:
        if _receiver is None:
            _receiver = ClipReceiver()
        return _receiver
