"""Is a pipeline currently subscribed to the broker?

Publishes a status message for a throwaway device. Only the running pipeline
writes sentinel_status.json, so if the entry appears, it is listening.
Cleans up after itself.
"""
import json
import os
import time

import paho.mqtt.client as mqtt

DEVICE = "liveness-probe"
TOPIC = f"devices/{DEVICE}/status"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATUS_FILE = os.path.join(ROOT, "sentinel_status.json")

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="liveness-probe")
c.connect("localhost", 1883, 30)
c.loop_start()
c.publish(TOPIC, json.dumps({"status": "online"}), qos=1, retain=False)

alive = False
for _ in range(6):
    time.sleep(1)
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            if DEVICE in json.load(f):
                alive = True
                break
    except Exception:
        pass

print("PIPELINE IS RUNNING and consuming MQTT" if alive
      else "NO PIPELINE LISTENING - nothing consumed the message")

try:
    with open(STATUS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if data.pop(DEVICE, None) is not None:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
except Exception as e:
    print(f"cleanup failed: {e}")

c.loop_stop()
c.disconnect()
