"""
Mobile API configuration.

Reads the "mobile_api" section of common_files/parameters.json via the
existing misc.load_parameters.parameters singleton -- the same pattern
ast_src/audio_transport and the (now removed) ast_src/api_server.py used
for their own config. No new config file format is introduced.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from misc.load_parameters import parameters

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@dataclass(frozen=True)
class MobileApiConfig:
    host: str
    port: int
    https: bool
    ca_certfile: str
    ca_keyfile: str
    access_token_minutes: int
    refresh_token_days: int
    audio_session_timeout_minutes: int
    login_rate_limit_per_minute: int


def load_config() -> MobileApiConfig:
    params = parameters.get_parameters() or {}
    section = params.get("mobile_api") or {}
    https_section = params.get("https") or {}

    return MobileApiConfig(
        host=section.get("host", "0.0.0.0"),
        port=int(section.get("port", 8001)),
        https=bool(section.get("https", True)),
        # Reuse the same TLS cert files ast_app.py and Mosquitto already use --
        # no new certificates are generated for this module.
        ca_certfile=os.path.join(_PROJECT_ROOT, https_section.get("ca_certfile", "certs/certs/hlias.crt")),
        ca_keyfile=os.path.join(_PROJECT_ROOT, https_section.get("ca_keyfile", "certs/certs/hlias.key")),
        access_token_minutes=int(section.get("access_token_minutes", 30)),
        refresh_token_days=int(section.get("refresh_token_days", 30)),
        audio_session_timeout_minutes=int(section.get("audio_session_timeout_minutes", 5)),
        login_rate_limit_per_minute=int(section.get("login_rate_limit_per_minute", 5)),
    )


PROJECT_ROOT = _PROJECT_ROOT


def load_device_capabilities() -> dict[str, dict[str, bool]]:
    """
    Which Sentinels support duplex audio (Push-to-Talk) and/or video call.

    No existing data source encodes this: remote_devices has no hardware-
    type column, and device_name is planned to become room names
    ("kitchen", "living_room"), which rules out inferring hardware type
    from a "box3"/"p4" substring in the name. So this is a small static
    map the user maintains directly in parameters.json's
    mobile_api.device_capabilities section as devices are added/renamed.
    Devices absent from the map default to {duplex: false, video: false}.
    """
    params = parameters.get_parameters() or {}
    section = params.get("mobile_api") or {}
    raw = section.get("device_capabilities") or {}
    result: dict[str, dict[str, bool]] = {}
    for device_id, caps in raw.items():
        result[device_id] = {
            "duplex": bool((caps or {}).get("duplex", False)),
            "video": bool((caps or {}).get("video", False)),
        }
    return result
