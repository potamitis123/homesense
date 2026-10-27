import sqlite3
import os
from misc.load_parameters import parameters

def migrate():
    db_name = parameters.get_parameters()['sqlite']['db_name']
    db_path = os.path.join(os.getcwd(), db_name)
    print(f"Migrating DB: {db_path}")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE ast_table ADD COLUMN llm_verdict TEXT")
        conn.commit()
        print("Success: Added llm_verdict column")
    except Exception as e:
        print(f"Info/Error: {e}")
    conn.close()

if __name__ == "__main__":
    migrate()
