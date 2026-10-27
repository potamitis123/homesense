#!/usr/bin/env python3
"""Score one scripted dialogue that was actually recorded at home.

    python tests/diagnostics/score_recorded.py --scenario 1 \
        --since "2026-09-03 18:30:00" --until "2026-09-03 18:33:00" --chatgpt

    python tests/diagnostics/score_recorded.py --all-rooms \
        --manifest tests/diagnostics/takes.txt

A take that several sentinels heard is only whole across all of them, so
--all-rooms scores every room together and collapses the copies they share.

Reads the events the live pipeline already wrote to ast_audio_db for that
window -- their transcription, speaker and emotion are whatever the running
system produced -- and pushes them through L0 and L1.5 exactly as the report
does. Nothing is re-transcribed: the point is to score the chain as it ran.

What it reports, in the order that matters:

  1. ATTRITION   how much of the performance reached the database at all, and
                 whether it formed ONE exchange or was split
  2. TURNS       each scripted line against what was heard, and who got it
  3. READING     the six dimensions, beside the text baseline for the same
                 script and beside the group this scenario belongs to
  4. VERDICT     whether the group's expected signature survived the recording

Attrition is separated first and deliberately: a line the microphones never
delivered is a recognition failure, not a misreading, and mixing the two would
let an ASR miss look like a semantic error.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ast_dash", "llm_report"))
sys.path.insert(0, HERE)

import derive_home_dynamics as H                                   # noqa: E402
import run_dialogue_suite as S                                     # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                                  # pragma: no cover
    pass

SOURCE_DB = os.path.join(ROOT, "ast_audio_db", "ast_audio.db")
DB_TO_DASHBOARD = {
    "id": "ID", "date": "Timestamp", "device_name": "Device",
    "sound1": "Class 1", "conf_sound1": "Prob 1",
    "sound2": "Class 2", "conf_sound2": "Prob 2",
    "sound3": "Class 3", "conf_sound3": "Prob 3",
    "rms": "rms", "transcription": "transcription",
    "emotion": "emotion", "room": "room", "file_name": "file_name",
}
DIMS = H.RELATIONAL_DIMENSIONS


def load_window(since, until, room=None):
    """Events the live pipeline wrote in this window, read-only."""
    conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cols = ", ".join(DB_TO_DASHBOARD)
    sql = f"SELECT {cols} FROM ast_table WHERE date >= ? AND date <= ?"
    args = [since, until]
    if room:
        sql += " AND room = ?"
        args.append(room)
    rows = conn.execute(sql + " ORDER BY date", args).fetchall()
    conn.close()
    return [{DB_TO_DASHBOARD[k]: r[k] for k in DB_TO_DASHBOARD} for r in rows]


def norm(text):
    lowered = (text or "").lower()
    stripped = "".join(c for c in unicodedata.normalize("NFD", lowered)
                       if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", stripped)).strip()


def align(scripted, heard):
    """Greedy best-match of scripted lines to transcribed turns.

    Greedy rather than a full alignment because the sentinel chunks on its own
    trigger, not on turn boundaries: lines merge, split and go missing, and an
    order-preserving alignment would cascade one drop into every line after it.
    """
    pool = list(enumerate(heard))
    out = []
    for speaker, text, _emotion in scripted:
        target = norm(text)
        best, best_score = None, 0.0
        for idx, turn in pool:
            score = difflib.SequenceMatcher(None, target, norm(turn["text"])).ratio()
            if score > best_score:
                best, best_score = (idx, turn), score
        if best and best_score >= 0.45:
            pool = [(i, t) for i, t in pool if i != best[0]]
            out.append((speaker, text, best[1], best_score))
        else:
            out.append((speaker, text, None, 0.0))
    return out, [t for _i, t in pool]


def load_manifest(path):
    """One recorded take per line: scenario, start, end, room, [distance].

        # scenario  start                  end                    room     dist
        1   2026-09-04 13:00:00   2026-09-04 13:01:10   Studio   NEAR
        2   2026-09-04 13:01:40   2026-09-04 13:02:50   Studio   NEAR

    Whitespace- or comma-separated; the two timestamps are recognised by shape
    so the times may contain spaces. Blank lines and # comments are ignored.
    """
    STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
    takes = []
    for lineno, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.split("#")[0].strip()
        if not line:
            continue
        stamps = STAMP.findall(line)
        if len(stamps) < 2:
            print(f"  manifest line {lineno} skipped (needs two timestamps): {line[:60]}")
            continue
        head = line[:line.index(stamps[0])].strip().rstrip(",").strip()
        tail = line[line.index(stamps[1]) + len(stamps[1]):].strip()
        rest = [p for p in re.split(r"[,\s]+", tail) if p]
        try:
            scenario = int(re.split(r"[,\s]+", head)[0])
        except (ValueError, IndexError):
            print(f"  manifest line {lineno} skipped (no scenario number): {line[:60]}")
            continue
        takes.append({
            "scenario": scenario,
            "since": stamps[0].replace("T", " "),
            "until": stamps[1].replace("T", " "),
            "room": rest[0] if rest else None,
            "distance": rest[1].upper() if len(rest) > 1 else None,
        })
    return takes


def _wer_tools():
    """Reuse the annotation tool's scoring rather than re-implementing it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ac", os.path.join(HERE, "annotate_components.py"))
    ac = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ac)
    return ac


