"""Run one mock evening through L0-L4 and print what each level did."""
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from ast_dash.llm_report import derive_home_dynamics as H

CFG = H.load_config(None)
CFG["device_room_map"] = {"bath-1": "Bathroom"}

# One evening. Each row is a dashboard export row exactly as the pipeline
# expects it, chosen so that between them they exercise every branch.
RAW = [
    # 1. Kitchen cooking: two mapped labels + one nullish (discarded).
    {"ID": 101, "Timestamp": "2026-08-30 19:00:00", "Device": "kitchen-1", "room": "Kitchen",
     "Class 1": "Frying (food)", "Prob 1": 0.82,
     "Class 2": "Dishes, pots, and pans", "Prob 2": 0.41,
     "Class 3": "None", "Prob 3": 0.0,
     "transcription": None, "emotion": "None", "rms": 0.14, "file_name": "k101.mp3"},

    # 2. Two speakers in one recording + emotion. The conversation seed.
    {"ID": 102, "Timestamp": "2026-08-30 19:02:30", "Device": "kitchen-1", "room": "Kitchen",
     "Class 1": "Speech", "Prob 1": 0.71, "Class 2": "Conversation", "Prob 2": 0.52,
     "Class 3": "Inside, small room", "Prob 3": 0.33,
     "transcription": "[ 00: 00. 00 - 00: 04. 20 ] Ilyas: Θα φάμε στις οκτώ. "
                      "[ 00: 04. 50 - 00: 09. 10 ] Voula: Ναι, τελειώνω εδώ.",
     "emotion": "HAP (0.71)", "rms": 0.22, "file_name": "k102.mp3"},

    # 3. Same room, 90s later: inside the 120s conversation gap, so same cluster.
    {"ID": 103, "Timestamp": "2026-08-30 19:04:00", "Device": "kitchen-1", "room": "Kitchen",
     "Class 1": "Speech", "Prob 1": 0.66, "Class 2": "Silence", "Prob 2": 0.10,
     "transcription": "[ 00: 00. 00 - 00: 03. 80 ] Voula: Το τηγάνι καίει.",
     "emotion": "NEU (0.55)", "rms": 0.19, "file_name": "k103.mp3"},

    # 4. Object access, folds into the cooking episode as supporting evidence.
    {"ID": 104, "Timestamp": "2026-08-30 19:06:00", "Device": "kitchen-1", "room": "Kitchen",
     "Class 1": "Chopping (food)", "Prob 1": 0.60,
     "Class 2": "Cupboard open or close", "Prob 2": 0.30,
     "transcription": None, "emotion": None, "rms": 0.17, "file_name": "k104.mp3"},

    # 5. Living room TV starts.
    {"ID": 105, "Timestamp": "2026-08-30 20:15:00", "Device": "living-1", "room": "Living",
     "Class 1": "Television", "Prob 1": 0.88, "Class 2": "Music", "Prob 2": 0.40,
     "transcription": None, "emotion": None, "rms": 0.31, "file_name": "l105.mp3"},

    # 6/7. Two residents speak near the TV -> co-presence + shared media.
    {"ID": 106, "Timestamp": "2026-08-30 20:16:00", "Device": "living-1", "room": "Living",
     "Class 1": "Speech", "Prob 1": 0.64, "Class 2": "Laughter", "Prob 2": 0.45,
     "transcription": "[ 00: 00. 00 - 00: 02. 90 ] Rafael: Δες αυτό!",
     "emotion": "HAP (0.80)", "rms": 0.25, "file_name": "l106.mp3"},
    {"ID": 107, "Timestamp": "2026-08-30 20:17:30", "Device": "living-1", "room": "Living",
     "Class 1": "Speech", "Prob 1": 0.69,
     "transcription": "[ 00: 00. 00 - 00: 03. 40 ] Voula: Το είδα.",
     "emotion": "NEU (0.61)", "rms": 0.23, "file_name": "l107.mp3"},

    # 8. Unknown speaker + an unmapped AudioSet label.
    {"ID": 108, "Timestamp": "2026-08-30 20:19:00", "Device": "living-1", "room": "Living",
     "Class 1": "Zither", "Prob 1": 0.44, "Class 2": "Speech", "Prob 2": 0.30,
     "transcription": "[ 00: 00. 00 - 00: 02. 10 ] Unknown: ...",
     "emotion": None, "rms": 0.20, "file_name": "l108.mp3"},

    # 9. No room on the event: resolved through device_room_map. Composite rule.
    {"ID": 109, "Timestamp": "2026-08-30 21:40:00", "Device": "bath-1", "room": None,
     "Class 1": "Water tap, faucet", "Prob 1": 0.70,
     "Class 2": "Toilet flush", "Prob 2": 0.50,
     "Class 3": "Breathing", "Prob 3": 0.21,
     "transcription": None, "emotion": None, "rms": 0.12, "file_name": "b109.mp3"},

    # 10. After a 79-minute silence -> a no_observation_interval is recorded.
    {"ID": 110, "Timestamp": "2026-08-30 22:59:00", "Device": "bed-1", "room": "Bedroom",
     "Class 1": "Snoring", "Prob 1": 0.66, "transcription": None, "emotion": None,
     "rms": 0.09, "file_name": "d110.mp3"},

    # 11. A second day, so Level 4 has something to compare against.
    {"ID": 201, "Timestamp": "2026-08-31 19:05:00", "Device": "kitchen-1", "room": "Kitchen",
     "Class 1": "Speech", "Prob 1": 0.72,
     "transcription": "[ 00: 00. 00 - 00: 05. 00 ] Ilyas: Καλημέρα.",
     "emotion": "NEU (0.60)", "rms": 0.20, "file_name": "k201.mp3"},
]


