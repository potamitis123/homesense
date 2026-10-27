import dash
from dash import html, dcc, callback, ctx, get_app, Output, Input, State
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc
import os
import json
import sqlite3
import sys
import traceback
import pandas as pd
import re
import string
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from openai import OpenAI

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
from misc.load_parameters import parameters
from misc.secrets import openai_api_key
from ast_dash import incident_panel
from graphrag import blind, judgements, prepare

dash.register_page(
    __name__,
    path='/llm_report',
    title='LLM Report',
    name='LLM'
)

# Helper to load prompts from assets
def load_prompt_from_file(file_name, default_text=""):
    prompt_path = os.path.join(project_root, 'assets', file_name)
    try:
        if os.path.exists(prompt_path):
            with open(prompt_path, 'r', encoding='utf-8') as f:
                return f.read()
        else:
            with open(prompt_path, 'w', encoding='utf-8') as f:
                f.write(default_text)
            return default_text
    except Exception as e:
        print(f"Error loading prompt file {file_name}: {e}")
        return default_text

# How many days of history the report covers BY DEFAULT. Was one day; the
# dynamics pipeline needs several days before pair and routine patterns mean
# anything. The page can now be pointed at any window, but a week is what it
# opens on.
REPORT_WINDOW_DAYS = 7


def parse_clock(value, fallback):
    """A time of day from a text box, as forgiving as the label promises.

    The field is labelled HH:MM, but people type what they say: "10", "10.00",
    "10:00:00", " 9:5 ". Those all mean the same thing here and rejecting them
    would be pedantry in a box whose only job is to narrow a report -- the
    request that prompted this was itself written as "10.00 up to 15.00".

    An empty box means the fallback, so leaving both alone reports on whole
    days. None is returned only when the text genuinely cannot be read, so the
    caller can say so rather than quietly reporting on the wrong hours.
    """
    text = (value or "").strip()
    if not text:
        return fallback
    parts = [p for p in text.replace('.', ':').replace(',', ':').split(':') if p]
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    if not 1 <= len(numbers) <= 3:
        return None
    numbers += [0] * (3 - len(numbers))
    hour, minute, second = numbers
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return time(hour, minute, second)


def resolve_window(start_date, end_date, start_clock, end_clock):
    """The four controls as one (start, end), or (None, None, why not).

    Dates arrive from the calendar as ISO strings, times as whatever was
    typed. Every failure is reported rather than substituted: a report over
    silently different hours than the ones asked for is worse than no report,
    because nothing downstream would reveal the substitution.
    """
    today = datetime.now().date()
    try:
        first = (date.fromisoformat(start_date[:10]) if start_date
                 else today - timedelta(days=REPORT_WINDOW_DAYS))
        last = date.fromisoformat(end_date[:10]) if end_date else today
    except (TypeError, ValueError):
        return None, None, "Could not read the dates. Pick them from the calendar."

    opened = parse_clock(start_clock, time(0, 0))
    closed = parse_clock(end_clock, time(23, 59, 59))
    if opened is None or closed is None:
        return None, None, (
            'Times are read as HH:MM — "10:00", "10.00" and "10" all mean ten '
            "o'clock. Leave a box empty for the whole day.")

    start = datetime.combine(first, opened)
    end = datetime.combine(last, closed)
    if end <= start:
        return None, None, (
            f"That window ends before it begins: {start:%d-%m-%Y %H:%M} to "
            f"{end:%d-%m-%Y %H:%M}.")
    return start, end, None

# Raw table column -> the dashboard names derive_home_dynamics.py expects.
# The pipeline is written against the dashboard's export format, so the SQL
# rows are relabelled here rather than teaching the pipeline a second schema.
_DB_TO_DASHBOARD = {
    "id": "ID",
    "date": "Timestamp",
    "device_name": "Device",
    "sound1": "Class 1", "conf_sound1": "Prob 1",
    "sound2": "Class 2", "conf_sound2": "Prob 2",
    "sound3": "Class 3", "conf_sound3": "Prob 3",
    "rms": "rms",
    "transcription": "transcription",
    "emotion": "emotion",
    "room": "room",
    "file_name": "file_name",
}


