import unittest
from unittest.mock import MagicMock, patch
import os
import json
import sys

# Ensure project root is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from mqtt_clent.mqtt_client import MQTTAudioReceiver

class TestSentinelDiscovery(unittest.TestCase):
    def setUp(self):
        # We will mock the client and status file path so we don't overwrite production files or actually connect
        self.proj_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        self.status_file = os.path.join(self.proj_root, 'sentinel_status.json')
        
        # Backup existing sentinel_status.json if it exists
        self.backup_data = None
        if os.path.exists(self.status_file):
            with open(self.status_file, 'r', encoding='utf-8') as f:
                self.backup_data = f.read()

    def tearDown(self):
        # Restore backup
        if self.backup_data is not None:
            with open(self.status_file, 'w', encoding='utf-8') as f:
                f.write(self.backup_data)
        elif os.path.exists(self.status_file):
            os.remove(self.status_file)

    @patch('paho.mqtt.client.Client')
    def test_on_message_device_status_with_and_without_prefix(self, mock_mqtt_client):
        # Instantiate receiver with dummy parameters
        receiver = MQTTAudioReceiver(
            host="localhost",
            port=1883,
            username="test",
            password="pwd",
            save_sound_path="test_sounds"
        )
        
        # Mock MQTT messages for devices/ format
        # 1. Non-prefixed status message for esp32s3-1 (devices/ style)
        msg1 = MagicMock()
        msg1.topic = "devices/esp32s3-1/status"
        msg1.payload = b"online"
        
        receiver._MQTTAudioReceiver__on_message(None, None, msg1)
        
        with open(self.status_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn("esp32s3-1", data)
        self.assertEqual(data["esp32s3-1"]["status"], "online")
        
        # 2. Prefixed status message for esp32s3-2 (devices/ style)
        msg2 = MagicMock()
        msg2.topic = "ast_surveillance_v1/devices/esp32s3-2/status"
        msg2.payload = b"online"
        
        receiver._MQTTAudioReceiver__on_message(None, None, msg2)
        
        with open(self.status_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn("esp32s3-2", data)
        self.assertEqual(data["esp32s3-2"]["status"], "online")

        # 3. Prefixed IP address message for esp32s3-3 (devices/ style)
        msg3 = MagicMock()
        msg3.topic = "ast_surveillance_v1/devices/esp32s3-3/ip"
        msg3.payload = b"192.168.2.55"
        
        receiver._MQTTAudioReceiver__on_message(None, None, msg3)
        
        with open(self.status_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn("esp32s3-3", data)
        self.assertEqual(data["esp32s3-3"]["ip_address"], "192.168.2.55")

        # 4. Non-prefixed status message for esp32s3-1 (audio/status style)
        msg4 = MagicMock()
        msg4.topic = "audio/status/esp32s3-1"
        msg4.payload = b"online"
        
        receiver._MQTTAudioReceiver__on_message(None, None, msg4)
        
        with open(self.status_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn("esp32s3-1", data)
        self.assertEqual(data["esp32s3-1"]["status"], "online")

        # 5. Prefixed status message for esp32s3-2 (audio/status style)
        msg5 = MagicMock()
        msg5.topic = "ast_surveillance_v1/audio/status/esp32s3-2"
        msg5.payload = b"online"
        
        receiver._MQTTAudioReceiver__on_message(None, None, msg5)
        
        with open(self.status_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn("esp32s3-2", data)
        self.assertEqual(data["esp32s3-2"]["status"], "online")

        # 6. Prefixed IP message for esp32s3-3 (audio/ip style)
        msg6 = MagicMock()
        msg6.topic = "ast_surveillance_v1/audio/ip/esp32s3-3"
        msg6.payload = b"192.168.2.66"
        
        receiver._MQTTAudioReceiver__on_message(None, None, msg6)
        
        with open(self.status_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn("esp32s3-3", data)
        self.assertEqual(data["esp32s3-3"]["ip_address"], "192.168.2.66")

if __name__ == '__main__':
    unittest.main()
