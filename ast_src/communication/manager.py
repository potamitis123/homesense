import abc
import os
import sys

# Add project root to sys.path to ensure correct imports
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from misc.log_test import logger
from misc.load_parameters import parameters

LOG_D = {'module_name': 'communication_manager'}

class BaseCommunicationAdapter(abc.ABC):
    """
    Abstract Base Class for communication adapters (Local MQTT, Remote MQTT, etc.)
    """
    @abc.abstractmethod
    def start(self):
        """Start the communication listener background thread/process."""
        pass

    @abc.abstractmethod
    def stop(self):
        """Stop the communication listener."""
        pass

    @abc.abstractmethod
    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> bool:
        """Publish a message to the broker."""
        pass

    @abc.abstractmethod
    def subscribe(self, topic: str, qos: int = 0, callback = None):
        """Subscribe to a topic."""
        pass

    @abc.abstractmethod
    def requestRecording(self, device_id: str) -> bool:
        """Request immediate audio recording from a device."""
        pass

    @abc.abstractmethod
    def sendCommand(self, device_id: str, command: str, payload: dict = None) -> bool:
        """Send a standard control command to a device."""
        pass


class CommunicationManager:
    """
    Singleton manager acting as the core entry point for all networking & messaging operations.
    Loads the appropriate adapter (profile) dynamically based on parameters.json.
    """
    _instance = None

    @classmethod
    def get_instance(cls) -> BaseCommunicationAdapter:
        if cls._instance is None:
            # Dynamic import to avoid circular dependencies
            from ast_src.communication.adapters import LocalMQTTAdapter
            
            try:
                params = parameters.get_parameters()
                # Default to "Home Server" mode if not configured
                mode = params.get("communication", {}).get("mode", "Home Server")
            except Exception as e:
                logger.warning(f"Failed to read communication mode from parameters.json: {e}. Falling back to 'Home Server' mode.", extra=LOG_D)
                mode = "Home Server"

            if mode == "Home Server":
                logger.info("Initializing CommunicationManager in Home Server (Local Broker) mode.", extra=LOG_D)
                cls._instance = LocalMQTTAdapter()
            else:
                logger.info(f"Initializing CommunicationManager in Remote Server mode: '{mode}'.", extra=LOG_D)
                # Reserved for future sync architectures, currently falls back to Local MQTT for compatibility
                cls._instance = LocalMQTTAdapter()
                
        return cls._instance
