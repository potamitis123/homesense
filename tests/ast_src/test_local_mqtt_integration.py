import os
import sys
import time
import glob
import unittest
import json
import paho.mqtt.client as mqtt
from datetime import datetime

# Ensure correct path resolution
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from ast_src.communication import CommunicationManager
from mock_mqtt_wav_sender import create_test_wav, CHUNK_SIZE

class TestLocalMQTTIntegration(unittest.TestCase):
    
    def test_end_to_end_local_audio_reception(self):
        """
        Integration test verifying that when an MQTT device publishes chunks to the local broker,
        the LocalMQTTAdapter successfully reassembles the chunks and writes a WAV file to disk.
        """
        # Ensure audio output folder exists
        output_dir = os.path.join(project_root, "assets", "audios")
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        # Get count of WAV files before the test
        wav_pattern = os.path.join(output_dir, "*_sentinel_wav_test_rec.wav")
        before_files = set(glob.glob(wav_pattern))
        
        print("\n[Integration] Starting LocalMQTTAdapter...")
        manager = CommunicationManager.get_instance()
        # Ensure it is running
        manager.start()
        
        # Connect a mock client to send the recording
        print("[Integration] Connecting mock client to local broker...")
        sender_client = mqtt.Client()
        try:
            sender_client.connect("localhost", 1883, 60)
            sender_client.loop_start()
            
            wav_data, samplerate = create_test_wav()
            total_size = len(wav_data)
            chunks = [wav_data[i:i + CHUNK_SIZE] for i in range(0, total_size, CHUNK_SIZE)]
            total_chunks = len(chunks)
            topic_base = "audio/sentinel_wav_test"
            
            # Send info
            info_payload = {
                "total_size": total_size,
                "duration": 5,
                "format": "wav",
                "chunks": total_chunks,
                "samplerate": samplerate,
                "bits": 16,
                "username": "sentinel_wav_test",
                "rms_threshold": 0.1,
                "esp_audio_duration": 5000
            }
            print("[Integration] Publishing info...")
            sender_client.publish(f"{topic_base}/info", json.dumps(info_payload), qos=1)
            time.sleep(0.5)
            
            # Send chunks
            for i, chunk in enumerate(chunks):
                print(f"[Integration] Publishing chunk {i}...")
                sender_client.publish(f"{topic_base}/chunk/{i}", chunk, qos=1)
                time.sleep(0.05)
                
            # Send complete
            complete_payload = {
                "total_chunks": total_chunks,
                "successful_chunks": total_chunks,
                "status": "success"
            }
            print("[Integration] Publishing complete...")
            sender_client.publish(f"{topic_base}/complete", json.dumps(complete_payload), qos=1)
            
            print("[Integration] Waiting for reassembly and write...")
            # Wait up to 5 seconds for reassembly
            for _ in range(10):
                time.sleep(0.5)
                after_files = set(glob.glob(wav_pattern))
                new_files = after_files - before_files
                if new_files:
                    break
                    
            print(f"[Integration] Files before: {before_files}")
            print(f"[Integration] Files after: {glob.glob(wav_pattern)}")
            
            self.assertTrue(len(new_files) > 0, "No new WAV file was created by LocalMQTTAdapter!")
            
            created_file = list(new_files)[0]
            print(f"[Integration] Successfully verified file creation: {created_file}")
            
            # Clean up created file
            if os.path.exists(created_file):
                os.remove(created_file)
                print("[Integration] Cleaned up created test WAV file.")
                
        finally:
            sender_client.loop_stop()
            sender_client.disconnect()
            manager.stop()

if __name__ == '__main__':
    unittest.main()
