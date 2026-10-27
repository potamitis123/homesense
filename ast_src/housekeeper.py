import os
import shutil
import sqlite3
import zipfile
import threading
import time
import json
from datetime import datetime, timedelta
import logging
import pandas as pd # Added import
from misc.log_test import logger
from misc.load_parameters import parameters

LOG_D = {'module_name': 'housekeeper'}
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BACKUP_DIR = os.path.join(PROJECT_ROOT, 'backups')
BACKUP_STATUS_FILE = os.path.join(PROJECT_ROOT, 'backup_status.json')
LIVE_STATUS_FILE = os.path.join(PROJECT_ROOT, 'housekeeper_live_status.json')
LOCK_FILE = os.path.join(PROJECT_ROOT, 'housekeeper.lock')
CHECK_INTERVAL_SECONDS = 3600  # Check every hour

# Fallback only. The live value comes from retention_days() below, so it can
# be changed from the Settings tab without editing code.
#
# The default is deliberately long. Until 30 Aug 2026 this was 7 days and the
# cleanup had never once run: MAX(date) was returning a mock row's literal
# 'MOCK-- MQ:TT:' string, so the housekeeper believed there were no recordings
# at all. Fixing that detection without also raising this would have deleted
# 18,484 rows -- every event before the last week, which is what the
# statistics, heatmap and daily report are built from -- on the first tick.
DATA_RETENTION_DAYS = 365


def _days(key, fallback):
    try:
        value = int(parameters.get_parameters().get('housekeeper', {}).get(key, fallback))
        # A zero or negative retention would delete everything on the next
        # tick; treat it as "not configured" rather than as an instruction.
        return value if value >= 1 else fallback
    except Exception:
        return fallback


def audio_retention_days():
    """How long to keep the recordings themselves.

    Separate from the rows because they cost wildly different amounts: 4,480
    recordings occupy 838 MB while all 27,980 database rows occupy 58 MB. The
    audio is the disk problem; the rows are what the statistics, heatmap and
    daily report are built from. A single number forced you to trade one for
    the other -- keeping two days of audio also meant keeping two days of
    history.
    """
    return _days('audio_retention_days', 30)


def row_retention_days():
    """How long to keep the event rows. Should normally be much longer."""
    return _days('row_retention_days', DATA_RETENTION_DAYS)


def retention_days():
    """Backwards-compatible alias for the row retention."""
    return row_retention_days()


def backup_dir():
    """Where the zipped archives go, from parameters.json."""
    try:
        configured = parameters.get_parameters().get('housekeeper', {}).get(
            'backup_dir')
    except Exception:
        configured = None
    if not configured:
        return BACKUP_DIR
    return configured if os.path.isabs(configured) else \
        os.path.join(PROJECT_ROOT, configured)

# Paths for assets (relative to project root or absolute)
# We assume the script runs from project root or paths are correctly resolved
ASSETS_DIR = os.path.join(PROJECT_ROOT, 'assets')
AUDIO_DIR = os.path.join(ASSETS_DIR, 'audio_mp3')
SPECTRO_DIR = os.path.join(ASSETS_DIR, 'specrogram_images')

def update_live_status(status, progress=0):
    """Updates a JSON file with the current operation status."""
    data = {
        "status": status,
        "progress": progress,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    try:
        with open(LIVE_STATUS_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

def get_live_status():
    if os.path.exists(LIVE_STATUS_FILE):
        try:
            with open(LIVE_STATUS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"status": "Idle", "progress": 0}

def is_already_running():
    if not os.path.exists(LOCK_FILE):
        return False
    try:
        # Check if process is actually alive (optional but good)
        with open(LOCK_FILE, 'r') as f:
            pid = int(f.read().strip())
        import psutil
        return psutil.pid_exists(pid)
    except:
        return False

def set_running():
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))

def clear_running():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

def get_db_path():
    try:
        db_name = parameters.get_parameters()['sqlite']['db_name']
        if not os.path.isabs(db_name):
            return os.path.join(PROJECT_ROOT, db_name)
        return db_name 
    except:
        return os.path.join(PROJECT_ROOT, 'ast_audio_db/ast_audio.db')

