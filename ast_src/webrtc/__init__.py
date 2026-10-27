"""Operator-to-sentinel WebRTC.

The peer connection itself lives in the browser (``assets/webrtc_intercom_v2.js``);
this package is only the signaling half. Import :func:`register` from
``ast_src.webrtc.routes`` to expose the HTTP endpoints on a Flask app.
"""

from .signaling import SignalingBridge, get_bridge

__all__ = ["SignalingBridge", "get_bridge"]
