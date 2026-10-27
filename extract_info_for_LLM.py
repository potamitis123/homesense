import pandas as pd
import re
import string

def analyze_audio_events(file_path):
    # 1. LOAD DATA
    try:
        df = pd.read_csv(file_path, encoding='utf-8')
    except UnicodeDecodeError:
        df = pd.read_csv(file_path, encoding='utf-8-sig')
    
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df['Date'] = df['Timestamp'].dt.date

    # 2. CATEGORY MAPPING
    mapping = {
        'Toilet Use': ['water', 'fill', 'waterfall', 'sink', 'water tap', 'faucet', 'drip', 'liquid', 'pump (liquid)'],
        'Cough': ['cough', 'throat clearing', 'sneeze'],
        'TV Use': ['television', 'radio', 'choir']
    }

    def get_mapped_categories(row):
        cats = set()
        for col in ['Class 1', 'Class 2', 'Class 3']:
            val = str(row[col]).lower() if pd.notna(row[col]) else ""
            for cat, keywords in mapping.items():
                if any(kw in val for kw in keywords):
                    cats.add(cat)
        return list(cats)

    df['Mapped_Cats'] = df.apply(get_mapped_categories, axis=1)

    # 3. TIME PARTITIONING
    def get_partition(dt):
        h = dt.hour
        if 5 <= h <= 11: return 'Morning'
        if 12 <= h <= 16: return 'Afternoon'
        if 17 <= h <= 20: return 'Evening'
        return 'Night' # 21:00 - 04:59

    df['Partition'] = df['Timestamp'].apply(get_partition)

    # --- A) TV USE: Session-based duration per Partition per Day ---
    tv_df = df[df['Mapped_Cats'].apply(lambda x: 'TV Use' in x)].sort_values('Timestamp')
    tv_results = {p: "0.00 hrs" for p in ['Morning', 'Afternoon', 'Evening', 'Night']}
    
    if not tv_df.empty:
        SESSION_GAP = pd.Timedelta(minutes=5)  # tune this threshold
        
        # Flag a new session whenever gap between consecutive events exceeds SESSION_GAP
        tv_df = tv_df.copy()
        tv_df['gap'] = tv_df['Timestamp'].diff() > SESSION_GAP
        tv_df['session_id'] = tv_df['gap'].cumsum()
        
        # Each session: duration = last event - first event in that session
        sessions = tv_df.groupby(['Date', 'Partition', 'session_id'])['Timestamp'].agg(['min', 'max'])
        sessions['duration_hrs'] = (sessions['max'] - sessions['min']).dt.total_seconds() / 3600
        
        # Sum durations per partition
        total_per_partition = sessions.groupby('Partition')['duration_hrs'].sum()
        
        for p in tv_results:
            tv_results[p] = f"{total_per_partition.get(p, 0.0):.2f} hrs"

    # --- B) COUGH: Event counts per partition ---
    cough_df = df[df['Mapped_Cats'].apply(lambda x: 'Cough' in x)]
    cough_counts = cough_df['Partition'].value_counts().to_dict()
    cough_results = {p: cough_counts.get(p, 0) for p in ['Morning', 'Afternoon', 'Evening', 'Night']}

    # --- C) TOILET: Consolidated count (5-min window) ---
    toilet_df = df[df['Mapped_Cats'].apply(lambda x: 'Toilet Use' in x)].sort_values('Timestamp')
    toilet_total = 0
    if not toilet_df.empty:
        toilet_total = (toilet_df['Timestamp'].diff() > pd.Timedelta(minutes=5)).sum() + 1

    # --- D) SPEECH: Greek Cleaning ---
    speech_mask = (
        df['transcription'].notna() & 
        ~df['transcription'].str.contains('No Speech Detected|NO SPEECH DETECTED', case=False, na=False)
    )
    raw_text = " ".join(df[speech_mask]['transcription'].astype(str).tolist())
    
    # Clean character repetitions (αααα)
    words = raw_text.split()
    words = [w for w in words if not re.search(r'(.)\1{3,}', w, re.UNICODE)]
    
    # Clean word repetitions (κύριε, κύριε...)
    final_words = []
    i = 0
    while i < len(words):
        clean_current = words[i].translate(str.maketrans('', '', string.punctuation)).lower()
        count = 1
        while (i + count < len(words) and 
               words[i + count].translate(str.maketrans('', '', string.punctuation)).lower() == clean_current):
            count += 1
        
        if count > 2:
            final_words.extend(words[i : i + 2])
            i += count
        else:
            final_words.append(words[i])
            i += 1

    return {
        "tv_usage": tv_results,
        "cough_counts": cough_results,
        "toilet_total": toilet_total,
        "speech_cleaned": " ".join(final_words)
    }

# --- EXAMPLE USAGE ---
path = 'audio_events.csv'
results = analyze_audio_events(path)

print("TV Durations:", results['tv_usage'])
print("Cough Counts:", results['cough_counts'])
print("Toilet Events:", results['toilet_total'])
print("Speech Summary:", results['speech_cleaned'])