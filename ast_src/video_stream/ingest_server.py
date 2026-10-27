"""Keep-alive listener for the sentinels' frame uplink.

Why this exists rather than just the Flask route in routes.py: Werkzeug's
development server, which is what serves the Dash app, closes the connection
after every response unconditionally --

    # Always close the connection. This disables HTTP/1.1
    # keep-alive connections.
    self.send_header("Connection", "close")
                                    -- werkzeug/serving.py, run_wsgi()

For a browser that costs nothing. For a sentinel posting a frame every 300 ms
over TLS it costs a full handshake per frame: on the P4 that is the single
most expensive thing in the loop, and it churns a few tens of kB of heap each
time. So frames get their own listener, on its own port, with HTTP/1.1
keep-alive left on -- one handshake when the sentinel boots, then frames flow
down the same socket for as long as the link holds.

It is deliberately tiny: no routing, no framework, one method. Frames land in
the same FrameStore the Flask endpoints read from, so the Visuals tab does not
know or care which door they came in through.

The Flask route (POST /video/api/ingest/<device_id>) stays as a fallback for a
sentinel that cannot reach this port; it works, it just reconnects per frame.
"""

from __future__ import annotations

import logging
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from misc.load_parameters import parameters

from ast_src.video_stream.store import get_store

log = logging.getLogger(__name__)

DEFAULT_PORT = 8060
DEFAULT_HOST = "0.0.0.0"

_server: Optional[ThreadingHTTPServer] = None
_lock = threading.Lock()


class _IngestHandler(BaseHTTPRequestHandler):
    """POST /ingest/<device_id>, body = one JPEG."""

    # The whole point of this listener. http.server only offers keep-alive on
    # HTTP/1.1, and only when every response carries an unambiguous length --
    # which a 204 does, by having no body at all.
    protocol_version = "HTTP/1.1"

    server_version = "SentinelIngest/1.0"

    def do_POST(self) -> None:  # noqa: N802  (http.server's naming)
        store = get_store()

        path = self.path.split("?", 1)[0].rstrip("/")
        prefix = "/ingest/"
        if not path.startswith(prefix) or len(path) <= len(prefix):
            # The body has not been read and would be parsed as the next
            # request line, so this connection ends here. Same reasoning as
            # the oversize case below.
            self._respond(404, b"expected POST /ingest/<device_id>", close=True)
            return
        device_id = path[len(prefix):]

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0:
            # Without a length there is no way to know where the body ends.
            self._respond(400, b"Content-Length required", close=True)
            return
        if length > store.max_frame_bytes:
            # The body is still on the socket and would be read as the next
            # request line, so this connection does not survive the refusal.
            self._respond(413, b"frame too large", close=True)
            return

        body = self._read_exactly(length)
        if body is None:
            self.close_connection = True
            return
        if not body.startswith(b"\xff\xd8"):
            self._respond(415, b"body is not a JPEG")
            return

        store.put(device_id, body)
        # 204: no body, so the connection stays unambiguous and is reused for
        # the next frame.
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0].rstrip("/") in ("/health", ""):
            self._respond(200, b"ok")
        else:
            self._respond(404, b"not found")

    # ── helpers ─────────────────────────────────────────────────────────

    def _read_exactly(self, length: int) -> Optional[bytes]:
        """Read the whole body, or give up on the connection.

        A partial frame is a broken connection, not a frame to store: the
        sentinel will reconnect and the next POST starts clean.
        """
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 32 * 1024))
            if not chunk:
                log.warning("Video ingest: %s cut a frame short (%d of %d "
                            "bytes)", self.path, length - remaining, length)
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _respond(self, code: int, body: bytes, close: bool = False) -> None:
        """Answer, optionally ending the connection.

        `close` is for the cases where the request body was never read: the
        header tells the client, and close_connection tells our own loop.
        Setting only the latter would leave the client reusing a socket we
        are about to drop.
        """
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # One line per frame at INFO would be several a second. The store
        # already counts them, and errors go through log.warning above.
        log.debug("Video ingest: " + fmt, *args)


class _IngestServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        # A sentinel that walks away mid-frame is routine, not a stack trace.
        log.debug("Video ingest: connection from %s failed", client_address,
                  exc_info=True)


def _tls_context() -> Optional[ssl.SSLContext]:
    """The same certificate the Dash app and the broker already present.

    The sentinel verifies it against the CA embedded in its firmware, so this
    must stay the cert issued to audiosense.hmu.gr rather than a new one.
    """
    cfg = parameters.get_parameters() or {}
    https_cfg = cfg.get("https", {}) or {}
    certfile = https_cfg.get("ca_certfile") or ""
    keyfile = https_cfg.get("ca_keyfile") or ""
    if not certfile or not keyfile:
        return None

    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".."))
    if not os.path.isabs(certfile):
        certfile = os.path.join(project_root, certfile)
    if not os.path.isabs(keyfile):
        keyfile = os.path.join(project_root, keyfile)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


def start() -> Optional[ThreadingHTTPServer]:
    """Start the listener, unless it is disabled or already up.

    Never raises: a port that is taken (the Flask reloader starting the app
    twice is the usual reason) leaves the uplink on its Flask fallback rather
    than taking the dashboard down with it.
    """
    global _server
    with _lock:
        if _server is not None:
            return _server

        cfg = (parameters.get_parameters() or {}).get("video_stream", {}) or {}
        if not cfg.get("ingest_enabled", True):
            log.info("Video ingest listener disabled by configuration")
            return None

        port = int(cfg.get("ingest_port") or DEFAULT_PORT)
        host = cfg.get("ingest_host") or DEFAULT_HOST
        want_tls = bool(cfg.get("ingest_tls", True))

        try:
            server = _IngestServer((host, port), _IngestHandler)
        except OSError as exc:
            log.warning("Video ingest listener not started on %s:%s (%s); "
                        "sentinels can still post to /video/api/ingest",
                        host, port, exc)
            return None

        scheme = "http"
        if want_tls:
            try:
                context = _tls_context()
            except (OSError, ssl.SSLError) as exc:
                log.error("Video ingest: cannot load the TLS certificate "
                          "(%s); refusing to fall back to plaintext", exc)
                server.server_close()
                return None
            if context is None:
                log.error("Video ingest: ingest_tls is on but no certificate "
                          "is configured under https in parameters.json")
                server.server_close()
                return None
            server.socket = context.wrap_socket(server.socket, server_side=True)
            scheme = "https"

        thread = threading.Thread(target=server.serve_forever,
                                  name="video-ingest", daemon=True)
        thread.start()
        _server = server
        log.info("Video ingest listening on %s://%s:%s/ingest/<device_id> "
                 "(keep-alive)", scheme, host, port)
        return server


def stop() -> None:
    """Shut the listener down. Mainly for tests."""
    global _server
    with _lock:
        if _server is None:
            return
        _server.shutdown()
        _server.server_close()
        _server = None


def address() -> Optional[str]:
    """host:port the listener is bound to, or None when it is not running."""
    with _lock:
        if _server is None:
            return None
        host, port = _server.server_address[:2]
        return f"{host}:{port}"


__all__ = ["start", "stop", "address"]
