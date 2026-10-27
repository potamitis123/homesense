
import unittest
import os
import sys
import numpy as np
from datetime import datetime

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from ast_src.create_spectrogram import create_freq_analyzer_spectrogram

class TestSpectrogramCreation(unittest.TestCase):
    def test_spectrogram_is_created_on_disk(self):
        # Create dummy audio data
        sr = 16000
        duration = 2
        t = np.linspace(0, duration, sr * duration)
        # 440Hz sine wave
        audio_data = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        
        # Define test path
        test_output_dir = os.path.join(project_root, 'assets', 'test_output')
        if not os.path.exists(test_output_dir):
            os.makedirs(test_output_dir)
            
        test_filename = f"test_spec_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        test_path = os.path.join(test_output_dir, test_filename)
        
        # Generate spectrogram
        print(f"Testing spectrogram creation at: {test_path}")
        result_path = create_freq_analyzer_spectrogram(audio_data, sr, test_path)
        
        # Assertions
        self.assertIsNotNone(result_path, "Result path should not be None")
        self.assertEqual(result_path, test_path, "Result path should match input path")
        self.assertTrue(os.path.exists(test_path), f"File should exist at {test_path}")
        self.assertGreater(os.path.getsize(test_path), 0, "File should not be empty")
        
        # Cleanup
        if os.path.exists(test_path):
            os.remove(test_path)

if __name__ == '__main__':
    unittest.main()
