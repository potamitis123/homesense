import sqlite3
import os
import sys

# Add project root to sys.path
sys.path.append(os.getcwd())
from misc.load_parameters import parameters

def list_names():
    db_name = parameters.get_parameters()['sqlite']['db_name']
    db_path = os.path.abspath(db_name)
    print(f"Connecting to {db_path}...")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check ontology table
    cursor.execute("SELECT name FROM ontology WHERE name LIKE '%Outdoors%'")
    rows = cursor.fetchall()
    print("Ontology Names matching 'Outdoors':")
    for row in rows:
        print(f" - {row[0]}")
        
    # Check groups just in case
    try:
        cursor.execute("SELECT group_name FROM group_of_clases")
        rows = cursor.fetchall()
        print("\nGroup Names:")
        for row in rows:
            print(f" - {row[0]}")
    except:
        pass
        
    conn.close()

if __name__ == "__main__":
    list_names()
