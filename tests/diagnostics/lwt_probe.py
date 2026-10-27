"""Verify the server still reacts to an MQTT Last Will.

Simulates a sentinel: connects with a will registered on the same topic the
firmware uses, announces itself online, then dies without sending DISCONNECT so
the broker has to publish the will on keepalive timeout.

Then watches sentinel_status.json to see whether the running pipeline wrote the
offline state back. Cleans up after itself.
"""
import json
import os
import time

import paho.mqtt.client as mqtt

DEVICE = "lwt-probe-01"
TOPIC = f"devices/{DEVICE}/status"
KEEPALIVE = 5
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATUS_FILE = os.path.join(ROOT, "sentinel_status.json")


def read_status():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get(DEVICE, {})
    except Exception:
        return {}


def step(msg):
    print(f"\n--- {msg}")


step("connecting with will registered")
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="lwt-probe")
c.will_set(TOPIC, json.dumps({"status": "offline"}), qos=1, retain=True)
c.connect("localhost", 1883, KEEPALIVE)
c.loop_start()
time.sleep(1)

step("publishing online")
c.publish(TOPIC, json.dumps({"status": "online"}), qos=1, retain=True)
time.sleep(3)
print(f"    sentinel_status.json -> {read_status()}")

step("killing the socket without DISCONNECT (simulates power loss)")
c.loop_stop()
try:
    c._sock.close()
except Exception as e:
    print(f"    socket close: {e}")

step(f"waiting for broker keepalive timeout (~{KEEPALIVE * 1.5:.0f}s) then the will")
deadline = time.time() + 45
seen_offline = False
while time.time() < deadline:
    time.sleep(2)
    st = read_status()
    print(f"    t+{45 - int(deadline - time.time()):>2}s  {st}")
    if st.get("status") == "offline":
        seen_offline = True
        break

print("\n" + "=" * 70)
if seen_offline:
    print(" RESULT: PASS - server received the will and wrote 'offline'.")
else:
    print(" RESULT: FAIL - no offline state was written; LWT is not reaching")
    print("         the status file.")
print("=" * 70)

step("cleanup: clearing retained message and status entry")
c2 = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="lwt-probe-cleanup")
c2.connect("localhost", 1883, 30)
c2.loop_start()
c2.publish(TOPIC, "", qos=1, retain=True)
time.sleep(2)
c2.loop_stop()
c2.disconnect()

try:
    with open(STATUS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.pop(DEVICE, None) is not None:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        print(f"    removed {DEVICE} from sentinel_status.json")
except Exception as e:
    print(f"    cleanup failed: {e}")
