import sqlite3
import os
import sys

# Ensure project root is in path
sys.path.append(os.getcwd())
from misc.load_parameters import parameters

def rename_outdoors():
    db_name = parameters.get_parameters()['sqlite']['db_name']
    db_path = os.path.abspath(db_name)
    print(f"Connecting to {db_path}...")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Update group_of_clases
        cursor.execute("UPDATE group_of_clases SET group_name = 'Outdoors' WHERE group_name = 'Outdoors acoustic surveillance'")
        print(f"Updated group_of_clases: {cursor.rowcount} rows")
        
        # Update clases_of_group
        cursor.execute("UPDATE clases_of_group SET group_name = 'Outdoors' WHERE group_name = 'Outdoors acoustic surveillance'")
        print(f"Updated clases_of_group: {cursor.rowcount} rows")
        
        conn.commit()
        print("Success.")
    except Exception as e:
        print(f"Error: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    rename_outdoors()
