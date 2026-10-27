import os
import sys
import glob
import torch
import numpy as np

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Import the function and its dependencies
from ast_src.main import load_audio_file 
from misc.log_test import logger
from ast_sql.ast_sql_lite import Ast_Sql_lite
from ast_src.ast_model import AST

def test_proc_loading():
    """
    A test function that simulates the file discovery and loading part
    of the proc() function in main.py, using the audio_examples directory.
    """
    print("--- Starting Test ---")
    
    # --- 1. Database and Model Setup (mocked or minimal) ---
    # We don't need a real DB or a full model for this test
    print("Initializing dummy components...")
    ast_db = None # Not needed for this test
    ast_model = None # Not needed for this test
    
    # --- 2. File Discovery ---
    # Use the corrected glob logic to find the test file
    assets_path = os.path.join(project_root, 'audio_examples')
    audio_files = glob.glob(os.path.join(assets_path, '*_rec.mp3'))
    
    if not audio_files:
        print("!!! TEST FAILED: No test .mp3 files found in audio_examples.")
        return

    print(f"Found {len(audio_files)} file(s) to test.")

    # --- 3. Processing Loop ---
    all_passed = True
    for audio_file in audio_files:
        print(f"--- Processing: {audio_file} ---")
        
        # Call the actual load_audio_file function from main.py
        data_float, audio, sr, bitrate, rms, dbrms = load_audio_file(audio_file)
        
        # --- 4. Verification ---
        if data_float is not None and audio is not None:
            print(f"  [SUCCESS] File loaded successfully.")
            print(f"  > Duration: {audio.duration_seconds:.2f}s")
            print(f"  > Sample Rate: {sr} Hz")
            print(f"  > RMS (dB): {dbrms}")
        else:
            print(f"  [FAILURE] File failed to load. 'load_audio_file' returned None.")
            all_passed = False
            
    print("--- Test Summary ---")
    if all_passed:
        print("✅ All test files in 'audio_examples' were loaded successfully.")
    else:
        print("❌ One or more test files failed to load.")

if __name__ == "__main__":
    # Add dummy patches for full main.py import if needed, but we only need load_audio_file
    # This avoids loading all the heavy models for a simple I/O test.
    
    # Ensure the assets/audiosuser directory exists as main.py might expect it
    os.makedirs(os.path.join(project_root, "assets", "audiosuser"), exist_ok=True)
    
    test_proc_loading()
