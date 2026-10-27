"""Print the most recent analysis results, to confirm the pipeline end to end."""

import os
import sqlite3

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB = os.path.join(PROJECT_ROOT, "ast_audio_db", "ast_audio.db")

connection = sqlite3.connect(DB)
rows = connection.execute(
    "SELECT id, date, device_name, sound1, conf_sound1, emotion, transcription "
    "FROM ast_table WHERE transcription IS NOT NULL AND transcription != '' "
    "ORDER BY id DESC LIMIT 6"
).fetchall()

print(f"{'ID':<7}{'WHEN':<21}{'DEVICE':<13}{'SOUND':<10}{'EMO':<6}TRANSCRIPTION")
print("-" * 110)
for event_id, when, device, sound, confidence, emotion, text in rows:
    text = " ".join((text or "").split())
    print(f"{event_id:<7}{when:<21}{device:<13}{str(sound)[:9]:<10}{str(emotion)[:5]:<6}{text[:60]}")
connection.close()
