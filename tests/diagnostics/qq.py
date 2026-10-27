import sqlite3
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
db_path = os.path.join(project_root, 'ast_audio_db', 'ast_audio.db')

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute("PRAGMA table_info(users);")
columns = cursor.fetchall()

for col in columns:
    print(f"Column: {col[1]}, Type: {col[2]}, NOT NULL: {col[3]}")

conn.close()