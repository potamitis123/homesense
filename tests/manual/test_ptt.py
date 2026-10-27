#!/usr/bin/env python3
"""
test_ptt.py
-----------
Dash-based push-to-talk UI that replicates the exact behaviour of
stream_to_box3_MQTT_3.py --push-to-talk.

Hold SPACEBAR → speak → release → utterance is normalised, padded,
encoded to WAV and published to the ESP32-BOX-3 via MQTT.

Usage
-----
    python test_ptt.py

    # Defaults match the CLI invocation:
    # python stream_to_box3_MQTT_3.py
    #   --broker audiosense.hmu.gr
    #   --device box3-1
    #   --cafile  certs/certs/ca.crt
    #   --certfile certs/certs/hlias.crt
    #   --keyfile  certs/certs/hlias.key
    #   --push-to-talk
"""

import io
import json
import os
import queue
import ssl
import sys
import threading
import time

import dash
import dash_bootstrap_components as dbc
import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd
from dash import Input, Output, State, callback, clientside_callback, ctx, html, dcc
from pydub import AudioSegment

# ── Audio constants (verbatim from stream_to_box3_MQTT_3.py) ─────────
SAMPLE_RATE   = 16000
CHANNELS      = 1
DTYPE         = "int16"
SAMPLE_WIDTH  = 2               # bytes per sample (int16)

FRAME_MS      = 30
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)   # 480 samples
FRAME_BYTES   = FRAME_SAMPLES * SAMPLE_WIDTH          # 960 bytes

TAIL_PAD_MS   = 250

# ── MQTT constants (verbatim from stream_to_box3_MQTT_3.py) ──────────
MQTT_CHUNK_SIZE     = 4096
MQTT_QOS            = 1
MQTT_KEEPALIVE      = 60
INTER_CHUNK_DELAY_S = 0.08
POST_INFO_DELAY_S   = 0.20
POST_CHUNKS_DELAY_S = 0.50


# ─────────────────────────────────────────────────────────────────────
# Topic helpers  (verbatim)
# ─────────────────────────────────────────────────────────────────────

def t_info(device_id: str) -> str:
    return f"commands/{device_id}/play/info"

def t_chunk(device_id: str, n: int) -> str:
    return f"commands/{device_id}/play/chunk/{n}"

def t_complete(device_id: str) -> str:
    return f"commands/{device_id}/play/complete"


# ─────────────────────────────────────────────────────────────────────
# Audio helpers  (verbatim)
# ─────────────────────────────────────────────────────────────────────

def normalize(pcm: np.ndarray, target_peak: float = 0.92) -> np.ndarray:
    peak = np.max(np.abs(pcm.astype(np.float32)))
    if peak < 50:
        return pcm
    gain = (target_peak * 32767.0) / peak
    return np.clip(pcm.astype(np.float32) * gain, -32768, 32767).astype(np.int16)


def utterance_to_wav(pcm_bytes: bytes) -> bytes:
    pcm    = np.frombuffer(pcm_bytes, dtype=np.int16).copy()
    normed = normalize(pcm)
    tail   = np.zeros(int(SAMPLE_RATE * TAIL_PAD_MS / 1000), dtype=np.int16)
    padded = np.concatenate([normed, tail])
    audio  = AudioSegment(
        data=padded.tobytes(),
        sample_width=SAMPLE_WIDTH,
        frame_rate=SAMPLE_RATE,
        channels=CHANNELS,
    )
    buf = io.BytesIO()
    audio.export(buf, format="wav")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────
# PTT segmenter  (verbatim from stream_to_box3_MQTT_3.py)
# ─────────────────────────────────────────────────────────────────────

