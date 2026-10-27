"""Sentinel device status for mobile: online/offline + last_seen, no MQTT
credentials or broker/network details exposed."""

from __future__ import annotations

from mobile_api.adapters import existing_app_adapter as existing
from mobile_api.config import load_device_capabilities

_DEFAULT_CAPS = {"duplex": False, "video": False}


def get_devices() -> list[dict]:
    registered_df = existing.strip_device_secrets(existing.get_registered_devices())
    status = existing.get_sentinel_status()
    capabilities = load_device_capabilities()

    registered_names = set()
    devices: list[dict] = []

    if not registered_df.empty:
        for _, row in registered_df.iterrows():
            device_id = str(row.get("device_name") or "")
            if not device_id:
                continue
            registered_names.add(device_id)
            st = status.get(device_id, {})
            devices.append({
                "device_id": device_id,
                "name": device_id,
                "online": st.get("status") == "online",
                "last_seen": st.get("last_seen"),
                "ip_address": st.get("ip_address"),
                "capabilities": capabilities.get(device_id, _DEFAULT_CAPS),
            })

    # Devices seen on MQTT but not (yet) present in the remote_devices table.
    for device_id, st in status.items():
        if device_id in registered_names:
            continue
        devices.append({
            "device_id": device_id,
            "name": device_id,
            "online": st.get("status") == "online",
            "last_seen": st.get("last_seen"),
            "ip_address": st.get("ip_address"),
            "capabilities": capabilities.get(device_id, _DEFAULT_CAPS),
        })

    devices.sort(key=lambda d: d["device_id"])
    return devices
