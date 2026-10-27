import os
import re
import sys
import ssl
import json
import base64
import wave
import uuid
import time
from threading import Thread
from io import BytesIO
from datetime import datetime
import numpy as np
import paho.mqtt.client as mqtt
from pydub import AudioSegment

# Ensure correct paths
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from ast_src.communication.manager import BaseCommunicationAdapter
from misc.log_test import logger
from misc.load_parameters import parameters

LOG_D = {'module_name': 'local_mqtt_adapter'}

# A device timestamp further from the server clock than this is not trusted.
# A sentinel whose SNTP sync failed reports a 1970 or otherwise wild time, and
# accepting it would write an event into the far past -- where the dashboard
# would never show it and the housekeeper's retention sweep would delete it on
# the next pass.
DEVICE_CLOCK_TOLERANCE_S = 24 * 3600


def _capture_stamp(record, label=''):
    """Event time for the filename: the device's, when it can be trusted.

    Falls back to the server clock when the field is absent (older firmware),
    unparseable, or too far from the server's own time to be believable. The
    fallback is the previous behaviour exactly, so a device that does not send
    a timestamp is unaffected by this change.
    """
    now = datetime.now()
    raw = getattr(record, 'device_timestamp', None)
    if raw:
        text = str(raw).strip()
        for fmt in ('%Y%m%d_%H%M%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                sent = datetime.strptime(text, fmt)
            except ValueError:
                continue
            drift = abs((now - sent).total_seconds())
            if drift <= DEVICE_CLOCK_TOLERANCE_S:
                # Positive lag is the transport delay this change removes.
                lag = (now - sent).total_seconds()
                if lag > 5:
                    logger.info(
                        f"Capture time from device{label}: {sent:%Y-%m-%d %H:%M:%S} "
                        f"({lag:.0f}s before reassembly)", extra=LOG_D)
                return sent.strftime('%Y%m%d_%H%M%S')
            logger.warning(
                f"Ignoring device timestamp{label} '{text}': {drift / 3600:.1f} h "
                f"from server time -- check the sentinel's SNTP sync. "
                f"Using server clock.", extra=LOG_D)
            break
        else:
            logger.warning(f"Unparseable device timestamp{label}: {text!r}. "
                           f"Using server clock.", extra=LOG_D)
    return now.strftime('%Y%m%d_%H%M%S')


class Record:
    """Helper class to track incoming audio chunks for reassembly."""
    def __init__(self, chunks, total_size, duration, format, total_chunks, samplerate, bits, sender_name, topic, rms_threshold, esp_audio_duration, device_timestamp=None):
        # When the sentinel says it started recording, as sent in the stream
        # announce. The devices sync over SNTP at boot, so this is real capture
        # time -- unlike the server clock at reassembly, which also carries
        # however long the chunks took to arrive.
        self.device_timestamp = device_timestamp
        self.chunks = chunks
        self.duration = duration
        self.total_size = total_size
        self.format = format
        self.total_chunks = total_chunks
        self.samplerate = samplerate
        self.bits = bits
        self.sender_name = sender_name
        self.topic = topic
        self.rms_threshold = rms_threshold
        self.esp_audio_duration = esp_audio_duration


class LocalMQTTAdapter(BaseCommunicationAdapter):
    """
    Adapter that implements the Local MQTT Broker communication.
    Encapsulates all logic previously in MQTTAudioReceiver.
    """
    def __init__(self, host=None, port=None, username=None, password=None, save_sound_path=None, ca_certfile=None, certfile=None, keyfile=None):
        # Load default configurations if not explicitly provided
        params = parameters.get_parameters()
        mqtt_params = params.get("mqtt", {})

        def resolve_path(p):
            if not p:
                return None
            return os.path.join(project_root, p) if not os.path.isabs(p) else p

        self.host = host or mqtt_params.get("broker_host", mqtt_params.get("host", "localhost"))
        self.port = port or mqtt_params.get("broker_port", mqtt_params.get("port", 1883))
        self.username = username or mqtt_params.get("username", "")
        self.password = password or mqtt_params.get("password", "")
        
        raw_save_path = save_sound_path or mqtt_params.get("save_sound_path", "assets/audios")
        self.save_sound_path = resolve_path(raw_save_path)

        # Handle certs
        ca_cert = ca_certfile or mqtt_params.get("ca_certfile")
        cert = certfile or mqtt_params.get("certfile")
        key = keyfile or mqtt_params.get("keyfile")

        self.ca_certfile = resolve_path(ca_cert)
        self.certfile = resolve_path(cert)
        self.keyfile = resolve_path(key)

        self.client = mqtt.Client()
        if self.username or self.password:
            self.client.username_pw_set(self.username, self.password)

        if self.ca_certfile or self.certfile or self.keyfile:
            try:
                self.client.tls_set(
                    ca_certs=self.ca_certfile,
                    certfile=self.certfile,
                    keyfile=self.keyfile
                )
            except Exception as e:
                logger.error(f"Failed to set TLS certificates: {e}", extra=LOG_D)

        self.client.on_connect = self.__on_connect
        self.client.on_message = self.__on_message
        self.client.on_disconnect = self.__on_disconnect
        self.client.on_connect_fail = self.__on_connect_fail

        self.connected = False
        self.rec = []
        self.proc_thread = None

        # Create audio directory if it doesn't exist
        try:
            if self.save_sound_path and not os.path.exists(self.save_sound_path):
                os.makedirs(self.save_sound_path)
        except Exception as e:
            logger.error(f"Error creating folder {self.save_sound_path}: {e}", extra=LOG_D)

    def start(self):
        logger.info("Starting Local MQTT Adapter background thread...", extra=LOG_D)
        try:
            self.client.connect(self.host, self.port, 60)
            self.connected = True
        except Exception as e:
            logger.error(f"MQTT Connection Failed: {e}", extra=LOG_D)
            self.connected = False

        self.proc_thread = Thread(target=self.__run_loop)
        self.proc_thread.daemon = True
        self.proc_thread.start()

    def stop(self):
        logger.info("Stopping Local MQTT Adapter background thread...", extra=LOG_D)
        if self.proc_thread is not None:
            try:
                self.client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting client: {e}", extra=LOG_D)
            self.proc_thread.join(timeout=2)
            self.proc_thread = None
            self.connected = False

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> bool:
        if self.client is None or not self.connected:
            try:
                self.client.connect(self.host, self.port, 60)
            except Exception as e:
                logger.error(f"Failed to reconnect during publish: {e}", extra=LOG_D)
                return False

        try:
            res = self.client.publish(topic=topic, payload=payload, qos=qos, retain=retain)
            return res.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error(f"Error publishing to {topic}: {e}", extra=LOG_D)
            return False

    def subscribe(self, topic: str, qos: int = 0, callback = None):
        if self.connected:
            self.client.subscribe(topic, qos)
            if callback:
                # Store dynamic callback if needed, or register directly with paho message callback list
                self.client.message_callback_add(topic, callback)
            logger.info(f"Subscribed to topic: {topic}", extra=LOG_D)
        else:
            logger.warning(f"Cannot subscribe to {topic}, MQTT client disconnected.", extra=LOG_D)

    def requestRecording(self, device_id: str) -> bool:
        """Publishes an immediate manual record request to the device's control topic."""
        topic = f"devices/{device_id}/command"
        payload = json.dumps({
            "command": "record_now",
            "duration": 10,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        logger.info(f"Requesting immediate recording from device {device_id} on {topic}", extra=LOG_D)
        return self.publish(topic, payload, qos=1)

    def sendCommand(self, device_id: str, command: str, payload: dict = None) -> bool:
        """Sends a structured command payload to the sentinel."""
        topic = f"devices/{device_id}/command"
        full_payload = {
            "command": command,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        if payload:
            full_payload.update(payload)
        logger.info(f"Sending command '{command}' to device {device_id} on {topic}", extra=LOG_D)
        return self.publish(topic, json.dumps(full_payload), qos=1)

    def __run_loop(self):
        self.client.loop_forever()

    def __on_disconnect(self, client, userdata, rc):
        self.connected = False
        logger.warning(f"Disconnected from MQTT broker with result code {rc}", extra=LOG_D)

    def __on_connect_fail(self, client, userdata):
        self.connected = False
        logger.error("Connection to MQTT broker failed", extra=LOG_D)

    def __on_connect(self, client, userdata, flags, rc):
        self.connected = True
        logger.info(f"Connected to MQTT broker with result code {rc}", extra=LOG_D)

        # sentinel_status.json outlives both this process and the broker, but a
        # Last Will can only fire for a client the broker still has a session
        # for. A device that vanished while we were down therefore stays
        # "online" in the file forever. Reset first, then subscribe: the broker
        # replays retained status for everything it genuinely knows about
        # within a second, so only the absent devices stay offline.
        self._reset_sentinel_statuses()
        
        # Subscribe to standard system topics (recordings, device statuses, capability advertisements)
        client.subscribe([
            ("audio/#", 0), 
            ("/audio/#", 0), 
            ("devices/+/status", 1), 
            ("devices/+/ip", 1),
            ("devices/+/capabilities", 1), # Support for device discovery (Phase 4)
            ("ast_surveillance_v1/devices/+/status", 1),
            ("ast_surveillance_v1/devices/+/ip", 1),
            ("ast_surveillance_v1/audio/#", 0),
            ("audio/status/#", 1),
            ("audio/ip/#", 1),
            ("ast_surveillance_v1/audio/status/#", 1),
            ("ast_surveillance_v1/audio/ip/#", 1)
        ])

    def __on_message(self, client, userdata, msg):
        try:
            topic = msg.topic.strip('/')
            topic_parts = topic.split('/')
            
            # Normalize prefix if present
            if len(topic_parts) > 0 and topic_parts[0] == "ast_surveillance_v1":
                topic_parts = topic_parts[1:]
            
            # 1. Device status/IP/Capability messages (e.g., devices/box3-2/status)
            if len(topic_parts) >= 3 and topic_parts[0] == "devices":
                device_id = topic_parts[1]
                msg_type = topic_parts[2]
                
                if msg_type == "status":
                    try:
                        status_data = json.loads(msg.payload.decode('utf-8'))
                        status_val = status_data.get("status", "offline")
                    except Exception:
                        status_val = msg.payload.decode('utf-8').strip().lower()
                        if "online" in status_val:
                            status_val = "online"
                        elif "offline" in status_val:
                            status_val = "offline"
                    self._update_sentinel_status_file(device_id, "status", status_val)
                    
                elif msg_type == "ip":
                    ip_val = msg.payload.decode('utf-8').strip()
                    self._update_sentinel_status_file(device_id, "ip_address", ip_val)

                elif msg_type == "capabilities":
                    try:
                        cap_data = json.loads(msg.payload.decode('utf-8'))
                        self._update_sentinel_status_file(device_id, "capabilities", cap_data)
                    except Exception as e:
                        logger.error(f"Failed to parse device capabilities: {e}", extra=LOG_D)
                return

            # Legacy topic scheme: audio/status/esp32s3-1 or audio/ip/esp32s3-1
            if len(topic_parts) >= 3 and topic_parts[0] == "audio":
                msg_type = topic_parts[1]
                device_id = topic_parts[2]
                
                if msg_type == "status":
                    try:
                        status_data = json.loads(msg.payload.decode('utf-8'))
                        status_val = status_data.get("status", "offline")
                    except Exception:
                        status_val = msg.payload.decode('utf-8').strip().lower()
                        if "online" in status_val:
                            status_val = "online"
                        elif "offline" in status_val:
                            status_val = "offline"
                    self._update_sentinel_status_file(device_id, "status", status_val)
                    return
                    
                elif msg_type == "ip":
                    ip_val = msg.payload.decode('utf-8').strip()
                    self._update_sentinel_status_file(device_id, "ip_address", ip_val)
                    return

            # 2. Recording streams (e.g. audio/recordings/DEVICE_ID/stream/info or chunk)
            if "info" in topic_parts:
                try:
                    info = json.loads(msg.payload)
                except Exception as e:
                    logger.error(f"Failed to parse info JSON: {e}", extra=LOG_D)
                    return

                topic_base = ('/').join(topic_parts[:-1])
                self.rec = [r for r in self.rec if r.topic != topic_base]

                # Prefer the firmware DEVICE_ID field. Fall back to username, then
                # the topic segment (audio/recordings/<id>/...). Never invent a name.
                sender_name = (
                    info.get("device_id")
                    or info.get("username")
                    or info.get("device")
                )
                if not sender_name or sender_name == "unknown":
                    if len(topic_parts) >= 3:
                        for part in topic_parts:
                            if any(x in part.lower() for x in ["box", "esp", "sentinel", "mic", "dev", "intercom", "p4"]):
                                sender_name = part
                                break
                        if not sender_name:
                            sender_name = topic_parts[2] if len(topic_parts) > 2 else topic_parts[1]
                    else:
                        sender_name = "unknown"
                sender_name = str(sender_name).strip()

                # Warn when the topic ID and payload ID disagree - usually means
                # the firmware DEVICE_ID and the MQTT topic path are out of sync.
                topic_device = None
                for part in topic_parts:
                    if any(x in part.lower() for x in ["box", "esp", "sentinel", "mic", "intercom", "p4"]):
                        topic_device = part
                        break
                if topic_device and topic_device != sender_name:
                    logger.warning(
                        f"Device ID mismatch: topic uses '{topic_device}' but payload "
                        f"device_id/username is '{sender_name}'. Using '{sender_name}'.",
                        extra=LOG_D,
                    )

                self.rec.append(Record(
                    chunks={},
                    total_size=info.get('total_size', 0),
                    duration=info.get('duration', 0),
                    format=info.get('format', 'mp3').lower(),
                    total_chunks=info.get('chunks', 0),
                    samplerate=info.get('samplerate', 16000),
                    bits=info.get('bits', 16),
                    sender_name=sender_name,
                    topic=topic_base,
                    rms_threshold=info.get('rms_threshold', 0),
                    esp_audio_duration=info.get('esp_audio_duration', 0),
                    device_timestamp=info.get('timestamp')
                ))
                logger.info(f"New recording starting: {topic_base} | Device: {sender_name} | Format: {info.get('format')} | Chunks: {info.get('chunks')}", extra=LOG_D)
                self._touch_sentinel_last_seen(info.get('device_id') or sender_name)
                
            elif "chunk" in topic_parts:
                try:
                    chunk_num = int(topic_parts[-1])
                    topic_base = ('/').join(topic_parts[:-2])
                    for r in self.rec:
                        if r.topic == topic_base:
                            r.chunks[chunk_num] = msg.payload
                            if chunk_num in [0, 1]:
                                logger.info(f"Received chunk {chunk_num} for {r.sender_name}", extra=LOG_D)
                            break
                except Exception as e:
                    logger.error(f"Error handling chunk: {e}", extra=LOG_D)
                
            elif "complete" in topic_parts:
                topic_base = ('/').join(topic_parts[:-1])
                for r in self.rec:
                    if r.topic == topic_base:
                        logger.info(f"Complete signal received for {r.sender_name}", extra=LOG_D)
                        try:
                            info = json.loads(msg.payload)
                            expected = r.total_chunks if r.total_chunks > 0 else len(r.chunks)
                            success_count = info.get('successful_chunks', info.get('total_chunks', expected))
                            
                            if len(r.chunks) >= expected or success_count >= expected:
                                self.__save_recording__(r)
                                self.rec.remove(r)
                                break
                        except Exception as e:
                            logger.error(f"Error handling complete: {e}", extra=LOG_D)
            else:
                logger.info(f"Unprocessed topic message: {topic_parts}", extra=LOG_D)
                    
        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}", extra=LOG_D)

    # ── recovering an address the device never announces ────────────
    #
    # A sentinel's address reaches the server on its own MQTT topic --
    # sentinels/<dev>/ip, devices/<dev>/ip, or legacy audio/ip/<dev> -- and
    # the box3 and P4 units publish it there. The ESP32-S3 firmware never
    # does, so those rows sat at ip_address: null for the life of the device
    # and the dashboard had nothing to show.
    #
    # The broker already knows. Mosquitto logs the source address of every
    # connection beside the client id, and the client id IS the device name,
    # which makes the join exact rather than a guess. Checked against the
    # four sentinels that do publish an address, the two sources agreed on
    # every one.
    #
    # Only the tail of the log is read. It is 221 MB and growing, while the
    # answer wanted is always the most recent connection; a device that has
    # not reconnected within that tail keeps its null, which is the honest
    # answer for something not heard from lately. Devices reconnect on the
    # 120 s keepalive, so anything live is in there.

    _BROKER_CONN = re.compile(
        r"^\d+: New client connected from ([0-9.]+):\d+ as (\S+) ")
    _BROKER_TAIL_BYTES = 4 * 1024 * 1024
    _broker_ips_cache = {}
    _broker_ips_at = 0.0

    @staticmethod
    def _broker_log_path():
        """Wherever mosquitto.conf says the log goes."""
        try:
            with open(os.path.join(project_root, 'mosquitto.conf'),
                      'r', encoding='utf-8', errors='ignore') as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) >= 3 and parts[0] == 'log_dest' \
                            and parts[1] == 'file':
                        return ' '.join(parts[2:]).strip()
        except Exception:
            pass
        return os.path.join(project_root, 'logs_ast_app', 'mosquitto.log')

    def _broker_ips(self):
        """{client id: address} from the tail of the broker log, cached 60 s."""
        now = time.time()
        if now - LocalMQTTAdapter._broker_ips_at < 60:
            return LocalMQTTAdapter._broker_ips_cache
        found = {}
        try:
            path = self._broker_log_path()
            size = os.path.getsize(path)
            with open(path, 'rb') as handle:
                handle.seek(max(0, size - self._BROKER_TAIL_BYTES))
                blob = handle.read().decode('utf-8', errors='ignore')
            # The first line is normally a fragment. The pattern is anchored,
            # so it fails to match rather than needing to be trimmed off.
            for line in blob.split('\n'):
                match = self._BROKER_CONN.match(line)
                if match:
                    found[match.group(2)] = match.group(1)   # last one wins
        except Exception as e:
            logger.warning(f"Could not read the broker log for addresses: {e}",
                           extra=LOG_D)
        LocalMQTTAdapter._broker_ips_cache = found
        LocalMQTTAdapter._broker_ips_at = now
        return found

    def _recover_missing_ips(self, data):
        """Fill blank ip_address entries from the broker log.

        Only ever fills a blank, so an address the device published always
        wins, and never adds a device: the broker log is full of short-lived
        clients -- ast-live-audio-<stamp>, ESP32_xxxxxx and the like -- which
        are not sentinels and must not become rows in the dashboard's list.

        Returns True when something changed, so the caller knows to write.
        """
        missing = [name for name, info in data.items()
                   if isinstance(info, dict) and not info.get("ip_address")]
        if not missing:
            return False
        known = self._broker_ips()
        changed = False
        for name in missing:
            address = known.get(name)
            if address:
                data[name]["ip_address"] = address
                changed = True
                logger.info(
                    f"Address for {name} taken from the broker log: {address} "
                    f"(the device does not publish one)", extra=LOG_D)
        return changed

    def _reset_sentinel_statuses(self):
        """Mark every known sentinel offline, pending the broker's retained state.

        Preserves last_seen and ip_address so the UI can still show when a
        device was last heard from.
        """
        try:
            status_file = os.path.join(project_root, 'sentinel_status.json')
            if not os.path.exists(status_file):
                return

            with open(status_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            cleared = [name for name, info in data.items()
                       if info.get("status") == "online"]
            for name in cleared:
                data[name]["status"] = "offline"

            # Startup is the natural moment to fill in addresses for the
            # sentinels that never announce their own.
            recovered = self._recover_missing_ips(data)

            if cleared or recovered:
                with open(status_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4)
            if cleared:
                logger.info(
                    f"Sentinel status reset pending broker confirmation: {', '.join(cleared)}",
                    extra=LOG_D)
        except Exception as e:
            logger.error(f"Error resetting sentinel statuses: {e}", extra=LOG_D)

    def _touch_sentinel_last_seen(self, device_id):
        """Record traffic from a device that is not a status/ip announcement.

        Audio is the only thing most sentinels send once they are up, so
        without this last_seen goes stale on a perfectly healthy device.
        """
        try:
            status_file = os.path.join(project_root, 'sentinel_status.json')
            data = {}
            if os.path.exists(status_file):
                try:
                    with open(status_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception:
                    data = {}

            if device_id not in data:
                return

            data[device_id]["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(status_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error(f"Error touching sentinel last_seen: {e}", extra=LOG_D)

    def _update_sentinel_status_file(self, device_id, key, value):
        try:
            status_file = os.path.join(project_root, 'sentinel_status.json')
            
            data = {}
            if os.path.exists(status_file):
                try:
                    with open(status_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            
            if device_id not in data:
                data[device_id] = {
                    "status": "offline",
                    "last_seen": None,
                    "ip_address": None,
                    "capabilities": {}
                }
            
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data[device_id][key] = value
            data[device_id]["last_seen"] = now_str

            # Also covers a sentinel that first appears after startup: it
            # announces its status, arrives here with a blank address, and
            # picks one up on the same write. The broker-log read behind
            # this is cached for a minute, so a burst of status messages
            # costs one parse at most.
            self._recover_missing_ips(data)

            with open(status_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
                
        except Exception as e:
            logger.error(f"Error updating sentinel status file: {e}", extra=LOG_D)

    def __save_recording__(self, r):
        try:
            logger.info(f"--- Assembling file for {r.sender_name} ---", extra=LOG_D)
            actual_total_chunks = r.total_chunks if r.total_chunks > 0 else (max(r.chunks.keys()) + 1 if r.chunks else 0)
            
            audio_data = bytearray()
            total_bytes_assembled = 0
            for i in range(actual_total_chunks):
                chunk_bytes = r.chunks.get(i, b'')
                if i not in r.chunks:
                    logger.warning(f"Missing chunk {i} during assembly.", extra=LOG_D)
                total_bytes_assembled += len(chunk_bytes)
                audio_data.extend(chunk_bytes)
            
            if r.format != 'mp3' and r.total_size > 0 and len(audio_data) > r.total_size:
                audio_data = audio_data[:r.total_size]
            
            if len(audio_data) == 0:
                logger.error(f"Assembled audio data is empty for {r.sender_name}. Dropping recording.", extra=LOG_D)
                return
            
            # The filename carries the event time: main.py parses the date and
            # time straight back out of it for the database row. Prefer the
            # sentinel's own capture time over the server clock -- reassembly
            # finishes whenever the chunks happen to arrive, which made two
            # sentinels record one moment 66 s apart and put them outside the
            # de-duplication window that was meant to catch exactly that.
            stamp = _capture_stamp(r, f" {r.sender_name}")
            if self.save_sound_path and os.path.exists(self.save_sound_path):
                filename = os.path.join(self.save_sound_path, f"{stamp}_{r.sender_name}_rec.wav")
            else:
                filename = f"{stamp}_{r.sender_name}_rec.wav"
          
            if r.samplerate is None:
                return

            try:
                temp_filename = f"{filename}.{uuid.uuid4().hex[:8]}.tmp"
                with open(temp_filename, "wb") as f:
                    f.write(audio_data)

                if r.format == 'mp3':
                    audio = AudioSegment.from_mp3(temp_filename)
                elif r.format in ['wav', 'pcm']:
                    # Raw PCM has no RIFF header, so from_wav cannot read it
                    # and pydub falls back to running ffprobe on it. On
                    # 2026-10-14 16:50:55 that ffprobe never returned, and this
                    # thread -- the one every recording arrives on -- waited
                    # for it for three hours while nothing was saved. Headerless
                    # data goes straight to from_raw, which needs no program.
                    try:
                        if not bytes(audio_data[:4]) == b'RIFF':
                            raise ValueError("no RIFF header: raw PCM")
                        audio = AudioSegment.from_wav(temp_filename)
                    except Exception:
                        audio = AudioSegment.from_raw(
                            BytesIO(audio_data),
                            sample_width=r.bits // 8 if r.bits else 2,
                            frame_rate=r.samplerate if r.samplerate else 16000,
                            channels=1
                        )
                else:
                    audio = AudioSegment.from_file(temp_filename)
                
                normalized_audio = audio.normalize()
                normalized_audio.export(filename, format="wav")
                logger.info(f"Normalized and saved WAV: '{filename}' | Duration: {len(audio)/1000:.1f}s", extra=LOG_D)

            except Exception as e:
                logger.error(f"Failed to process WAV output '{filename}': {e}", extra=LOG_D)
            finally:
                if 'temp_filename' in locals() and os.path.exists(temp_filename):
                    os.remove(temp_filename)

        except Exception as e:
            logger.error(f"Error saving recording: {e}", extra=LOG_D)