def get_recent_events(days: int = REPORT_WINDOW_DAYS, start=None, end=None):
    """Events from a window, in dashboard column format.

    `start` and `end` name the window outright, to the minute. Without them it
    is the last `days` days, which is what the page opens on and what every
    caller wanted before the window became configurable.

    Returns (df, start_dt, end_dt, events) where `events` is the list of dicts
    the dynamics pipeline consumes. `room` is selected explicitly: it drives
    all of the pipeline's location reasoning and was absent from the previous
    one-day query.
    """
    end = end or datetime.now()
    start = start or (end - timedelta(days=days))
    db_path = os.path.join(project_root, parameters.get_parameters()['sqlite']['db_name'])

    try:
        conn = sqlite3.connect(db_path)
        # Older databases predate the room column, so ask the schema first
        # rather than failing the whole report on a missing field.
        available = {row[1] for row in conn.execute("PRAGMA table_info(ast_table)")}
        columns = [c for c in _DB_TO_DASHBOARD if c in available]
        query = (
            f"SELECT {', '.join(columns)} FROM ast_table "
            f"WHERE date >= ? AND date <= ? ORDER BY date ASC"
        )
        df = pd.read_sql_query(
            query, conn,
            params=(start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')),
        )
        conn.close()

        if df.empty:
            return None, start, end, []

        df = df.rename(columns={c: _DB_TO_DASHBOARD[c] for c in columns})
        events = json.loads(df.to_json(orient='records'))
        return df, start, end, events
    except Exception as e:
        print(f"Error extracting events: {e}")
        return None, start, end, []


def run_dynamics_pipeline(events, start, end, semantic_model=None):
    """Run derive_home_dynamics over the extracted events and save the result.

    The saved filename carries the first and last event dates so a report can
    always be traced back to the window it describes.
    `semantic_model` is (kind, model_name) for the L1.5 interpreter, or None
    to run without semantic interpretation.
    Returns (payload, path) or (None, None) if the pipeline is unavailable.
    """
    pipeline_dir = os.path.join(project_root, 'ast_dash', 'llm_report')
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    try:
        import derive_home_dynamics as dhd
    except Exception as exc:
        print(f"Dynamics pipeline unavailable: {exc}")
        return None, None

    try:
        cfg = dhd.load_config(os.path.join(pipeline_dir, 'home_dynamics_config.json'))
        level0 = dhd.derive_level0(events, cfg)
        level1 = dhd.derive_level1(level0, cfg)
        # L1.5 reads what the speech MEANT between people. It is the one stage
        # that calls a model, so a failure here must cost only the semantics:
        # derive_level1_5 records a per-exchange failure and the rest of the
        # pipeline continues on the evidence it already has.
        interpreter = None
        if semantic_model:
            try:
                interpreter = make_semantic_interpreter(*semantic_model)
            except Exception as exc:
                print(f"Semantic interpreter unavailable: {exc}")
        level1_5 = dhd.derive_level1_5(level0, cfg, interpreter=interpreter,
                                       cache={})
        level2 = dhd.derive_level2(level1, cfg)
        level3 = dhd.derive_level3(level0, level2, cfg, level1_5=level1_5)
        gaps = dhd.derive_observation_gaps(level0, cfg)

        # Previous reports act as the longitudinal baseline, so a run can say
        # "below the usual range" instead of only describing today.
        historical = load_historical_level4(pipeline_dir)
        level4 = dhd.derive_level4(level0, level2, level3, gaps, cfg, historical)

        payload = dhd.build_output(level0, level1, level2, level3, level4,
                                   gaps, len(events), level1_5=level1_5)

        stamps = sorted(o.timestamp for o in level0 if o.timestamp)
        first = stamps[0][:10] if stamps else start.strftime('%Y-%m-%d')
        last = stamps[-1][:10] if stamps else end.strftime('%Y-%m-%d')
        # Day-month-year, as requested; '/' is not usable in a filename.
        span = (f"{datetime.strptime(first, '%Y-%m-%d').strftime('%d-%m-%Y')}"
                f"_to_{datetime.strptime(last, '%Y-%m-%d').strftime('%d-%m-%Y')}")

        reports_dir = os.path.join(pipeline_dir, 'reports')
        os.makedirs(reports_dir, exist_ok=True)
        path = os.path.join(reports_dir, f"home_dynamics_{span}.json")
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return payload, path
    except Exception as exc:
        print(f"Dynamics pipeline failed: {exc}")
        traceback.print_exc()
        return None, None


def load_historical_level4(pipeline_dir):
    """Level 4 summaries from previous saved analyses, for baseline comparison."""
    reports_dir = os.path.join(pipeline_dir, 'reports')
    if not os.path.isdir(reports_dir):
        return []
    history, seen = [], set()
    for name in sorted(os.listdir(reports_dir)):
        if not name.startswith('home_dynamics_') or not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(reports_dir, name), 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        except Exception:
            continue
        for day in data.get('level_4_daily_dynamics', []):
            # Windows overlap by design; keep one summary per date.
            if day.get('date') and day['date'] not in seen:
                seen.add(day['date'])
                history.append(day)
    return history

# Episode types worth sending individually, most relational first: the order
# is the priority used when the per-day budget cannot hold them all.
#
# `speech_episode` and `person_presence_evidence` were always excluded as
# numerous and already aggregated. `single_person_speech_episode` is now
# excluded for the same reason and it is the important one: it was 1,146 of
# the 1,526 episodes in the 21-28 Aug window -- three quarters of the list,
# and 130k tokens -- while saying only "one person spoke", which the per-day
# `social_event_counts` already reports. Its count is still sent; its 1,146
# individual rows are not.
_SOCIAL_TYPES_FOR_LLM = (
    "reciprocal_conversation_candidate",
    "shared_activity_candidate",
    "shared_media_candidate",
    "same_room_copresence_candidate",
)

# Episodes sent per day, and in total. A week of six observed days therefore
# costs at most ~60 episodes (~5k tokens) instead of the whole Level 3 list.
# Level 4 is the compressed view; individual episodes are illustration, so
# past a handful per day they add volume rather than evidence.
_MAX_EPISODES_PER_DAY = 10
_MAX_EPISODES_TOTAL = 80


def compact_for_llm(payload):
    """Shrink the hierarchy to what a model can actually use.

    The full analysis runs to ~90k characters, most of it repeated: Level 4
    embeds a `social_events` list that restates Level 3, and every episode
    carries evidence rows meant for auditing rather than reading. Sending it
    whole costs tokens for no benefit, and would silently overflow a local
    Llama running the usual small context window.

    What is kept: the per-day aggregates, the socially meaningful episodes in
    short form, and -- in full -- `uncertainties` and `data_quality`, because
    those are what stop the model reading missing evidence as evidence of
    absence. The saved analysis file remains the complete record.

    Trimming each episode's fields is not on its own enough. The episode list
    is unbounded, so a busy week overflows any context window no matter how
    short each row is: the 21-28 Aug window produced 1,526 episodes = 173k
    tokens, against a 128k limit. So the list is now also *bounded* -- ranked
    by relational value and capped per day -- and what was dropped is stated
    in the payload, because a model that cannot see the omission would read
    the survivors as the complete record.
    """
    days = []
    for day in payload.get('level_4_daily_dynamics', []):
        trimmed = {k: v for k, v in day.items()
                   if k not in ('social_events', 'no_observation_intervals')}
        # The episodes themselves are sent once, below; keep only the shape.
        trimmed['social_event_counts'] = dict(sorted(
            Counter(e['type'] for e in day.get('social_events', [])).items()))
        gaps = day.get('no_observation_intervals', [])
        trimmed['no_observation_intervals'] = {
            "count": len(gaps),
            "total_hours": round(
                sum(g.get('duration_seconds', 0) for g in gaps) / 3600.0, 1),
            "note": "Unobserved, not silent. The system records only on trigger.",
        }
        days.append(trimmed)

    def summarise(episode):
        entry = {
            "id": episode.get('episode_id'),
            "type": episode.get('type'),
            "start": episode.get('start'),
            "room": episode.get('room'),
            "participants": episode.get('participants'),
            "confidence": episode.get('confidence'),
        }
        details = episode.get('details') or {}
        for key in ('estimated_turns', 'turn_balance', 'speaker_changes'):
            if details.get(key) is not None:
                entry[key] = details[key]
        emotion = (details.get('emotion_summary') or {})
        if emotion.get('dominant'):
            entry['emotion'] = {
                "dominant": emotion['dominant'],
                "distribution": emotion.get('distribution'),
                "evidence_count": emotion.get('evidence_count'),
            }
        return entry

    # Group by day first, so a single busy day cannot consume the whole budget
    # and leave the rest of the week unrepresented.
    by_day, considered = defaultdict(list), Counter()
    for episode in payload.get('level_3_social_episodes', []):
        etype = episode.get('type')
        considered[etype] += 1
        if etype not in _SOCIAL_TYPES_FOR_LLM:
            continue
        day = (episode.get('start') or '')[:10] or 'unknown'
        by_day[day].append(episode)

    priority = {name: i for i, name in enumerate(_SOCIAL_TYPES_FOR_LLM)}

    episodes, kept_per_type, dropped_per_type = [], Counter(), Counter()
    for day in sorted(by_day):
        # Most relational type first, then most confident within a type.
        ranked = sorted(
            by_day[day],
            key=lambda e: (priority.get(e.get('type'), 99),
                           -(e.get('confidence') or 0)),
        )
        room = _MAX_EPISODES_TOTAL - len(episodes)
        keep = ranked[:max(0, min(_MAX_EPISODES_PER_DAY, room))]
        for episode in keep:
            episodes.append(summarise(episode))
            kept_per_type[episode.get('type')] += 1
        for episode in ranked[len(keep):]:
            dropped_per_type[episode.get('type')] += 1

    # State the omission in the payload itself. The per-day counts above are
    # complete; only the individual rows are sampled, and the model must not
    # mistake "not listed" for "did not happen".
    sampling = {
        "episodes_listed": len(episodes),
        "episodes_detected_total": int(sum(considered.values())),
        "listed_by_type": dict(sorted(kept_per_type.items())),
        "omitted_from_listing_by_type": dict(sorted(dropped_per_type.items())),
        "types_never_listed_individually": sorted(
            t for t in considered if t not in _SOCIAL_TYPES_FOR_LLM),
        "selection_rule": (
            f"Per day, at most {_MAX_EPISODES_PER_DAY} episodes "
            f"(at most {_MAX_EPISODES_TOTAL} overall), ranked by episode type "
            "then confidence."),
        "note": (
            "This is a sample for illustration, not a census. Every episode "
            "detected is counted in each day's social_event_counts; the counts "
            "are complete even where the individual rows are not. An episode "
            "absent from this listing was still observed."),
    }

    return {"daily_dynamics": days,
            "episode_sampling": sampling,
            "social_episodes": episodes}


def analyze_audio_events_df(df):
    if df is None or df.empty:
        return None

    df = df.copy()
    # The extractor now hands back dashboard column names ('Timestamp'), while
    # older callers passed the raw table's 'date'. Accept either.
    time_column = 'Timestamp' if 'Timestamp' in df.columns else 'date'
    df['Timestamp'] = pd.to_datetime(df[time_column], errors='coerce')
    df = df.dropna(subset=['Timestamp'])
    if df.empty:
        return None
    df['Date'] = df['Timestamp'].dt.date
    num_days = df['Date'].nunique() if not df.empty else 1

    mapping = {
        'Toilet Use': ['water', 'fill', 'waterfall', 'sink', 'water tap', 'faucet', 'drip', 'liquid', 'pump (liquid)'],
        'Cough': ['cough', 'throat clearing', 'sneeze'],
        'TV Use': ['television', 'radio', 'choir', 'music', 'Pop Music', 'synthesizer'],
        'Mobility aids': ['bouncing','tick-tock','tick','wood','tap','footsteps','crack'],
        'Speech': ['speech','monologue','narration','conversation'],
        'Snoring': ['snoring', 'snort', 'breathing']
    }

    # Same dual-naming problem as the timestamp: 'Class N' from the dashboard
    # extractor, 'soundN' from the raw table.
    label_columns = [c for c in ('Class 1', 'Class 2', 'Class 3',
                                 'sound1', 'sound2', 'sound3') if c in df.columns]

    def get_mapped_categories(row):
        cats = set()
        for col in label_columns:
            val = str(row[col]).lower() if pd.notna(row[col]) else ""
            for cat, keywords in mapping.items():
                if any(kw in val for kw in keywords):
                    cats.add(cat)
        return list(cats)

    df['Mapped_Cats'] = df.apply(get_mapped_categories, axis=1)

    def get_partition(dt):
        h = dt.hour
        if 5 <= h <= 11: return 'Morning'
        if 12 <= h <= 16: return 'Afternoon'
        if 17 <= h <= 20: return 'Evening'
        return 'Night'

    df['Partition'] = df['Timestamp'].apply(get_partition)

    # TV Use
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

    # Cough
    cough_df = df[df['Mapped_Cats'].apply(lambda x: 'Cough' in x)]
    cough_counts = cough_df['Partition'].value_counts()
    cough_results = {p: round(cough_counts.get(p, 0) / num_days, 1) for p in ['Morning', 'Afternoon', 'Evening', 'Night']}

    # Toilet Use
    toilet_df = df[df['Mapped_Cats'].apply(lambda x: 'Toilet Use' in x)].sort_values('Timestamp')
    toilet_avg = 0
    if not toilet_df.empty:
        diffs = toilet_df.groupby('Date')['Timestamp'].diff()
        count = (diffs > pd.Timedelta(minutes=5)).sum() + num_days
        toilet_avg = round(count / num_days, 1)

    # Mobility Aids
    mobility_df = df[df['Mapped_Cats'].apply(lambda x: 'Mobility aids' in x)]
    mob_counts = mobility_df['Partition'].value_counts()
    mobility_results = {p: round(mob_counts.get(p, 0) / num_days, 1) for p in ['Morning', 'Afternoon', 'Evening', 'Night']}

    # Speech Counts
    speech_df = df[df['Mapped_Cats'].apply(lambda x: 'Speech' in x)]
    sp_counts = speech_df['Partition'].value_counts()
    speech_results = {p: round(sp_counts.get(p, 0) / num_days, 1) for p in ['Morning', 'Afternoon', 'Evening', 'Night']}
    
    # Silence
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
        
    # Partitioned Speech Text
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

    # Snoring
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
        "speech_cleaned_by_partition": speech_cleaned_by_partition,
        "snoring_duration_night_avg": snoring_avg,
    }