class PttSegmenter:
    """
    Buffering mode (chunk_interval_s == 0, default used here):
        press()   → arm the recorder
        push()    → silently accumulate; always returns None
        release() → return the full PCM utterance
    """

    def __init__(self, chunk_interval_s: float = 0.0):
        self._streaming         = chunk_interval_s > 0.0
        self._bytes_per_chunk   = int(SAMPLE_RATE * SAMPLE_WIDTH * chunk_interval_s)
        self._recording         = False
        self._accumulator: list = []
        self._accumulated_bytes = 0
        self._lock              = threading.Lock()

    def press(self) -> None:
        with self._lock:
            if self._recording:
                return                      # guard against OS key-repeat
            self._recording         = True
            self._accumulator       = []
            self._accumulated_bytes = 0
        mode = "streaming" if self._streaming else "buffering"
        print(f"[PTT] ▶ recording ({mode}) …  (release Space to send)")

    def release(self) -> "bytes | None":
        with self._lock:
            if not self._recording:
                return None
            self._recording         = False
            if not self._accumulator:
                return None
            utterance               = b"".join(self._accumulator)
            self._accumulator       = []
            self._accumulated_bytes = 0

        dur   = len(utterance) / (SAMPLE_RATE * SAMPLE_WIDTH)
        label = "tail" if self._streaming else "utterance"
        print(f"[PTT] ■ {label}  ({dur:.2f}s, {len(utterance) // 1024} kB PCM)")
        return utterance

    def push(self, frame: bytes) -> "bytes | None":
        chunk = None
        with self._lock:
            if not self._recording:
                return None
            self._accumulator.append(frame)
            self._accumulated_bytes += len(frame)
            if self._streaming and self._accumulated_bytes >= self._bytes_per_chunk:
                chunk                   = b"".join(self._accumulator)
                self._accumulator       = []
                self._accumulated_bytes = 0

        if chunk is not None:
            dur = len(chunk) / (SAMPLE_RATE * SAMPLE_WIDTH)
            print(f"[PTT] → live chunk  {dur:.2f}s  {len(chunk) // 1024} kB")
        return chunk


# ─────────────────────────────────────────────────────────────────────
# BoxStreamer  (logic verbatim; adapted for headless Dash use)
# ─────────────────────────────────────────────────────────────────────

