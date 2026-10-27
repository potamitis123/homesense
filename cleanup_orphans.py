import os
import sys
import sqlite3
import shutil
from misc.load_parameters import parameters

def cleanup_orphans(dry_run=True):
    print("Starting orphan cleanup...")
    if dry_run:
        print("DRY RUN MODE: No files will be deleted.")

    # 1. Connect to Database
    try:
        # Assuming we are running from root, but let's be safe
        base_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Try to load parameters to get DB name, otherwise default
        try:
            db_name = parameters.get_parameters()['sqlite']['db_name']
        except:
            db_name = 'ast_audio_db/ast_audio.db'
            
        db_path = os.path.join(base_dir, db_name)
        
        if not os.path.exists(db_path):
            print(f"Error: Database not found at {db_path}")
            return

        print(f"Connected to database: {db_path}")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get all file references
        cursor.execute("SELECT file_name, spectrogram_image FROM ast_table")
        rows = cursor.fetchall()
        conn.close()
        
        # Create sets of valid filenames (basenames only to be safe, as paths might differ)
        valid_audio_files = set()
        valid_image_files = set()
        
        for r in rows:
            if r[0]:
                valid_audio_files.add(os.path.basename(r[0]))
            if r[1]:
                valid_image_files.add(os.path.basename(r[1]))
                
        print(f"Found {len(rows)} records in database.")
        print(f"Unique audio files referenced: {len(valid_audio_files)}")
        print(f"Unique image files referenced: {len(valid_image_files)}")

    except Exception as e:
        print(f"Database error: {e}")
        return

    # 2. Define directories to scan
    assets_dir = os.path.join(base_dir, 'assets')
    audio_dirs = ['audio_mp3', 'audios', 'audiosuser']
    image_dirs = ['specrogram_images'] # Add 'spectrogram_images' if it exists
    
    # 3. Scan and Clean Audio Directories
    for d in audio_dirs:
        full_path = os.path.join(assets_dir, d)
        if not os.path.exists(full_path):
            continue
            
        print(f"\nScanning {d}...")
        files = os.listdir(full_path)
        orphans = 0
        for f in files:
            if os.path.isfile(os.path.join(full_path, f)):
                # Skip non-audio files if necessary, or just check against DB
                # Assuming all files in these folders should be in DB
                if f not in valid_audio_files:
                    orphans += 1
                    file_to_del = os.path.join(full_path, f)
                    if not dry_run:
                        try:
                            os.remove(file_to_del)
                            print(f"Deleted: {f}")
                        except Exception as e:
                            print(f"Error deleting {f}: {e}")
                    else:
                        print(f"Would delete: {f}")
        
        print(f"Found {orphans} orphans in {d}")

    # 4. Scan and Clean Image Directories
    for d in image_dirs:
        full_path = os.path.join(assets_dir, d)
        if not os.path.exists(full_path):
            continue
            
        print(f"\nScanning {d}...")
        files = os.listdir(full_path)
        orphans = 0
        for f in files:
            if os.path.isfile(os.path.join(full_path, f)):
                if f not in valid_image_files:
                    orphans += 1
                    file_to_del = os.path.join(full_path, f)
                    if not dry_run:
                        try:
                            os.remove(file_to_del)
                            print(f"Deleted: {f}")
                        except Exception as e:
                            print(f"Error deleting {f}: {e}")
                    else:
                        print(f"Would delete: {f}")
        
        print(f"Found {orphans} orphans in {d}")

if __name__ == "__main__":
    # check for --execute flag to actually delete
    dry_run = True
    if len(sys.argv) > 1 and sys.argv[1] == "--execute":
        dry_run = False
    
    cleanup_orphans(dry_run)
    print("\nDone.")
