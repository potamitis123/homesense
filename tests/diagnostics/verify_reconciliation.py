"""End-to-end test of startup reconciliation against the live broker.

Backs up sentinel_status.json, deliberately corrupts it so every device claims
to be online (the stale state the bug produced), then starts a real adapter.
The reset should blank all of them and the broker's retained status should
restore only the devices it genuinely knows about.

Restores the backup afterwards either way.
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATUS = os.path.join(ROOT, "sentinel_status.json")
BACKUP = STATUS + ".verify-backup"


def show(title):
    with open(STATUS, encoding="utf-8") as f:
        data = json.load(f)
    print(f"\n{title}")
    for name, info in sorted(data.items()):
        print(f"   {name:<18} {info.get('status'):<8} last_seen={info.get('last_seen')}")
    return data


shutil.copyfile(STATUS, BACKUP)
try:
    with open(STATUS, encoding="utf-8") as f:
        data = json.load(f)
    for name in data:
        data[name]["status"] = "online"
        data[name]["last_seen"] = "2026-07-30 20:06:36"
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

    show("BEFORE - every device forced to a stale 'online' (the bug state):")

    from ast_src.communication.adapters import LocalMQTTAdapter

    print("\nstarting adapter against localhost:1883 ...")
    adapter = LocalMQTTAdapter(host="localhost", port=1883,
                               ca_certfile="", certfile="", keyfile="")
    adapter.start()
    time.sleep(8)
    adapter.stop()

    after = show("AFTER - reset, then restored from the broker's retained state:")

    print("\n" + "=" * 72)
    absent = [n for n, i in after.items() if i.get("status") != "online"]
    present = [n for n, i in after.items() if i.get("status") == "online"]
    print(f" confirmed online by broker : {', '.join(sorted(present)) or 'none'}")
    print(f" not known to broker        : {', '.join(sorted(absent)) or 'none'}")
    if "p4-intercom-01" in absent:
        print("\n RESULT: PASS - p4-intercom-01 is no longer claiming to be online.")
    else:
        print("\n RESULT: FAIL - p4-intercom-01 still shows online.")
    print("=" * 72)
finally:
    shutil.move(BACKUP, STATUS)
    print("\nrestored original sentinel_status.json")