class BoxStreamer:

    def __init__(self,
                 broker:   str,
                 port:     int,
                 device_id: str,
                 username: str  = None,
                 password: str  = None,
                 cafile:   str  = None,
                 certfile: str  = None,
                 keyfile:  str  = None):

        self.broker    = broker
        self.port      = port
        self.device_id = device_id
        self._q        = queue.Queue(maxsize=8)
        self._seg      = PttSegmenter(chunk_interval_s=0.0)   # buffering mode

        # paho 1.x / 2.x compatibility  (verbatim from original)
        try:
            self._client = mqtt.Client(
                client_id=f"box3-dash-ptt-{int(time.time())}",
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            )
        except AttributeError:
            self._client = mqtt.Client(
                client_id=f"box3-dash-ptt-{int(time.time())}",
            )

        if username:
            self._client.username_pw_set(username, password)

        # TLS — exact logic from original:
        # mTLS when cafile is given; server-only TLS (insecure) as fallback
        if cafile:
            self._client.tls_set(
                ca_certs=cafile,
                certfile=certfile,
                keyfile=keyfile,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            print(f"[TLS]  CA={cafile}  cert={certfile}  key={keyfile}")
        elif port == 8883:
            self._client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
            self._client.tls_insecure_set(True)
            print("[TLS]  server-only TLS (no client cert)")

        self._connected = threading.Event()
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    # ── MQTT callbacks ────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected.set()
            print(f"[MQTT] Connected  →  {self.broker}:{self.port}")
            print(f"[MQTT] Target device : {self.device_id}")
            print(f"[MQTT] Topics        : "
                  f"commands/{self.device_id}/play/{{info,chunk/N,complete}}")
        else:
            print(f"[MQTT] Connect failed  rc={rc}", file=sys.stderr)

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()
        if rc != 0:
            print(f"[MQTT] Unexpected disconnect  rc={rc} — "
                  "paho will auto-reconnect …", file=sys.stderr)

    # ── Chunked three-step publisher  (verbatim) ──────────────────────

    def _publish_utterance(self, wav: bytes, seq: int) -> bool:
        total_size   = len(wav)
        total_chunks = (total_size + MQTT_CHUNK_SIZE - 1) // MQTT_CHUNK_SIZE
        dur_sec      = total_size / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)

        print(f"\n[TX]  seq={seq:04d}  {total_size/1024:.1f} kB  "
              f"{total_chunks} chunks  {dur_sec:.2f}s")

        if not self._connected.wait(timeout=10):
            print(f"[TX]  seq={seq:04d}  ✗ MQTT not connected — skipped",
                  file=sys.stderr)
            return False

        # Step 1: info
        info_payload = json.dumps({
            "total_size":         total_size,
            "duration":           round(dur_sec),
            "format":             "wav",
            "chunks":             total_chunks,
            "samplerate":         SAMPLE_RATE,
            "bits":               SAMPLE_WIDTH * 8,
            "username":           self.device_id,
            "rms_threshold":      0.0,
            "esp_audio_duration": round(dur_sec),
        })
        r = self._client.publish(t_info(self.device_id), info_payload, qos=MQTT_QOS)
        r.wait_for_publish(timeout=10)
        print(f"[TX]    → info  ({len(info_payload)} B)")
        time.sleep(POST_INFO_DELAY_S)

        # Step 2: chunks
        for i in range(total_chunks):
            start = i * MQTT_CHUNK_SIZE
            end   = min(start + MQTT_CHUNK_SIZE, total_size)
            chunk = wav[start:end]
            r = self._client.publish(t_chunk(self.device_id, i), chunk, qos=MQTT_QOS)
            r.wait_for_publish(timeout=10)
            if i % 10 == 0 or i == total_chunks - 1:
                print(f"[TX]    → chunk {i+1}/{total_chunks}  ({len(chunk)} B)")
            time.sleep(INTER_CHUNK_DELAY_S)

        time.sleep(POST_CHUNKS_DELAY_S)

        # Step 3: complete
        complete_payload = json.dumps({
            "successful_chunks": total_chunks,
            "total_chunks":      total_chunks,
        })
        r = self._client.publish(t_complete(self.device_id), complete_payload, qos=MQTT_QOS)
        r.wait_for_publish(timeout=10)
        print("[TX]    → complete  ✓")
        return True

    # ── Background sender thread  (verbatim) ─────────────────────────

    def _sender_loop(self):
        seq = 0
        while True:
            item = self._q.get()
            if item is None:          # sentinel — clean shutdown
                break
            wav = utterance_to_wav(item)
            try:
                self._publish_utterance(wav, seq)
            except Exception as exc:
                print(f"[TX]  seq={seq:04d}  ✗ {exc}", file=sys.stderr)
            seq += 1

    # ── Thread-safe enqueue  (verbatim) ──────────────────────────────

    def _enqueue(self, pcm: bytes, source: str) -> None:
        try:
            self._q.put_nowait(pcm)
        except queue.Full:
            print(f"[{source}] queue full — chunk dropped", file=sys.stderr)

    # ── Start mic + MQTT + sender thread ─────────────────────────────

    def start(self):
        print(f"[PTT]  Connecting to {self.broker}:{self.port} …")
        self._client.connect(self.broker, self.port, MQTT_KEEPALIVE)
        self._client.loop_start()

        if not self._connected.wait(timeout=10):
            print(f"[FATAL] Could not reach broker at "
                  f"{self.broker}:{self.port}", file=sys.stderr)
            self._client.loop_stop()
            sys.exit(1)

        threading.Thread(target=self._sender_loop, daemon=True).start()

        # ── Audio callback (runs in sounddevice's C-level audio thread) ──
        leftover = b""

        def audio_callback(indata, frames, time_info, status):
            nonlocal leftover
            if status:
                print(f"[MIC] {status}", file=sys.stderr)
            raw = leftover + indata.tobytes()
            while len(raw) >= FRAME_BYTES:
                frame, raw = raw[:FRAME_BYTES], raw[FRAME_BYTES:]
                live_chunk = self._seg.push(frame)   # None in buffering mode
                if live_chunk is not None:
                    self._enqueue(live_chunk, "PTT")
            leftover = raw

        self.mic_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=FRAME_SAMPLES,
            callback=audio_callback,
        )
        self.mic_stream.start()
        print("[PTT]  Ready — hold SPACEBAR to record, release to send.\n")


