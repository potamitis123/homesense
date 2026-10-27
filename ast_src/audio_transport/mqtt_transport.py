"""
MQTTAudioTransport — existing Push-to-Talk path over MQTT chunked WAV play.

Wire protocol (unchanged, firmware-compatible):
  1. commands/{device_id}/play/info      JSON metadata
  2. commands/{device_id}/play/chunk/N   raw WAV bytes (4096)
  3. commands/{device_id}/play/complete  JSON ack

Sentinels reassemble the WAV, write it to SD, and play it on the loudspeaker
when idle. No firmware changes are required for this transport.
"""

from __future__ import annotations

import json
import queue
import ssl
import sys
import threading
import time
from typing import List, Optional, Sequence

import paho.mqtt.client as mqtt

from .base import (
    AudioCodec,
    AudioFrame,
    AudioSessionConfig,
    AudioTransport,
    SessionMode,
    TransportCapabilities,
    TransportDirection,
)
from .constants import (
    CHANNELS,
    INTER_CHUNK_DELAY_S,
    MQTT_CHUNK_SIZE,
    MQTT_KEEPALIVE,
    MQTT_QOS,
    POST_CHUNKS_DELAY_S,
    POST_INFO_DELAY_S,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    topic_play_chunk,
    topic_play_complete,
    topic_play_info,
)
from .pcm_utils import utterance_to_wav


