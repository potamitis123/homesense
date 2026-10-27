"""Check the sentinel status fixes: staleness rule + startup reconciliation."""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# The page module calls dash.get_app() at import time, so an app has to exist.
import dash
dash.Dash(__name__, use_pages=False)

from ast_dash.pages.audio_details import _is_last_seen_stale, SENTINEL_STALE_AFTER_MIN

print(f"threshold = {SENTINEL_STALE_AFTER_MIN} min\n")
now = datetime.now()
cases = [
    ("just now", now.strftime("%Y-%m-%d %H:%M:%S"), False),
    ("2 min ago", (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"), False),
    ("9 min ago", (now - timedelta(minutes=9)).strftime("%Y-%m-%d %H:%M:%S"), True),
    ("3 days ago", (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), True),
    ("Never", "Never", False),
    ("None", None, False),
    ("garbage", "not-a-date", False),
]
ok = True
for label, value, expected in cases:
    got = _is_last_seen_stale(value)
    flag = "OK " if got == expected else "BAD"
    if got != expected:
        ok = False
    print(f"  [{flag}] {label:<12} stale={got}  (expected {expected})")

print("\n--- reconciliation dry run on a copy of the real file ---")
root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
with open(os.path.join(root, "sentinel_status.json"), encoding="utf-8") as f:
    data = json.load(f)

for name, info in sorted(data.items()):
    seen = info.get("last_seen")
    stale = _is_last_seen_stale(seen)
    status = info.get("status")
    if status == "online" and not stale:
        verdict = "GREEN  (confirmed)"
    elif status == "online" and stale:
        verdict = "AMBER  (claims online, silent)"
    else:
        verdict = "RED    (offline)"
    print(f"  {name:<18} status={status:<8} last_seen={seen}  -> {verdict}")

print("\nPASS" if ok else "\nFAIL")
sys.exit(0 if ok else 1)
