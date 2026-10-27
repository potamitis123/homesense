import os
import sys

# Ensure correct paths
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from ast_src.communication.adapters import LocalMQTTAdapter

class MQTTAudioReceiver(LocalMQTTAdapter):
    """
    Backward-compatibility wrapper for MQTTAudioReceiver.
    Inherits all core capabilities from the refactored LocalMQTTAdapter.
    """
    def send_message(self, topic, message, qos=0, retain=False):
        """Legacy naming alias for publish."""
        return self.publish(topic, message, qos=qos, retain=retain)
