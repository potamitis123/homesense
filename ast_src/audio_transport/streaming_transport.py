"""
StreamingAudioTransport — continuous / duplex live audio over MQTT.

Sentinel Live Audio Mode
------------------------
When ``start_live_audio`` is received on ``commands/<id>/control`` the
Sentinel leaves Event Mode and transmits Opus-encoded packets continuously
to ``audio/recordings/<id>/live/opus``. It exits Live Audio Mode on
``stop_live_audio`` and restores Event Mode exactly as before.

Independently of that, the Sentinel is *always* listening on
``commands/<id>/stream/#`` for real-time PCM to play on its loudspeaker —
no control command is needed to arm that path.

There is no UDP or WebRTC media plane on this device: both directions ride
the same MQTT connection used for control, so this class needs nothing more
than paho-mqtt to be fully duplex:

  * TX (you -> Sentinel speaker): ``stream/info`` once, then one
    ``stream/chunk/<n>`` publish per audio frame, then ``stream/complete``.
  * RX (Sentinel mic -> you): ``start_live_audio``/``stop_live_audio`` on
    the control topic, then subscribe to ``live/opus`` and unpack the
    bundle format documented in the firmware's live_audio.h. Decoding the
    Opus payload is left to whatever sink consumes the emitted AudioFrame
    (see AudioSessionManager.register_sink) — this module has no codec
    dependency of its own.

Direction is taken from AudioSessionConfig.direction (TX / RX / DUPLEX),
which PushToTalkController sets to DUPLEX by default.
"""

from __future__ import annotations

import json
import ssl
import sys
import threading
import time
from datetime import datetime
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
    MQTT_KEEPALIVE,
    MQTT_QOS,
    SAMPLE_RATE,
    topic_live_complete,
    topic_live_info,
    topic_live_opus,
    topic_start_live_audio,
    topic_stop_live_audio,
    topic_stream_chunk,
    topic_stream_complete,
    topic_stream_info,
)


