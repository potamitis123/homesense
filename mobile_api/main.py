"""
Mobile API entrypoint.

    python -u mobile_api/main.py

Single integration point for the rest of the app: `start_mobile_api()`
(also re-exported from mobile_api/__init__.py) builds the FastAPI app,
wires config-driven auth/rate-limit/audio-session state, and runs uvicorn.
Nothing in the existing ast_src/ast_dash trees imports from mobile_api;
the only direction of dependency is mobile_api -> existing app.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import uvicorn
from fastapi import FastAPI

from mobile_api.config import load_config
from mobile_api.logging_setup import get_logger, log_extra
from mobile_api.routes import (
    audio,
    auth,
    dashboard,
    devices,
    events,
    heatmap,
    recordings,
    statistics,
    webrtc,
    webrtc_page,
)
from mobile_api.security import authentication, rate_limit
from mobile_api.services import mobile_audio_service

logger = get_logger()

_IDLE_SWEEP_INTERVAL_SECONDS = 60


def create_app() -> FastAPI:
    config = load_config()

    authentication.token_store = authentication.TokenStore(
        access_minutes=config.access_token_minutes,
        refresh_days=config.refresh_token_days,
    )
    rate_limit.login_rate_limiter = rate_limit.SlidingWindowRateLimiter(
        max_requests=config.login_rate_limit_per_minute,
    )
    mobile_audio_service.audio_session_service = mobile_audio_service.AudioSessionService(
        timeout_minutes=config.audio_session_timeout_minutes,
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        async def _sweep_loop():
            while True:
                await asyncio.sleep(_IDLE_SWEEP_INTERVAL_SECONDS)
                try:
                    mobile_audio_service.audio_session_service.sweep_idle_sessions()
                except Exception as e:
                    logger.error(f"idle sweep error: {e}", extra=log_extra("MOBILE_AUDIO"))

        task = asyncio.create_task(_sweep_loop())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(
        title="Sentinel Home Server Mobile API",
        description="Thin adapter/gateway over the existing Sentinel Home Server, for the Android app. "
                    "See mobile_api/README.md.",
        version="1.0.0",
        lifespan=_lifespan,
    )

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(events.router)
    app.include_router(recordings.router)
    app.include_router(statistics.router)
    app.include_router(heatmap.router)
    app.include_router(devices.router)
    app.include_router(audio.router)
    app.include_router(webrtc.router)
    app.include_router(webrtc_page.router)

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()


def start_mobile_api() -> None:
    config = load_config()
    logger.info(f"starting on {config.host}:{config.port} (https={config.https})", extra=log_extra("MOBILE_API"))

    ssl_kwargs = {}
    if config.https and os.path.exists(config.ca_certfile) and os.path.exists(config.ca_keyfile):
        ssl_kwargs = {"ssl_certfile": config.ca_certfile, "ssl_keyfile": config.ca_keyfile}
    elif config.https:
        logger.warning(
            "https requested but cert files not found; falling back to plain HTTP",
            extra=log_extra("MOBILE_API"),
        )

    uvicorn.run(app, host=config.host, port=config.port, **ssl_kwargs)


if __name__ == "__main__":
    start_mobile_api()
