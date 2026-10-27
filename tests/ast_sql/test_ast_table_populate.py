
import unittest
from unittest.mock import patch, MagicMock
import os
import sys
from datetime import datetime
import pandas as pd
import sqlite3
import numpy as np # Added numpy import

# Add the project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# We need to mock ast_model for the ontology import in ast_sql_lite.py
sys.modules['ast_src.ast_model'] = MagicMock()
sys.modules['ast_src.ast_model'].AST = MagicMock()
sys.modules['ast_src.ast_model'].AST.return_value.get_classes.return_value = ['mock_class_1', 'mock_class_2']


from ast_dash.mock_data_loader import get_local_audio_files
from ast_src.create_spectrogram import generate_spectrogram_from_audio_file
from ast_sql.ast_sql_lite import Ast_Sql_lite
# No need to import Ast_table directly, as it's accessed via Ast_Sql_lite

# Define a new utility function here to avoid circular imports if it were in Ast_Sql_lite directly
def populate_ast_table_from_audio_examples(db_path, audio_examples_dir='audio_examples', spectrogram_output_dir='../assets/specrogram_images'):
    """
    Orchestrates loading audio file metadata, generating spectrograms, and inserting into ast_table.
    """
    db_manager = None
    try:
        db_manager = Ast_Sql_lite(db_path)
        audio_files_metadata = get_local_audio_files(audio_dir=audio_examples_dir) # Corrected argument name

        for item in audio_files_metadata:
            # Generate spectrogram
            full_audio_filepath = os.path.join(audio_examples_dir, item['filename'])
            spectrogram_image_path = generate_spectrogram_from_audio_file(full_audio_filepath, output_dir=spectrogram_output_dir)

            if spectrogram_image_path:
                # Extract just the filename for storage in DB
                spectrogram_image_filename = os.path.basename(spectrogram_image_path)

                db_manager.ast_table.insert_ast_table(
                    date=item['timestamp'],
                    device_name=item['device_name'],
                    file_name=item['filename'],
                    spectrogram_image=spectrogram_image_filename,
                    sound1=item['sound1'],
                    conf_sound1=item['conf_sound1'],
                    sound2=item['sound2'],
                    conf_sound2=item['conf_sound2'],
                    sound3=item['sound3'],
                    conf_sound3=item['conf_sound3'],
                    rms=item['rms']
                    # latitude and longitude are optional and not mocked in mock_data_loader yet
                )
    finally:
        if db_manager:
            db_manager.close() # Ensure connection is closed

