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
    
    # Identify the number of unique days to calculate daily averages
    num_days = df['Date'].nunique() if not df.empty else 1

    # 2. CATEGORY MAPPING
    mapping = {
        'Toilet Use': ['water', 'fill', 'waterfall', 'sink', 'water tap', 'faucet', 'drip', 'liquid', 'pump (liquid)'],
        'Cough': ['cough', 'throat clearing', 'sneeze'],
        'TV Use': ['television', 'radio', 'choir', 'music', 'Pop Music', 'synthesizer'],
        'Mobility aids': ['bouncing','tick-tock','tick','wood','tap','footsteps','crack'],
        'Speech': ['speech','monologue','narration','conversation'],
        'Snoring': ['snoring', 'snort', 'breathing']
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

    # --- A) TV USE: Bridging Sparse Detections ---
    tv_df = df[df['Mapped_Cats'].apply(lambda x: 'TV Use' in x)].sort_values('Timestamp')
    tv_results = {p: {"duration": "0.00 hrs", "sessions": 0.0} for p in ['Morning', 'Afternoon', 'Evening', 'Night']}
    
    if not tv_df.empty:
        SESSION_GAP = pd.Timedelta(minutes=30) 
        MIN_SESSION_DURATION = pd.Timedelta(minutes=5)
        tv_df = tv_df.copy()
        tv_df['gap'] = tv_df['Timestamp'].diff() > SESSION_GAP
        tv_df['session_id'] = tv_df['gap'].cumsum()
        sessions = tv_df.groupby(['Date', 'Partition', 'session_id'])['Timestamp'].agg(['min', 'max'])
        sessions['diff'] = (sessions['max'] - sessions['min'])
        sessions['duration_hrs'] = sessions['diff'].apply(lambda x: max(x, MIN_SESSION_DURATION).total_seconds() / 3600)
        stats = sessions.groupby('Partition').agg(total_hrs=('duration_hrs', 'sum'), session_count=('duration_hrs', 'count'))
        for p in tv_results:
            if p in stats.index:
                tv_results[p] = {"duration": f"{(stats.loc[p, 'total_hrs'] / num_days):.2f} hrs", "sessions": round(stats.loc[p, 'session_count'] / num_days, 1)}
            
    # --- B) COUGH: Average counts per partition ---
    cough_df = df[df['Mapped_Cats'].apply(lambda x: 'Cough' in x)]
    cough_counts = cough_df['Partition'].value_counts()
    cough_results = {p: round(cough_counts.get(p, 0) / num_days, 1) for p in ['Morning', 'Afternoon', 'Evening', 'Night']}

    # --- C) TOILET: Average daily count (5-min window) ---
    toilet_df = df[df['Mapped_Cats'].apply(lambda x: 'Toilet Use' in x)].sort_values('Timestamp')
    toilet_avg = 0
    if not toilet_df.empty:
        diffs = toilet_df.groupby('Date')['Timestamp'].diff()
        count = (diffs > pd.Timedelta(minutes=5)).sum() + num_days
        toilet_avg = round(count / num_days, 1)

    # --- D) MOBILITY AIDS ---
    mobility_df = df[df['Mapped_Cats'].apply(lambda x: 'Mobility aids' in x)]
    mob_counts = mobility_df['Partition'].value_counts()
    mobility_results = {p: round(mob_counts.get(p, 0) / num_days, 1) for p in ['Morning', 'Afternoon', 'Evening', 'Night']}

    # --- E) SPEECH COUNTS ---
    speech_df = df[df['Mapped_Cats'].apply(lambda x: 'Speech' in x)]
    sp_counts = speech_df['Partition'].value_counts()
    speech_results = {p: round(sp_counts.get(p, 0) / num_days, 1) for p in ['Morning', 'Afternoon', 'Evening', 'Night']}
    
    # --- F) SILENCE ---
    partition_capacities = {'Morning': 7.0, 'Afternoon': 5.0, 'Evening': 4.0, 'Night': 8.0}
    active_df = df.sort_values('Timestamp').copy()
    active_results = {p: 0.0 for p in ['Morning', 'Afternoon', 'Evening', 'Night']}
    if not active_df.empty:
        SESSION_GAP, CHUNK_DURATION = pd.Timedelta(minutes=5), pd.Timedelta(seconds=15)
        active_df['gap'] = active_df['Timestamp'].diff() > SESSION_GAP
        active_df['session_id'] = active_df['gap'].cumsum()
        active_sessions = active_df.groupby(['Date', 'Partition', 'session_id'])['Timestamp'].agg(['min', 'max'])
        active_sessions['duration_hrs'] = ((active_sessions['max'] - active_sessions['min']) + CHUNK_DURATION).dt.total_seconds() / 3600
        total_active_per_partition = active_sessions.groupby('Partition')['duration_hrs'].sum()
        for p in active_results: active_results[p] = total_active_per_partition.get(p, 0.0) / num_days
    silence_results = {p: f"{max(0, partition_capacities[p] - active_results[p]):.2f} hrs" for p in active_results}
        
    # --- G) SPEECH TEXT: Cleaned by Partition ---
    def clean_speech_text(text):
        if not text: return ""
        words = text.split()
        words = [w for w in words if not re.search(r'(.)\1{3,}', w, re.UNICODE)]
        final_words = []
        i = 0
        while i < len(words):
            clean_current = words[i].translate(str.maketrans('', '', string.punctuation)).lower()
            count = 1
            while (i + count < len(words) and words[i+count].translate(str.maketrans('', '', string.punctuation)).lower() == clean_current):
                count += 1
            if count > 2:
                final_words.extend(words[i : i+2]); i += count
            else:
                final_words.append(words[i]); i += 1
        return " ".join(final_words)

    speech_cleaned_by_partition = {}
    for p in ['Morning', 'Afternoon', 'Evening', 'Night']:
        p_mask = (df['Partition'] == p) & (df['transcription'].notna()) & (~df['transcription'].str.contains('No Speech Detected', case=False, na=False))
        raw_partition_text = " ".join(df[p_mask]['transcription'].astype(str).tolist())
        speech_cleaned_by_partition[p] = clean_speech_text(raw_partition_text)

    # --- H) SNORING ---
    snoring_df = df[df['Mapped_Cats'].apply(lambda x: 'Snoring' in x)].sort_values('Timestamp')
    snoring_avg = "0.00 hrs"
    if not snoring_df.empty:
        SN_GAP, CHUNK = pd.Timedelta(minutes=2), pd.Timedelta(seconds=15)
        snoring_df = snoring_df.copy()
        snoring_df['gap'] = snoring_df['Timestamp'].diff() > SN_GAP
        snoring_df['session_id'] = snoring_df['gap'].cumsum()
        sn_sessions = snoring_df.groupby(['Date', 'Partition', 'session_id'])['Timestamp'].agg(['min', 'max'])
        sn_sessions['duration_hrs'] = ((sn_sessions['max'] - sn_sessions['min']) + CHUNK).dt.total_seconds() / 3600
        try:
            night_snoring = sn_sessions.xs('Night', level='Partition')
            if not night_snoring.empty: snoring_avg = f"{(night_snoring['duration_hrs'].sum() / num_days):.2f} hrs"
        except KeyError: pass

    return {
        "days_analyzed": num_days,
        "tv_usage_avg": tv_results,
        "cough_counts_avg": cough_results,
        "toilet_total_avg": toilet_avg,
        "mobility_counts_avg": mobility_results,
        "speech_counts_avg": speech_results,
        "silence_durations_avg": silence_results,
        "speech_cleaned": speech_cleaned_by_partition, # Now a dictionary
        "snoring_duration_night_avg": snoring_avg,
    }

if __name__ == "__main__":
    path = 'audio_events.csv' 
    results = analyze_audio_events(path)
    
    print("="*30)
    print(f"ANALYSIS SUMMARY ({results['days_analyzed']} Days Detected)")
    print("="*30)
    print(f"\n[1] Silence Durations (Daily Avg):")
    for part, val in results['silence_durations_avg'].items():
        print(f"    - {part:9}: {val}")
    print(f"\n[2] TV Usage (Daily Avg):")
    for part, data in results['tv_usage_avg'].items():
        print(f"    - {part:9}: {data['duration']} across {data['sessions']} sessions")
    print(f"\n[3] Event Counts (Daily Avg):")
    print(f"    - Coughs:   {results['cough_counts_avg']}")
    print(f"    - Mobility: {results['mobility_counts_avg']}")
    print(f"    - Speech:   {results['speech_counts_avg']}")
    print(f"    - Snoring (Night Daily Avg): {results['snoring_duration_night_avg']}")
    print(f"    - Toilet:   {results['toilet_total_avg']} times/day")
    
    print(f"\n[4] Partitioned Discussions (Cleaned Speech):")
    for part, text in results['speech_cleaned'].items():
        print(f"    --- {part} ---")
        if text:
            # Print first 300 characters for brevity
            print(f"    {text[:300]}..." if len(text) > 300 else f"    {text}")
        else:
            print("    (No speech detected in this partition)")