def format_analysis_summary(results):
    if not results:
        return "No data available for analysis."
    
    lines = []
    lines.append("="*30)
    lines.append(f"ANALYSIS SUMMARY ({results['days_analyzed']} Days Detected)")
    lines.append("="*30)
    lines.append(f"\n[1] Silence Durations (Daily Avg):")
    for part, val in results['silence_durations_avg'].items():
        lines.append(f"    - {part:9}: {val}")
    lines.append(f"\n[2] TV Usage (Daily Avg):")
    for part, data in results['tv_usage_avg'].items():
        lines.append(f"    - {part:9}: {data['duration']} across {data['sessions']} sessions")
    lines.append(f"\n[3] Event Counts (Daily Avg):")
    lines.append(f"    - Coughs:   {results['cough_counts_avg']}")
    lines.append(f"    - Mobility: {results['mobility_counts_avg']}")
    lines.append(f"    - Speech:   {results['speech_counts_avg']}")
    lines.append(f"    - Snoring (Night Daily Avg): {results['snoring_duration_night_avg']}")
    lines.append(f"    - Toilet:   {results['toilet_total_avg']} times/day")
    
    return "\n".join(lines)

OLLAMA_HOST = "http://localhost:11434"

# Ollama allocates its own default context (4096 on this install) and ignores
# the model's real limit, so a model advertising 131k tokens still truncates at
# 4k. This prompt does not fit in 4k -- assets/LLM_prompt.txt alone is ~6.6k
# tokens before any data -- which is why local reports read as if the model had
# not seen the instructions. It had not.
#
# The size must be requested per call, and only the native /api/chat endpoint
# honours it: the OpenAI-compatible /v1 endpoint silently drops `options`, so
# the local path deliberately does not go through the OpenAI SDK.
# The context has to hold the prompt AND the answer. A measured 7-day report
# prompt is ~30k tokens, so 32768 left ~3k for generation -- which qwen3.5 spent
# entirely on reasoning tokens and returned an empty report. Sized for the
# prompt plus a full report with room to spare.
OLLAMA_NUM_CTX = 65536

# Generation is slow at this context on a laptop GPU and runs unattended at
# night, so the ceiling is generous. A truncated report is worse than a late one.
OLLAMA_TIMEOUT_S = 1800

# Shown if Ollama cannot be reached when the page is built.
_FALLBACK_LOCAL_MODELS = ["qwen3.5:4b", "gemma2:9b", "llama3.2:latest"]

# Default local model. qwen3.5:4b carries a 262k-token context against
# gemma2:9b's 8192, and this prompt does not fit in 8192.
DEFAULT_LOCAL_MODEL = "qwen3.5:4b"


