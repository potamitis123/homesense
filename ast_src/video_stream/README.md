# Video uplink (ESP32-P4 OV5647 → server)

A continuous, low-rate MJPEG feed from each P4 sentinel's camera, for
situational awareness: seeing who is at the door without ringing it. It is
**not** part of the video-call path in `ast_src/webrtc`, and the two run at the
same time without interacting.

```
P4 sentinel                          home server (the Dash process)
-----------                          ------------------------------
OV5647 --> esp_capture sink 1
             |
             |  HTTPS POST, one JPEG per frame, 2-5 fps
             +------------------> :8060 /ingest/<device_id>   ingest_server.py
                                        (keep-alive, one TLS handshake)
                                              |
                                          FrameStore            store.py
                                        newest frame per device
                                              |
             browser <---------------- :8050 /video/api/stream/<device_id>
                     multipart/x-mixed-replace     routes.py
                          |
                  Visuals tab, "What the sentinel sees"   ast_dash/pages/visuals.py
```

## Why one POST per frame

The obvious alternative, a single long chunked upload, saves a few hundred
bytes of headers per frame and costs the property that actually matters here:
every frame is independently framed by its own `Content-Length`, so a
connection that dies mid-stream loses exactly one frame and the next POST
starts clean. The server holds no per-device connection state that can wedge.
Frames are never queued — a viewer that falls behind skips to the present.

## Why a second port

Werkzeug's development server, which serves the Dash app on 8050, closes the
connection after every response unconditionally:

> Always close the connection. This disables HTTP/1.1 keep-alive connections.
> — `werkzeug/serving.py`, `run_wsgi()`

For a browser that is free. For a sentinel posting three frames a second over
TLS it is a full handshake per frame — the most expensive thing in the
firmware's loop, and a few tens of kB of heap churned each time. So frames get
their own listener, in the same process, with keep-alive left on: one handshake
when the sentinel boots, then frames flow down the same socket.

`POST /video/api/ingest/<device_id>` on 8050 accepts frames too, at that cost.
It is the fallback for a sentinel that cannot reach 8060.

## Endpoints

| Method | Path | Port | Description |
|---|---|---|---|
| POST | `/ingest/<device_id>` | 8060 | One JPEG, body is the raw image. `204` on success |
| POST | `/video/api/ingest/<device_id>` | 8050 | Same, without keep-alive (fallback) |
| GET | `/video/api/stream/<device_id>` | 8050 | Continuous MJPEG, for an `<img>` or any MJPEG client |
| GET | `/video/api/frame/<device_id>` | 8050 | The newest frame, once, as `image/jpeg` |
| GET | `/video/api/devices` | 8050 | Who is streaming, with fps, frame size and age |
| GET | `/health` | 8060 | Liveness of the ingest listener |

Ingest rejects anything that is not a JPEG (`415`), empty (`400`) or larger
than `max_frame_bytes` (`413`). No authentication: same trust model as
`/webrtc/api`, i.e. LAN-only. Do not port-forward 8060.

## Configuration

`common_files/parameters.json`, section `video_stream`:

```json
"video_stream": {
    "ingest_enabled": true,
    "ingest_host": "0.0.0.0",
    "ingest_port": 8060,
    "ingest_tls": true,
    "max_frame_bytes": 1048576,
    "stale_after_seconds": 15,
    "viewer_idle_timeout_seconds": 120
}
```

`ingest_tls` reuses the certificate the Dash app and Mosquitto already present
(`https.ca_certfile` / `https.ca_keyfile`) — no new certificates. That cert is
issued to `audiosense.hmu.gr`, which is the name the sentinel must use in its
`VIDEO_STREAM_URL`; the LAN resolves it through `dns_redirector.py`. There is no
plaintext fallback: if TLS is on and the certificate will not load, the listener
refuses to start rather than quietly downgrading.

`stale_after_seconds` is when a device stops counting as live in the Visuals
panel. `viewer_idle_timeout_seconds` bounds how long a browser can sit on the
stream endpoint of a device that has never sent anything.

## Sentinel side

`p4_webrtc_firmware/settings.txt` on the unit's SD card:

```text
ov5647_video_enabled=1
VIDEO_STREAM_URL=https://audiosense.hmu.gr:8060/ingest
VIDEO_STREAM_WIDTH=640
VIDEO_STREAM_HEIGHT=480
VIDEO_STREAM_FPS=3
```

`DEVICE_ID` is appended to the URL as a path element, so the same line works on
every unit. The setting is absent-tolerant and defaults to **off**, so an
existing installation is unaffected until its settings.txt says otherwise.

## Testing without a sentinel

```bash
curl -k --data-binary @frame.jpg -H "Content-Type: image/jpeg" https://audiosense.hmu.gr:8060/ingest/p4-door-1
```

Then open the Visuals tab and pick `p4-door-1`, or check
`https://audiosense.hmu.gr:8050/video/api/devices`. A single POST shows up as a
still frame that goes stale after `stale_after_seconds`; loop the curl to see
it move.