def _matrix(pairs, rows, cols, title):
    """Print a confusion matrix plus per-class precision/recall."""
    counts = {r: {c: 0 for c in cols} for r in rows}
    for truth, pred in pairs:
        if truth in counts and pred in counts[truth]:
            counts[truth][pred] += 1
    n = sum(sum(r.values()) for r in counts.values())
    if not n:
        print(f"\n  {title}: nothing to score")
        return
    print(f"\n  {title}  (n={n})")
    header = "true \\ pred"
    print(f"    {header:<14}" + "".join(f"{c[:8]:>9}" for c in cols) + "   total")
    for r in rows:
        tot = sum(counts[r].values())
        print(f"    {r[:14]:<14}" + "".join(f"{counts[r][c]:>9}" for c in cols)
              + f"   {tot}")
    correct = sum(counts[r].get(r, 0) for r in rows)
    print(f"    accuracy {correct}/{n} ({100*correct/n:.0f}%)")
    for r in rows:
        tp = counts[r].get(r, 0)
        fn = sum(counts[r].values()) - tp
        fp = sum(counts[o].get(r, 0) for o in rows if o != r)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        print(f"      {r:<12} precision {prec:>5.0%}   recall {rec:>5.0%}   n={tp+fn}")


def report_collective(takes_results, spec, ac):
    """Everything 40 recorded takes can tell you, hardest evidence first."""
    scored = [t for t in takes_results if t.get("record")]

    # -- 1. attrition, by stage -------------------------------------------
    print("\n" + "=" * 78)
    print("A. ATTRITION — how much of the performance survived to L1.5")
    print("=" * 78)
    print(f"  {'#':<4}{'scenario':<30}{'events':>7}{'turns':>7}{'copies':>7}"
          f"{'script':>7}{'clust':>6}{'L1.5':>6}  {'dist':<5}")
    tot_script = tot_heard = tot_copies = 0
    for t in takes_results:
        tot_script += t["scripted"]
        tot_heard += t["heard"]
        tot_copies += t.get("copies", 0)
        print(f"  {t['scenario']:<4}{t['title'][:28]:<30}{t['events']:>7}"
              f"{t['heard']:>7}{t.get('copies', 0):>7}{t['scripted']:>7}"
              f"{t['clusters']:>6}{t['windows']:>6}  {t.get('distance') or '':<5}"
              + ("" if t.get("record") else "   NOT SCORED"))
    print(f"\n  takes scored          : {len(scored)} of {len(takes_results)}")
    print(f"  scripted turns        : {tot_script}")
    print(f"  turns that reached DB : {tot_heard}  "
          f"({100*tot_heard/max(tot_script,1):.0f}%)")
    if tot_copies:
        print(f"  copies collapsed      : {tot_copies}  (a line heard by more than "
              f"one sentinel, counted once)")
    split = [t['scenario'] for t in takes_results if t['clusters'] > 1]
    if split:
        print(f"  takes that SPLIT into >1 exchange: {split}")
        print("    (turns fell outside the 120 s / same-room clustering window)")

    # -- 2. ASR against an exact reference ---------------------------------
    print("\n" + "=" * 78)
    print("B. SPEECH RECOGNITION — against the script, which is exact")
    print("=" * 78)
    pairs = []
    matched = missed = 0
    for t in takes_results:
        for speaker, text, m, _ratio in t["aligned"]:
            if m:
                matched += 1
                pairs.append((ac.normalise(text), ac.normalise(m["text"])))
            else:
                missed += 1
                pairs.append((ac.normalise(text), ""))
    if pairs:
        w = ac.error_rate(pairs, "word")
        ch = ac.error_rate(pairs, "char")
        print(f"  turn recall : {matched}/{matched+missed} "
              f"({100*matched/max(matched+missed,1):.0f}%)")
        print(f"  WER         : {w['rate']:.1%}   ({w['reference_units']} reference words)")
        print(f"  CER         : {ch['rate']:.1%}")
        print(f"  errors      : {w['substitutions']} sub / {w['deletions']} del "
              f"/ {w['insertions']} ins")
        print("  A missed turn counts as a full deletion, which is what it is.")
        print("\n  by speaker -- who the script says spoke the line:")
        for who in ("Ilyas", "Voula", "Rafael"):
            mine = [(ac.normalise(text), ac.normalise(m["text"]) if m else "")
                    for t in takes_results for spk, text, m, _r in t["aligned"]
                    if spk == who]
            if not mine:
                continue
            got = sum(1 for t in takes_results
                      for spk, _x, m, _r in t["aligned"] if spk == who and m)
            wr = ac.error_rate(mine, "word")
            print(f"    {who:<7} recall {got:>3}/{len(mine):<3} "
                  f"({100*got/len(mine):>3.0f}%)   WER {wr['rate']:>6.1%}")
        if any(t.get("copies") for t in takes_results):
            best = [(ac.normalise(text), ac.normalise(m["text"]) if m else "")
                    for t in takes_results for _s, text, m, _r in t["best_case"]]
            got = sum(1 for t in takes_results
                      for _s, _x, m, _r in t["best_case"] if m)
            bw = ac.error_rate(best, "word")
            print(f"\n  ceiling     : recall {got}/{len(best)} "
                  f"({100*got/max(len(best),1):.0f}%), WER {bw['rate']:.1%}"
                  f"  -- each line's best copy, chosen WITH the script")
            print("  The distance from the lines above is what a better choice "
                  "between sentinels' copies could still recover.")

    # -- 3. speaker, ground truth for free ---------------------------------
    print("\n" + "=" * 78)
    print("C. SPEAKER IDENTIFICATION — you wrote who speaks each line")
    print("=" * 78)
    residents = ["Ilyas", "Voula", "Rafael"]
    spk = [(s, (m["speaker"] or "Unknown"))
           for t in takes_results for s, _x, m, _r in t["aligned"] if m]
    _matrix(spk, residents, residents + ["Unknown"], "speaker")

    # Which sentinel's copy each line was scored from. When several heard a
    # take this shows who was heard where -- and so which rooms a report
    # restricted to one sentinel would have lost.
    heard_by = {}
    for t in takes_results:
        for who, _x, m, _r in t["aligned"]:
            if m:
                device = m.get("device") or "?"
                heard_by.setdefault(who, {}).setdefault(device, 0)
                heard_by[who][device] += 1
    devices = sorted({d for row in heard_by.values() for d in row})
    if len(devices) > 1:
        print("\n  lines scored from each sentinel's copy:")
        print(f"    {'':<8}" + "".join(f"{d:>13}" for d in devices))
        for who in residents:
            row = heard_by.get(who, {})
            print(f"    {who:<8}" + "".join(f"{row.get(d, 0):>13}" for d in devices))

    # -- 4. emotion, also free ---------------------------------------------
    print("\n" + "=" * 78)
    print("D. EMOTION — against the tag each line was performed with")
    print("=" * 78)
    emo = [(e, m.get("emotion") or "none")
           for t in takes_results for e, m in t["emotion_pairs"]]
    _matrix(emo, ["happy", "sad", "angry", "neutral"],
            ["happy", "sad", "angry", "neutral", "none"], "emotion")
    print("    The recogniser labels a whole CLIP, so turns sharing a clip share")
    print("    a label. Treat this as clip-level, not turn-level, accuracy.")

    # -- 5. distance, the designed factor ----------------------------------
    by_dist = {}
    for t in takes_results:
        if t.get("distance"):
            by_dist.setdefault(t["distance"], []).append(t)
    if len(by_dist) > 1:
        print("\n" + "=" * 78)
        print("E. DISTANCE — the condition the sessions were designed around")
        print("=" * 78)
        print(f"  {'condition':<10}{'takes':>7}{'turn recall':>13}{'reached L1.5':>14}")
        for d, group in sorted(by_dist.items()):
            s = sum(x["scripted"] for x in group)
            h = sum(x["heard"] for x in group)
            w = sum(1 for x in group if x["windows"])
            print(f"  {d:<10}{len(group):>7}{f'{100*h/max(s,1):.0f}%':>13}"
                  f"{f'{w}/{len(group)}':>14}")

    # -- 6. the semantic layer, via the group machinery --------------------
    if spec and scored:
        print("\n" + "=" * 78)
        print("F. SEMANTIC READING — by group")
        print("=" * 78)
        results = [(t["scenario"], t["title"], "", t["record"]) for t in scored]
        S.score_groups(results, spec)