def list_local_models():
    """Installed Ollama models, newest-looking first. Never raises."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=2) as fh:
            tags = json.load(fh).get("models") or []
        names = [m["name"] for m in tags if m.get("name")]
        return names or list(_FALLBACK_LOCAL_MODELS)
    except Exception:
        # Ollama not running, or not installed: the dropdown still renders and
        # the error surfaces at generation time with a usable message.
        return list(_FALLBACK_LOCAL_MODELS)


def estimate_tokens(text):
    """Rough token count. Greek runs denser per character than English, so
    this deliberately over-estimates rather than promising a fit that is not
    there."""
    return int(len(text or "") / 2.8)


# ── L1.5: the semantic interpreter ──────────────────────────────────
#
# derive_home_dynamics.py stays free of LLM wiring: it asks for a callable and
# this is where that callable is built, next to the model configuration the
# rest of the page already owns.

def make_semantic_interpreter(kind, model_name, on_progress=None):
    """A callable that reads one exchange and returns the decoded JSON.

    Returns None when no prompt is available, which leaves L1.5 running in its
    not-attempted mode rather than failing the report.
    """
    prompt = load_prompt_from_file("LLM_prompt_L1_5.txt")
    if not prompt.strip():
        return None
    counter = {"n": 0}

    def interpret(payload):
        counter["n"] += 1
        if on_progress:
            on_progress(counter["n"])
        exchange = json.dumps(payload, ensure_ascii=False, indent=2)
        reply = call_llm(kind, model_name, f"{prompt}\n\n{exchange}")
        if reply.startswith("Error calling LLM"):
            # Raised so derive_level1_5 records the failure per exchange
            # instead of storing a calm reading of an exchange never read.
            raise RuntimeError(reply)
        return parse_part_b(reply)     # same tolerant JSON extraction

    return interpret


# ── Part B: the reflection panel ────────────────────────────────────
#
# Six indicators, one overall reading and three questions, rendered as a small
# dashboard rather than prose. The LLM judges green vs amber; whether there is
# enough evidence to judge at all is decided here, by rule, because that is a
# counting question and the measured speaker-identification rate makes it the
# one the model is least equipped to answer honestly about itself.

# key -> (display name, blurb, inverted?, minimum evidence to have an opinion)
# `inverted` marks the two indicators that name a problem: for those, green
# means the problem was NOT detected.
PART_B_BULBS = [
    ("warmth", "Warmth", "Affection, support and positive tone", False, 3),
    ("joy", "Joy", "Laughter, play and shared enjoyment", False, 2),
    ("withdrawal", "Withdrawal", "Isolation or reduced participation", True, 1),
    ("tension", "Tension", "Conflict or unresolved strain", True, 1),
]
PART_B_STATES = ("green", "amber", "grey")
OVERALL_LABELS = ("Sparse", "Strained", "Mixed", "Steady", "Good", "Strong")

# The panel says how many lamps it has, and counts them from the list above
# to say it. Written out, the sentence went on saying six after two of the six
# had been removed.
_COUNT_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven",
                "eight", "nine", "ten")


def _in_words(n):
    return _COUNT_WORDS[n] if 0 <= n < len(_COUNT_WORDS) else str(n)


def compact_for_part_b(payload):
    """The daily aggregates only.

    Part B needs the shape of the week, not its episodes, so the long
    `social_events` list is dropped. This keeps the second call small enough
    that it is fast even on a local model.
    """
    days = []
    for day in payload.get("level_4_daily_dynamics", []) or []:
        days.append({k: v for k, v in day.items()
                     if k not in ("social_events", "no_observation_intervals")})
    return {"days": days}


def part_b_evidence(payload):
    """How much evidence exists behind each indicator, counted not judged.

    Returns {bulb key: count}. A bulb whose count falls under its minimum is
    forced grey no matter what the model said -- see normalise_part_b.

    Two sources feed this. L1.5 semantic readings are preferred: they are
    evidence about the dimensions themselves, so an interpreted exchange
    counts for every bulb it scored. Without them the count falls back to
    proxies -- laughter labels for joy, raised voices for tension -- which is
    why Joy sat permanently grey before L1.5 existed: the AudioSet laughter
    label fired zero times in a real week.
    """
    counts = {key: 0 for key, *_ in PART_B_BULBS}
    baseline_days = observed_days = 0
    for day in payload.get("level_4_daily_dynamics", []) or []:
        semantics = (day.get("household_semantics") or {}).get("dimensions") or {}
        for key, *_ in PART_B_BULBS:
            scored = semantics.get(key)
            if isinstance(scored, dict) and scored.get("n"):
                counts[key] += int(scored["n"])
    for day in payload.get("level_4_daily_dynamics", []) or []:
        activity = (day.get("household_activity") or {}).get("activity_counts") or {}
        pairs = day.get("pair_dynamics") or {}
        people = day.get("individual_patterns") or {}
        quality = day.get("data_quality") or {}

        # Reciprocal conversation is the only direct evidence of engagement
        # between two people that this pipeline can produce.
        # Warmth rests on the emotion channel, which is weak; require more of it.
        counts["warmth"] += quality.get("emotion_event_count", 0)
        counts["joy"] += (activity.get("laughter", 0)
                          + activity.get("positive_social_candidate", 0))
        counts["tension"] += (activity.get("raised_voice_candidate", 0)
                              + activity.get("distress_vocalisation_candidate", 0))
        if sum((p or {}).get("reciprocal_conversation_candidate_count", 0)
               for p in pairs.values()):
            observed_days += 1
        if (day.get("baseline_comparison") or {}).get("status") == "compared":
            baseline_days += 1

    # Withdrawal is a claim about change, so without semantic readings it is
    # unanswerable without a baseline to change from, however much speech was
    # recorded. With them it is directly observable in what people said, so the
    # semantic count already gathered above stands and the baseline only tops
    # it up.
    counts["withdrawal"] = max(counts["withdrawal"], baseline_days)
    # Tension has no positive evidence requirement: not finding conflict on a
    # well-observed week IS the finding. What is needed is a test of whether the
    # week was observed at all, which used to borrow connection's coverage.
    # Connection is no longer a dimension, so the reciprocal conversations it
    # was counting are read directly.
    if observed_days >= 2:
        counts["tension"] = max(counts["tension"], 1)
    return counts


# Where a lamp turns on. Positive indicators light green when the evidence
# says the thing is present; the two inverted ones light AMBER when the problem
# is present and green when it is not, which is why their thresholds read the
# other way round.
SEMANTIC_AMBER = 0.40        # problem dimension worth noticing

# A positive lamp goes green when this share of a week's interpreted exchanges
# actually showed the thing. Whether one exchange showed it is decided by
# SEMANTIC_PRESENT_FLOOR, beside the rollup that counts them.
#
# These lamps used to compare a weekly MEAN against 0.55, and a mean is the
# wrong instrument: it averages the warmth of a family's evening together with
# the warmth of asking who moved the charger. Measured against a listener on 90
# exchanges (2026-09-22, tests/diagnostics/dynamics_pilot), the interpreter's
# warmth had a median of 0.50 and a mean of 0.39, so the mean sat under 0.55
# whatever the week held and the warmth lamp could not go green at all -- it
# reached 0.55 on 3 exchanges in five days. Lowering the line to 0.40 would not
# have helped either, because 0.39 is still below it. Counting is the fix, not
# a smaller number.
#
# The share below is a JUDGEMENT, unlike the floor, which is measured: no
# ground truth exists for what makes a WEEK warm. It is set where a lamp lit by
# two exchanges in five seems a fair reading. Move it if the household
# disagrees.
SEMANTIC_GREEN_SHARE = 0.40


def bulbs_from_semantics(payload):
    """Bulb states read straight off the L1.5 relational dimensions.

    The dimensions ARE the lamps, so when exchanges have been
    interpreted the state is computed here rather than judged again by a model:
    one fewer place for an opinion to enter, and every lamp traces back to
    coded exchanges. Returns {} when nothing was interpreted, and the LLM's own
    reading is used instead.
    """
    totals = {}
    for day in payload.get("level_4_daily_dynamics", []) or []:
        dims = (day.get("household_semantics") or {}).get("dimensions") or {}
        for key, scored in dims.items():
            if isinstance(scored, dict) and scored.get("n"):
                bucket = totals.setdefault(
                    key, {"sum": 0.0, "n": 0, "max": 0.0, "present": 0,
                          "counted": False})
                bucket["sum"] += (scored.get("mean") or 0.0) * scored["n"]
                bucket["n"] += scored["n"]
                bucket["max"] = max(bucket["max"], scored.get("max") or 0.0)
                if "present" in scored:
                    bucket["present"] += scored["present"] or 0
                    bucket["counted"] = True

    out = {}
    for key, name, _blurb, inverted, _floor in PART_B_BULBS:
        bucket = totals.get(key)
        if not bucket or not bucket["n"]:
            continue
        mean = bucket["sum"] / bucket["n"]
        if inverted:
            # A single sharp exchange matters even in a calm week, so the peak
            # can raise the flag on its own -- an argument is not averaged away.
            flagged = mean >= SEMANTIC_AMBER or bucket["max"] >= 0.75
            state = "amber" if flagged else "green"
            note = (f"{name.lower()} averaged {mean:.2f} across "
                    f"{bucket['n']} interpreted exchange(s), peak {bucket['max']:.2f}")
        elif bucket["counted"]:
            # How many exchanges showed it, not how warm the average one was.
            share = bucket["present"] / bucket["n"]
            state = "green" if share >= SEMANTIC_GREEN_SHARE else "amber"
            note = (f"{name.lower()} was there in {bucket['present']} of "
                    f"{bucket['n']} interpreted exchange(s) "
                    f"({share:.0%}), averaging {mean:.2f}")
        else:
            # A report stored before exchanges were counted. Reading its mean
            # against the share line would turn every old report's positive
            # lamps amber at once, which would look like the household changed.
            state = "green" if mean >= 0.55 else "amber"
            note = (f"{name.lower()} averaged {mean:.2f} across "
                    f"{bucket['n']} interpreted exchange(s) "
                    f"(older report: exchanges were not counted)")
        out[key] = {"state": state, "evidence": note,
                    "mean": round(mean, 3), "n": bucket["n"],
                    "max": round(bucket["max"], 3)}
    return out


def parse_part_b(text):
    """Pull the JSON object out of a model reply. Returns None if unusable.

    Models wrap JSON in fences or preambles however firmly they are asked not
    to, so the object is located rather than assumed to be the whole reply.
    """
    if not text:
        return None
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def normalise_part_b(data, evidence, semantics=None):
    """Coerce a model reply into something the renderer can always draw.

    Every field is defaulted rather than trusted: a malformed reply degrades to
    a grey panel with no questions, which is honest, instead of raising inside
    a callback and losing Part A along with it.
    """
    data = data or {}
    raw_bulbs = data.get("bulbs") if isinstance(data.get("bulbs"), dict) else {}
    bulbs = {}
    for key, name, blurb, inverted, floor in PART_B_BULBS:
        item = raw_bulbs.get(key) if isinstance(raw_bulbs.get(key), dict) else {}
        state = str(item.get("state", "")).strip().lower()
        if state not in PART_B_STATES:
            state = "grey"
        note = str(item.get("evidence", "") or "").strip()
        # Coded exchanges outrank the model's own impression: they are direct
        # evidence about this dimension, and the model is reading a summary.
        measured = (semantics or {}).get(key)
        if measured:
            state = measured["state"]
            note = note or measured["evidence"]
        # The evidence floor overrides in one direction only: it can turn a
        # judgement into "not enough evidence", never the reverse.
        if evidence.get(key, 0) < floor:
            state = "grey"
            # Replaced, not kept: a measured average printed under a "no
            # evidence" chip reads as a contradiction, and the average is
            # exactly what the floor has just judged too thin to report.
            note = (f"Only {evidence.get(key, 0)} interpreted exchange(s) — "
                    f"below the {floor} needed to judge this.")
        bulbs[key] = {"state": state, "evidence": note,
                      "name": name, "blurb": blurb, "inverted": inverted}

    raw_overall = data.get("overall") if isinstance(data.get("overall"), dict) else {}
    try:
        score = int(round(float(raw_overall.get("score"))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(10, score))
    label = str(raw_overall.get("label", "") or "").strip().title()
    if label not in OVERALL_LABELS:
        label = "Mixed"
    grey_count = sum(1 for b in bulbs.values() if b["state"] == "grey")
    if grey_count >= 3:
        # A week the system barely saw is a sparse record, not a good one. The
        # prompt says so; this makes it true whatever the model returned.
        label = "Sparse"
        score = min(score, 5)

    questions = []
    for item in (data.get("questions") or [])[:3]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("question", "") or "").strip()
        if not text:
            continue
        priority = str(item.get("priority", "") or "").strip().lower()
        questions.append({
            "question": text,
            "priority": priority if priority in ("highest", "medium", "lower") else "lower",
            "because": str(item.get("because", "") or "").strip(),
        })

    return {"bulbs": bulbs, "questions": questions,
            "overall": {"score": score, "label": label,
                        "basis": str(raw_overall.get("basis", "") or "").strip()},
            "grey_count": grey_count}


def generate_part_b(kind, model_name, payload):
    """Second LLM call: the same evidence, a different question."""
    prompt = load_prompt_from_file("LLM_prompt_partB.txt")
    if not prompt.strip():
        return None, "LLM_prompt_partB.txt is missing or empty."
    facts = json.dumps(compact_for_part_b(payload), ensure_ascii=False, indent=2)
    reply = call_llm(kind, model_name, f"{prompt}\n\n{facts}")
    if reply.startswith("Error calling LLM"):
        return None, reply
    parsed = parse_part_b(reply)
    if parsed is None:
        return None, ("The model did not return usable JSON for Part B. "
                      f"It replied: {reply[:200]}")
    return parsed, None


_BULB_INK = {"green": ("#57C98A", "#BFF3D6"),
             "amber": ("#F0A63C", "#FFE0B0"),
             "grey":  ("#4A5765", "#5C6A79")}

# What the chip says. The two inverted indicators get an explicit reading, so a
# green Tension bulb cannot be mistaken for "plenty of healthy tension".
_CHIP = {
    (False, "green"): "Present",  (True, "green"): "Not detected",
    (False, "amber"): "Worth noticing", (True, "amber"): "Worth noticing",
    (False, "grey"): "No evidence", (True, "grey"): "No evidence",
}


def bulb_svg(state):
    """One lamp. Glass, screw base, filament and a halo that lights with state.

    Self-contained: the animation and every colour live inside the SVG, because
    it is served as a data URI in an <img>, which is isolated from the page's
    stylesheet. That isolation is the point -- it renders identically without
    depending on Dash passing raw markup through untouched.
    """
    glow, filament = _BULB_INK.get(state, _BULB_INK["grey"])
    uid = f"{state}"
    lit = state in ("green", "amber")
    halo_opacity = "0.55" if lit else "0.06"
    # Only a lit filament breathes; a dark bulb sits still.
    animation = ("""
    .halo{transform-origin:60px 56px;animation:breathe 4.5s ease-in-out infinite}
    @keyframes breathe{0%,100%{opacity:.50;transform:scale(1)}
                       50%{opacity:.72;transform:scale(1.045)}}
    @media (prefers-reduced-motion:reduce){.halo{animation:none}}
    """ if lit else "")
    return f'''
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 150" role="img">
  <style>{animation}</style>
  <defs>
    <radialGradient id="halo-{uid}" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="{glow}" stop-opacity="0.95"/>
      <stop offset="55%" stop-color="{glow}" stop-opacity="0.28"/>
      <stop offset="100%" stop-color="{glow}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="glass-{uid}" cx="42%" cy="34%" r="72%">
      <stop offset="0%" stop-color="{glow}" stop-opacity="0.30"/>
      <stop offset="70%" stop-color="{glow}" stop-opacity="0.10"/>
      <stop offset="100%" stop-color="{glow}" stop-opacity="0.03"/>
    </radialGradient>
  </defs>

  <circle class="halo" cx="60" cy="56" r="54" fill="url(#halo-{uid})"
          opacity="{halo_opacity}"/>

  <!-- glass envelope: a round bulb drawn down into the neck -->
  <path d="M60 12
           a34 34 0 0 1 20 61
           c-3 3 -4 6 -4 10
           v4 h-32 v-4
           c0 -4 -1 -7 -4 -10
           A34 34 0 0 1 60 12 z"
        fill="url(#glass-{uid})" stroke="{glow}" stroke-opacity="0.55" stroke-width="2"/>

  <!-- filament -->
  <path
        d="M50 66 v-9 a10 10 0 0 1 20 0 v9"
        fill="none" stroke="{filament}" stroke-width="2.6"
        stroke-linecap="round" stroke-linejoin="round"/>
  <path
        d="M54 48 q6 -9 12 0" fill="none" stroke="{filament}"
        stroke-width="2.2" stroke-linecap="round"/>

  <!-- screw base -->
  <rect x="44" y="88" width="32" height="9"  rx="3" fill="#3A4652"/>
  <rect x="44" y="99" width="32" height="9"  rx="3" fill="#43505E"/>
  <rect x="47" y="110" width="26" height="10" rx="4" fill="#333E49"/>
</svg>'''


def bulb_data_uri(state):
    """The lamp as an inline image source. Cached: there are only three."""
    from urllib.parse import quote
    svg = " ".join(bulb_svg(state).split())
    return "data:image/svg+xml;charset=utf-8," + quote(svg, safe="")


def render_part_b(data, note=None):
    """The panel itself. Pure rendering -- every value is already normalised."""
    bulbs = []
    for key, name, blurb, inverted, _floor in PART_B_BULBS:
        b = data["bulbs"][key]
        children = [
            html.Img(src=bulb_data_uri(b["state"]), className="fb-glass",
                     alt=f"{name}: {_CHIP[(inverted, b['state'])]}"),
            html.Div(name, className="fb-name"),
            html.Div(blurb, className="fb-blurb"),
            html.Div(_CHIP[(inverted, b["state"])], className="fb-chip"),
        ]
        if b["evidence"]:
            # Marked so the figure ties to the footnote below. "averaged 0.59"
            # says nothing about what was averaged, and a reader is left to
            # guess whether it is over days, people or conversations.
            children.append(html.Div(f"* {b['evidence']}",
                                     className="fb-evidence"))
        bulbs.append(html.Div(children, className="fb-bulb",
                              **{"data-state": b["state"]}))

    overall = data["overall"]
    segs = [html.Div(className="fb-seg on" if i < overall["score"] else "fb-seg")
            for i in range(10)]
    meter = html.Div([
        html.Div("Family Connection", className="fb-meter-label"),
        html.Div(segs, className="fb-segs"),
        html.Div(overall["label"], className="fb-verdict"),
        html.Div(overall["basis"], className="fb-basis") if overall["basis"] else None,
    ], className="fb-meter sparse" if overall["label"] == "Sparse" else "fb-meter")

    if data["questions"]:
        rows = [
            html.Div([
                html.Div(str(i), className="fb-qnum"),
                html.Div([
                    html.Div(q["question"], className="fb-qtext"),
                    html.Div(q["because"], className="fb-qwhy") if q["because"] else None,
                ]),
            ], className="fb-q", **{"data-priority": q["priority"]})
            for i, q in enumerate(data["questions"], 1)
        ]
    else:
        rows = [html.Div("No questions were produced for this window.",
                         className="fb-empty")]

    tail = []
    if any(data["bulbs"][key]["evidence"] for key, *_rest in PART_B_BULBS):
        tail.append(html.Div(
            "* Each figure is the average over the exchanges the model could "
            "read this week — an exchange being one back-and-forth between "
            "people in the same room, scored 0 to 1 for each quality. It is "
            "not an average over days or over people. “Peak” is the "
            "single strongest exchange, shown for Withdrawal and Tension so "
            "that one sharp moment is not averaged away by a calm week.",
            className="fb-note"))
    if data["grey_count"]:
        unlit, one = data["grey_count"], data["grey_count"] == 1
        tail.append(html.Div(
            f"{unlit} of {len(PART_B_BULBS)} indicators {'is' if one else 'are'} "
            f"unlit because the week was not observed closely enough to judge "
            f"{'it' if one else 'them'}. Unlit means unseen, not bad.",
            className="fb-note"))
    if note:
        tail.append(html.Div(note, className="fb-note"))

    return html.Div([
        html.Div([
            html.H3("This week at home"),
            html.P(f"{_in_words(len(PART_B_BULBS)).capitalize()} lamps for the "
                   "week, and three things worth talking about."),
        ], className="fb-head"),
        html.Div(bulbs, className="fb-bulbs"),
        meter,
        html.Div([
            html.Div("Worth talking about", className="fb-qhead"),
            html.Div("Chosen by priority from what was actually observed.",
                     className="fb-qsub"),
            *rows,
        ], className="fb-questions"),
        *tail,
    ], className="fb-panel")


def get_llm_client(model_choice):
    """(kind, model name). 'chatgpt' is the only cloud choice; anything else is
    treated as the name of a local Ollama model."""
    if model_choice == "chatgpt":
        return "openai", "gpt-4o-mini"
    return "ollama", (model_choice or DEFAULT_LOCAL_MODEL)


def call_llm(kind, model, full_prompt):
    if kind == "openai":
        try:
            client = OpenAI(api_key=openai_api_key())
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": full_prompt}],
                stream=False,
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"Error calling LLM: {e}"

    import urllib.error
    import urllib.request
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": full_prompt}],
        "stream": False,
        # Reasoning models (qwen3.x) otherwise spend the remaining context on a
        # `thinking` field and return an empty `content`. The report is the
        # deliverable, not the reasoning, so the tokens go to the report.
        # Accepted and ignored by models without a thinking mode.
        "think": False,
        "options": {"num_ctx": OLLAMA_NUM_CTX},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as fh:
            payload = json.load(fh)
    except urllib.error.URLError as e:
        return (f"Error calling Ollama at {OLLAMA_HOST}: {e}\n\n"
                "Is `ollama serve` running?")
    except Exception as e:
        return f"Error calling LLM: {e}"

    message = payload.get("message") or {}
    text = (message.get("content") or "").strip()
    used = payload.get("prompt_eval_count")
    if not text:
        # Almost always a context problem rather than a model failure, so say
        # which one instead of leaving "empty response" to be puzzled over.
        detail = [f"empty response from {model}."]
        if used:
            headroom = OLLAMA_NUM_CTX - used
            detail.append(f"The prompt used {used:,} of {OLLAMA_NUM_CTX:,} context "
                          f"tokens, leaving {headroom:,} for the report.")
            if headroom < 4000:
                detail.append("That is too little to write one -- raise "
                              "OLLAMA_NUM_CTX, or shorten REPORT_WINDOW_DAYS.")
        if (message.get("thinking") or "").strip():
            detail.append("The model produced reasoning but no answer, which "
                          "means it ran out of room mid-thought.")
        return "Error calling LLM: " + " ".join(detail)
    # Ollama reports how much of the prompt it actually consumed. When that
    # fills the window the prompt was truncated, and the report is being
    # written from a fragment -- worth saying in the report rather than leaving
    # the reader to wonder why it ignored half the data.
    if used and used >= OLLAMA_NUM_CTX * 0.95:
        text += (f"\n\n---\n*Warning: the prompt filled the {OLLAMA_NUM_CTX}-token "
                 f"context ({used} tokens counted). Some input was almost "
                 f"certainly truncated; raise OLLAMA_NUM_CTX or shorten the "
                 f"window.*")
    return text

def layout(**kwargs):
    # Queried per page load rather than at import, so a model pulled while the
    # app is running appears on a refresh instead of needing a restart.
    local_models = list_local_models()
    return dbc.Container([
        dbc.Row([
            dbc.Col([
                html.H2("LLM Activity Report", className="text-primary mb-4"),
                html.P("Generate an AI-powered household dynamics report over a "
                       "window you choose — a whole week, or a single "
                       "conversation's worth of hours.", className="text-muted")
            ], width=12)
        ]),
        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader("Configure Report", className="bg-light font-weight-bold"),
                    dbc.CardBody([
                        dbc.Row([
                            dbc.Col([
                                dbc.Label("Select AI Model:"),
                                dcc.Dropdown(
                                    id="llm-model-selector",
                                    # Built from what Ollama actually has
                                    # installed, so pulling a new model makes it
                                    # selectable without touching this file.
                                    options=(
                                        [{"label": f"{name}  (local)", "value": name}
                                         for name in local_models]
                                        + [{"label": "ChatGPT (Cloud)",
                                            "value": "chatgpt"}]
                                    ),
                                    value=(DEFAULT_LOCAL_MODEL
                                           if DEFAULT_LOCAL_MODEL in local_models
                                           else (local_models[0] if local_models
                                                 else "chatgpt")),
                                    clearable=False,
                                    className="mb-3"
                                ),
                                html.Div(
                                    f"Local models run through Ollama at "
                                    f"{OLLAMA_NUM_CTX:,} tokens of context. "
                                    f"Context limits differ sharply between "
                                    f"models — gemma2:9b caps at 8,192 and "
                                    f"cannot hold this prompt.",
                                    className="text-muted small mb-3"),
                            ], width=4),

                            # Dates from a calendar, times from a text box.
                            # A full datetime picker would be a heavier control
                            # than this needs: the hours are set occasionally,
                            # to cut a report down to one conversation, while
                            # the dates are set every time.
                            dbc.Col([
                                dbc.Label("Reporting window:"),
                                dcc.DatePickerRange(
                                    id="report-date-range",
                                    start_date=(datetime.now()
                                                - timedelta(days=REPORT_WINDOW_DAYS)).date(),
                                    end_date=datetime.now().date(),
                                    display_format="DD-MM-YYYY",
                                    className="d-block mb-2",
                                ),
                                dbc.Row([
                                    dbc.Col([
                                        dbc.Label("From time",
                                                  className="small text-muted mb-0"),
                                        dbc.Input(id="report-start-time",
                                                  value="00:00",
                                                  placeholder="HH:MM",
                                                  size="sm"),
                                    ], width=6),
                                    dbc.Col([
                                        dbc.Label("To time",
                                                  className="small text-muted mb-0"),
                                        dbc.Input(id="report-end-time",
                                                  value="23:59",
                                                  placeholder="HH:MM",
                                                  size="sm"),
                                    ], width=6),
                                ], className="g-2"),
                                html.Div(
                                    'Opens on the last week. "10:00", "10.00" '
                                    'and "10" all mean ten o\'clock; leave a '
                                    "time empty for the whole day.",
                                    className="text-muted small mt-2"),
                            ], width=5),
                        ]),
                        dbc.Button([html.I(className="fas fa-robot me-2"), "Generate Report"],
                                   id="btn-generate-report", color="primary", n_clicks=0)
                    ])
                ], className="shadow-sm mb-4")
            ], width=12)
        ]),
        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader("Generated Report", className="bg-dark text-white font-weight-bold"),
                    dbc.CardBody([
                        html.Div(id="report-metadata", className="mb-3 text-muted small"),
                        # The lamps, then what happened, then the written
                        # report. The incident list sits between the two
                        # generated parts but is not one of them: it is read
                        # from the store for whatever window is chosen, so it
                        # is there to be judged before any report is asked
                        # for, and stays usable while one is being written.
                        dbc.Spinner(html.Div(id="llm-report-output"), color="primary"),
                        html.Div(id="incident-panel"),
                        # Ticks only while a day's list is being prepared, so
                        # that the list appears when it is ready without the
                        # page being reloaded. Off the rest of the time.
                        dcc.Interval(id="incident-poll", interval=5000,
                                     disabled=True),
                        html.Div(id="llm-report-text"),
                    ])
                ], className="shadow-sm")
            ], width=12)
        ])
    ], fluid=True, className="p-4")


def _judged(clicked):
    """Record a Confirm or a Reject. Returns a line to show, or None."""
    verdict = None if clicked["act"] == "clear" else clicked["verdict"]
    try:
        judgements.judge(clicked["key"], verdict)
    except LookupError:
        return ("That day has been read again since this list was drawn, so "
                "the answer was not recorded. The list below is the current one.")
    except OSError as exc:
        # Said, not swallowed: a button that looks pressed over an answer that
        # was never written would be a record with a hole in it.
        return f"The answer could not be saved, and was not recorded: {exc}"
    return None


def _answered(clicked):
    """Act on a Yes, a No or an "ask me again". Returns a line to show, or None."""
    try:
        if clicked["answer"] == "yes":
            prepare.start(clicked["day"])
        elif clicked["answer"] == "no":
            prepare.decline(clicked["day"])
        elif clicked["answer"] == "again":
            prepare.ask_again(clicked["day"].split(","))
    except ValueError as why:
        # Not a date, a day that is not over, a day that has its list by now,
        # or another day already being prepared: the reason is the message.
        return f"The list was not started: {why}."
    except OSError as exc:
        return f"The list could not be started: {exc}"
    return None


def _tagged(clicked):
    """Tag a stretch in the blind step, or close the step. Returns a line to show."""
    try:
        if clicked["kind"] == "seal":
            blind.seal(clicked["day"])
        else:
            blind.toggle(clicked["day"], int(clicked["window"]), clicked["kind"])
    except ValueError as why:
        return f"Not recorded: {why}."
    except OSError as exc:
        return f"The answer could not be saved, and nothing was recorded: {exc}"
    return None


@callback(
    Output("incident-panel", "children"),
    Output("incident-poll", "disabled"),
    Input("report-date-range", "start_date"),
    Input("report-date-range", "end_date"),
    Input("report-start-time", "value"),
    Input("report-end-time", "value"),
    Input(incident_panel.VERDICT_BUTTONS, "n_clicks"),
    Input(incident_panel.PREPARE_BUTTONS, "n_clicks"),
    Input(incident_panel.BLIND_BUTTONS, "n_clicks"),
    Input("incident-poll", "n_intervals"),
)
def show_incidents(start_date, end_date, start_clock, end_clock,
                   _verdicts, _answers, _tags, _tick):
    """The incident list for the chosen window, and what its buttons do.

    One callback for all of it, so the list is always redrawn from the record
    a click was just written to, and the count above it cannot disagree with
    the rows beneath.
    """
    notice = None
    clicked = ctx.triggered_id
    if isinstance(clicked, dict):
        # Dash may also call this when the buttons APPEAR, which they do every
        # time the list is drawn. Nobody clicked then, and redrawing would
        # draw them again, without end.
        if len(ctx.triggered) != 1 or not ctx.triggered[0]["value"]:
            raise PreventUpdate
        if clicked["type"] == "incident-verdict":
            notice = _judged(clicked)
        elif clicked["type"] == "blind-act":
            notice = _tagged(clicked)
        else:
            notice = _answered(clicked)

    # The clock ticks only while a day is being prepared.
    idle = prepare.running() is None
    start, end, window_error = resolve_window(
        start_date, end_date, start_clock, end_clock)
    if window_error:
        # The same message the Generate button gives, shown where the list
        # would be. A half-typed time passes through here on its way to a
        # whole one.
        return html.Div(window_error, className="fb-panel inc-panel fb-empty"), idle
    return incident_panel.render(start, end, notice=notice), idle


@callback(
    Output("llm-report-output", "children"),
    Output("report-metadata", "children"),
    Output("llm-report-text", "children"),
    Input("btn-generate-report", "n_clicks"),
    State("llm-model-selector", "value"),
    State("report-date-range", "start_date"),
    State("report-date-range", "end_date"),
    State("report-start-time", "value"),
    State("report-end-time", "value"),
    prevent_initial_call=True
)
def handle_generate_report(n_clicks, model_choice, start_date, end_date,
                           start_clock, end_clock):
    if n_clicks == 0:
        return "", "", ""

    # 1. Extract the reporting window
    requested_start, requested_end, window_error = resolve_window(
        start_date, end_date, start_clock, end_clock)
    if window_error:
        return dbc.Alert(window_error, color="danger"), "", ""

    df, window_start, window_end, events = get_recent_events(
        start=requested_start, end=requested_end)

    if df is None or df.empty:
        return dbc.Alert(
            f"No events between {window_start:%d-%m-%Y %H:%M} and "
            f"{window_end:%d-%m-%Y %H:%M}.", color="warning"), "", ""

    # Minutes are shown because they can now be chosen; a window that reads
    # as two whole dates when it was actually two hours would misdescribe
    # every number underneath it.
    date_str = (f"{window_start:%d-%m-%Y %H:%M} to "
                f"{window_end:%d-%m-%Y %H:%M}")
    start_time = df['Timestamp'].min()
    end_time = df['Timestamp'].max()

    # 2. Choose the model first: L1.5 runs inside the pipeline below and needs
    #    an interpreter, so the choice has to be made before the pipeline, not
    #    after it as when only Part A used a model.
    kind, model_name = get_llm_client(model_choice)
    llm_prompt = load_prompt_from_file('LLM_prompt.txt')

    # 3. Derive the Level 0-4 hierarchy and save it alongside the report
    payload, analysis_path = run_dynamics_pipeline(
        events, window_start, window_end, semantic_model=(kind, model_name))

    # 4. Build the LLM input from the hierarchy.
    #    Level 4 carries the daily dynamics, its own uncertainties and its data
    #    quality, so the model is told what the evidence will not support
    #    rather than being left to assume. Level 3 is summarised rather than
    #    included whole: the full episode list is long and repetitive, and the
    #    saved analysis file remains the complete record.
    if payload:
        structured = json.dumps(compact_for_llm(payload), ensure_ascii=False, indent=2)

        llm_input = f"""REPORTING WINDOW: {date_str}
