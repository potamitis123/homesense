import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import numpy as np

# Add the project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

class TestCreateSpectrogram(unittest.TestCase):

    @patch('matplotlib.pyplot.savefig')
    @patch('matplotlib.pyplot.close')
    @patch('matplotlib.pyplot.figure')
    @patch('librosa.stft')
    @patch('librosa.amplitude_to_db')
    @patch('librosa.fft_frequencies')
    @patch('librosa.display.specshow')
    def test_create_freq_analyzer_spectrogram_saves_file(self, mock_specshow, mock_fft_frequencies, mock_amplitude_to_db, mock_stft, mock_figure, mock_close, mock_savefig):
        """
        Tests that create_freq_analyzer_spectrogram calls savefig and close, and returns the path.
        This is a 'Red Phase' test, focusing on mocking the external dependencies.
        """
        # Mock inputs
        audio_data = np.random.rand(16000 * 5) # 5 seconds of audio
        audio_sr = 16000
        save_path = "test_spectrogram.png"

        # Mock librosa and matplotlib functions
        mock_stft.return_value = np.random.rand(1025, 157) # Dummy STFT output
        mock_amplitude_to_db.return_value = np.random.rand(1025, 157) # Dummy dB output
        mock_fft_frequencies.return_value = np.random.rand(1025) # Dummy frequencies
        mock_figure.return_value = MagicMock() # Mock the figure object
        
        # Call the function
        from ast_src.create_spectrogram import create_freq_analyzer_spectrogram
        returned_path = create_freq_analyzer_spectrogram(audio_data, audio_sr, save_path)

        # Assertions
        mock_savefig.assert_called_once_with(save_path, dpi=300, bbox_inches='tight')
        mock_close.assert_called_once()
        self.assertEqual(returned_path, save_path)

    @patch('matplotlib.pyplot.savefig')
    @patch('matplotlib.pyplot.close')
    @patch('matplotlib.pyplot.figure')
    @patch('librosa.stft')
    @patch('librosa.amplitude_to_db')
    @patch('librosa.fft_frequencies')
    @patch('librosa.display.specshow')
    def test_create_freq_analyzer_spectrogram_no_save_path(self, mock_specshow, mock_fft_frequencies, mock_amplitude_to_db, mock_stft, mock_figure, mock_close, mock_savefig):
        """
        Tests that create_freq_analyzer_spectrogram does not call savefig if no save_path is provided.
        """
        audio_data = np.random.rand(16000 * 5)
        audio_sr = 16000

        from ast_src.create_spectrogram import create_freq_analyzer_spectrogram
        returned_path = create_freq_analyzer_spectrogram(audio_data, audio_sr, None)

        mock_savefig.assert_not_called()
        mock_close.assert_not_called()
        self.assertIsNone(returned_path)

    @patch('matplotlib.pyplot.savefig')
    @patch('matplotlib.pyplot.close')
    @patch('matplotlib.pyplot.figure')
    @patch('librosa.stft', side_effect=Exception("Test STFT Error"))
    @patch('librosa.amplitude_to_db')
    @patch('librosa.fft_frequencies')
    @patch('librosa.display.specshow')
    def test_create_freq_analyzer_spectrogram_error_handling(self, mock_specshow, mock_fft_frequencies, mock_amplitude_to_db, mock_stft, mock_figure, mock_close, mock_savefig):
        """
        Tests that create_freq_analyzer_spectrogram handles errors gracefully.
        """
        audio_data = np.random.rand(16000 * 5)
        audio_sr = 16000
        save_path = "test_spectrogram.png"

        from ast_src.create_spectrogram import create_freq_analyzer_spectrogram
        returned_path = create_freq_analyzer_spectrogram(audio_data, audio_sr, save_path)

        mock_savefig.assert_not_called()
        mock_close.assert_not_called()
        self.assertIsNone(returned_path)


if __name__ == '__main__':
    unittest.main()
