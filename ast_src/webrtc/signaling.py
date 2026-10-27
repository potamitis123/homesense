"""MQTT signaling bridge between the operator's browser and a P4 sentinel.

``sentinel/mqtt_signaling.c`` makes the P4 a permanent *callee*: it subscribes
to ``<prefix>/<device>/callee`` and answers whatever offer lands there, then
publishes its answer and ICE candidates on ``<prefix>/<device>/caller``. It
never offers, and it reports ``is_initiator = false`` so esp_peer takes the
controlled ICE role. The caller therefore has to be us.

Media never touches this process. The microphone, the webcam, the JPEG encoder
and the DTLS/SRTP stack all live in the operator's browser tab; Python owns one
MQTT client and shuttles JSON between those two topics and the HTTP endpoints
in ``routes.py``.

Relaying through here rather than letting the browser speak MQTT directly
avoids two problems: the broker has no WebSocket listener, and its LAN listener
sets ``require_certificate true``, which a browser cannot satisfy on a
WebSocket without prompting for a client certificate on every reconnect.
"""

from __future__ import annotations

import json
import logging
import queue
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

from misc.load_parameters import parameters

log = logging.getLogger(__name__)

# Must match MQTT_SIGNAL_TOPIC_PREFIX in sentinel/settings.h.
DEFAULT_TOPIC_PREFIX = "videocall"

# A session nobody has polled for this long is a closed browser tab. Reaping it
# publishes a bye, so the sentinel stops streaming to an operator who has gone.
SESSION_IDLE_TIMEOUT_S = 90

# Long-poll ceiling. Short enough to stay under any proxy idle timeout, long
# enough that an idle call costs a handful of requests per minute.
MAX_POLL_TIMEOUT_S = 20.0


@dataclass
class _Session:
    session_id: str
    device_id: str
    inbox: "queue.Queue[Dict[str, Any]]" = field(default_factory=queue.Queue)
    last_polled: float = field(default_factory=time.monotonic)