EVENTS ANALYSED: {len(events)}

HIERARCHICALLY DERIVED HOUSEHOLD DYNAMICS (Levels 3-4):

The data below is produced by a deterministic pipeline, not by a model. Raw
audio labels were normalised into semantic events, merged into activity
episodes, then into person and social episodes, and finally aggregated per
day. Confidence values, `_candidate` suffixes, `uncertainties` and
`data_quality` are part of the evidence: treat them as limits on what can be
concluded.

Two properties of this system matter for interpretation:

  * Recording is event-triggered. Intervals with no events are unobserved,
    not silent, and must not be read as inactivity or absence.
  * Emotion values are raw model output aggregated as a distribution. They
    are observations about audio, not assessments of anyone's state of mind.

{structured}

ANALYSIS TASK:

Review the structured dynamics above and apply your reflection framework to
identify observable patterns worth the household's attention. Ground every
observation in the evidence given, and say plainly where the data is too
sparse to support a conclusion."""
    else:
        # Pipeline unavailable: fall back to the original flat summary rather
        # than failing the report outright.
        analysis_results = analyze_audio_events_df(df)
        llm_input = f"""REPORTING WINDOW: {date_str}

AUDIO FACTS FROM SENTINEL ANALYSIS:

{format_analysis_summary(analysis_results)}

