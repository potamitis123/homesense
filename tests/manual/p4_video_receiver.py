"""Receive and watch a P4 sentinel's OV5647 uplink, without the dashboard.

The dashboard takes a while to come up (torch, the DB, the pipeline), which is
a lot to wait through when the only question is "is the camera streaming?".
This runs the same ingest listener and the same MJPEG endpoints the Dash app
would, and nothing else:

    python tests/manual/p4_video_receiver.py

    -> https ingest on :8060  (what the sentinel posts to, per settings.txt)
    -> preview   on http://localhost:8070  (open this in a browser)

Frames land in the same FrameStore ast_dash reads from, so what you see here
is exactly what the Visuals tab will show once the dashboard is started.
Nothing is written to disk unless --save-dir is given.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
os.chdir(project_root)

from flask import Flask

from misc.load_parameters import parameters
from ast_src.video_stream import get_store
from ast_src.video_stream.routes import register

PREVIEW_PORT = 8070

INDEX = """<!doctype html>
<title>P4 uplink preview</title>
<style>
 body{background:#101014;color:#ddd;font:14px system-ui;margin:0;padding:20px}
 h1{font-size:18px;margin:0 0 4px} .sub{color:#888;margin-bottom:16px}
 .cam{margin-bottom:24px} img{max-width:100%;border:1px solid #2a2a35;
   border-radius:8px;background:#000;display:block}
 .meta{color:#8a8;margin:6px 0 0;font-variant-numeric:tabular-nums}
 .none{color:#a66}
</style>
<h1>What the sentinel sees</h1>
<div class="sub">Same store and same endpoints as the Visuals tab.</div>
<div id="cams"><div class="none">Waiting for the first frame&hellip;</div></div>
<script>
const seen = {};
async function tick() {
  let devices = [];
  try {
    const r = await fetch('/video/api/devices');
    devices = (await r.json()).devices || [];
  } catch (e) { return; }
  const root = document.getElementById('cams');
  if (!devices.length) return;
  for (const d of devices) {
    let box = seen[d.device_id];
    if (!box) {
      if (root.querySelector('.none')) root.innerHTML = '';
      box = document.createElement('div');
      box.className = 'cam';
      // src is set once: changing it would tear the MJPEG stream down.
      box.innerHTML = '<img src="/video/api/stream/' + d.device_id + '">' +
                      '<p class="meta"></p>';
      root.appendChild(box);
      seen[d.device_id] = box;
    }
    box.querySelector('.meta').textContent =
      d.device_id + ' - ' + (d.live ? 'live' : 'STALE') + ' - ' +
      d.fps.toFixed(1) + ' fps - ' + Math.round(d.frame_bytes/1024) +
      ' kB/frame - ' + d.frames + ' frames - last ' + d.age_seconds + 's ago';
  }
}
tick(); setInterval(tick, 2000);
</script>
"""


def status_loop(save_dir: str | None) -> None:
    """One line per second, so the console alone tells you if it is working."""
    store = get_store()
    last_counts: dict[str, int] = {}
    quiet_since = time.time()
    while True:
        time.sleep(1.0)
        devices = store.devices()
        if not devices:
            if time.time() - quiet_since > 10:
                print("  ...no frames yet. Check: settings.txt has "
                      "ov5647_video_enabled=1, the unit is on the network, "
                      "and 8060 is not blocked by the firewall.")
                quiet_since = time.time()
            continue
        for d in devices:
            dev = d["device_id"]
            delta = d["frames"] - last_counts.get(dev, 0)
            last_counts[dev] = d["frames"]
            print(f"  {dev:<16} {'live ' if d['live'] else 'STALE'} "
                  f"{d['fps']:5.2f} fps  {d['frame_bytes']//1024:4d} kB/frame  "
                  f"{d['frames']:6d} frames  (+{delta} this second)")
            if save_dir and delta:
                frame = store.latest(dev)
                if frame:
                    path = os.path.join(save_dir, f"{dev}.jpg")
                    with open(path, "wb") as handle:
                        handle.write(frame.jpeg)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preview-port", type=int, default=PREVIEW_PORT)
    ap.add_argument("--save-dir", default=None,
                    help="write each device's newest frame here as <id>.jpg")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = (parameters.get_parameters() or {}).get("video_stream", {}) or {}
    port = cfg.get("ingest_port", 8060)
    scheme = "https" if cfg.get("ingest_tls", True) else "http"

    app = Flask(__name__)

    @app.get("/")
    def index():
        return INDEX

    # Registers the MJPEG endpoints and starts the ingest listener.
    register(app)

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)

    print()
    print(f"  sentinel posts to : {scheme}://audiosense.hmu.gr:{port}/ingest/<device_id>")
    print(f"  preview           : http://localhost:{args.preview_port}")
    print(f"  device list       : http://localhost:{args.preview_port}/video/api/devices")
    print()

    threading.Thread(target=status_loop, args=(args.save_dir,),
                     name="status", daemon=True).start()
    # threaded so the MJPEG stream and the ingest listener do not block each
    # other; no reloader, which would start the listener twice.
    app.run(host="127.0.0.1", port=args.preview_port, threaded=True,
            debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
