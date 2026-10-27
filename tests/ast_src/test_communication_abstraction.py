import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure correct path resolution
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from ast_src.communication import CommunicationManager, BaseCommunicationAdapter
from ast_src.communication.adapters import LocalMQTTAdapter
from mqtt_clent.mqtt_client import MQTTAudioReceiver

class TestCommunicationAbstraction(unittest.TestCase):
    
    def test_manager_singleton_resolves_local_adapter(self):
        """Verify the CommunicationManager singleton loads LocalMQTTAdapter by default."""
        with patch('misc.load_parameters.parameters.get_parameters') as mock_params:
            # Mock configuration to return Home Server mode
            mock_params.return_value = {
                "communication": {"mode": "Home Server"},
                "mqtt": {"broker_host": "localhost", "broker_port": 1883}
            }
            
            # Reset singleton instance for clean test execution
            CommunicationManager._instance = None
            manager = CommunicationManager.get_instance()
            
            self.assertIsInstance(manager, BaseCommunicationAdapter)
            self.assertIsInstance(manager, LocalMQTTAdapter)

    @patch('paho.mqtt.client.Client')
    def test_adapter_implements_commands_and_publish(self, mock_mqtt_client_class):
        """Verify LocalMQTTAdapter implements publish, subscribe, sendCommand and requestRecording."""
        mock_client_instance = mock_mqtt_client_class.return_value
        mock_client_instance.publish.return_value.rc = 0 # MQTT_ERR_SUCCESS
        
        adapter = LocalMQTTAdapter(host="localhost", port=1883)
        adapter.connected = True
        
        # Test basic publish
        success = adapter.publish("test/topic", "hello_world")
        self.assertTrue(success)
        mock_client_instance.publish.assert_called_with(topic="test/topic", payload="hello_world", qos=0, retain=False)
        
        # Test requestRecording
        success_recording = adapter.requestRecording("box-test-1")
        self.assertTrue(success_recording)
        
        # Test sendCommand
        success_command = adapter.sendCommand("box-test-1", "reboot")
        self.assertTrue(success_command)

    def test_backward_compatible_mqtt_audio_receiver(self):
        """Ensure that the legacy MQTTAudioReceiver class inherits from LocalMQTTAdapter and supports send_message."""
        with patch('paho.mqtt.client.Client') as mock_client:
            receiver = MQTTAudioReceiver(host="localhost", port=1883)
            receiver.connected = True
            
            # Legacy send_message method should route to publish
            with patch.object(receiver, 'publish', return_value=True) as mock_publish:
                res = receiver.send_message("test/legacy", "message_payload")
                self.assertTrue(res)
                mock_publish.assert_called_with("test/legacy", "message_payload", qos=0, retain=False)

if __name__ == '__main__':
    unittest.main()