ANALYSIS TASK:

Review the above audio facts and apply your reflection framework to identify
observable patterns worth the household's attention."""

    # 5. LLM call with the existing prompt over the structured data
    full_prompt = f"{llm_prompt}\n\n{llm_input}"
    prompt_tokens = estimate_tokens(full_prompt)
    started = datetime.now()
    report = call_llm(kind, model_name, full_prompt)
    elapsed = (datetime.now() - started).total_seconds()

    # 6. Display result. The written report goes to its own container, below
    #    the incident list; the lamps go above it.
    written_report = html.Div([
        html.Hr(className="my-4"),
        html.H5("Household Dynamics Report", className="mt-3"),
        dcc.Markdown(report),
    ])

    # ---- Part B: the reflection panel -------------------------------------
    # A second call over the same evidence, asking a different question. It is
    # built after Part A and never allowed to break it: any failure here shows
    # as an unlit panel with the reason, and the written report still stands.
    part_b_panel = None
    if payload:
        part_b_raw, part_b_error = generate_part_b(kind, model_name, payload)
        panel_data = normalise_part_b(part_b_raw, part_b_evidence(payload),
                                      semantics=bulbs_from_semantics(payload))
        part_b_panel = render_part_b(panel_data, note=part_b_error)
    else:
        part_b_panel = html.Div(
            "The reflection panel needs the dynamics pipeline, which did not run "
            "for this window.", className="fb-panel fb-empty")

    # Save report to disk
    report_path = os.path.join(project_root, 'assets', 'daily_report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    pipeline_note = (
        html.Span(f"Analysis: {os.path.basename(analysis_path)}",
                  className="me-3 text-success")
        if analysis_path else
        html.Span("Analysis: pipeline unavailable (flat summary used)",
                  className="me-3 text-warning")
    )

    # The prompt size is shown because it is the number that decides whether a
    # local model saw the whole report or a fragment of it.
    fits = (kind != "ollama") or prompt_tokens < OLLAMA_NUM_CTX
    metadata = html.Div([
        html.Span(f"Window: {date_str}", className="me-3 fw-bold"),
        html.Span(f"Data Range: {start_time} to {end_time}", className="me-3"),
        html.Span(f"Total Events: {len(df)}", className="me-3"),
        pipeline_note,
        html.Span(f"Model: {model_name}", className="me-3 fw-bold text-info"),
        html.Span(f"Prompt: ~{prompt_tokens:,} tokens"
                  + (f" / {OLLAMA_NUM_CTX:,} context" if kind == "ollama" else ""),
                  className="me-3 " + ("text-muted" if fits else "text-danger")),
        html.Span(f"{elapsed:.0f}s", className="text-muted"),
    ])

    return part_b_panel, metadata, written_report