def get_last_backup_info():
    """Reads the last backup info from a JSON file."""
    if os.path.exists(BACKUP_STATUS_FILE):
        try:
            with open(BACKUP_STATUS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading backup status: {e}", extra=LOG_D)
    return {"last_backup": "Never", "files_archived": 0}

def update_last_backup_info(files_count):
    """Updates the last backup timestamp and count."""
    info = {
        "last_backup": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files_archived": files_count
    }
    try:
        with open(BACKUP_STATUS_FILE, 'w') as f:
            json.dump(info, f)
    except Exception as e:
        logger.error(f"Error saving backup status: {e}", extra=LOG_D)

def get_most_recent_recording_time():
    """
    Finds the timestamp of the most recent recording.
    Checks both DB and file system to be safe, prioritizing DB.
    """
    latest_time = None

    # 1. Check Database
    #
    # Restricted to rows whose date actually looks like a timestamp. SQLite
    # compares TEXT lexicographically, and two mock rows carrying the literal
    # string 'MOCK-- MQ:TT:' sorted above every real date ('M' > '2'), so a
    # plain MAX(date) returned junk, strptime failed, and this returned None.
    # Combined with the fallback below also failing, the housekeeper decided
    # there were no recordings at all and skipped every run since 13 August.
    try:
        conn = sqlite3.connect(get_db_path())
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(date) FROM ast_table "
            "WHERE date LIKE '____-__-__ __:__:__%'")
        result = cursor.fetchone()
        conn.close()

        if result and result[0]:
            # Handle potential different date formats
            try:
                latest_time = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
            except ValueError:
                # Try with fractional seconds
                 try:
                    latest_time = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S.%f')
                 except:
                    pass
    except Exception as e:
        logger.error(f"Error checking DB for latest time: {e}", extra=LOG_D)

    # 2. Check File System (Fallback or Validation)
    if not latest_time:
        try:
            # Any recording, whatever the container. The folder is named
            # audio_mp3 but holds .wav, so an .mp3-only test found nothing
            # and this fallback never rescued the case above.
            files = [os.path.join(AUDIO_DIR, f) for f in os.listdir(AUDIO_DIR)
                     if f.lower().endswith(('.mp3', '.wav', '.flac', '.m4a'))]
            if files:
                # Use os.path.getmtime as requested, but on the *files*
                latest_file = max(files, key=os.path.getmtime)
                latest_ts = os.path.getmtime(latest_file)
                latest_time_fs = datetime.fromtimestamp(latest_ts)
                latest_time = latest_time_fs
        except Exception as e:
             logger.error(f"Error checking FS for latest time: {e}", extra=LOG_D)

    return latest_time

