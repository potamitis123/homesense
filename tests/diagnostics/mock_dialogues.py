#!/usr/bin/env python3
"""Four scripted family exchanges, pushed through L0 -> L1.5 -> L3 -> L4.

The point is to show that the six relational dimensions -- and therefore the
six dashboard lamps -- actually move with the MEANING of what was said, rather
than with the fact that speech occurred. The four scenarios are chosen to pull
in different directions:

    conflict     high connection AND high tension, low warmth
    connection   high connection, moderate warmth, high inclusion
    warmth       high warmth, high inclusion, low tension
    withdrawal   low warmth, high withdrawal, low inclusion

The critical assertion is the first one: an argument is a strong interaction.
If tension rises while connection falls, the level has confused engagement with
relational quality, which is the exact failure L1.5 exists to prevent.

    python tests/diagnostics/mock_dialogues.py            # no LLM, structure only
    python tests/diagnostics/mock_dialogues.py --chatgpt  # real interpretation
    python tests/diagnostics/mock_dialogues.py --model qwen3.5:4b
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "ast_dash", "llm_report")))

import derive_home_dynamics as H                                   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                                  # pragma: no cover
    pass


# Each scenario sits in its own room and hour so the shared turn-clustering
# keeps them apart: same room within 120 s is one exchange, and these are hours
# apart in different rooms.
SCENARIOS = {
    "conflict": {
        "room": "Living", "device": "living-1", "hour": 20,
        "blurb": "Couple arguing — disagreement, criticism, unresolved tension",
        "turns": [
            ("Voula", "Πάλι δεν έπλυνες τίποτα. Κάθε μέρα το ίδιο πράγμα."),
            ("Ilyas", "Μη μου μιλάς έτσι. Δούλευα όλη μέρα."),
            ("Voula", "Κι εγώ δούλευα! Αλλά εσύ δεν κάνεις ποτέ τίποτα εδώ μέσα."),
            ("Ilyas", "Είσαι αδύνατη. Πάντα βρίσκεις κάτι να γκρινιάξεις."),
            ("Voula", "Μη με λες αδύνατη. Βαρέθηκα να τα κάνω όλα μόνη μου."),
            ("Ilyas", "Τότε μην τα κάνεις. Κανείς δε σου το ζήτησε."),
        ],
    },
    "connection": {
        "room": "Kitchen", "device": "kitchen-1", "hour": 19,
        "blurb": "Family talking and planning together — cooperation, mutual attention",
        "turns": [
            ("Voula", "Ραφαήλ, θέλεις να με βοηθήσεις με τη σαλάτα;"),
            ("Rafael", "Ναι! Να κόψω εγώ τις ντομάτες;"),
            ("Voula", "Κόψ' τες, αλλά πρόσεχε το μαχαίρι."),
            ("Ilyas", "Τι λέτε να πάμε αύριο στη θάλασσα όλοι μαζί;"),
            ("Rafael", "Ναι ναι! Μπαμπά να πάρουμε και τη μπάλα;"),
            ("Ilyas", "Θα την πάρουμε. Βούλα, εσύ τι λες, βολεύει το πρωί;"),
            ("Voula", "Το πρωί μια χαρά. Να ετοιμάσουμε φαγητό από το βράδυ."),
        ],
    },
    "warmth": {
        "room": "Bedroom", "device": "bed-1", "hour": 21,
        "blurb": "Parent comforting a child — affection, reassurance, praise",
        "turns": [
            ("Rafael", "Μαμά, δεν τα κατάφερα καλά στο διαγώνισμα."),
            ("Voula", "Έλα εδώ. Δεν πειράζει, αγάπη μου."),
            ("Rafael", "Όλοι οι άλλοι γράψανε καλύτερα."),
            ("Voula", "Εσύ διάβασες πολύ και το ξέρω. Είμαι περήφανη για σένα."),
            ("Voula", "Το επόμενο θα πάει καλύτερα. Θα το δούμε μαζί, εντάξει;"),
            ("Rafael", "Εντάξει. Ευχαριστώ μαμά."),
            ("Voula", "Σ' αγαπάω πολύ. Κοιμήσου τώρα ήσυχος."),
        ],
    },
    "withdrawal": {
        "room": "Hall", "device": "hall-1", "hour": 22,
        "blurb": "Family member disengaging — avoidance, rejection, social distance",
        "turns": [
            ("Voula", "Ηλία, θα φάμε. Έρχεσαι;"),
            ("Ilyas", "Όχι."),
            ("Voula", "Δεν έφαγες τίποτα όλη μέρα. Κάτσε μαζί μας λίγο."),
            ("Ilyas", "Άσε με ήσυχο."),
            ("Voula", "Θέλεις να μιλήσουμε για κάτι;"),
            ("Ilyas", "Δε θέλω να μιλήσω. Κλείσε την πόρτα."),
        ],
    },
    # Tension that is then repaired. The repair must not erase the tension:
    # both have to survive into L4 or "conflict load" and "repair rate" can
    # never be measured separately.
    "repair": {
        "room": "Study", "device": "study-1", "hour": 18,
        "blurb": "Argument followed by an apology — tension THEN repair",
        "turns": [
            ("Ilyas", "Είσαι αδύνατη μερικές φορές, στ' αλήθεια."),
            ("Voula", "Αυτό δεν είναι δίκαιο. Δεν σου αξίζει να μου μιλάς έτσι."),
            ("Ilyas", "Έχεις δίκιο. Συγγνώμη, δεν έπρεπε να το πω."),
            ("Voula", "Εντάξει. Ας το αφήσουμε."),
            ("Ilyas", "Ήμουν κουρασμένος, αλλά δεν είναι δικαιολογία. Συγγνώμη."),
        ],
    },
    # A final case: garbled ASR that must NOT produce a confident reading.
    "garbled": {
        "room": "Bathroom", "device": "bath-1", "hour": 23,
        "blurb": "Corrupted ASR — must abstain, not invent meaning",
        "turns": [
            ("Voula", "Με καθίστηκα ο Βουράλης στο πράσινο έβυλο δεν"),
            ("Ilyas", "και έτσι ο Βιωφυλάκης θα το πνιέσει φρέγγα"),
            ("Voula", "τα οποία μπορεί να"),
        ],
    },
}


def parse_dialogue(text, name="custom", room="Kitchen", hour=19):
    """Turn `Speaker: line` text into a scenario.

    Blank lines and lines starting with # are ignored, so a dialogue can be
    pasted in with notes around it. Everything before the first colon is the
    speaker; use a name from household_members for it to count as a resident.
    """
    turns = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        speaker, sep, said = line.partition(":")
        if not sep:
            # A line with no speaker continues the previous turn rather than
            # being dropped, so wrapped pasted text still works.
            if turns:
                turns[-1] = (turns[-1][0], f"{turns[-1][1]} {line}".strip())
            continue
        turns.append((speaker.strip(), said.strip()))
    return {"room": room, "device": f"{name}-1", "hour": hour,
            "blurb": f"custom dialogue ({len(turns)} turns)", "turns": turns}


def check_gate(spec, cfg=None):
    """Would this dialogue reach L1.5? Returns (ok, explanation).

    Worth answering before an interpretation is attempted: the commonest
    surprise with a small test dialogue is silence, and the cause is almost
    always the intelligibility gate rather than anything going wrong.
    """
    cfg = cfg or H.load_config(None)
    thresholds = cfg["thresholds"]
    solo = int(thresholds.get("semantic_min_words", 8))
    pair = int(thresholds.get("semantic_min_words_exchange", 4))
    min_turns = int(thresholds.get("semantic_min_turns_for_exchange", 2))
    turns = spec["turns"]
    words = sum(len(t.split()) for _, t in turns)
    voices = len({s for s, _ in turns})
    if words >= solo:
        return True, f"{words} words clears the {solo}-word bar"
    if len(turns) >= min_turns and voices >= 2 and words >= pair:
        return True, (f"{len(turns)} turns from {voices} voices, {words} words "
                      f"clears the {pair}-word exchange bar")
    return False, (f"{words} words across {len(turns)} turn(s) from {voices} "
                   f"voice(s): needs {solo} words, or {pair} words across "
                   f"{min_turns}+ turns from 2+ voices")


def build_events(scenarios=None):
    """One dashboard-format row per turn, with the diarisation marker inline."""
    rows, event_id = [], 9000
    for name, spec in SCENARIOS.items():
        if scenarios and name not in scenarios:
            continue
        for index, (speaker, text) in enumerate(spec["turns"]):
            event_id += 1
            second = index * 9          # inside the 120 s conversation gap
            stamp = (f"2026-09-01 {spec['hour']:02d}:"
                     f"{second // 60:02d}:{second % 60:02d}")
            rows.append({
                "ID": event_id,
                "Timestamp": stamp,
                "Device": spec["device"],
                "room": spec["room"],
                "Class 1": "Speech", "Prob 1": 0.78,
                "Class 2": "Conversation", "Prob 2": 0.54,
                "transcription": f"[ 00: 00. 00 - 00: 04. 00 ] {speaker}: {text}",
                "emotion": "NEU (0.55)",
                "rms": 0.2,
                "file_name": f"mock_{event_id}.mp3",
                "scenario": name,
            })
    return rows


def run(interpreter=None, scenarios=None):
    cfg = H.load_config(None)
    cfg["device_room_map"] = {s["device"]: s["room"] for s in SCENARIOS.values()}
    events = build_events(scenarios)
    level0 = H.derive_level0(events, cfg)
    level1 = H.derive_level1(level0, cfg)
    level1_5 = H.derive_level1_5(level0, cfg, interpreter=interpreter, cache={})
    level2 = H.derive_level2(level1, cfg)
    level3 = H.derive_level3(level0, level2, cfg, level1_5=level1_5)
    gaps = H.derive_observation_gaps(level0, cfg)
    level4 = H.derive_level4(level0, level2, level3, gaps, cfg)
    return cfg, level0, level1_5, level3, level4


DIMS = H.RELATIONAL_DIMENSIONS


def bar(value, width=22):
    if value is None:
        return "·" * width + "   —"
    filled = int(round(value * width))
    return "█" * filled + "░" * (width - filled) + f"  {value:.2f}"


def report(level1_5, level3, level4):
    room_to_scenario = {s["room"]: name for name, s in SCENARIOS.items()}
    print("\n" + "=" * 78)
    print("L1.5 — WHAT EACH EXCHANGE MEANT")
    print("=" * 78)
    for item in level1_5:
        name = room_to_scenario.get(item.room, item.room)
        spec = SCENARIOS.get(name, {})
        print(f"\n[{name.upper()}]  {spec.get('blurb', '')}")
        print(f"  room={item.room}  turns={item.turn_count}  words={item.word_count}")
        print(f"  status={item.semantic_status}  confidence={item.semantic_confidence}")
        print(f"  participants={item.participants}  target={item.target}")
        print(f"  types={item.interaction_types}")
        if item.evidence:
            print(f"  evidence: {item.evidence}")
        if item.reason:
            print(f"  reason  : {item.reason}")
        for dim in DIMS:
            print(f"    {dim:<11} {bar(item.relational_dimensions.get(dim))}")

    print("\n" + "=" * 78)
    print("L4 — WHAT THE SIX LAMPS WOULD READ")
    print("=" * 78)
    for day in level4:
        semantics = day.get("household_semantics")
        if not semantics:
            print(f"\n{day['date']}: no semantic evidence")
            continue
        print(f"\n{day['date']}  "
              f"interpreted={semantics['interpreted_exchanges']}  "
              f"uninterpretable={semantics['uninterpretable_exchanges']}  "
              f"not_attempted={semantics['not_attempted_exchanges']}")
        print(f"  types seen: {semantics['interaction_types']}")
        for dim in DIMS:
            scored = (semantics.get("dimensions") or {}).get(dim)
            if not scored:
                print(f"    {dim:<11} {bar(None)}")
            else:
                print(f"    {dim:<11} {bar(scored['mean'])}   peak {scored['max']:.2f}"
                      f"  (n={scored['n']})")
        if semantics.get("tension_peaks"):
            print(f"  tension peaks: {semantics['tension_peaks']}")
        for key, pair in (day.get("pair_dynamics") or {}).items():
            ps = pair.get("semantic_summary")
            if not ps or not ps.get("dimensions"):
                continue
            warmth = (ps["dimensions"].get("warmth") or {}).get("mean")
            tension = (ps["dimensions"].get("tension") or {}).get("mean")
            conn = (ps["dimensions"].get("connection") or {}).get("mean")
            print(f"  {key:<18} connection={conn}  warmth={warmth}  tension={tension}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chatgpt", action="store_true",
                    help="interpret with the OpenAI model")
    ap.add_argument("--model", help="interpret with this local Ollama model")
    ap.add_argument("--only", nargs="*", help="run only these scenarios")
    ap.add_argument("--dialogue", metavar="FILE",
                    help="interpret one dialogue of 'Speaker: line' rows "
                         "instead of the built-in scenarios ('-' for stdin)")
    ap.add_argument("--room", default="Kitchen",
                    help="room for --dialogue (default: Kitchen)")
    ap.add_argument("--json", help="also write the full payload here")
    args = ap.parse_args(argv)

    if args.dialogue:
        text = (sys.stdin.read() if args.dialogue == "-"
                else open(args.dialogue, encoding="utf-8").read())
        spec = parse_dialogue(text, room=args.room)
        if not spec["turns"]:
            sys.exit("no 'Speaker: line' rows found in that file")
        # Replaces the built-ins for this run; the scenario table is a module
        # global the rest of the script reads from.
        SCENARIOS.clear()
        SCENARIOS["custom"] = spec
        args.only = ["custom"]
        ok, why = check_gate(spec)
        print(f"dialogue: {len(spec['turns'])} turns in {spec['room']}")
        print(f"gate    : {'PASSES' if ok else 'BLOCKED'} — {why}")
        if not ok:
            print("\nNothing will reach L1.5. Lengthen the dialogue, or give it "
                  "a second speaker.")
            return 1

    interpreter = None
    if args.chatgpt or args.model:
        sys.path.insert(0, os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..")))
        import dash
        dash.Dash(__name__, use_pages=True, pages_folder="")
        from ast_dash.pages import llm_report as LR
        kind, model = ("openai", "gpt-4o-mini") if args.chatgpt \
            else ("ollama", args.model)
        print(f"interpreting with {kind}:{model} ...")
        interpreter = LR.make_semantic_interpreter(
            kind, model,
            on_progress=lambda n: print(f"  exchange {n} ...", flush=True))

    cfg, level0, level1_5, level3, level4 = run(interpreter, args.only)
    report(level1_5, level3, level4)

    if args.json:
        payload = H.build_output(level0, [], [], level3, level4, [], 0,
                                 level1_5=level1_5)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