def score_one(scen, since, until, room, cfg, spec, interpreter,
              baseline_rows=None, verbose=True, distance=None):
    """One recorded take, start to finish.

    Returns everything the collective report needs and prints the per-take
    detail only when verbose, so forty takes do not bury their own summary.
    """
    number, title, scripted, _dynamic = scen
    gid = next((g for g, v in (spec or {}).get("groups", {}).items()
                if number in v["scenarios"]), None)

    if verbose:
        print("=" * 78)
        print(f"SCENARIO {number} — {title}")
        if gid:
            print(f"group {gid} {spec['groups'][gid]['name']}   expected: "
                  f"{spec['groups'][gid].get('expect')}")
        print(f"window {since}  ..  {until}" + (f"   room={room}" if room else ""))
        print("=" * 78)

    events = load_window(since, until, room)
    level0 = H.derive_level0(events, cfg)
    clusters = H.cluster_speech_turns(level0, cfg)
    windows = H.build_semantic_windows(level0, cfg)
    # The report's own turns, so what is scored is what the report reads:
    # every copy, kept for the ceiling below, then the report's own merge.
    raw = H._collect_turns(level0, cfg, merge=False)
    if cfg.get("thresholds", {}).get("cross_sentinel_merge", True):
        kept, merged_pairs = H.merge_cross_sentinel_turns(raw, cfg)
    else:
        kept, merged_pairs = raw, []

    def as_row(turn):
        return {"speaker": turn.speaker, "text": turn.text or "",
                "event": turn.event_id, "time": H._iso(turn.start),
                "device": turn.device, "start": turn.start, "end": turn.end,
                # The recogniser labels a clip, not a turn, so turns from
                # one clip share an emotion. Carried so the collective
                # report can say so rather than imply turn-level accuracy.
                "emotion": (turn.emotion or {}).get("label")}

    every_copy = [as_row(t) for t in raw if (t.text or "").strip()]
    heard_turns = [as_row(t) for t in kept if (t.text or "").strip()]
    merged = [(as_row(a), as_row(b)) for a, b in merged_pairs]
    aligned, extra = align(scripted, heard_turns)
    # What the alignment reaches when it may pick each line's best copy. Only
    # possible with the script in hand, so it is a ceiling for how much a
    # smarter choice between copies could recover, not a score.
    best_case = align(scripted, every_copy)[0] if merged else aligned
    emotion_pairs = [(sc[2], m) for sc, (_s, _t, m, _r) in zip(scripted, aligned)
                     if m and sc[2]]

    out = {"scenario": number, "title": title, "group": gid,
           "events": len(events), "heard": len(heard_turns),
           "copies": len(merged), "merged": merged, "best_case": best_case,
           "scripted": len(scripted), "clusters": len(clusters),
           "windows": len(windows), "aligned": aligned, "extra": extra,
           "emotion_pairs": emotion_pairs, "record": None,
           "distance": distance}

    if verbose:
        print("\n1. ATTRITION")
        print(f"  events in window        : {len(events)}")
        print(f"  with any speech         : "
              f"{sum(1 for o in level0 if o.speech.get('has_speech'))}")
        print(f"  transcribed turns       : {len(heard_turns)}   "
              f"(scripted: {len(scripted)})")
        print(f"  turn clusters formed    : {len(clusters)}"
              + ("   <- one exchange, good" if len(clusters) == 1 else
                 "   <- SPLIT: turns fell outside the 120 s / same-room window"))
        print(f"  reached L1.5            : {len(windows)}")

        print("\n2. TURNS — scripted against heard")
        for speaker, text, m, ratio in aligned:
            if m:
                flag = "OK " if (m["speaker"] or "") == speaker else "SPK"
                print(f"  {flag} {speaker:<7} {text[:44]!r}")
                print(f"      heard as {str(m['speaker']):<7} {m['text'][:44]!r}"
                      f"  (match {ratio:.2f})")
            else:
                print(f"  --  {speaker:<7} {text[:44]!r}   NOT HEARD")
        for t in extra:
            print(f"  +   {str(t['speaker']):<7} {t['text'][:44]!r}   (not in script)")
        for dropped, kept_copy in merged:
            print(f"  =   {str(dropped['device']):<10} {dropped['text'][:40]!r}"
                  f"   same line as {kept_copy['device']}'s, collapsed")
        hits = sum(1 for _s, _t, m, _r in aligned if m)
        right = sum(1 for sp, _t, m, _r in aligned if m and (m["speaker"] or "") == sp)
        print(f"\n  turn recall      : {hits}/{len(scripted)} "
              f"({100*hits/max(len(scripted),1):.0f}%)")
        print(f"  speaker correct  : {right}/{max(hits,1)}"
              + ("  <- free ground truth, you wrote who speaks" if hits else ""))

    # With every room scored, read the take as ONE exchange. The manifest says
    # a take is one conversation; the report still splits a conversation by
    # room (see cluster_speech_turns), and the attrition above counts those
    # pieces. Reading only the first piece -- records[0] below -- would score
    # a fragment. One exchange across rooms measures what this section is
    # for: how much meaning survived recognition, not where the split fell.
    if room is None:
        whole = []
        for obs in level0:
            joined = copy.copy(obs)
            joined.room = "(all sentinels)"
            whole.append(joined)
        level0, windows = whole, H.build_semantic_windows(whole, cfg)

    if not events or not windows:
        if verbose:
            print("\n  Nothing reached L1.5 — a recognition failure, not a "
                  "misclassification.\n")
        return out

    records = H.derive_level1_5(level0, cfg, interpreter=interpreter, cache={})
    rec = records[0] if records else None
    if rec is None or rec.semantic_status not in H.SEMANTIC_USABLE:
        if verbose:
            print(f"\n3. READING\n  status: "
                  f"{rec.semantic_status if rec else 'no exchange'} — "
                  f"{rec.reason if rec else ''}")
        return out
    out["record"] = rec

    if verbose:
        baseline = {}
        for row in (baseline_rows or []):
            if row.get("n") == number:
                baseline = row.get("dimensions") or {}
        print("\n3. READING")
        print(f"  status={rec.semantic_status}  confidence={rec.semantic_confidence}")
        print(f"  types={rec.interaction_types}   participants={rec.participants}")
        print(f"  evidence: {rec.evidence}")
        print(f"\n  {'dimension':<12}{'recorded':>10}{'text run':>11}")
        for d in DIMS:
            got = rec.relational_dimensions.get(d)
            base = baseline.get(d)
            print(f"  {d:<12}{('—' if got is None else f'{got:.2f}'):>10}"
                  f"{('—' if base is None else f'{base:.2f}'):>11}")
        if gid and spec:
            print("\n4. VERDICT — does the group's expected signature survive?")
            for dim, band in (spec["groups"][gid].get("expect") or {}).items():
                got = rec.relational_dimensions.get(dim)
                lo, hi = spec["bands"][band]
                if got is None:
                    print(f"  ?    {dim:<12} not scored (expected {band})")
                    continue
                ok = lo <= got <= hi
                print(f"  {'PASS' if ok else 'FAIL'} {dim:<12} {got:.2f} "
                      f"(expected {band} {lo}-{hi})")
            print("\n  Bands were calibrated on clean text and compress when "
                  "recorded.\n  Compare a FAIL against the text-run column "
                  "before calling it a semantic error.")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", type=int)
    ap.add_argument("--manifest",
                    help="txt file of takes: scenario, start, end, room, "
                         "[distance] -- scores all of them and reports "
                         "collective metrics")
    ap.add_argument("--since", help='"YYYY-MM-DD HH:MM:SS"')
    ap.add_argument("--until", help='"YYYY-MM-DD HH:MM:SS"')
    ap.add_argument("--last", type=float,
                    help="minutes back from now, instead of --since/--until")
    ap.add_argument("--room", help="only events from this room, e.g. Studio")
    ap.add_argument("--all-rooms", action="store_true",
                    help="score every sentinel together, ignoring the room in "
                         "the manifest -- for takes several sentinels heard; "
                         "the copies they share are collapsed first")
    ap.add_argument("--file", default=os.path.join(HERE, "dialogues_gr.txt"))
    ap.add_argument("--groups", default=os.path.join(HERE, "dialogue_groups.json"))
    ap.add_argument("--baseline", help="a text-run results JSON to compare against")
    ap.add_argument("--chatgpt", action="store_true")
    ap.add_argument("--model", help="local Ollama model instead")
    ap.add_argument("--json", help="write per-take results here")
    args = ap.parse_args(argv)

    scenarios = {s[0]: s for s in S.parse_file(args.file)}
    spec = S.load_groups(args.groups) if os.path.exists(args.groups) else None
    cfg = H.load_config(os.path.join(ROOT, "ast_dash", "llm_report",
                                     "home_dynamics_config.json"))
    baseline_rows = []
    if args.baseline and os.path.exists(args.baseline):
        baseline_rows = json.load(open(args.baseline, encoding="utf-8"))

    interpreter = None
    if args.chatgpt or args.model:
        import dash
        dash.Dash(__name__, use_pages=True, pages_folder="")
        from ast_dash.pages import llm_report as LR
        kind, model = (("openai", "gpt-4o-mini") if args.chatgpt
                       else ("ollama", args.model))
        interpreter = LR.make_semantic_interpreter(kind, model)
        print(f"interpreting with {kind}:{model}")

    if args.manifest:
        takes = load_manifest(args.manifest)
        print(f"manifest: {len(takes)} take(s) from "
              f"{os.path.basename(args.manifest)}")
        missing = [t["scenario"] for t in takes if t["scenario"] not in scenarios]
        if missing:
            print(f"  scenarios not in the script file, skipped: {missing}")
        results = []
        for t in takes:
            if t["scenario"] not in scenarios:
                continue
            print(f"  scoring #{t['scenario']} ...", flush=True)
            results.append(score_one(
                scenarios[t["scenario"]], t["since"], t["until"],
                None if args.all_rooms else (t["room"] or args.room),
                cfg, spec, interpreter,
                baseline_rows, verbose=False, distance=t.get("distance")))
        report_collective(results, spec, _wer_tools())
        if args.json:
            payload = []
            for r in results:
                row = {k: v for k, v in r.items()
                       if k not in ("record", "aligned", "extra", "emotion_pairs",
                                    "merged", "best_case")}
                row["dimensions"] = (r["record"].relational_dimensions
                                     if r["record"] else {})
                row["types"] = (r["record"].interaction_types
                                if r["record"] else [])
                payload.append(row)
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            print(f"\nwrote {args.json}")
        return 0

    if not args.scenario:
        ap.error("give --scenario, or --manifest for a whole session")
    if args.last:
        end = datetime.now()
        since = (end - timedelta(minutes=args.last)).strftime("%Y-%m-%d %H:%M:%S")
        until = end.strftime("%Y-%m-%d %H:%M:%S")
    elif args.since and args.until:
        since, until = args.since, args.until
    else:
        ap.error("give --since and --until, or --last N")
    if args.scenario not in scenarios:
        ap.error(f"scenario {args.scenario} not in {os.path.basename(args.file)}")
    score_one(scenarios[args.scenario], since, until,
              None if args.all_rooms else args.room, cfg, spec,
              interpreter, baseline_rows, verbose=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