def show(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


l0 = H.derive_level0(RAW, CFG)
show("LEVEL 0 — canonical observations (%d)" % len(l0))
for o in l0:
    print(f"\n  event {o.event_id}  {o.timestamp}  room={o.room!r} (source: {o.room_source})")
    print(f"    labels kept     : {[(l['label'], l['probability']) for l in o.audio_labels]}")
    if o.raw_labels_discarded:
        print(f"    labels discarded: {o.raw_labels_discarded}")
    sp = o.speech
    print(f"    has_speech={sp['has_speech']}  has_words={sp['has_words']}  speakers={sp['speakers']}")
    for s in sp["segments"]:
        print(f"      turn {s['offset_start']}-{s['offset_end']}s  {s['speaker']!r}: {(s['text'] or '')[:38]!r}")
    print(f"    emotion         : {o.emotion}")

l1 = H.derive_level1(l0, CFG)
show("LEVEL 1 — semantic events")
for r in l1:
    print(f"\n  event {r.source_event_id}  room={r.room}")
    for e in r.semantic_events:
        extra = f"  ({', '.join(e['uncertainty'])})" if e.get("uncertainty") else ""
        person = f"  person={e['person']}" if e.get("person") else ""
        print(f"    {e['type']:<32} conf={e['confidence']:<6} via {e['modality']}{person}{extra}")

l2 = H.derive_level2(l1, CFG)
show("LEVEL 2 — activity episodes (%d)" % len(l2))
for e in l2:
    print(f"\n  {e.episode_id}  {e.activity}  room={e.room}")
    print(f"    {e.start} -> {e.end}   ({e.duration_seconds:.0f}s)  conf={e.confidence}")
    print(f"    semantic types : {e.semantic_types}")
    print(f"    from events    : {e.source_event_ids}")
    if e.uncertainty:
        print(f"    uncertainty    : {e.uncertainty}")

l3 = H.derive_level3(l0, l2, CFG)
show("LEVEL 3 — person / social episodes (%d)" % len(l3))
for e in l3:
    print(f"\n  {e.episode_id}  {e.type}  room={e.room}  conf={e.confidence}")
    print(f"    {e.start} -> {e.end}   participants={e.participants}")
    for k, v in (e.details or {}).items():
        print(f"      {k}: {json.dumps(v, ensure_ascii=False)[:150]}")
    if e.uncertainty:
        for u in e.uncertainty:
            print(f"    ! {u}")

gaps = H.derive_observation_gaps(l0, CFG)
show("OBSERVATION GAPS (%d)" % len(gaps))
for g in gaps:
    print(f"  {g['start']} -> {g['end']}   {g['duration_seconds'] / 60:.0f} min   {g.get('note', '')}")

l4 = H.derive_level4(l0, l2, l3, gaps, CFG)
show("LEVEL 4 — daily dynamics")
print(json.dumps(l4, ensure_ascii=False, indent=2, default=float))
