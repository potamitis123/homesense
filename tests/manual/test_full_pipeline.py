import os
import sys
import shutil
import time
import torch
import json
import numpy as np
import glob
import ctypes
import torchaudio

# --- 1. SETUP & PATCHING ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Import misc early to apply the SSL certificate store monkeypatch
import misc

# Suppress Windows Error Dialogs
try:
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except:
    pass

# Patch torchaudio for compatibility
if not hasattr(torchaudio, "set_audio_backend"):
    def dummy_set_audio_backend(backend): pass
    torchaudio.set_audio_backend = dummy_set_audio_backend
if not hasattr(torchaudio, "list_audio_backends"):
    def dummy_list_audio_backends(): return ["soundfile"]
    torchaudio.list_audio_backends = dummy_list_audio_backends

# --- 2. IMPORTS ---
from ast_src.main import proc, load_audio_file
from mqtt_clent.mqtt_client import MQTTAudioReceiver, Record, logger
from ast_src.ast_model import AST
from ast_src.emotion_module import EmotionRecognizer
from ast_src.streaming_ASR import ASRTranscriber
from ast_sql.ast_sql_lite import Ast_Sql_lite
from misc.load_parameters import parameters

LOG_D={'module_name':'test_full_pipeline'}

def simulate_mqtt_reception(test_file_path, chunk_size=6000):
    print("--- Simulating MQTT File Reception ---")
    logger.info("--- Simulating MQTT File Reception ---", extra=LOG_D)
    
    if not os.path.exists(test_file_path):
        print(f"!!! ERROR: Test file not found at {test_file_path}")
        return None
        
    with open(test_file_path, 'rb') as f:
        file_bytes = f.read()

    total_size = len(file_bytes)
    # Split into chunks
    chunks = {}
    for i, start in enumerate(range(0, total_size, chunk_size)):
        chunks[i] = file_bytes[start : start + chunk_size]
    
    total_chunks = len(chunks)
    print(f"Simulating {total_size} bytes in {total_chunks} chunks.")

    mock_record = Record(
        chunks=chunks,
        total_size=total_size,
        duration=15,
        format='mp3',
        total_chunks=total_chunks,
        samplerate=16000,
        bits=16,
        sender_name='test_device',
        topic='mock/topic',
        rms_threshold=17.0,
        esp_audio_duration=15
    )

    save_path = os.path.join(project_root, "assets", "audios")
    os.makedirs(save_path, exist_ok=True)
    
    # We need to ensure LOG_D is handled in MQTTAudioReceiver too
    mqtt_receiver = MQTTAudioReceiver(host="localhost", port=1883, username="", password="", save_sound_path=save_path)

    print("Calling internal '__save_recording__' method...")
    mqtt_receiver.__save_recording__(mock_record)

    # Find the newly created file (WAV)
    list_of_files = glob.glob(os.path.join(save_path, '*_test_device_rec.wav'))
    if not list_of_files:
        print("!!! TEST FAILED: No file created.")
        return None
        
    latest_file = max(list_of_files, key=os.path.getctime)
    print(f"File created: {latest_file}")
    return latest_file

def run_processing_test(created_file_path):
    print("--- Verifying Created File & Starting Processing ---")
    
    # Check if file is empty
    if os.path.getsize(created_file_path) == 0:
        print("!!! TEST FAILED: Created file is empty (0 bytes).")
        return

    print("Attempting to load with 'load_audio_file'...")
    data_float, audio, _, _, _, _ = load_audio_file(created_file_path)

    if audio is None:
        print("!!! TEST FAILED: The file is corrupt and cannot be loaded by pydub/ffmpeg.")
        return
    else:
        print(f"✅ Success: Loaded {audio.duration_seconds:.2f}s of audio.")

    print("--- Initializing models for full pipeline test ---")
    os.environ["RUN_ONCE"] = "1"
    
    # Set db_path for online_scan
    db_name = parameters.get_parameters()['sqlite']['db_name']
    db_path = os.path.join(project_root, db_name)
    
    ast_db = Ast_Sql_lite(db_path)
    ast_model = AST()
    emotion_model = EmotionRecognizer()
    asr_model = ASRTranscriber()
    
    print("--- Starting 'proc' loop ---")
    # Note: proc loop in main.py also has a global db_path variable requirement for online_scan
    # We might need to be careful here if main.py uses a global variable.
    import ast_src.main
    ast_src.main.db_path = db_path # Inject it
    
    proc(ast=ast_model, ast_db=ast_db, db_path=db_path, emotion_model=emotion_model, asr_model=asr_model)
    print("--- 'proc' loop finished ---")
    
    if not os.path.exists(created_file_path):
        print("✅✅✅ E2E TEST PASSED!")
    else:
        print("❌❌❌ E2E TEST FAILED: File still exists.")
        
    ast_db.close()

if __name__ == "__main__":
    test_src = os.path.join(project_root, "audio_examples", "20260309_172205_esp32s3-1_rec.mp3")
    created = None
    try:
        created = simulate_mqtt_reception(test_src)
        if created:
            run_processing_test(created)
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if created and os.path.exists(created):
            os.remove(created)
            print(f"Cleaned up: {created}")