# ─────────────────────────────────────────────────────────────────────
# Streamer instance  (edit credentials / paths here as needed)
# ─────────────────────────────────────────────────────────────────────

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
from misc.secrets import mqtt_password

streamer = BoxStreamer(
    broker="audiosense.hmu.gr",
    port=8883,
    device_id="box3-1",
    username="hlias",
    password=mqtt_password(),
    cafile=os.path.join(project_root, "certs/certs/ca.crt"),
    certfile=os.path.join(project_root, "certs/certs/hlias.crt"),
    keyfile=os.path.join(project_root, "certs/certs/hlias.key"),
)
streamer.start()


# ─────────────────────────────────────────────────────────────────────
# Dash UI
# ─────────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://use.fontawesome.com/releases/v5.15.4/css/all.css",
    ],
)

app.layout = dbc.Container([
    html.H2("PTT — Push to Talk", className="mt-4"),

    html.Div([
        html.I(className="fas fa-microphone fa-5x mb-3", id="mic-icon"),
        html.Div("Hold SPACEBAR to speak", id="ptt-text"),
    ],
        id="ptt-container",
        className="p-5 border rounded bg-light text-secondary fs-3 fw-bold",
        style={"minHeight": "300px"},
    ),

    # Two hidden buttons — one for press, one for release.
    # JavaScript clicks these directly; Dash callbacks react to n_clicks.
    html.Button(id="ptt-press",   style={"display": "none"}),
    html.Button(id="ptt-release", style={"display": "none"}),

], className="text-center p-5")


# ── Clientside: wire spacebar → hidden buttons ────────────────────────
# Runs once after the page loads (triggered by the stable id of ptt-press).
# Clicking ptt-press / ptt-release increments their n_clicks, which
# triggers the server callback below.
app.clientside_callback(
    """
    function(id) {
        if (window._pttInit) return window.dash_clientside.no_update;
        let held = false;
        window.addEventListener('keydown', function(e) {
            if (e.code === 'Space' && !held && e.target.tagName !== 'INPUT') {
                e.preventDefault();
                held = true;
                document.getElementById('ptt-press').click();
            }
        });
        window.addEventListener('keyup', function(e) {
            if (e.code === 'Space') {
                held = false;
                document.getElementById('ptt-release').click();
            }
        });
        window._pttInit = true;
        return window.dash_clientside.no_update;
    }
    """,
    Output("ptt-press", "title"),   # dummy output — never displayed
    Input("ptt-press", "id"),       # fires once on page load
)


# ── Server: react to press / release ─────────────────────────────────
@callback(
    Output("ptt-container", "className"),
    Output("ptt-text",      "children"),
    Input("ptt-press",      "n_clicks"),
    Input("ptt-release",    "n_clicks"),
    prevent_initial_call=True,
)
def handle_ptt(_press, _release):
    triggered = ctx.triggered_id

    if triggered == "ptt-press":
        streamer._seg.press()
        return (
            "p-5 border rounded bg-success text-white fs-3 fw-bold",
            "Listening …",
        )

    # triggered == "ptt-release"
    tail = streamer._seg.release()
    if tail:
        streamer._enqueue(tail, "PTT")
    return (
        "p-5 border rounded bg-light text-secondary fs-3 fw-bold",
        "Hold SPACEBAR to speak",
    )


if __name__ == "__main__":
    app.run(debug=True, port=8060)
