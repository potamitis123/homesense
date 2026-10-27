#!/usr/bin/env python3
"""
send_test_audio.py
-----------------
Sends a test audio file to the local secure MQTT broker (audiosense.hmu.gr:8883)
chunk-by-chunk, mimicking the ESP32 (Sentinel) device protocol.
"""

import json
import os
import ssl
import sys
import time
import paho.mqtt.client as mqtt
from pydub import AudioSegment

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from misc.secrets import mqtt_password, mqtt_username

# --- Configuration ---
BROKER = "audiosense.hmu.gr"
PORT = 8883
USERNAME = mqtt_username()
PASSWORD = mqtt_password()
DEVICE_NAME = "box3-2"

# Certificate Paths (Relative to project root)
CA_CERT = "certs/certs/ca.crt"
CERT = "certs/certs/hlias.crt"
KEY = "certs/certs/hlias.key"

# Audio File to Send
TEST_WAV = "mic_test.wav"

def on_connect(client, userdata, flags, rc, properties=None):
    print(f"[*] Connected to broker with code {rc}")

def send_audio(client, file_path):
    print(f"[*] Processing audio file: {file_path}")
    if not os.path.exists(file_path):
        print(f"[!] Error: File '{file_path}' does not exist.")
        return

    try:
        audio = AudioSegment.from_file(file_path)
        # Ensure correct formatting: 16kHz Mono 16-bit PCM (standard for Sentinel)
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        
        # Save to temporary normalized WAV
        temp_normalized = "temp_normalized_test.wav"
        audio.export(temp_normalized, format="wav")
        
        with open(temp_normalized, 'rb') as f:
            file_data = f.read()
            
        total_size = len(file_data)
        duration = len(audio) / 1000.0  # Duration in seconds
        chunk_size = 3200  # 3.2KB chunks
        total_chunks = (total_size + chunk_size - 1) // chunk_size

        print(f"[*] Sending '{file_path}' ({duration:.2f}s, {total_size} bytes, {total_chunks} chunks)...")

        # 1. Send Info Topic
        topic_base = f"audio/recordings/{DEVICE_NAME}"
        info_topic = f"{topic_base}/info"
        info_payload = {
            "total_size": total_size,
            "duration": duration,
            "format": "pcm",
            "chunks": total_chunks,
            "samplerate": 16000,
            "bits": 16,
            "username": DEVICE_NAME,
            "rms_threshold": 0,
            "esp_audio_duration": duration
        }
        
        print(f"[*] Publishing info to '{info_topic}'")
        client.publish(info_topic, json.dumps(info_payload), qos=1)
        time.sleep(0.5)

        # 2. Send Chunks
        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, total_size)
            chunk = file_data[start:end]

            chunk_topic = f"{topic_base}/chunk/{i}"
            client.publish(chunk_topic, chunk, qos=1)
            time.sleep(0.02)  # Control stream rate

        time.sleep(0.5)

        # 3. Send Complete
        complete_topic = f"{topic_base}/complete"
        complete_payload = {
            "successful_chunks": total_chunks,
            "total_chunks": total_chunks
        }
        print(f"[*] Publishing complete to '{complete_topic}'")
        client.publish(complete_topic, json.dumps(complete_payload), qos=1)
        print("[+] Test audio sent successfully!")

        # Clean up
        if os.path.exists(temp_normalized):
            os.remove(temp_normalized)

    except Exception as e:
        print(f"[!] Error preparing or sending file: {e}")

def main():
    print(f"[*] Connecting to {BROKER}:{PORT} with TLS mTLS...")
    
    # Initialize Paho MQTT Client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(USERNAME, PASSWORD)
    client.on_connect = on_connect

    # Setup TLS
    if not (os.path.exists(CA_CERT) and os.path.exists(CERT) and os.path.exists(KEY)):
        print("[!] Error: Certificate files not found. Make sure you run this from the project root.")
        sys.exit(1)

    client.tls_set(
        ca_certs=CA_CERT,
        certfile=CERT,
        keyfile=KEY,
        tls_version=ssl.PROTOCOL_TLSv1_2
    )
    # Disable hostname verification because the domain maps to localhost
    client.tls_insecure_set(True)

    try:
        client.connect(BROKER, PORT, 60)
    except Exception as e:
        print(f"[!] Failed to connect to broker: {e}")
        sys.exit(1)

    client.loop_start()
    
    # Wait for connection to establish
    time.sleep(1.0)
    
    send_audio(client, TEST_WAV)
    
    # Let client complete any queued publish requests
    time.sleep(2.0)
    client.loop_stop()
    client.disconnect()

if __name__ == "__main__":
    main()