class MQTTAudioTransport(AudioTransport):
    """
    Chunked MQTT WAV play transport used by Push-to-Talk.

    ``send_audio_frame`` accepts raw PCM (int16). When ``frame.is_last`` is True
    (or always, in the default PTT path), the frame is normalised, padded,
    encoded to WAV and published with the historical info/chunk/complete
    sequence.
    """

    def __init__(
        self,
        broker: str,
        port: int,
        username: str = "",
        password: str = "",
        cafile: Optional[str] = None,
        certfile: Optional[str] = None,
        keyfile: Optional[str] = None,
        client_id_prefix: str = "box3-dash-ptt",
    ):
        super().__init__()
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.cafile = cafile
        self.certfile = certfile
        self.keyfile = keyfile

        self._device_ids: List[str] = []
        self._q: queue.Queue = queue.Queue(maxsize=8)
        self._connected = threading.Event()
        self._sender_started = False
        self._lock = threading.Lock()
        self._seq = 0

        try:
            self._client = mqtt.Client(
                client_id=f"{client_id_prefix}-{int(time.time())}",
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            )
        except AttributeError:
            self._client = mqtt.Client(
                client_id=f"{client_id_prefix}-{int(time.time())}",
            )

        if username:
            self._client.username_pw_set(username, password)

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

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    # ── AudioTransport API ───────────────────────────────────────────

    @property
    def name(self) -> str:
        return "mqtt_ptt"

    @property
    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            name=self.name,
            directions=(TransportDirection.TX,),
            codecs=(AudioCodec.PCM_WAV, AudioCodec.PCM_RAW),
            modes=(SessionMode.PTT,),
            supports_bitrate_selection=False,
            supports_negotiation=False,
            duplex=False,
        )

    @property
    def device_ids(self) -> List[str]:
        return list(self._device_ids)

    @device_ids.setter
    def device_ids(self, ids: Sequence[str]) -> None:
        self._device_ids = list(ids)

    def connect(self) -> None:
        if self._connected.is_set():
            return
        print(f"[PTT]  Connecting to {self.broker}:{self.port} …")
        self._client.connect(self.broker, self.port, MQTT_KEEPALIVE)
        self._client.loop_start()
        if not self._connected.wait(timeout=10):
            print(
                f"[FATAL] Could not reach broker at {self.broker}:{self.port}",
                file=sys.stderr,
            )
            return
        self._ensure_sender()

    def disconnect(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass
        self._connected.clear()
        self._active_session_id = None

    def start_session(self, config: AudioSessionConfig) -> str:
        if not self._connected.is_set():
            self.connect()
        session_id = self._new_session_id()
        with self._lock:
            self._active_session_id = session_id
            self._config = config
            self._device_ids = list(config.device_ids)
        print(f"[MQTTAudioTransport] session {session_id} -> {self._device_ids}")
        return session_id

    def stop_session(self, session_id: Optional[str] = None) -> None:
        with self._lock:
            if session_id is None or session_id == self._active_session_id:
                self._active_session_id = None
                # Keep device_ids / MQTT connection for the next PTT press.

    def send_audio_frame(
        self,
        frame: AudioFrame,
        session_id: Optional[str] = None,
    ) -> bool:
        """
        Enqueue PCM for async MQTT publish.

        Production PTT always sends a complete utterance (is_last=True).
        Partial frames are accepted and queued the same way for compatibility
        with experimental streaming segmenter mode.
        """
        if not frame.pcm:
            return False
        if session_id is not None and self._active_session_id not in (None, session_id):
            print(
                f"[MQTTAudioTransport] frame for unknown session {session_id}",
                file=sys.stderr,
            )
            return False
        return self._enqueue(frame.pcm, source="PTT")

    # ── MQTT publish pipeline (legacy BoxStreamer behaviour) ─────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected.set()
            print(f"[MQTT] Connected  ->  {self.broker}:{self.port}")
        else:
            print(f"[MQTT] Connect failed  rc={rc}", file=sys.stderr)

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()

    def _ensure_sender(self) -> None:
        if self._sender_started:
            return
        self._sender_started = True
        threading.Thread(target=self._sender_loop, daemon=True).start()

    def _enqueue(self, pcm: bytes, source: str) -> bool:
        try:
            self._q.put_nowait(pcm)
            return True
        except queue.Full:
            print(f"[{source}] queue full — chunk dropped", file=sys.stderr)
            return False

    def _sender_loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            wav = utterance_to_wav(item)
            seq = self._seq
            self._seq += 1
            try:
                self._publish_utterance(wav, seq)
            except Exception as exc:
                print(f"[TX]  seq={seq:04d}  FAIL {exc}", file=sys.stderr)

    def _publish_utterance(self, wav: bytes, seq: int) -> bool:
        total_size = len(wav)
        total_chunks = (total_size + MQTT_CHUNK_SIZE - 1) // MQTT_CHUNK_SIZE
        dur_sec = total_size / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)

        target_devices = list(self._device_ids)
        if not target_devices:
            print(
                f"[TX]  seq={seq:04d}  No target devices selected -- skipped",
                file=sys.stderr,
            )
            return False

        print(
            f"\n[TX]  seq={seq:04d}  {total_size / 1024:.1f} kB  "
            f"{total_chunks} chunks  {dur_sec:.2f}s to {target_devices}"
        )

        if not self._connected.wait(timeout=10):
            print(
                f"[TX]  seq={seq:04d}  MQTT not connected -- skipped",
                file=sys.stderr,
            )
            return False

        info_payload = json.dumps(
            {
                "total_size": total_size,
                "duration": round(dur_sec),
                "format": "wav",
                "chunks": total_chunks,
                "samplerate": SAMPLE_RATE,
                "bits": SAMPLE_WIDTH * 8,
                "username": ",".join(target_devices),
                "rms_threshold": 0.0,
                "esp_audio_duration": round(dur_sec),
            }
        )

        for dev_id in target_devices:
            try:
                self._client.publish(topic_play_info(dev_id), info_payload, qos=MQTT_QOS)
            except Exception as e:
                print(f"[TX] Failed to publish info to {dev_id}: {e}", file=sys.stderr)
        time.sleep(POST_INFO_DELAY_S)

        for i in range(total_chunks):
            start = i * MQTT_CHUNK_SIZE
            end = min(start + MQTT_CHUNK_SIZE, total_size)
            chunk = wav[start:end]
            for dev_id in target_devices:
                try:
                    self._client.publish(
                        topic_play_chunk(dev_id, i), chunk, qos=MQTT_QOS
                    )
                except Exception as e:
                    print(
                        f"[TX] Failed to publish chunk {i} to {dev_id}: {e}",
                        file=sys.stderr,
                    )
            time.sleep(INTER_CHUNK_DELAY_S)

        time.sleep(POST_CHUNKS_DELAY_S)
        complete_payload = json.dumps(
            {
                "successful_chunks": total_chunks,
                "total_chunks": total_chunks,
            }
        )

        for dev_id in target_devices:
            try:
                self._client.publish(
                    topic_play_complete(dev_id), complete_payload, qos=MQTT_QOS
                )
            except Exception as e:
                print(
                    f"[TX] Failed to publish complete to {dev_id}: {e}",
                    file=sys.stderr,
                )
        print("[TX]    -> complete  OK")
        return True