def run_cleanup():
    """
    Core logic:
    1. Determine reference date (latest recording).
    2. Identify items older than 7 days from reference.
    3. Zip them.
    4. Delete originals and DB entries.
    """
    if is_already_running():
        logger.info("Housekeeper already running in another process/thread. Skipping.", extra=LOG_D)
        update_live_status("Waiting (Already Running)")
        return
    
    set_running()
    try:
        logger.info("Starting Housekeeper cleanup run...", extra=LOG_D)
        update_live_status("Scanning DB and Files...", 10)
        
        reference_time = get_most_recent_recording_time()
        if not reference_time:
            logger.info("No recordings found. Skipping cleanup.", extra=LOG_D)
            update_live_status("Idle (No data found)")
            return

        # Two independent cutoffs. Audio is the disk cost and can be archived
        # aggressively; the rows are the analytical record and should outlive it.
        audio_days = audio_retention_days()
        row_days = row_retention_days()
        audio_cutoff = reference_time - timedelta(days=audio_days)
        cutoff_time = reference_time - timedelta(days=row_days)
        logger.info(f"Reference Time (Latest Rec): {reference_time}", extra=LOG_D)
        logger.info(f"Row cutoff ({row_days}d): {cutoff_time} | "
                    f"audio cutoff ({audio_days}d): {audio_cutoff}", extra=LOG_D)

        # Candidates for deletion
        files_to_archive = [] # List of tuples (file_path, 'type')
        db_ids_to_delete = []

        # --- 1. Identify DB Records to Archive ---
        try:
            conn = sqlite3.connect(get_db_path())
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Select records older than cutoff
            # Note: String comparison works for ISO8601 dates in SQLite
            cutoff_str = cutoff_time.strftime('%Y-%m-%d %H:%M:%S')
            query = f"SELECT id, file_name, spectrogram_image, date FROM ast_table WHERE date < '{cutoff_str}'"
            cursor.execute(query)
            rows = cursor.fetchall()
            
            logger.info(f"Found {len(rows)} old records in DB.", extra=LOG_D)

            # Store full rows for CSV export
            db_rows_to_archive = []

            for row in rows:
                db_ids_to_delete.append(row['id'])
                db_rows_to_archive.append(dict(row)) # Store as dict to preserve column names

                # A row old enough to delete implies its audio is older still,
                # so its recording is collected here too. Audio that is old but
                # whose row is still being kept is picked up by the sweep below.
                if row['file_name']:
                    mp3_path = os.path.join(AUDIO_DIR, os.path.basename(row['file_name']))
                    if os.path.exists(mp3_path):
                        files_to_archive.append(mp3_path)
                
                # Check Spectrogram
                if row['spectrogram_image']:
                    # Spectrogram paths might be stored relative or absolute or just filename
                    # Usually they are in assets/specrogram_images
                    spec_name = os.path.basename(row['spectrogram_image'])
                    spec_path = os.path.join(SPECTRO_DIR, spec_name)
                    if os.path.exists(spec_path):
                        files_to_archive.append(spec_path)
            
            conn.close()

            # --- Export DB data to CSV ---
            if db_rows_to_archive:
                csv_filename = f"dashboard_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                csv_path = os.path.join(PROJECT_ROOT, csv_filename) # Temporarily in project root
                try:
                    df = pd.DataFrame(db_rows_to_archive)
                    df.to_csv(csv_path, index=False)
                    files_to_archive.append(csv_path)
                    logger.info(f"Dashboard data exported to {csv_path}", extra=LOG_D)
                except Exception as e:
                    logger.error(f"Failed to export dashboard data to CSV: {e}", extra=LOG_D)

        except Exception as e:
            logger.error(f"Error querying DB for cleanup: {e}", extra=LOG_D)
            update_live_status(f"Error: {str(e)}")
            return

        # --- 2. Identify Orphaned Files (Optional but good) ---
        update_live_status("Scanning for orphaned files...", 30)
        # Check MP3 folder
        if os.path.exists(AUDIO_DIR):
            for f in os.listdir(AUDIO_DIR):
                f_path = os.path.join(AUDIO_DIR, f)
                if os.path.isfile(f_path) and f_path not in files_to_archive:
                    # Any recording, not just .mp3 -- the folder is named
                    # audio_mp3 but holds .wav, so an .mp3-only test archived
                    # nothing. Judged against the AUDIO cutoff, so recordings
                    # can be reclaimed long before their rows are.
                    if f.lower().endswith(('.mp3', '.wav', '.flac', '.m4a')):
                        try:
                            mtime = datetime.fromtimestamp(os.path.getmtime(f_path))
                            if mtime < audio_cutoff:
                                files_to_archive.append(f_path)
                        except: pass
        
        # Check Spectrogram folder
        if os.path.exists(SPECTRO_DIR):
            for f in os.listdir(SPECTRO_DIR):
                f_path = os.path.join(SPECTRO_DIR, f)
                if os.path.isfile(f_path) and f_path not in files_to_archive:
                    if f.lower().endswith('.png'):
                        try:
                            mtime = datetime.fromtimestamp(os.path.getmtime(f_path))
                            if mtime < cutoff_time:
                                files_to_archive.append(f_path)
                        except: pass

        # Remove duplicates
        files_to_archive = list(set(files_to_archive))

        if not files_to_archive:
            logger.info("No files to archive.", extra=LOG_D)
            update_last_backup_info(0)
            update_live_status("Idle (Cleanup Complete)")
            return

        # --- 3. Zip Files ---
        archive_dir = backup_dir()
        if not os.path.exists(archive_dir):
            os.makedirs(archive_dir)
        
        zip_filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        zip_path = os.path.join(archive_dir, zip_filename)
        
        logger.info(f"Archiving {len(files_to_archive)} files to {zip_path}...", extra=LOG_D)
        update_live_status(f"Archiving {len(files_to_archive)} files...", 50)
        
        archive_success = False
        try:
            total_files = len(files_to_archive)
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for idx, file_path in enumerate(files_to_archive):
                    arcname = os.path.basename(file_path)
                    zipf.write(file_path, arcname=arcname)
                    if idx % 500 == 0:
                         update_live_status(f"Archiving ({idx}/{total_files})...", 50 + int((idx/total_files)*30))

            archive_success = True
            logger.info("Zip archive created successfully.", extra=LOG_D)
        except Exception as e:
            logger.error(f"Failed to create zip archive: {e}", extra=LOG_D)
            update_live_status(f"Zip Error: {str(e)}")
            # Cleanup incomplete zip if exists
            if os.path.exists(zip_path):
                os.remove(zip_path)
        finally:
            # Clean up the temporary CSV file if it was created
            for f in files_to_archive:
                if f.endswith('.csv') and os.path.exists(f):
                    try:
                        os.remove(f)
                        logger.info(f"Cleaned up temporary CSV file: {f}", extra=LOG_D)
                    except Exception as e:
                        logger.error(f"Error cleaning up temporary CSV file {f}: {e}", extra=LOG_D)

        # --- 4. Delete Originals & Clean DB ---
        if archive_success:
            update_live_status("Deleting original files...", 85)
            # Delete Files
            deleted_count = 0
            for file_path in files_to_archive:
                if not file_path.endswith('.csv'): # Exclude CSV from this deletion pass, as it's already handled
                    try:
                        os.remove(file_path)
                        deleted_count += 1
                    except Exception as e:
                        logger.error(f"Failed to delete {file_path}: {e}", extra=LOG_D)
            
            logger.info(f"Deleted {deleted_count} files.", extra=LOG_D)

            # Clean DB
            update_live_status("Cleaning up database...", 95)
            if db_ids_to_delete:
                try:
                    conn = sqlite3.connect(get_db_path())
                    cursor = conn.cursor()
                    if len(db_ids_to_delete) == 1:
                        ids_tuple = f"({db_ids_to_delete[0]})"
                    else:
                        ids_tuple = str(tuple(db_ids_to_delete))
                    
                    cursor.execute(f"DELETE FROM ast_table WHERE id IN {ids_tuple}")
                    conn.commit()
                    conn.close()
                    logger.info(f"Deleted {len(db_ids_to_delete)} records from DB.", extra=LOG_D)
                except Exception as e:
                    logger.error(f"Failed to clean DB: {e}", extra=LOG_D)

            update_last_backup_info(len(files_to_archive))
            update_live_status("Idle (Cleanup Complete)", 100)
    finally:
        clear_running()