class TestAstTablePopulate(unittest.TestCase):

    def setUp(self):
        self.test_db_path = 'test_ast_audio.db'
        self.test_audio_examples_dir = 'test_audio_examples'
        self.test_spectrogram_output_dir = 'test_spectrogram_output'

        # Clean up previous test artifacts
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        if os.path.exists(self.test_audio_examples_dir):
            import shutil
            shutil.rmtree(self.test_audio_examples_dir)
        if os.path.exists(self.test_spectrogram_output_dir):
            import shutil
            shutil.rmtree(self.test_spectrogram_output_dir)

        # Create dummy audio_examples directory and files
        os.makedirs(self.test_audio_examples_dir, exist_ok=True)
        with open(os.path.join(self.test_audio_examples_dir, '20260114_124616_esp32s3-2_rec.mp3'), 'w') as f:
            f.write('dummy_audio_content_1')
        with open(os.path.join(self.test_audio_examples_dir, '20260115_103000_deviceX_rec.mp3'), 'w') as f:
            f.write('dummy_audio_content_2')
        
        # Create dummy spectrogram output dir
        os.makedirs(self.test_spectrogram_output_dir, exist_ok=True)

    def tearDown(self):
        # Ensure db connection is closed before attempting to remove the file
        # The connection created by Ast_Sql_lite in populate_ast_table_from_audio_examples is already closed.
        # So this tearDown only needs to clean up the files.
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        if os.path.exists(self.test_audio_examples_dir):
            import shutil
            shutil.rmtree(self.test_audio_examples_dir)
        if os.path.exists(self.test_spectrogram_output_dir):
            import shutil
            shutil.rmtree(self.test_spectrogram_output_dir)

    @patch(__name__ + '.get_local_audio_files')
    @patch('ast_src.create_spectrogram.generate_spectrogram_from_audio_file')
    @patch('librosa.load') # Patch librosa.load here
    def test_populate_ast_table_inserts_data(self, mock_librosa_load, mock_generate_spectrogram, mock_get_local_audio_files):
        """
        Tests that populate_ast_table_from_audio_examples correctly calls insert_ast_table
        with the processed data, including spectrogram info.
        """
        # Mock librosa.load to return dummy audio data and sample rate
        mock_librosa_load.return_value = (np.zeros(16000 * 5), 16000)

        # The paths returned by generate_spectrogram_from_audio_file
        spectrogram_path_f1 = os.path.join(self.test_spectrogram_output_dir, '20260115_103000_deviceX_rec.png')
        spectrogram_path_f2 = os.path.join(self.test_spectrogram_output_dir, '20260114_124616_esp32s3-2_rec.mp3')

        # Define the exact mock data that get_local_audio_files will return.
        # This list is in the expected sorted order (latest timestamp first)
        mock_audio_metadata_return = [
            {
                'event_id': 2, 
                'filename': '20260115_103000_deviceX_rec.mp3',
                'device_name': 'deviceX',
                'timestamp': '2026-01-15 10:30:00',
                'filepath': os.path.join(self.test_audio_examples_dir, '20260115_103000_deviceX_rec.mp3'),
                'sound1': 'mock_sound_1_f1', 'conf_sound1': 0.91,
                'sound2': 'mock_sound_2_f1', 'conf_sound2': 0.51,
                'sound3': 'mock_sound_3_f1', 'conf_sound3': 0.21,
                'rms': 0.71,
                'spectrogram_image': os.path.basename(spectrogram_path_f1)
            },
            {
                'event_id': 1,
                'filename': '20260114_124616_esp32s3-2_rec.mp3',
                'device_name': 'esp32s3-2',
                'timestamp': '2026-01-14 12:46:16',
                'filepath': os.path.join(self.test_audio_examples_dir, '20260114_124616_esp32s3-2_rec.mp3'),
                'sound1': 'mock_sound_1_f2', 'conf_sound1': 0.92,
                'sound2': 'mock_sound_2_f2', 'conf_sound2': 0.52,
                'sound3': 'mock_sound_3_f2', 'conf_sound3': 0.22,
                'rms': 0.72,
                'spectrogram_image': os.path.basename(spectrogram_path_f2)
            }
        ]
        mock_get_local_audio_files.return_value = mock_audio_metadata_return
        
        # Mock spectrogram generation
        mock_generate_spectrogram.side_effect = [
            spectrogram_path_f1,
            spectrogram_path_f2
        ]

        populate_ast_table_from_audio_examples(self.test_db_path, 
                                                 audio_examples_dir=self.test_audio_examples_dir,
                                                 spectrogram_output_dir=self.test_spectrogram_output_dir)
        
        # Verify data was inserted by querying directly
        db_conn = sqlite3.connect(self.test_db_path)
        cursor = db_conn.cursor()
        cursor.execute("SELECT file_name, spectrogram_image, sound1, conf_sound1, rms FROM ast_table ORDER BY date DESC")
        results = cursor.fetchall()
        db_conn.close()

        self.assertEqual(len(results), 2)
        
        # Check first file (latest timestamp)
        file1_file_name, file1_spectrogram, file1_sound1, file1_conf1, file1_rms = results[0]
        self.assertEqual(file1_file_name, '20260115_103000_deviceX_rec.mp3')
        self.assertEqual(file1_spectrogram, os.path.basename(spectrogram_path_f1))
        self.assertEqual(file1_sound1, 'mock_sound_1_f1')
        self.assertEqual(file1_conf1, 0.91)
        self.assertEqual(file1_rms, 0.71)

        # Check second file (earlier timestamp)
        file2_file_name, file2_spectrogram, file2_sound1, file2_conf1, file2_rms = results[1]
        self.assertEqual(file2_file_name, '20260114_124616_esp32s3-2_rec.mp3')
        self.assertEqual(file2_spectrogram, os.path.basename(spectrogram_path_f2))
        self.assertEqual(file2_sound1, 'mock_sound_1_f2')
        self.assertEqual(file2_conf1, 0.92)
        self.assertEqual(file2_rms, 0.72)

if __name__ == '__main__':
    unittest.main()
