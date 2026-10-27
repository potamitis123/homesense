import os
import sqlite3
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from misc.load_parameters import parameters

# 1. SETUP
db_name = parameters.get_parameters()['sqlite']['db_name']
db_path = os.path.join(project_root, db_name)

print(f"--- Database Verification ---")
print(f"Checking DB: {db_path}")

if not os.path.exists(db_path):
    print(f"!!! ERROR: Database file not found!")
    exit(1)

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    # Get the most recent recordings and find the first valid one on disk
    cursor.execute("SELECT id, file_name, date FROM ast_table ORDER BY date DESC")
    rows = cursor.fetchall()
    conn.close()
    
    found_valid = False
    if rows:
        for row in rows:
            evt_id, file_name, date = row
            for folder in ['audio_mp3', 'audios', 'audiosuser']:
                full_path = os.path.join(project_root, "assets", folder, file_name)
                if os.path.exists(full_path) and os.path.getsize(full_path) > 10000: # > 10KB
                    print(f"Found valid recording: ID={evt_id}, File='{file_name}', Size={os.path.getsize(full_path)} bytes")
                    print(f"✅ SUCCESS: A healthy file exists at 'assets/{folder}/'")
                    found_valid = True
                    break
            if found_valid: break
        
        if not found_valid:
            print("❌ FAILURE: No recordings larger than 10KB found on disk.")
    else:
        print("!!! No entries found in 'ast_table'.")

except Exception as e:
    print(f"!!! DB Error: {e}")
