#!/usr/bin/env python3
"""
test_end_to_end.py
------------------
Automated end-to-end integration test script for the Sentinel Home Server.
This script:
1. Stops any conflicting Python background processes (main.py, ast_app.py).
2. Spawns the main processing server (ast_src/main.py) in the background.
3. Spawns the Dash app (ast_dash/ast_app.py) in the background.
4. Waits for the services and AI models to initialize.
5. Sends the mic_test.wav file to audiosense.hmu.gr using the send_test_audio.py logic.
6. Polls the SQLite database for the newly inserted record.
7. Queries the Dash dashboard to ensure it's up and visualizes the results.
8. Gracefully shuts down the background processes.
9. Prints a comprehensive report of the findings and errors.
"""

import os
import sys
import time
import sqlite3
import subprocess
import requests
from datetime import datetime

PYTHON_EXE = r"C:\Users\potam\anaconda3\envs\dash_env\python.exe"
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DB_PATH = os.path.join(PROJECT_ROOT, "ast_audio_db", "ast_audio.db")

def kill_running_instances():
    """Finds and kills any existing running main.py or ast_app.py python scripts."""
    print("[*] Terminating any existing main.py or ast_app.py processes...")
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info['cmdline']
                if cmdline and any('python' in arg.lower() for arg in cmdline):
                    if any('main.py' in arg or 'ast_app.py' in arg for arg in cmdline):
                        print(f"[*] Killing process {proc.info['pid']}: {' '.join(cmdline)}")
                        proc.terminate()
                        proc.wait(timeout=3)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                pass
    except ImportError:
        # Fallback to taskkill if psutil is not available
        os.system('taskkill /F /FI "IMAGENAME eq python.exe" /T >nul 2>&1')
        time.sleep(2)

