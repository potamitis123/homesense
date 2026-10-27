"""Sync sentinel_status.json to what the broker actually knows, right now.

The pipeline does this automatically on connect, but a pipeline started before
that change is in place will not. Running this brings the file back in line
without restarting anything: subscribe, collect the retained status the broker
replays, and mark everything it does not know about as offline.
"""
import json
import os
import time

import paho.mqtt.client as mqtt

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATUS_FILE = os.path.join(ROOT, "sentinel_status.json")
WAIT = 5

confirmed = {}


def on_connect(client, userdata, flags, rc, properties=None):
    client.subscribe([("devices/+/status", 1), ("audio/status/#", 1),
                      ("ast_surveillance_v1/devices/+/status", 1)])


def on_message(client, userdata, msg):
    parts = msg.topic.strip("/").split("/")
    if parts and parts[0] == "ast_surveillance_v1":
        parts = parts[1:]
    if len(parts) < 3:
        return
    device_id = parts[1] if parts[0] == "devices" else parts[2]

    raw = msg.payload.decode("utf-8", "replace").strip()
    if not raw:
        return
    try:
        status = json.loads(raw).get("status", "offline")
    except Exception:
        status = "online" if "online" in raw.lower() else "offline"
    confirmed[device_id] = status
    print(f"  broker reports {device_id:<18} {status}")


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="reconciler")
c.on_connect = on_connect
c.on_message = on_message
c.connect("localhost", 1883, 30)
c.loop_start()
print(f"collecting retained device status for {WAIT}s ...")
time.sleep(WAIT)
c.loop_stop()
c.disconnect()

with open(STATUS_FILE, encoding="utf-8") as f:
    data = json.load(f)

changed = []
for name, info in data.items():
    was = info.get("status")
    now = confirmed.get(name, "offline")
    if was != now:
        info["status"] = now
        changed.append(f"{name}: {was} -> {now}")

with open(STATUS_FILE, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=4)

print("\nchanges:")
for line in changed or ["  (none - file already matched the broker)"]:
    print(f"  {line}" if changed else line)

print("\nfinal state:")
for name, info in sorted(data.items()):
    print(f"  {name:<18} {info['status']:<8} last_seen={info.get('last_seen')}")
