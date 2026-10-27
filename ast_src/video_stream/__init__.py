"""Continuous MJPEG uplink from the ESP32-P4 sentinels.

The sentinel posts one JPEG per frame (firmware:
``p4_webrtc_firmware/video_stream.c``, enabled with ``ov5647_video_enabled=1``
in its settings.txt). This package keeps the newest frame per device and
re-serves it as MJPEG to the Visuals tab.

Frames come in through one of two doors, both feeding the same store:

    :8060 /ingest/<device_id>            ingest_server.py -- keep-alive, the
                                         one the firmware uses by default
    :8050 /video/api/ingest/<device_id>  routes.py -- the Dash app's own
                                         server, which closes the connection
                                         after every frame

and go out through ``/video/api/stream/<device_id>`` on the Dash app.

Deliberately separate from ``ast_src.webrtc``: that path only exists while an
operator has a call up, this one runs continuously and must survive calls
starting and ending. Import :func:`register` from ``ast_src.video_stream.routes``
to expose the HTTP endpoints on a Flask app.
"""

from .store import Frame, FrameStore, get_store

__all__ = ["Frame", "FrameStore", "get_store"]

# ingest_server is imported by routes.py rather than here: importing it eagerly
# would drag ssl and the certificate paths into every consumer of the store,
# including the Dash page that only wants to know whether a device is live.