def check_db_record_count():
    """Returns the current number of records in the ast_table."""
    if not os.path.exists(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM ast_table")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        print(f"[!] Error reading database count: {e}")
        return 0

def check_latest_record():
    """Retrieves the latest record in the ast_table."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        # Use row factory to get a dictionary
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ast_table ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
        return None
    except Exception as e:
        print(f"[!] Error fetching latest database record: {e}")
        return None

def main():
    print("="*80)
    print("           SENTINEL HOME SERVER END-TO-END INTEGRATION TEST")
    print("="*80)
    
    # 1. Cleanup old instances
    kill_running_instances()
    
    # Check current DB record count
    initial_count = check_db_record_count()
    print(f"[*] Initial ast_table record count: {initial_count}")
    
    # Create logs directory if not exists
    os.makedirs(os.path.join(PROJECT_ROOT, "logs_ast_app"), exist_ok=True)
    
    # 2. Spawn ast_src/main.py
    print("[*] Launching main.py (Background Processing Pipeline with ML Models)...")
    main_log_file = open("main_stdout_stderr.log", "w", encoding="utf-8")
    main_proc = subprocess.Popen(
        [PYTHON_EXE, "ast_src/main.py"],
        stdout=main_log_file,
        stderr=main_log_file,
        cwd=PROJECT_ROOT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    
    # 3. Spawn ast_dash/ast_app.py
    print("[*] Launching ast_dash/ast_app.py (Dash UI & Housekeeper)...")
    dash_log_file = open("dash_stdout_stderr.log", "w", encoding="utf-8")
    dash_proc = subprocess.Popen(
        [PYTHON_EXE, "ast_dash/ast_app.py"],
        stdout=dash_log_file,
        stderr=dash_log_file,
        cwd=PROJECT_ROOT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    
    # 4. Wait for models to load and Dash server to spin up
    print("[*] Waiting 25 seconds for models (AST, Silero-VAD, Whisper, CLAP) and Dash server to initialize...")
    for i in range(25):
        time.sleep(1)
        print(f"[*] Waiting... {25 - i}s left")
        
    # Check if the processes are still alive
    if main_proc.poll() is not None:
        print("[!] ERROR: main.py exited prematurely!")
        with open("main_stdout_stderr.log", "r", encoding="utf-8", errors="replace") as lf:
            print("--- main.py LOG TAIL ---")
            print("".join(lf.readlines()[-30:]))
            print("------------------------")
            
    if dash_proc.poll() is not None:
        print("[!] ERROR: ast_app.py exited prematurely!")
        with open("dash_stdout_stderr.log", "r", encoding="utf-8", errors="replace") as lf:
            print("--- ast_app.py LOG TAIL ---")
            print("".join(lf.readlines()[-30:]))
            print("---------------------------")
            
    # 5. Run send_test_audio.py to publish the audio file to audiosense.hmu.gr
    print("[*] Executing send_test_audio.py to transmit mic_test.wav...")
    send_result = subprocess.run(
        [PYTHON_EXE, "send_test_audio.py"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT
    )
    
    print("--- send_test_audio.py OUTPUT ---")
    print(send_result.stdout)
    if send_result.stderr:
        print("[!] send_test_audio.py stderr:")
        print(send_result.stderr)
    print("---------------------------------")
    
    # 6. Poll the database and check for processing progress
    print("[*] Polling database and monitoring files for up to 45 seconds to observe processing...")
    success = False
    latest_rec = None
    
    start_time = time.time()
    while time.time() - start_time < 45:
        time.sleep(3)
        current_count = check_db_record_count()
        latest_rec = check_latest_record()
        
        # Check if we have a new record from our simulated device "box3-2"
        if current_count > initial_count and latest_rec and latest_rec.get("device_name") == "box3-2":
            print(f"[+] Success! A new record has been inserted into the database!")
            success = True
            break
        else:
            print(f"[*] Checking database... current count: {current_count} (Waiting for box3-2 processing...)")
            
    # 7. Check if Dash web server responds
    print("[*] Testing Dash Server responsiveness (https://localhost:8050)...")
    dash_online = False
    try:
        # Disable SSL warning for test
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get("https://localhost:8050", timeout=5, verify=False)
        if resp.status_code == 200:
            print("[+] Success: Dash App is ONLINE on HTTPS and responded with 200 OK!")
            dash_online = True
        else:
            print(f"[!] Dash App returned status code: {resp.status_code}")
    except Exception as e:
        print(f"[!] Failed to reach Dash App via HTTPS: {e}")
        # Try fallback to HTTP
        try:
            resp = requests.get("http://localhost:8050", timeout=5)
            if resp.status_code == 200:
                print("[+] Success: Dash App is ONLINE on HTTP and responded with 200 OK!")
                dash_online = True
        except Exception as e2:
            print(f"[!] Fallback HTTP check also failed: {e2}")
        
    # 8. Shutdown background processes gracefully
    print("[*] Shutting down background processes gracefully...")
    try:
        main_proc.terminate()
        dash_proc.terminate()
        main_proc.wait(timeout=5)
        dash_proc.wait(timeout=5)
    except Exception as e:
        print(f"[!] Warning during shutdown: {e}")
        # Force kill if needed
        try:
            main_proc.kill()
            dash_proc.kill()
        except:
            pass
            
    main_log_file.close()
    dash_log_file.close()
    
    # 9. Print final report
    print("\n" + "="*80)
    print("                             FINAL VERIFICATION REPORT")
    print("="*80)
    
    if success:
        print("[STATUS] SUCCESS: The audio file was successfully received, processed, and database verified!")
        print(f"  - Record ID: {latest_rec.get('id')}")
        print(f"  - Timestamp: {latest_rec.get('date')}")
        print(f"  - Device: {latest_rec.get('device_name')}")
        print(f"  - Processed File: {latest_rec.get('file_name')}")
        print(f"  - Spectrogram Image: {latest_rec.get('spectrogram_image')}")
        print(f"  - Sound Labels: {latest_rec.get('sound1')} ({latest_rec.get('conf_sound1')}), {latest_rec.get('sound2')} ({latest_rec.get('conf_sound2')}), {latest_rec.get('sound3')} ({latest_rec.get('conf_sound3')})")
        print(f"  - Transcription: {latest_rec.get('transcription')}")
        print(f"  - Emotion: {latest_rec.get('emotion')}")
        print(f"  - LLM Verdict: {latest_rec.get('llm_verdict')}")
        print(f"  - CLAP Embedding Exists: {'Yes (blob)' if latest_rec.get('clap_embedding') else 'No'}")
    else:
        print("[STATUS] FAILED: Audio file was not processed or did not result in a new database entry.")
        
    if dash_online:
        print("  - Dash Dashboard Interface Status: ONLINE (200 OK)")
    else:
        print("  - Dash Dashboard Interface Status: OFFLINE / Error")
        
    print("\n--- Summary of Processing Server Logs ---")
    if os.path.exists("main_stdout_stderr.log"):
        with open("main_stdout_stderr.log", "r", encoding="utf-8", errors="replace") as lf:
            lines = lf.readlines()
            print(f"Total lines in log: {len(lines)}")
            # Show lines mentioning errors, warnings or important info
            important_lines = [l for l in lines if any(w in l.upper() for w in ["ERROR", "WARN", "FAIL", "SUCCESS", "PROCESS", "CONNECTED"])]
            print(f"Showing last 15 important log lines:")
            for line in important_lines[-15:]:
                print(f"  {line.strip()}")
                
    print("\n--- Summary of Dash Frontend Logs ---")
    if os.path.exists("dash_stdout_stderr.log"):
        with open("dash_stdout_stderr.log", "r", encoding="utf-8", errors="replace") as lf:
            lines = lf.readlines()
            print(f"Total lines in log: {len(lines)}")
            important_lines = [l for l in lines if any(w in l.upper() for w in ["ERROR", "WARN", "FAIL", "PORT", "RUNNING", "ADDRESS"])]
            print(f"Showing last 15 important log lines:")
            for line in important_lines[-15:]:
                print(f"  {line.strip()}")
                
    print("="*80)
    
    # Exit with code based on success
    if success and dash_online:
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
