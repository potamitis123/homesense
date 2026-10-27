import sqlite3
import pandas as pd
import os
import sys
import time
from datetime import datetime, timedelta
from misc.log_test import logger
from misc.secrets import openai_api_key

# Try importing OpenAI, handle if not installed
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# Credentials live in common_files/parameters.json (see misc/secrets.py).
API_KEY = openai_api_key()

LOG_D = {'module_name': 'online_scan'}

def get_db_connection(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    return conn

def format_event(row):
    # Format: Time | Labels(sound1, sound2, sound3) | Transcription | Emotion
    timestamp = row['date']
    
    # Sound labels
    sounds = []
    if row['sound1'] and row['sound1'] != 'None': sounds.append(f"{row['sound1']}({row['conf_sound1']})")
    if row['sound2'] and row['sound2'] != 'None': sounds.append(f"{row['sound2']}({row['conf_sound2']})")
    if row['sound3'] and row['sound3'] != 'None': sounds.append(f"{row['sound3']}({row['conf_sound3']})")
    sound_str = ", ".join(sounds)
    
    transcription = row['transcription'] if row['transcription'] else ""
    emotion = row['emotion'] if row['emotion'] else ""
    
    return f"{timestamp} | Sounds: [{sound_str}] | ASR: {transcription} | Emotion: {emotion}"

def call_ollama(model, messages):
    import requests
    try:
        # Assuming Ollama is running locally on default port 11434
        url = "http://localhost:11434/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            return response.json()['message']['content']
        else:
            logger.error(f"Ollama error: {response.text}", extra=LOG_D)
            return None
    except Exception as e:
        logger.error(f"Ollama connection failed: {e}", extra=LOG_D)
        return None

def scan_latest(db_path, current_event_id):
    """
    Scans the rolling window ending at current_event_id.
    Returns the verdict string.
    """
    conn = None
    try:
        conn = get_db_connection(db_path)
        
        # 1. Get current event timestamp
        query_current = f"SELECT date FROM ast_table WHERE id = {current_event_id}"
        cursor = conn.cursor()
        cursor.execute(query_current)
        result = cursor.fetchone()
        
        if not result:
            logger.error(f"Event ID {current_event_id} not found", extra=LOG_D)
            return "Error: Event not found"
            
        current_time_str = result[0]
        # Handle potential fractional seconds or different formats
        try:
            current_time = datetime.strptime(current_time_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                current_time = datetime.strptime(current_time_str, '%Y-%m-%d %H:%M:%S.%f')
            except:
                 # Fallback for other formats if necessary
                 current_time = pd.to_datetime(current_time_str).to_pydatetime()

        start_time = current_time - timedelta(minutes=2)
        start_time_str = start_time.strftime('%Y-%m-%d %H:%M:%S')
        
        # 2. Get events in window [start_time, current_time], sorted Past to Present (ASC)
        # Note: 'date' column in sqlite is text, so string comparison works for ISO format
        query_window = f"""
            SELECT date, sound1, conf_sound1, sound2, conf_sound2, sound3, conf_sound3, transcription, emotion 
            FROM ast_table 
            WHERE date >= '{start_time_str}' AND date <= '{current_time_str}'
            ORDER BY date ASC
        """
        
        df = pd.read_sql_query(query_window, conn)
        
        if df.empty:
            logger.warning("No events found in window", extra=LOG_D)
            return "No Data"

        # 3. Format lines
        lines = [format_event(row) for _, row in df.iterrows()]
        
        # Log input
        logger.info(f"LLM Input Window ({len(lines)} events):", extra=LOG_D)
        for line in lines:
            logger.info(f"  {line}", extra=LOG_D)

        # 4. Load Prompt
        # Assuming assets is in the parent or current directory relative to execution
        # We need to find the assets folder. 
        # Since this script is called from main.py, we can use a relative path or find it.
        # Try standard locations
        prompt_path = os.path.join("assets", "prompt_elderly.txt")
        if not os.path.exists(prompt_path):
             prompt_path = os.path.join("..", "assets", "prompt_elderly.txt")
        
        if os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                prompt_system = f.read()
        else:
            logger.error("Prompt file not found", extra=LOG_D)
            prompt_system = "You are an audio surveillance agent. Identify hazards."

        prompt_instruction = "Be brief. Reply strictly as: OK or [Hazard] <brief explanation>."
        
        user_content = (
            "Timeline (rolling window):\n" +
            "\n".join(f"- {ln}" for ln in lines) + 
            "\n\n" + prompt_instruction
        )
        
        messages = [
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": user_content},
        ]

        # 5. Call LLM
        verdict = None
        
        # Try Ollama
        logger.info("Attempting Ollama...", extra=LOG_D)
        #verdict = call_ollama("llama3.2:latest", messages)
        verdict = 'Ok' # too slow. Only chatGPT API call works. Put None for GPT
        if verdict:
            logger.info(f"Ollama Verdict: {verdict}", extra=LOG_D)
        
        # Fallback to OpenAI
        if verdict is None and OpenAI:
            logger.info("Attempting fallback to OpenAI...", extra=LOG_D)
            try:
                client = OpenAI(api_key=API_KEY)
                resp = client.chat.completions.create(
                    model="gpt-4o-mini", # Using a fast model
                    messages=messages,
                    timeout=10
                )
                verdict = resp.choices[0].message.content.strip()
                logger.info(f"OpenAI Verdict: {verdict}", extra=LOG_D)
            except Exception as e:
                logger.error(f"OpenAI failed: {e}", extra=LOG_D)
        
        if verdict is None:
            verdict = "Error: LLM failed"

        return f"[ID: {current_event_id} | {current_time_str}] {verdict}"

    except Exception as e:
        logger.error(f"Scan failed: {e}", extra=LOG_D)
        return f"Error: {str(e)}"
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    # Test run
    # Usage: python online_scan.py <db_path> <event_id>
    if len(sys.argv) > 2:
        print(scan_latest(sys.argv[1], sys.argv[2]))
