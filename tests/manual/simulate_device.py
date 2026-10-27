import paho.mqtt.client as mqtt
import json
import os
import time
import random
import glob
from datetime import datetime
import threading
from pydub import AudioSegment
import math
import uuid

# Load parameters to connect to the same broker
try:
    with open('common_files/parameters.json', 'r') as f:
        params = json.load(f)
        mqtt_params = params['mqtt']
except Exception as e:
    print(f"Error loading parameters: {e}")
    exit(1)

BROKER = mqtt_params['host']
PORT = mqtt_params['port']
USERNAME = mqtt_params.get('username', '')
PASSWORD = mqtt_params.get('password', '')
CA_CERT = mqtt_params.get('ca_certfile')
CERT = mqtt_params.get('certfile')
KEY = mqtt_params.get('keyfile')

# Unique prefix for public broker to avoid crosstalk
TOPIC_PREFIX = "ast_surveillance_v1"
AUDIO_EXAMPLES_DIR = "audio_examples"
DEVICE_NAME = "simulated_device_1"

def on_connect(client, userdata, flags, rc, properties=None):
    print(f"Connected to MQTT Broker ({BROKER}) with code {rc}")

def send_audio_file(client, file_path):
    print(f"Sending {file_path}...")
    try:
        audio = AudioSegment.from_file(file_path)
        
        file_format = file_path.split('.')[-1].lower()
        if file_format not in ['mp3', 'wav']:
            print(f"Unsupported format {file_format}")
            return

        with open(file_path, 'rb') as f:
            file_data = f.read()
        
        total_size = len(file_data)
        chunk_size = 4096 # larger chunks for faster transmission
        total_chunks = math.ceil(total_size / chunk_size)
        duration = audio.duration_seconds
        
        # 1. Send Info
        topic_base = f"{TOPIC_PREFIX}/audio/recordings/{DEVICE_NAME}"
        info_topic = f"{topic_base}/info"
        info_payload = {
            "total_size": total_size,
            "duration": duration,
            "format": file_format,
            "chunks": total_chunks,
            "samplerate": audio.frame_rate,
            "bits": audio.sample_width * 8,
            "username": DEVICE_NAME,
            "rms_threshold": 0,
            "esp_audio_duration": duration
        }
        client.publish(info_topic, json.dumps(info_payload), qos=1)
        time.sleep(0.5) 

        # 2. Send Chunks
        for i in range(total_chunks):
            start = i * chunk_size
            end = start + chunk_size
            chunk = file_data[start:end]
            
            chunk_topic = f"{topic_base}/chunk/{i}"
            client.publish(chunk_topic, chunk, qos=1)
            # time.sleep(0.01) 
            
        time.sleep(0.5)

        # 3. Send Complete
        complete_topic = f"{topic_base}/complete"
        complete_payload = {
            "successful_chunks": total_chunks,
            "total_chunks": total_chunks
        }
        client.publish(complete_topic, json.dumps(complete_payload), qos=1)
        print("Done sending.")

    except Exception as e:
        print(f"Error sending file: {e}")

def main_loop():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if USERNAME:
        client.username_pw_set(USERNAME, PASSWORD)
    
    if CA_CERT and os.path.exists(CA_CERT):
        client.tls_set(ca_certs=CA_CERT, certfile=CERT, keyfile=KEY)
    
    client.on_connect = on_connect
    
    connected = False
    while not connected:
        try:
            print(f"Connecting to {BROKER}:{PORT}...")
            client.connect(BROKER, PORT, 60)
            client.loop_start()
            connected = True
        except Exception as e:
            print(f"Connection failed: {e}. Retrying in 10 seconds...")
            time.sleep(10)

    while True:
        files = glob.glob(os.path.join(AUDIO_EXAMPLES_DIR, "*"))
        if not files:
            print("No files in audio_examples")
            time.sleep(10)
            continue
            
        random_file = random.choice(files)
        send_audio_file(client, random_file)
        
        print("Waiting 1 minute...")
        time.sleep(60)

if __name__ == "__main__":
    main_loop()