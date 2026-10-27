#!/usr/bin/env python3
"""Run the labelled dialogue set through L1.5 and score the result.

dialogues.txt carries a `Dynamic:` line under each scenario giving the intended
reading. That line is GROUND TRUTH and is stripped before anything reaches the
model -- otherwise the answer travels in the prompt and the exercise measures
nothing. `--show-input` prints exactly what was sent, so that stays checkable
rather than merely asserted.

    python tests/diagnostics/run_dialogue_suite.py --chatgpt
    python tests/diagnostics/run_dialogue_suite.py --chatgpt --only 16 17 20 21
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ast_dash", "llm_report"))

import derive_home_dynamics as H                                   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                                  # pragma: no cover
    pass

DIMS = H.RELATIONAL_DIMENSIONS
# "Voula [angry]: text" -- the bracket is a performance direction for the
# reader and ground truth for the emotion model. It is captured here and kept
# out of the text, so it never travels to the interpreter as a hint.
TURN_RE = re.compile(
    r"^([A-Za-zΆ-ώ][\w'’\-]{0,20})\s*(?:\[\s*(\w+)\s*\])?\s*:\s*(.+)$")
EMOTIONS = {"happy", "sad", "angry", "neutral"}
_EMO_CODE = {"happy": "HAP", "sad": "SAD", "angry": "ANG",
             "neutral": "NEU"}

# Words that would turn an observation into a clinical claim. The prompt forbids
# them; scenarios 9-11 are the cases that would tempt a model into them.
CLINICAL = ("abuse", "abusive", "narcissis", "depress", "anxiet", "trauma",
            "disorder", "patholog", "toxic", "gaslight", "victim", "perpetrator")


def parse_file(path):
    """[(number, title, turns, dynamic_label)] with the label kept OUT of turns."""
    text = open(path, encoding="utf-8").read()
    out = []
    for block in re.split(r"^###\s*", text, flags=re.M)[1:]:
        lines = [l.rstrip() for l in block.splitlines()]
        heading = lines[0].strip()
        number = int(re.match(r"(\d+)", heading).group(1)) if re.match(r"\d", heading) else len(out) + 1
        title = re.sub(r"\s*\[[^\]]*\]\s*$", "",
                       re.sub(r"^\d+\.\s*", "", heading)).strip()
        turns, dynamic = [], ""
        for line in lines[1:]:
            if line.lower().startswith("dynamic"):
                dynamic = line.split(":", 1)[1].strip()
                continue                    # ground truth: never sent
            m = TURN_RE.match(line)
            if m and m.group(1).strip().lower() not in ("dynamic", "speakers", "note"):
                emotion = (m.group(2) or "").lower()
                turns.append((m.group(1).strip(),
                              m.group(3).strip(),
                              emotion if emotion in EMOTIONS else None))
        if turns:
            out.append((number, title, turns, dynamic))
    return out


def as_events(turns, number, leak_emotion=False):
    """Dashboard rows for one scenario, spaced so they form a single exchange.

    The performed emotion is NOT put into the payload by default. It is ground
    truth for the emotion model, and the real pipeline's emotion classifier
    measured 31% macro-F1 -- so handing the interpreter the true label would
    flatter this run and, worse, confound the text-versus-recorded comparison
    these scripts exist to make. `leak_emotion` exposes it for the separate
    question of how much the interpreter leans on that channel at all.
    """
    room = f"Room{number:02d}"                 # own room: scenarios never merge
    rows = []
    for index, (speaker, text, _emotion) in enumerate(turns):
        second = index * 9                     # inside the 120 s cluster gap
        rows.append({
            "ID": number * 100 + index,
            "Timestamp": f"2026-09-03 {8 + number // 8:02d}:"
                         f"{second // 60:02d}:{second % 60:02d}",
            "Device": f"dev{number}", "room": room,
            "Class 1": "Speech", "Prob 1": 0.8,
            "transcription": f"[ 00: 00. 00 - 00: 04. 00 ] {speaker}: {text}",
            "emotion": ((_EMO_CODE.get(_emotion, "NEU") + " (0.60)")
                       if leak_emotion else "NEU (0.55)"),
            "rms": 0.2,
        })
    return rows


def interpret(scenario, interpreter, cfg, show_input=False,
              leak_emotion=False):
    number, title, turns, dynamic = scenario
    level0 = H.derive_level0(as_events(turns, number, leak_emotion), cfg)
    if show_input:
        windows = H.build_semantic_windows(level0, cfg)
        if windows:
            print(json.dumps(H.window_payload(windows[0]), ensure_ascii=False,
                             indent=2))
    records = H.derive_level1_5(level0, cfg, interpreter=interpreter, cache={})
    return number, title, dynamic, (records[0] if records else None)


def bar(value, width=10):
    if value is None:
        return "·" * width
    return "█" * int(round(value * width)) + "░" * (width - int(round(value * width)))


# ── scoring by group ────────────────────────────────────────────────
#
# The four measures, in increasing strictness. Order matters: attrition is
# separated out FIRST, because a scenario the pipeline never heard is a
# recognition failure, not a misclassification, and letting the two mix would
# let a 36% ASR-recall hole present itself as semantic error.


def load_groups(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _vector(rec, dims):
    if rec is None:
        return None
    return {d: rec.relational_dimensions.get(d) for d in dims}


def _mean(values):
    got = [v for v in values if v is not None]
    return (sum(got) / len(got)) if got else None


def _distance(a, b):
    """Euclidean over the dimensions BOTH vectors scored, per-dimension mean.

    Normalised by the count of shared dimensions rather than summed, because
    L1.5 abstains per dimension: a scenario that scored two dimensions would
    otherwise always look closer than one that scored six.
    """
    shared = [d for d in a if a[d] is not None and b.get(d) is not None]
    if not shared:
        return None
    return (sum((a[d] - b[d]) ** 2 for d in shared) / len(shared)) ** 0.5


def score_groups(results, spec):
    dims = spec["dimensions"]
    groups = spec["groups"]
    of_scenario = {n: (t, r) for n, t, _d, r in results}
    member_of = {n: gid for gid, g in groups.items() for n in g["scenarios"]}

    # -- 1. attrition, reported before anything is scored ----------------
    heard, lost = {}, []
    for n, (title, rec) in of_scenario.items():
        if rec is None or rec.semantic_status not in H.SEMANTIC_USABLE:
            lost.append((n, title, rec.semantic_status if rec else "no exchange"))
        else:
            heard[n] = _vector(rec, dims)
    print("\n" + "=" * 78)
    print("1. ATTRITION — reached L1.5 with a usable reading")
    print("=" * 78)
    print(f"  scenarios scored : {len(heard)} of {len(of_scenario)}")
    if lost:
        print(f"  not scored       : {len(lost)}")
        for n, title, why in lost:
            print(f"      #{n:<3} {title[:38]:<40} {why}")
        print("  These are recognition failures, excluded from every measure below.")

    # -- 2. per-group means ----------------------------------------------
    print("\n" + "=" * 78)
    print("2. GROUP MEANS")
    print("=" * 78)
    print(f"  {'group':<28}{'n':>3}" + "".join(f"{d[:4]:>8}" for d in dims))
    means, thin = {}, []
    for gid, g in groups.items():
        vecs = [heard[n] for n in g["scenarios"] if n in heard]
        means[gid] = {d: _mean([v[d] for v in vecs]) for d in dims}
        cells = "".join(
            f"{means[gid][d]:>8.2f}" if means[gid][d] is not None else f"{'—':>8}"
            for d in dims)
        # A group holding one take has no mean, only a reading. Marked in the
        # row itself: the column header says "means", and a single value
        # printed under it invites being read as one.
        mark = ""
        if len(vecs) == 1:
            mark = "   <- one take, not a mean"
            thin.append(gid)
        elif len(vecs) == 0:
            mark = "   <- no takes"
        elif len(vecs) < 3:
            mark = f"   <- only {len(vecs)}"
            thin.append(gid)
        print(f"  {gid + ' ' + g['name']:<28}{len(vecs):>3}{cells}{mark}")
    if thin:
        print(f"\n  Groups with fewer than 3 takes: {', '.join(thin)}.")
        print("  Sections 3-5 below compare these numbers against each other.")
        print("  With one take per group they compare single readings, which is")
        print("  an anecdote, not a measurement. Sections A-D score turns and")
        print("  are already meaningful at this size.")

    # -- 3. orderings and contrasts, threshold-free ----------------------
    print("\n" + "=" * 78)
    print("3. ORDERINGS — must hold whatever the absolute scale")
    print("=" * 78)
    ok = 0
    for rule in spec.get("orderings", []):
        d, hi, lo = rule["dimension"], rule["greater"], rule["less"]
        a, b = means.get(hi, {}).get(d), means.get(lo, {}).get(d)
        if a is None or b is None:
            print(f"  SKIP  {d:<11} {hi} > {lo}   (no evidence)")
            continue
        good = a > b
        ok += good
        print(f"  {'PASS' if good else 'FAIL'}  {d:<11} {hi} > {lo}"
              f"   {a:.2f} vs {b:.2f}   {rule['note']}")

    print("\n" + "=" * 78)
    print("4. PAIRED CONTRASTS — the strongest test, no thresholds at all")
    print("=" * 78)
    OPS = {"<": lambda x, y: x < y, ">": lambda x, y: x > y,
           "<=": lambda x, y: x <= y, ">=": lambda x, y: x >= y}
    for c in spec.get("contrasts", []):
        va, vb = heard.get(c["a"]), heard.get(c["b"])
        if va is None or vb is None:
            print(f"  SKIP  #{c['a']} vs #{c['b']}  {c['name']} (not scored)")
            continue
        parts, all_ok = [], True
        for d, op in c["expect"].items():
            x, y = va.get(d), vb.get(d)
            if x is None or y is None:
                parts.append(f"{d} —"); all_ok = False; continue
            good = OPS[op](x, y)
            all_ok &= good
            parts.append(f"{d} {x:.2f}{op}{y:.2f}{'' if good else ' X'}")
        print(f"  {'PASS' if all_ok else 'FAIL'}  #{c['a']:<2} vs #{c['b']:<2} "
              f"{c['name'][:38]:<40} {'  '.join(parts)}")

    # -- 5. nearest-centroid confusion, leave-one-out --------------------
    # Leave-one-out because the centroids come from the same run: including a
    # scenario in its own group's centroid would let it classify itself.
    print("\n" + "=" * 78)
    print("5. NEAREST-CENTROID CONFUSION (leave-one-out)")
    print("=" * 78)
    gids = list(groups)
    confusion = {g: {h: 0 for h in gids} for g in gids}
    correct = 0
    for n, vec in heard.items():
        true_g = member_of.get(n)
        best, best_d = None, None
        for gid, g in groups.items():
            peers = [heard[m] for m in g["scenarios"] if m in heard and m != n]
            if not peers:
                continue
            centroid = {d: _mean([p[d] for p in peers]) for d in dims}
            dist = _distance(vec, centroid)
            if dist is not None and (best_d is None or dist < best_d):
                best, best_d = gid, dist
        if best is None:
            continue
        confusion[true_g][best] += 1
        correct += (best == true_g)
    total = sum(sum(r.values()) for r in confusion.values())
    header = "true \\ assigned"
    print(f"  {header:<20}" + "".join(f"{g:>5}" for g in gids) + "   n")
    for g in gids:
        row = confusion[g]
        print(f"  {g + ' ' + groups[g]['name'][:14]:<20}"
              + "".join(f"{row[h]:>5}" for h in gids)
              + f"   {sum(row.values())}")
    if total:
        print(f"\n  group accuracy: {correct}/{total} ({100*correct/total:.0f}%)")
    print("  Group sizes are 1-9, so this is indicative, not a measurement.")

    # -- 6. repair shape --------------------------------------------------
    expected_repair = set(spec.get("repair_expected", []))
    if expected_repair:
        print("\n" + "=" * 78)
        print("6. REPAIR — a tension peak followed by a repair move")
        print("=" * 78)
        found = []
        for n in sorted(expected_repair):
            _t, rec = of_scenario.get(n, (None, None))
            if rec is None:
                continue
            types = set(rec.interaction_types)
            has = bool(types & H.REPAIR_TYPES)
            found.append(has)
            print(f"  {'yes' if has else 'no ':<4} #{n:<3} types={','.join(rec.interaction_types)[:44]}")
        if found:
            print(f"\n  repair labelled on {sum(found)}/{len(found)} scenarios "
                  f"scripted to contain one")
    return means


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.join(HERE, "dialogues.txt"))
    ap.add_argument("--chatgpt", action="store_true")
    ap.add_argument("--model", help="local Ollama model instead")
    ap.add_argument("--only", nargs="*", type=int, help="scenario numbers")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--show-input", action="store_true",
                    help="print the exact payload sent for the first scenario")
    ap.add_argument("--leak-emotion", action="store_true",
                    help="put the performed emotion in the payload "
                         "(off by default: it is ground truth)")
    ap.add_argument("--groups",
                    help="grouping JSON (default: dialogue_groups.json "
                         "beside the dialogue file); pass 'none' to skip")
    ap.add_argument("--json", help="write raw results here")
    args = ap.parse_args(argv)

    scenarios = parse_file(args.file)
    if args.only:
        scenarios = [s for s in scenarios if s[0] in args.only]
    print(f"parsed {len(scenarios)} scenario(s) from {os.path.basename(args.file)}")
    print(f"ground-truth 'Dynamic:' lines stripped from model input\n")

    interpreter = None
    if args.chatgpt or args.model:
        import dash
        dash.Dash(__name__, use_pages=True, pages_folder="")
        from ast_dash.pages import llm_report as LR
        kind, model = ("openai", "gpt-4o-mini") if args.chatgpt else ("ollama", args.model)
        print(f"interpreting with {kind}:{model}, {args.workers} workers\n")
        interpreter = LR.make_semantic_interpreter(kind, model)

    cfg = H.load_config(None)
    if args.show_input and scenarios:
        print("=== exact payload for scenario 1 ===")
        interpret(scenarios[0], None, cfg, show_input=True)
        print("=== end payload ===\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(
            lambda s: interpret(s, interpreter, cfg,
                                leak_emotion=args.leak_emotion), scenarios))
    results.sort(key=lambda r: r[0])

    print(f"{'#':<4}{'scenario':<34}" + "".join(f"{d[:4]:>7}" for d in DIMS)
          + "  types")
    print("-" * 116)
    for number, title, dynamic, rec in results:
        if rec is None:
            print(f"{number:<4}{title[:32]:<34}  (no exchange produced)")
            continue
        cells = "".join(
            f"{rec.relational_dimensions.get(d):>7.2f}"
            if rec.relational_dimensions.get(d) is not None else f"{'—':>7}"
            for d in DIMS)
        print(f"{number:<4}{title[:32]:<34}{cells}  "
              f"{','.join(rec.interaction_types)[:34]}")

    groups_path = args.groups or os.path.join(HERE, "dialogue_groups.json")
    if str(groups_path).lower() != "none" and os.path.exists(groups_path):
        score_groups(results, load_groups(groups_path))
    elif str(groups_path).lower() != "none":
        print(f"\n(no grouping file at {groups_path}; skipped group scoring)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([{
                "n": n, "title": t, "dynamic": d,
                "status": r.semantic_status if r else None,
                "confidence": r.semantic_confidence if r else None,
                "types": r.interaction_types if r else [],
                "dimensions": r.relational_dimensions if r else {},
                "evidence": r.evidence if r else None,
                "participants": r.participants if r else [],
            } for n, t, d, r in results], fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json}")
    return results


if __name__ == "__main__":
    raise SystemExit(0 if main() else 0)
