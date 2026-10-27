"""
Minimal in-memory rate limiter, applied only to POST /api/v1/auth/login.

Sliding window per source IP, no external dependency (no slowapi/limits
package) -- consistent with "not heavy stuff, no security libs" for this
research project. Not suitable for a multi-process/multi-instance
deployment (state is per-process), which matches the rest of this
module's in-memory-only design (see authentication.TokenStore).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int = 60):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._hits: dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self._window_seconds
        with self._lock:
            hits = [t for t in self._hits[key] if t > cutoff]
            if len(hits) >= self._max_requests:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


# Populated by mobile_api.main at startup from MobileApiConfig.
login_rate_limiter: SlidingWindowRateLimiter | None = None
