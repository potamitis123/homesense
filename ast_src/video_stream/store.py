"""Latest-frame store for the sentinels' MJPEG uplink.

One JPEG per device is held in memory and overwritten as the next one lands.
Nothing is queued and nothing is written to disk: this feed is for knowing
what is in front of a sentinel *now*, and a viewer that falls behind should
skip to the present rather than replay a backlog. Recording, if it is ever
wanted, belongs with the existing event recorder, not here.

Readers block on a condition variable rather than polling, so a browser
watching the stream costs one waiting thread and no CPU between frames.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from misc.load_parameters import parameters

log = logging.getLogger(__name__)

# A frame bigger than this is not a 640x480 JPEG from a sentinel, so it is
# refused rather than kept: the store is memory the Dash process cannot page.
DEFAULT_MAX_FRAME_BYTES = 1024 * 1024

# How long after its last frame a device stops counting as live. Three
# missed frames at the firmware's slowest sane rate.
DEFAULT_STALE_AFTER_S = 15.0

# A viewer that has waited this long with nothing to send is dropped, so a
# closed browser tab cannot pin a server thread forever on a device that
# never streams.
DEFAULT_VIEWER_IDLE_TIMEOUT_S = 120.0

# Frames the rate estimate is averaged over.
_RATE_WINDOW = 20


@dataclass
class Frame:
    """One JPEG, with the sequence number a viewer resumes from."""

    jpeg: bytes
    seq: int
    timestamp: float


@dataclass
class _Device:
    device_id: str
    frame: Optional[Frame] = None
    frames: int = 0
    bytes_total: int = 0
    first_seen: float = field(default_factory=time.time)
    recent: Deque[float] = field(default_factory=lambda: deque(maxlen=_RATE_WINDOW))


class FrameStore:
    """Newest frame per device, plus the counters the dashboard reports."""

    def __init__(self) -> None:
        cfg = (parameters.get_parameters() or {}).get("video_stream", {}) or {}
        self.max_frame_bytes = int(
            cfg.get("max_frame_bytes") or DEFAULT_MAX_FRAME_BYTES)
        self.stale_after = float(
            cfg.get("stale_after_seconds") or DEFAULT_STALE_AFTER_S)
        self.viewer_idle_timeout = float(
            cfg.get("viewer_idle_timeout_seconds")
            or DEFAULT_VIEWER_IDLE_TIMEOUT_S)

        self._devices: Dict[str, _Device] = {}
        # One condition for every device. There are a handful of sentinels and
        # a handful of viewers; waking all of them on each frame costs less
        # than the bookkeeping of per-device locks would.
        self._cond = threading.Condition()
        self._seq = 0

    # ── ingest ──────────────────────────────────────────────────────────

    def put(self, device_id: str, jpeg: bytes) -> Frame:
        """Store one frame and wake everyone watching that device."""
        with self._cond:
            device = self._devices.get(device_id)
            if device is None:
                device = _Device(device_id=device_id)
                self._devices[device_id] = device
                log.info("Video uplink: %s started streaming (%d bytes/frame)",
                         device_id, len(jpeg))
            elif not self._is_live_locked(device):
                log.info("Video uplink: %s resumed streaming", device_id)

            self._seq += 1
            frame = Frame(jpeg=jpeg, seq=self._seq, timestamp=time.time())
            device.frame = frame
            device.frames += 1
            device.bytes_total += len(jpeg)
            device.recent.append(frame.timestamp)
            self._cond.notify_all()
            return frame

    # ── viewers ─────────────────────────────────────────────────────────

    def latest(self, device_id: str) -> Optional[Frame]:
        with self._cond:
            device = self._devices.get(device_id)
            return device.frame if device else None

    def wait_for(self, device_id: str, after_seq: int,
                 timeout: float) -> Optional[Frame]:
        """Block for the first frame newer than ``after_seq``.

        Returns None on timeout, which the caller uses as its cue to send a
        keepalive and so notice a viewer that has gone away.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                device = self._devices.get(device_id)
                frame = device.frame if device else None
                if frame is not None and frame.seq > after_seq:
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    # ── status ──────────────────────────────────────────────────────────

    def _is_live_locked(self, device: _Device) -> bool:
        return (device.frame is not None
                and time.time() - device.frame.timestamp <= self.stale_after)

    def is_live(self, device_id: str) -> bool:
        with self._cond:
            device = self._devices.get(device_id)
            return bool(device and self._is_live_locked(device))

    def status(self, device_id: str) -> Optional[Dict[str, object]]:
        with self._cond:
            device = self._devices.get(device_id)
            return self._status_locked(device) if device else None

    def devices(self) -> List[Dict[str, object]]:
        with self._cond:
            return [self._status_locked(d) for d in self._devices.values()]

    def _status_locked(self, device: _Device) -> Dict[str, object]:
        fps = 0.0
        if len(device.recent) >= 2:
            span = device.recent[-1] - device.recent[0]
            if span > 0:
                fps = (len(device.recent) - 1) / span
        frame = device.frame
        return {
            "device_id": device.device_id,
            "live": self._is_live_locked(device),
            "frames": device.frames,
            "bytes": device.bytes_total,
            "fps": round(fps, 2),
            "frame_bytes": len(frame.jpeg) if frame else 0,
            "last_frame_at": frame.timestamp if frame else None,
            "age_seconds": round(time.time() - frame.timestamp, 1) if frame else None,
            "first_seen": device.first_seen,
        }


_store: Optional[FrameStore] = None
_store_lock = threading.Lock()


def get_store() -> FrameStore:
    """The process-wide store, created on first use."""
    global _store
    with _store_lock:
        if _store is None:
            _store = FrameStore()
        return _store
