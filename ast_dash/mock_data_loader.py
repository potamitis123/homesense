
import os
from datetime import datetime
import random

def get_local_audio_files(audio_dir=None):
    """
    Scans the specified directory for audio files and returns a list of their properties,
    mimicking the data structure from the database.
    """
    if audio_dir is None:
        audio_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'audio_examples'))
    
    audio_files = [] # Initialize the list here
    if not os.path.exists(audio_dir):
        return []

    # Define a list of mock audio classes for simulation
    mock_audio_classes = ["Dog bark", "Speech", "Music", "Vehicle", "Chainsaw", "Siren", "Alarm"]

    for idx, filename in enumerate(os.listdir(audio_dir)):
        if filename.endswith('.mp3') or filename.endswith('.wav'):
            file_path = os.path.join(audio_dir, filename)
            try:
                stat = os.stat(file_path)
                creation_time = datetime.fromtimestamp(stat.st_ctime)
                
                # Parsing the filename to get device and timestamp
                clean_name = filename.replace('_rec.mp3', '').replace('_rec.wav', '')
                parts = clean_name.split('_')
                if len(parts) >= 3:
                    date_str = parts[0]
                    time_str = parts[1]
                    device_name = parts[2]
                    timestamp_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}"
                else:
                    device_name = "unknown"
                    timestamp_str = creation_time.strftime('%Y-%m-%d %H:%M:%S')

                # Generate mock classification and RMS data
                sound1 = random.choice(mock_audio_classes)
                conf1 = round(random.uniform(0.6, 0.99), 2)
                
                # Ensure sound2 and sound3 are different from sound1 and each other
                remaining_classes = [c for c in mock_audio_classes if c != sound1]
                sound2 = random.choice(remaining_classes) if remaining_classes else "None"
                conf2 = round(random.uniform(0.3, 0.59), 2) if sound2 != "None" else 0.0

                remaining_classes = [c for c in remaining_classes if c != sound2]
                sound3 = random.choice(remaining_classes) if remaining_classes else "None"
                conf3 = round(random.uniform(0.1, 0.29), 2) if sound3 != "None" else 0.0
                
                rms_value = round(random.uniform(0.1, 0.8), 2) # Mock RMS value
                
                # Spectrogram and audio source for Dash
                # We assume these files have been processed by main.py and exist in assets
                spectrogram_name = filename.rsplit('.', 1)[0] + '.png'
                
                audio_files.append({
                    'event_id': idx + 1, # Simple sequential ID
                    'filename': filename,
                    'device_name': device_name,
                    'timestamp': timestamp_str,
                    'filepath': f"/assets/audio_mp3/{filename}", # URL for Dash
                    'creation_date': creation_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'sound': sound1, # Matching 'Classification' column in date_sounds.py
                    'sound1': sound1,
                    'conf_sound1': conf1,
                    'sound2': sound2,
                    'conf_sound2': conf2,
                    'sound3': sound3,
                    'conf_sound3': conf3,
                    'rms': rms_value,
                    'spectrogram_image': f"/assets/specrogram_images/{spectrogram_name}"
                })
            except Exception as e:
                print(f"Error processing file {filename}: {e}")

    # Sort by timestamp, newest first
    audio_files.sort(key=lambda x: x['timestamp'], reverse=True)
    return audio_files

if __name__ == '__main__':
    # For testing the function
    files = get_local_audio_files()
    for f in files:
        print(f)
