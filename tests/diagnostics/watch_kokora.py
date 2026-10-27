#!/usr/bin/env python3
"""Watch what the server actually sends when the Kokora stop command is heard.

    python tests/diagnostics/watch_kokora.py [seconds]

Subscribes to the production broker with the same credentials and client
certificate the Push-to-Talk transport uses, and reports two things:

  * which sentinels are uploading recordings (so you can see which one heard
    you), on audio/#
  * every play/stream message the server sends to a loudspeaker, on
    commands/+/play/# and commands/+/stream/#

Read-only: it subscribes and never publishes, so running it cannot disturb a
live sentinel.
"""

import os
import ssl
import sys
import time
from collections import Counter
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # pragma: no cover
    pass

import paho.mqtt.client as mqtt
from misc.secrets import mqtt_password

BROKER, PORT, USER = "audiosense.hmu.gr", 8883, "hlias"
CERTS = os.path.join(PROJECT_ROOT, "certs", "certs")

WATCH = [
    ("commands/+/play/info", 1),
    ("commands/+/play/complete", 1),
    ("commands/+/stream/info", 0),
    ("audio/+/stream/info", 0),
    ("devices/+/status", 1),
]

seen = Counter()
started = time.time()


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def on_connect(client, _u, _f, rc, *_):
    if rc != 0:
        print(f"  connect failed rc={rc}")
        return
    print(f"  connected to {BROKER}:{PORT}\n")
    for topic, qos in WATCH:
        client.subscribe(topic, qos)
        print(f"  watching {topic}")
    print("\n  Say it now. Ctrl-C to stop.\n" + "-" * 62)


def on_message(_c, _u, msg):
    topic = msg.topic
    seen[topic.split("/")[0]] += 1
    parts = topic.split("/")
    device = parts[1] if len(parts) > 1 else "?"

    if "/play/" in topic:
        # This is the acknowledgement being pushed to a loudspeaker: the whole
        # point of the test. If this never appears for p4-door-1, the server
        # never asked that device to speak.
        body = msg.payload[:120].decode("utf-8", "replace")
        print(f"{stamp()}  >>> PLAY -> {device}   {topic}")
        print(f"           {body}")
    elif topic.startswith("audio/"):
        print(f"{stamp()}  recording uploaded by {device}")
    elif topic.startswith("devices/"):
        state = msg.payload.decode("utf-8", "replace")[:40]
        print(f"{stamp()}  status {device}: {state}")
    elif "/stream/" in topic:
        print(f"{stamp()}  stream -> {device}   {topic}")


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    client = mqtt.Client(client_id=f"kokora-watch-{int(time.time())}")
    client.username_pw_set(USER, mqtt_password())
    client.tls_set(ca_certs=os.path.join(CERTS, "ca.crt"),
                   certfile=os.path.join(CERTS, "hlias.crt"),
                   keyfile=os.path.join(CERTS, "hlias.key"),
                   tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"connecting as {USER} ...")
    client.connect(BROKER, PORT, keepalive=30)
    client.loop_start()
    try:
        while time.time() - started < seconds:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    client.loop_stop()
    client.disconnect()

    print("-" * 62)
    print(f"  {int(time.time() - started)}s watched; messages by prefix: "
          f"{dict(seen) or 'none'}")


if __name__ == "__main__":
    main()