class StreamingAudioTransport(AudioTransport):
    """
    Continuous / duplex audio transport. MQTT is the media plane.

      * negotiate()        – still a placeholder: sample rate / channels are
                              fixed by the firmware (16 kHz / mono / 16-bit),
                              only bitrate (uplink Opus) is meaningfully
                              negotiable, and only in one direction.
      * _open_media() /
        _close_media()     – arm/disarm the downlink (stream/info,
                              stream/complete) and (un)subscribe the uplink
                              (live/*). No sockets to open — the MQTT client
                              from connect() carries both directions.
      * send_audio_frame() – publishes one stream/chunk/<n> per call.
      * _on_message()       – unpacks inbound live/opus bundles and calls
                              _emit_received() per Opus frame.
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
        client_id_prefix: str = "ast-live-audio",
        # Reserved for future media plane:
        media_host: Optional[str] = None,
        media_port: Optional[int] = None,
    ):
        super().__init__()
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.cafile = cafile
        self.certfile = certfile
        self.keyfile = keyfile
        self.media_host = media_host
        self.media_port = media_port

        self._device_ids: List[str] = []
        # Announced format and running byte count of the raw-PCM uplink,
        # keyed by device (see _handle_stream_info).
        self._uplink_format: dict = {}
        self._uplink_bytes: dict = {}
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._frames_sent = 0
        self._chunk_seq = 0
        self._tx_enabled = False
        self._rx_enabled = False

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
        elif port == 8883:
            self._client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
            self._client.tls_insecure_set(True)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # ── AudioTransport API ───────────────────────────────────────────

    @property
    def name(self) -> str:
        return "streaming"

    @property
    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            name=self.name,
            directions=(
                TransportDirection.TX,
                TransportDirection.RX,
                TransportDirection.DUPLEX,
            ),
            codecs=(AudioCodec.OPUS, AudioCodec.PCM_RAW, AudioCodec.G711_ULAW),
            modes=(SessionMode.CONTINUOUS,),
            supports_bitrate_selection=True,
            supports_negotiation=True,
            duplex=True,
        )

    def connect(self) -> None:
        if self._connected.is_set():
            return
        print(f"[StreamingAudio] Connecting control plane {self.broker}:{self.port} …")
        self._client.connect(self.broker, self.port, MQTT_KEEPALIVE)
        self._client.loop_start()
        if not self._connected.wait(timeout=10):
            print(
                f"[StreamingAudio] Could not reach broker at {self.broker}:{self.port}",
                file=sys.stderr,
            )

    def disconnect(self) -> None:
        if self._active_session_id is not None:
            self.stop_session(self._active_session_id)
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass
        self._connected.clear()

    def start_session(self, config: AudioSessionConfig) -> str:
        """
        Open a TX and/or RX session per config.direction: arm the Sentinel's
        loudspeaker downlink (TX) and/or switch its mic into Live Audio Mode
        and subscribe to the Opus uplink (RX).

        Raises RuntimeError if the MQTT control plane is not connected.
        """
        if not self._connected.is_set():
            self.connect()
        if not self._connected.is_set():
            raise RuntimeError(
                f"Streaming control plane not connected to {self.broker}:{self.port}"
            )

        negotiated = self.negotiate(config)
        session_id = self._new_session_id()

        tx_enabled = negotiated.direction in (TransportDirection.TX, TransportDirection.DUPLEX)
        rx_enabled = negotiated.direction in (TransportDirection.RX, TransportDirection.DUPLEX)

        with self._lock:
            self._active_session_id = session_id
            self._config = negotiated
            self._device_ids = list(negotiated.device_ids)
            self._frames_sent = 0
            self._chunk_seq = 0
            self._tx_enabled = tx_enabled
            self._rx_enabled = rx_enabled

        if rx_enabled:
            # Tell the Sentinel's own mic to start uploading, and actually
            # listen for what it sends -- previously we published the
            # command but never subscribed to the reply.
            self._publish_live_command(
                start=True,
                device_ids=self._device_ids,
                config=negotiated,
                session_id=session_id,
            )
            self._subscribe_live(self._device_ids)

        if tx_enabled:
            # Arm the Sentinel's playback buffer for the real-time downlink.
            # No control command needed -- it's subscribed from boot.
            self._publish_stream_info(negotiated, self._device_ids)

        self._open_media(negotiated, session_id)
        print(
            f"[StreamingAudio] session {session_id} on {self._device_ids} "
            f"tx={tx_enabled} rx={rx_enabled} "
            f"(codec={negotiated.codec.value}, bitrate={negotiated.bitrate})"
        )
        return session_id

    def stop_session(self, session_id: Optional[str] = None) -> None:
        """Exit Live Audio Mode and restore Event Mode on Sentinels."""
        with self._lock:
            sid = session_id or self._active_session_id
            devices = list(self._device_ids)
            config = self._config
            tx_enabled = self._tx_enabled
            rx_enabled = self._rx_enabled
            if sid is None:
                return
            if session_id is not None and session_id != self._active_session_id:
                return
            self._active_session_id = None
            self._tx_enabled = False
            self._rx_enabled = False

        self._close_media(sid)

        if tx_enabled:
            self._publish_stream_complete(devices)

        if rx_enabled:
            self._publish_live_command(
                start=False,
                device_ids=devices,
                config=config,
                session_id=sid,
            )
            self._unsubscribe_live(devices)

        print(
            f"[StreamingAudio] session {sid} stopped "
            f"({self._frames_sent} frames sent) tx={tx_enabled} rx={rx_enabled}"
        )

    def send_audio_frame(
        self,
        frame: AudioFrame,
        session_id: Optional[str] = None,
    ) -> bool:
        """
        Publish one raw-PCM chunk to the Sentinel's real-time downlink.

        Each call becomes one MQTT publish to stream/chunk/<n>; the
        firmware queues chunks for playback in arrival order, so no
        buffering or WAV framing is needed here (unlike MQTTAudioTransport).
        """
        sid = session_id or self._active_session_id
        if sid is None:
            print("[StreamingAudio] send_audio_frame: no active session", file=sys.stderr)
            return False
        if not self._tx_enabled:
            print("[StreamingAudio] send_audio_frame: TX not enabled for this session", file=sys.stderr)
            return False
        if not frame.pcm:
            return False

        with self._lock:
            n = self._chunk_seq
            self._chunk_seq += 1

        ok = True
        for dev_id in self._device_ids:
            try:
                self._client.publish(topic_stream_chunk(dev_id, n), frame.pcm, qos=0)
            except Exception as e:
                ok = False
                print(f"[StreamingAudio] chunk {n} -> {dev_id} failed: {e}", file=sys.stderr)

        self._frames_sent += 1
        if self._frames_sent == 1 or self._frames_sent % 100 == 0:
            print(
                f"[StreamingAudio] sent frame #{self._frames_sent} "
                f"({len(frame.pcm)} bytes) session={sid}"
            )
        return ok

    def negotiate(self, preferred: AudioSessionConfig) -> AudioSessionConfig:
        """
        Placeholder capability negotiation.

        TODO: exchange preferred codec / bitrate / sample rate with the
        Sentinel (e.g. over MQTT before opening the media plane) and return
        the agreed AudioSessionConfig.
        """
        # Default continuous session preferences until real negotiation exists.
        codec = preferred.codec
        if codec not in self.capabilities.codecs:
            codec = AudioCodec.OPUS
        bitrate = preferred.bitrate if preferred.bitrate is not None else 24000
        return preferred.copy_with(
            mode=SessionMode.CONTINUOUS,
            codec=codec,
            bitrate=bitrate,
            direction=preferred.direction
            if preferred.direction in self.capabilities.directions
            else TransportDirection.DUPLEX,
        )

    # ── MQTT Live Audio Mode commands ────────────────────────────────

    def _publish_live_command(
        self,
        start: bool,
        device_ids: Sequence[str],
        config: Optional[AudioSessionConfig],
        session_id: str,
    ) -> None:
        if not self._connected.wait(timeout=5):
            print("[StreamingAudio] MQTT not connected -- live command skipped", file=sys.stderr)
            return

        payload = {
            "command": "start_live_audio" if start else "stop_live_audio",
            "session_id": session_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if config is not None and start:
            payload.update(
                {
                    "codec": config.codec.value,
                    "sample_rate": config.sample_rate,
                    "channels": config.channels,
                    "bitrate": config.bitrate,
                    "direction": config.direction.value,
                    "media_host": self.media_host,
                    "media_port": self.media_port,
                }
            )

        body = json.dumps(payload)
        for dev_id in device_ids:
            topic = (
                topic_start_live_audio(dev_id) if start else topic_stop_live_audio(dev_id)
            )
            try:
                self._client.publish(topic, body, qos=MQTT_QOS)
                print(f"[StreamingAudio] published {topic}")
            except Exception as e:
                print(f"[StreamingAudio] failed to publish {topic}: {e}", file=sys.stderr)

    # ── Real-time downlink (server -> Sentinel speaker) ──────────────

    def _publish_stream_info(
        self, config: AudioSessionConfig, device_ids: Sequence[str]
    ) -> None:
        if not self._connected.wait(timeout=5):
            print("[StreamingAudio] MQTT not connected -- stream/info skipped", file=sys.stderr)
            return
        payload = json.dumps(
            {
                "sample_rate": config.sample_rate,
                "bits_per_sample": config.sample_width * 8,
                "channels": config.channels,
            }
        )
        for dev_id in device_ids:
            try:
                self._client.publish(topic_stream_info(dev_id), payload, qos=MQTT_QOS)
                print(f"[StreamingAudio] published {topic_stream_info(dev_id)}")
            except Exception as e:
                print(f"[StreamingAudio] stream/info -> {dev_id} failed: {e}", file=sys.stderr)

    def _publish_stream_complete(self, device_ids: Sequence[str]) -> None:
        if not self._connected.is_set():
            return
        for dev_id in device_ids:
            try:
                self._client.publish(topic_stream_complete(dev_id), b"", qos=MQTT_QOS)
            except Exception as e:
                print(f"[StreamingAudio] stream/complete -> {dev_id} failed: {e}", file=sys.stderr)

    # ── Live Audio Mode uplink (Sentinel mic -> server, Opus) ─────────

    def _subscribe_live(self, device_ids: Sequence[str]) -> None:
        for dev_id in device_ids:
            topics = [
                topic_live_info(dev_id),
                topic_live_opus(dev_id),
                topic_live_complete(dev_id),
                # The BOX-3 firmware does not use the live/opus topics above.
                # Its microphone uplink is raw PCM published as
                #   audio/recordings/<id>/stream/{info,chunk/N,complete}
                # Without this subscription a DUPLEX session reports rx=True
                # and still plays nothing back, which is what made
                # Push-to-Talk look one-way.
                f"audio/recordings/{dev_id}/stream/#",
            ]
            for topic in topics:
                try:
                    self._client.subscribe(topic, qos=0)
                except Exception as e:
                    print(f"[StreamingAudio] subscribe {topic} failed: {e}", file=sys.stderr)

    def _unsubscribe_live(self, device_ids: Sequence[str]) -> None:
        for dev_id in device_ids:
            for topic in (
                topic_live_info(dev_id),
                topic_live_opus(dev_id),
                topic_live_complete(dev_id),
            ):
                try:
                    self._client.unsubscribe(topic)
                except Exception:
                    pass

    def _on_message(self, client, userdata, msg) -> None:
        """Route inbound uplink messages; everything else is ignored.

        Two shapes are accepted:
          live/*   Opus bundles, per the documented protocol
          stream/* raw PCM, which is what the BOX-3 firmware actually sends
        """
        parts = msg.topic.split("/")
        if len(parts) < 5 or parts[0] != "audio" or parts[1] != "recordings":
            return
        device_id, kind, sub = parts[2], parts[3], parts[4]

        sid = self._active_session_id
        if sid is None:
            return

        if kind == "live":
            if sub == "opus":
                self._handle_live_bundle(sid, device_id, msg.payload)
            elif sub == "info":
                print(f"[StreamingAudio] live/info {device_id}: {msg.payload[:200]!r}")
            elif sub == "complete":
                print(f"[StreamingAudio] live/complete {device_id}: {msg.payload[:200]!r}")
            return

        if kind == "stream":
            if sub == "info":
                self._handle_stream_info(device_id, msg.payload)
            elif sub == "chunk" and len(parts) >= 6:
                self._handle_stream_chunk(sid, device_id, msg.payload)
            elif sub == "complete":
                self._emit_stream_complete(sid, device_id)
                self._uplink_bytes.pop(device_id, None)

    # ── raw-PCM uplink (BOX-3) ───────────────────────────────────────

    def _handle_stream_info(self, device_id: str, payload: bytes) -> None:
        """Remember the rate the device announces for the burst that follows."""
        rate, channels = SAMPLE_RATE, CHANNELS
        try:
            meta = json.loads(payload.decode("utf-8"))
            rate = int(meta.get("sample_rate", rate))
            channels = int(meta.get("channels", channels))
        except Exception:
            pass
        self._uplink_format[device_id] = (rate, channels)
        self._uplink_bytes[device_id] = 0
        print(f"[StreamingAudio] uplink from {device_id}: {rate} Hz x{channels}")

    def _emit_stream_complete(self, session_id: str, device_id: str) -> None:
        """Mark the end of an uplink burst.

        An empty frame with is_last=True. Sinks that just play audio ignore
        it (there are no samples), while consumers that need turn
        boundaries -- anything doing speech recognition -- get told where
        the utterance stopped without having to guess from silence.
        """
        rate, channels = self._uplink_format.get(device_id, (SAMPLE_RATE, CHANNELS))
        self._emit_received(
            session_id,
            AudioFrame(
                pcm=b"",
                codec=AudioCodec.PCM_RAW,
                sample_rate=rate,
                channels=channels,
                is_last=True,
                metadata={"device_id": device_id, "uplink": "stream"},
            ),
        )

    def _handle_stream_chunk(self, session_id: str, device_id: str,
                             payload: bytes) -> None:
        if not payload:
            return
        rate, channels = self._uplink_format.get(device_id, (SAMPLE_RATE, CHANNELS))
        self._uplink_bytes[device_id] = self._uplink_bytes.get(device_id, 0) + len(payload)
        self._emit_received(
            session_id,
            AudioFrame(
                pcm=payload,
                codec=AudioCodec.PCM_RAW,
                sample_rate=rate,
                channels=channels,
                metadata={"device_id": device_id, "uplink": "stream"},
            ),
        )

    def _handle_live_bundle(self, session_id: str, device_id: str, payload: bytes) -> None:
        """
        Unpack one Live Audio Mode bundle (see firmware live_audio.h):

            offset 0 : uint32_le  seq
            offset 4 : repeated { uint16_le len; uint8[len] opus_frame }

        Each Opus frame in the bundle is emitted as its own AudioFrame;
        decoding to PCM is a sink's job, not this transport's.
        """
        if len(payload) < 4:
            return
        seq = int.from_bytes(payload[0:4], "little")
        off = 4
        idx = 0
        while off + 2 <= len(payload):
            flen = int.from_bytes(payload[off : off + 2], "little")
            off += 2
            if flen <= 0 or off + flen > len(payload):
                print(
                    f"[StreamingAudio] malformed opus bundle from {device_id} "
                    f"seq={seq} (frame {idx}) -- dropping remainder",
                    file=sys.stderr,
                )
                break
            self._emit_received(
                session_id,
                AudioFrame(
                    pcm=payload[off : off + flen],
                    codec=AudioCodec.OPUS,
                    sequence=seq,
                    metadata={
                        "device_id": device_id,
                        "bundle_seq": seq,
                        "frame_index": idx,
                    },
                ),
            )
            off += flen
            idx += 1

    # ── Media plane hooks (no sockets needed -- MQTT carries both directions) ──

    def _open_media(self, config: AudioSessionConfig, session_id: str) -> None:
        """Extension point if a lower-latency media plane is added later."""

    def _close_media(self, session_id: str) -> None:
        """Extension point if a lower-latency media plane is added later."""

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected.set()
            print(f"[StreamingAudio] MQTT control plane connected -> {self.broker}:{self.port}")
        else:
            print(f"[StreamingAudio] MQTT connect failed rc={rc}", file=sys.stderr)

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()
