"""Subscribe to every topic on the local broker and report what arrives.

Answers two questions:
  1. Do the sentinels publish anything resembling a heartbeat?
  2. Which devices are actually talking right now?

Run:  python temp_scripts/mqtt_sniff.py [seconds]
"""
import sys
import time
from collections import defaultdict
from datetime import datetime

import paho.mqtt.client as mqtt

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 90

topics = defaultdict(lambda: {"count": 0, "first": None, "last": None, "sample": b""})
retained = {}
start = time.time()


def on_connect(client, userdata, flags, rc, properties=None):
    print(f"connected rc={rc}; subscribing to '#'")
    client.subscribe("#", qos=1)


def on_message(client, userdata, msg):
    now = time.time()
    # Audio chunks would drown out everything else; collapse them.
    key = msg.topic
    if "/chunk/" in key:
        key = key.rsplit("/chunk/", 1)[0] + "/chunk/*"

    entry = topics[key]
    entry["count"] += 1
    if entry["first"] is None:
        entry["first"] = now
        entry["sample"] = msg.payload[:120]
    entry["last"] = now

    if msg.retain and msg.topic not in retained:
        retained[msg.topic] = msg.payload[:120]

    if "chunk" not in msg.topic:
        stamp = datetime.now().strftime("%H:%M:%S")
        flag = " [RETAINED]" if msg.retain else ""
        print(f"{stamp}  {msg.topic}{flag}  {msg.payload[:100]!r}")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sniffer-probe")
client.on_connect = on_connect
client.on_message = on_message
client.connect("localhost", 1883, 60)
client.loop_start()

print(f"Listening on localhost:1883 for {DURATION}s ...\n")
time.sleep(DURATION)
client.loop_stop()

print("\n" + "=" * 78)
print(f" TOPIC SUMMARY over {DURATION}s")
print("=" * 78)
if not topics:
    print("  NOTHING RECEIVED - no device published anything in this window.")
for topic, e in sorted(topics.items()):
    span = e["last"] - e["first"]
    if e["count"] > 1:
        cadence = f"every ~{span / (e['count'] - 1):.1f}s"
    else:
        cadence = "once"
    print(f"  {e['count']:5d} msg  {cadence:<18} {topic}")
    print(f"                             sample={e['sample'][:80]!r}")

print("\n" + "=" * 78)
print(" RETAINED MESSAGES (what the broker replays to any new subscriber)")
print("=" * 78)
if not retained:
    print("  none")
for topic, payload in sorted(retained.items()):
    print(f"  {topic}  =  {payload!r}")
