import sqlite3
import logging
import os
import sys
from datetime import datetime

# Ensure we can import from the project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from online_scan import scan_latest
from misc.load_parameters import parameters
from misc.log_test import logger

def setup_console_logging():
    """Force logger to print to console for this test"""
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)

def insert_mock_hazard(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Insert a mock hazardous event
    # Labels: Speech, Scream, Thud
    # Transcription: "Help me I fell"
    # Emotion: SAD (or distress)
    
    sql = """
        INSERT INTO ast_table (
            date, file_name, device_name, spectrogram_image,
            sound1, conf_sound1,
            sound2, conf_sound2,
            sound3, conf_sound3,
            rms, transcription, emotion, clap_embedding
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    
    # Mock data
    file_name = f"mock_hazard_{now_str.replace(':', '').replace(' ', '_')}.mp3"
    
    values = (
        now_str, 
        file_name, 
        "mock_device", 
        "mock_spectrogram.png",
        "Scream", 0.95,
        "Thud/Impact", 0.88, 
        "Speech", 0.90,
        -10.5, # RMS
        "Help me, I've fallen!", 
        "SAD",
        None # clap_embedding
    )
    
    cursor.execute(sql, values)
    conn.commit()
    
    new_id = cursor.lastrowid
    conn.close()
    
    print(f"\n[INFO] Inserted Mock Hazard Event ID: {new_id}")
    return new_id

def run_test():
    setup_console_logging()
    
    # 1. Locate Database
    try:
        params = parameters.get_parameters()
        db_name = params['sqlite']['db_name']
    except Exception as e:
        print(f"[WARN] Could not load parameters: {e}. Defaulting.")
        db_name = "ast_audio_db/ast_audio.db"

    if not os.path.exists(db_name):
        potential_path = os.path.join(os.getcwd(), db_name)
        if os.path.exists(potential_path):
            db_path = potential_path
        elif os.path.exists(os.path.join("ast_audio_db", "ast_audio.db")):
             db_path = os.path.join("ast_audio_db", "ast_audio.db")
        else:
            print(f"[ERROR] Database file not found at {db_name}")
            return
    else:
        db_path = db_name

    # 2. Insert Mock Hazard
    event_id = insert_mock_hazard(db_path)
    
    # 3. Run Scan
    print("[INFO] Running online_scan...")
    verdict = scan_latest(db_path, event_id)
    
    print("\n" + "="*50)
    print(" VERDICT FOR MOCK HAZARD")
    print("="*50)
    print(f"\n{verdict}\n")
    print("="*50)
    
    # Clean up (Optional, but good for repetitive testing)
    # conn = sqlite3.connect(db_path)
    # conn.execute(f"DELETE FROM ast_table WHERE id={event_id}")
    # conn.commit()
    # conn.close()

if __name__ == "__main__":
    run_test()
