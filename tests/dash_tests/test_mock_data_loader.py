import unittest
from unittest.mock import patch, MagicMock
import os
import sys
from datetime import datetime

# Add the project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from ast_dash import mock_data_loader
import random # Import random to patch it

class TestMockDataLoader(unittest.TestCase):

    @patch('os.listdir')
    @patch('os.path.exists')
    @patch('os.stat')
    def test_get_local_audio_files_parsing(self, mock_stat, mock_exists, mock_listdir):
        """
        Tests that get_local_audio_files correctly parses filenames and extracts metadata.
        """
        # Mock file system
        mock_exists.return_value = True
        mock_listdir.return_value = [
            '20260114_124616_esp32s3-2_rec.mp3',
            '20260115_103000_deviceX_rec.mp3',
        ]
        mock_stat_obj = MagicMock()
        mock_stat_obj.st_ctime = datetime(2026, 1, 14, 12, 0, 0).timestamp()
        mock_stat.return_value = mock_stat_obj

        files = mock_data_loader.get_local_audio_files()

        self.assertEqual(len(files), 2)
        
        # Test the first file
        file1 = files[1] # Sorted newest first
        self.assertEqual(file1['filename'], '20260114_124616_esp32s3-2_rec.mp3')
        self.assertEqual(file1['device_name'], 'esp32s3-2')
        self.assertEqual(file1['timestamp'], '2026-01-14 12:46:16')
        self.assertIn('filepath', file1)
        self.assertIn('creation_date', file1)

        # Test the second file
        file2 = files[0] # Sorted newest first
        self.assertEqual(file2['filename'], '20260115_103000_deviceX_rec.mp3')
        self.assertEqual(file2['device_name'], 'deviceX')
        self.assertEqual(file2['timestamp'], '2026-01-15 10:30:00')

    @patch('os.listdir')
    @patch('os.path.exists')
    def test_get_local_audio_files_no_dir(self, mock_exists, mock_listdir):
        """
        Tests that get_local_audio_files returns an empty list if the directory doesn't exist.
        """
        mock_exists.return_value = False
        mock_listdir.return_value = [] # Should not be called if exists is False

        files = mock_data_loader.get_local_audio_files()
        self.assertEqual(files, [])
        mock_exists.assert_called_once()
        # mock_exists.assert_called_once_with('audio_examples')
        mock_listdir.assert_not_called()

    @patch('os.listdir')
    @patch('os.path.exists')
    @patch('os.stat')
    def test_get_local_audio_files_mp3_filter(self, mock_stat, mock_exists, mock_listdir):
        """
        Tests that get_local_audio_files only processes .mp3 files.
        """
        mock_exists.return_value = True
        mock_listdir.return_value = [
            'audio.mp3',
            'document.txt',
            'image.jpg',
            'another.mp3',
        ]
        mock_stat_obj = MagicMock()
        mock_stat_obj.st_ctime = datetime(2026, 1, 1, 0, 0, 0).timestamp()
        mock_stat.return_value = mock_stat_obj

        files = mock_data_loader.get_local_audio_files()
        self.assertEqual(len(files), 2)
        self.assertTrue(any(f['filename'] == 'audio.mp3' for f in files))
        self.assertTrue(any(f['filename'] == 'another.mp3' for f in files))
        self.assertFalse(any(f['filename'] == 'document.txt' for f in files))

    @patch('os.listdir')
    @patch('os.path.exists')
    @patch('os.stat')
    @patch('random.choice')
    @patch('random.uniform')
    def test_get_local_audio_files_mock_data_fields(self, mock_uniform, mock_choice, mock_stat, mock_exists, mock_listdir):
        """
        Tests that get_local_audio_files adds mock classification and RMS data.
        """
        mock_exists.return_value = True
        mock_listdir.return_value = ['20260114_124616_esp32s3-2_rec.mp3']
        mock_stat_obj = MagicMock()
        mock_stat_obj.st_ctime = datetime(2026, 1, 14, 12, 0, 0).timestamp()
        mock_stat.return_value = mock_stat_obj

        # Set return values for random mocks
        # Ensure a list is provided for side_effect if choice is called multiple times
        mock_choice.side_effect = ['mock_sound_1', 'mock_sound_2', 'mock_sound_3', 'mock_sound_4', 'mock_sound_5', 'mock_sound_6'] 
        mock_uniform.side_effect = [0.75, 0.45, 0.25, 0.5] # For conf1, conf2, conf3, rms
        
        files = mock_data_loader.get_local_audio_files()
        self.assertEqual(len(files), 1)
        file = files[0]
        
        self.assertIn('event_id', file)
        self.assertIn('sound1', file)
        self.assertIn('conf_sound1', file)
        self.assertIn('sound2', file)
        self.assertIn('conf_sound2', file)
        self.assertIn('sound3', file)
        self.assertIn('conf_sound3', file)
        self.assertIn('rms', file)
        self.assertIn('spectrogram_image', file)

        self.assertEqual(file['event_id'], 1)
        self.assertEqual(file['sound1'], 'mock_sound_1')
        self.assertEqual(file['conf_sound1'], 0.75)
        self.assertEqual(file['sound2'], 'mock_sound_2')
        self.assertEqual(file['conf_sound2'], 0.45)
        self.assertEqual(file['sound3'], 'mock_sound_3')
        self.assertEqual(file['conf_sound3'], 0.25)
        self.assertEqual(file['rms'], 0.5)
        # self.assertEqual(file['spectrogram_image'], 'mock_spectrogram_1.png')
        self.assertTrue(file['spectrogram_image'].endswith('rec.png'))
        self.assertIn('/assets/specrogram_images/', file['spectrogram_image'])


if __name__ == '__main__':
    unittest.main()