class SignalingBridge:
    """One MQTT client, many browser sessions.

    Sessions are keyed by an opaque id rather than by device, so two operators
    can watch the same sentinel and each gets their own copy of its answers.
    Only one of them will win the call — esp_webrtc holds a single peer
    connection — but neither ends up silently swallowing the other's messages.
    """

    def __init__(self) -> None:
        cfg = parameters.get_parameters() or {}
        mqtt_cfg = cfg.get("mqtt", {}) or {}
        rtc_cfg = cfg.get("webrtc", {}) or {}

        self.host: str = mqtt_cfg.get("broker_host") or "localhost"
        self.port: int = int(mqtt_cfg.get("broker_port") or 1883)
        self.keepalive: int = int(mqtt_cfg.get("keepalive") or 60)
        self.username: str = mqtt_cfg.get("username") or ""
        self.password: str = mqtt_cfg.get("password") or ""
        self.cafile: str = mqtt_cfg.get("ca_certfile") or ""
        self.certfile: str = mqtt_cfg.get("certfile") or ""
        self.keyfile: str = mqtt_cfg.get("keyfile") or ""

        self.prefix: str = rtc_cfg.get("signal_topic_prefix") or DEFAULT_TOPIC_PREFIX

        self._sessions: Dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._client: Optional[mqtt.Client] = None
        self._reaper: Optional[threading.Thread] = None

    # ── topics ──────────────────────────────────────────────────────────

    def _to_device(self, device_id: str) -> str:
        return f"{self.prefix}/{device_id}/callee"

    def _from_device_wildcard(self) -> str:
        return f"{self.prefix}/+/caller"

    # ── connection ──────────────────────────────────────────────────────

    def _build_client(self) -> mqtt.Client:
        client_id = f"ast-webrtc-signal-{uuid.uuid4().hex[:8]}"
        try:
            client = mqtt.Client(
                client_id=client_id,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            )
        except AttributeError:
            client = mqtt.Client(client_id=client_id)

        if self.username:
            client.username_pw_set(self.username, self.password)
        if self.cafile:
            client.tls_set(
                ca_certs=self.cafile,
                certfile=self.certfile or None,
                keyfile=self.keyfile or None,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def start(self) -> None:
        """Connect and begin relaying. Safe to call more than once."""
        with self._lock:
            if self._client is not None:
                return
            self._client = self._build_client()

        try:
            self._client.connect(self.host, self.port, self.keepalive)
        except Exception:
            log.exception("WebRTC signaling: cannot reach broker %s:%s",
                          self.host, self.port)
            with self._lock:
                self._client = None
            raise

        self._client.loop_start()
        self._reaper = threading.Thread(
            target=self._reap_idle_sessions,
            name="webrtc-signal-reaper",
            daemon=True,
        )
        self._reaper.start()
        log.info("WebRTC signaling relaying %s/+/{callee,caller} via %s:%s",
                 self.prefix, self.host, self.port)

    def _on_connect(self, client, userdata, flags, rc, *args) -> None:
        if rc != 0:
            log.error("WebRTC signaling: broker refused the connection (rc=%s)", rc)
            return
        self._connected.set()
        client.subscribe(self._from_device_wildcard(), qos=1)

    def _on_disconnect(self, client, userdata, rc, *args) -> None:
        self._connected.clear()

    def _on_message(self, client, userdata, msg) -> None:
        parts = msg.topic.split("/")
        if len(parts) < 3:
            return
        device_id = parts[-2]

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("WebRTC signaling: discarding malformed payload on %s",
                        msg.topic)
            return

        with self._lock:
            targets = [s for s in self._sessions.values()
                       if s.device_id == device_id]
        if not targets:
            # A retained bye or an answer for an operator who has already gone.
            return
        for session in targets:
            session.inbox.put(payload)

    # ── sessions ────────────────────────────────────────────────────────

    def open_session(self, device_id: str) -> str:
        self.start()
        # Without a broker there is nothing to negotiate over, and failing here
        # gives the operator an error instead of a call that never rings.
        if not self._connected.wait(timeout=5):
            raise RuntimeError(
                f"signaling broker {self.host}:{self.port} did not respond")

        session = _Session(session_id=uuid.uuid4().hex, device_id=device_id)
        with self._lock:
            self._sessions[session.session_id] = session
        log.info("WebRTC session %s opened for %s", session.session_id[:8],
                 device_id)
        return session.session_id

    def close_session(self, session_id: str, send_bye: bool = True) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            # Whether anyone else is still on this sentinel, decided under
            # the same lock that removed us so two closes cannot both see
            # the other as live and neither hang up.
            others_live = session is not None and any(
                s.device_id == session.device_id for s in self._sessions.values()
            )
        if session is None:
            return
        # bye goes to the device topic, not to a session, so it ends whatever
        # call the sentinel currently has -- not necessarily this one. A stale
        # session reaped 90s after its operator moved on would therefore hang
        # up a perfectly good call that had started since: reload the intercom
        # page, and the abandoned session takes the new one down with it a
        # minute and a half later. Only the last session on a device gets to
        # say goodbye.
        if send_bye and not others_live:
            self._publish(session.device_id, {"type": "bye"})
        elif send_bye:
            log.info(
                "WebRTC session %s closed; another session still holds %s, "
                "so no bye was sent", session_id[:8], session.device_id,
            )
        log.info("WebRTC session %s closed", session_id[:8])

    def _require(self, session_id: str) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    # ── relaying ────────────────────────────────────────────────────────

    def _publish(self, device_id: str, message: Dict[str, Any]) -> None:
        if self._client is None:
            raise RuntimeError("signaling bridge is not started")
        self._client.publish(
            self._to_device(device_id),
            json.dumps(message, separators=(",", ":")),
            qos=1,
        )

    def send(self, session_id: str, message: Dict[str, Any]) -> None:
        """Forward one browser message to the sentinel."""
        session = self._require(session_id)
        self._publish(session.device_id, message)

    def poll(self, session_id: str, timeout: float) -> List[Dict[str, Any]]:
        """Block until the sentinel says something, or the timeout expires."""
        session = self._require(session_id)
        session.last_polled = time.monotonic()
        timeout = max(0.0, min(timeout, MAX_POLL_TIMEOUT_S))

        messages: List[Dict[str, Any]] = []
        try:
            messages.append(session.inbox.get(timeout=timeout))
        except queue.Empty:
            return messages

        # Drain whatever else is already queued: a trickled candidate burst
        # should reach the peer connection in one round trip, not five.
        while True:
            try:
                messages.append(session.inbox.get_nowait())
            except queue.Empty:
                break
        session.last_polled = time.monotonic()
        return messages

    def _reap_idle_sessions(self) -> None:
        while True:
            time.sleep(15)
            cutoff = time.monotonic() - SESSION_IDLE_TIMEOUT_S
            with self._lock:
                stale = [s.session_id for s in self._sessions.values()
                         if s.last_polled < cutoff]
            for session_id in stale:
                log.info("WebRTC session %s went quiet, hanging up",
                         session_id[:8])
                self.close_session(session_id)

    # ── client configuration ────────────────────────────────────────────

    def ice_servers(self) -> List[Dict[str, Any]]:
        """ICE servers for the browser peer.

        The sentinel gets its own list from its settings.txt, so this only has
        to cover the browser. On a flat LAN host candidates are enough and the
        STUN entry is merely harmless; it starts to matter the moment operator
        and sentinel are on different subnets.
        """
        rtc_cfg = (parameters.get_parameters() or {}).get("webrtc", {}) or {}
        servers: List[Dict[str, Any]] = []

        stun_url = rtc_cfg.get("stun_url", "stun:stun.l.google.com:19302")
        if stun_url:
            servers.append({"urls": stun_url})

        turn_url = rtc_cfg.get("turn_url", "")
        if turn_url:
            servers.append({
                "urls": turn_url,
                "username": rtc_cfg.get("turn_user", ""),
                "credential": rtc_cfg.get("turn_password", ""),
            })
        return servers

    def video_profile(self) -> Dict[str, Any]:
        """Uplink JPEG geometry, which the firmware fixes rather than negotiates.

        With ``video_over_data_channel`` set, esp_webrtc never puts a video
        m-line in the SDP. It declares the decoder's geometry from the
        compile-time ``VIDEO_WIDTH``/``VIDEO_HEIGHT`` in settings.h instead, so
        frames of any other size are decoded against the wrong stream info.
        These two values have to track that header.
        """
        rtc_cfg = (parameters.get_parameters() or {}).get("webrtc", {}) or {}
        return {
            "width": int(rtc_cfg.get("video_width", 640)),
            "height": int(rtc_cfg.get("video_height", 480)),
            "fps": int(rtc_cfg.get("video_fps", 8)),
            "quality": float(rtc_cfg.get("jpeg_quality", 0.6)),
        }


_bridge: Optional[SignalingBridge] = None
_bridge_lock = threading.Lock()


def get_bridge() -> SignalingBridge:
    """The process-wide bridge, created on first use."""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = SignalingBridge()
        return _bridge