def housekeeper_loop():
    logger.info("Housekeeper thread started.", extra=LOG_D)
    while True:
        try:
            run_cleanup()
        except Exception as e:
            logger.error(f"Housekeeper crash: {e}", extra=LOG_D)

        # Sleep for interval
        time.sleep(CHECK_INTERVAL_SECONDS)


def dedupe_loop():
    """Remove duplicate captures on their own, much faster, cadence.

    Deliberately not folded into run_cleanup(): that runs hourly, so a voice
    heard by two sentinels would sit in the dashboard as two events for up to
    an hour, and the LLM report would read it as two utterances if it ran in
    between. This pass is cheap -- it only examines the last few minutes -- so
    it can run every minute instead.

    It cannot be event-driven either. When the first sentinel's event is
    written the second sentinel has not finished recording yet, so there is
    nothing to compare against; the settle delay below is what waits for the
    other copies to arrive before judging.
    """
    from ast_src import dedupe_events

    logger.info("Dedupe thread started.", extra=LOG_D)
    while True:
        cfg = parameters.get_parameters().get('dedupe', {}) or {}
        interval = float(cfg.get('interval_seconds', 60))
        if not cfg.get('enabled', True):
            time.sleep(interval)
            continue

        lookback = float(cfg.get('lookback_seconds', 300))
        settle = float(cfg.get('settle_seconds', 20))
        try:
            now = datetime.now()
            # Leave the newest events alone: the pipeline may still be writing
            # them, and a sibling capture may still be in flight.
            until = now - timedelta(seconds=settle)
            since = now - timedelta(seconds=lookback)
            decisions = dedupe_events.run(
                since=since.strftime('%Y-%m-%d %H:%M:%S'),
                until=until.strftime('%Y-%m-%d %H:%M:%S'))
            if decisions:
                logger.info(f"Dedupe removed {len(decisions)} duplicate capture(s)",
                            extra=LOG_D)
        except Exception as e:
            logger.error(f"Dedupe crash: {e}", extra=LOG_D)
        time.sleep(interval)


def start_housekeeper():
    t = threading.Thread(target=housekeeper_loop, daemon=True)
    t.start()
    d = threading.Thread(target=dedupe_loop, daemon=True)
    d.start()
