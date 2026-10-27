import sqlite3
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from misc import hash_password

def inspect_users_table():
    db_path = os.path.join(project_root, 'ast_audio_db', 'ast_audio.db')
    
    if not os.path.exists(db_path):
        print(f"Error: Database file not found at {db_path}")
        return
    
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        print(f"Inspecting database: {db_path}")
        
        # Check if users table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users';")
        if cursor.fetchone() is None:
            print("Error: 'users' table does not exist in the database.")
            return
        
        # Insert a test user if it doesn't exist
        cursor.execute("SELECT * FROM users WHERE username='user1'")
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT INTO users (username, password, email) VALUES (?, ?, ?)",
                ('user1', hash_password('user1'), 'user1@example.com')
            )
            conn.commit()
            print("✓ User 'user1' inserted successfully")
        else:
            print("User 'user1' already exists")
        
        # Query users table
        cursor.execute("SELECT username, password FROM users")
        users = cursor.fetchall()
        if users:
            print("\nUsers in 'users' table:")
            for user in users:
                print(f"  Username: {user[0]}, Hashed Password: {user[1]}")
        else:
            print("\n'users' table is empty.")
        
        # Calculate expected hash for 'user1'
        expected_hash = hash_password('user1')
        print(f"\nExpected hash for password 'user1': {expected_hash}")
        
    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    inspect_users_table()