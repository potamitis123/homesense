import paho.mqtt.client as mqtt
import json
import time
import os
import sys
import numpy as np
from pydub import AudioSegment
from io import BytesIO

# --- CONFIGURATION ---
MQTT_HOST = "localhost" # Change if your broker is elsewhere
MQTT_PORT = 1883
DEVICE_NAME = "sentinel_wav_test"
TOPIC_BASE = f"audio/{DEVICE_NAME}"
CHUNK_SIZE = 8192 # Bytes

def create_test_wav():
    """Create 5 seconds of a 440Hz sine wave."""
    print("Generating test WAV data...")
    sample_rate = 16000
    duration_sec = 5
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec))
    samples = (np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    
    audio = AudioSegment(
        samples.tobytes(),
        frame_rate=sample_rate,
        sample_width=2,
        channels=1
    )
    
    buf = BytesIO()
    audio.export(buf, format="wav")
    return buf.getvalue(), sample_rate

def send_mock_recording():
    wav_data, samplerate = create_test_wav()
    total_size = len(wav_data)
    
    # Calculate chunks
    chunks = [wav_data[i:i + CHUNK_SIZE] for i in range(0, total_size, CHUNK_SIZE)]
    total_chunks = len(chunks)
    
    client = mqtt.Client()
    
    try:
        print(f"Connecting to {MQTT_HOST}:{MQTT_PORT}...")
        client.connect(MQTT_HOST, MQTT_PORT, 60)
        client.loop_start()
        
        # 1. Send /info
        info_payload = {
            "total_size": total_size,
            "duration": 5,
            "format": "wav",
            "chunks": total_chunks,
            "samplerate": samplerate,
            "bits": 16,
            "username": DEVICE_NAME,
            "rms_threshold": 0.1,
            "esp_audio_duration": 5000
        }
        print(f"Sending /info to {TOPIC_BASE}/info...")
        client.publish(f"{TOPIC_BASE}/info", json.dumps(info_payload), qos=1)
        time.sleep(0.5)
        
        # 2. Send /chunks
        for i, chunk in enumerate(chunks):
            print(f"Sending chunk {i+1}/{total_chunks} ({len(chunk)} bytes)...")
            client.publish(f"{TOPIC_BASE}/chunk/{i}", chunk, qos=1)
            time.sleep(0.05) # Small delay to prevent flooding
            
        # 3. Send /complete
        complete_payload = {
            "total_chunks": total_chunks,
            "successful_chunks": total_chunks,
            "status": "success"
        }
        print(f"Sending /complete to {TOPIC_BASE}/complete...")
        client.publish(f"{TOPIC_BASE}/complete", json.dumps(complete_payload), qos=1)
        
        print("\n✅ Mock WAV recording sent successfully!")
        time.sleep(1) # Let the background loop process it
        
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    send_mock_recording()
