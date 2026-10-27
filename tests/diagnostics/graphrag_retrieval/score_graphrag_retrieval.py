"""Score graphrag against an annotated slice, on the four retrieval axes.

    python score_graphrag_retrieval.py --slice 20260917_1800 \\
        --answers graphrag_slice_20260917_1800.json

Reads the frozen population written by make_graphrag_slice.py and the file the
sheet downloaded, runs each operation scoped to the slice's day, keeps only
what falls inside the slice, and fills four of the five rows. The fifth --
negative query -- comes from graphrag_negative_probes.py and is merged in if
its output is on disk.

Why the query has to be scoped to the slice
-------------------------------------------
Recall needs the complete relevant set. Over a week that is unobtainable and
top-k retrieval is unmeasurable, which is what section 3.4 discovered the
expensive way. Inside an annotated hour every exchange has a judgement, so
nothing a query returns from it is unjudged and nothing it misses is unknown.
Both numbers exist only because the window is small and complete.

Precision is reported beside the BASE RATE, always
--------------------------------------------------
A retriever returning k random exchanges scores precision equal to the base
rate of the thing it was asked for. In this household warmth at moderate
strength fires on about two thirds of exchanges, so a precision of 0.7 for
warmth would be indistinguishable from chance. Precision without its base rate
is not a measurement, so the lift is printed and a run where the system does
not beat chance is marked.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # pragma: no cover
    pass

import graphrag_axes                                          # noqa: E402

RESIDENTS = ["Ilyas", "Voula", "Rafael"]
# annotation key -> the operation that is supposed to find it
QUESTIONS = [
    ("quarrel", "tension_moments", "When were the quarrels?"),
    ("warm", "warm_moments", "When were the warmest moments?"),
    ("complaint", "complaints", "What does the family complain about?"),
]
# `rebuff` has no operation: withdrawal is the dimension that never fires, and
# a question with no operation behind it belongs in the negative probes, not
# here. It is annotated anyway, because whether such moments OCCUR is the
# prevalence question the dimension's future depends on.
# The ordinal suffix is inside the match; anchoring \b straight after the
# digits misses "September 17th" entirely. See graphrag_negative_probes.
DAY_IN_TEXT = re.compile(
    r"(\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?\b)", re.I)


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def fmt(k, n):
    if not n:
        return "n=0"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k/n:.2f} [{lo:.2f}, {hi:.2f}]"


def returned_items(result):
    """(event id set, when) for each thing an operation offered.

    Operations differ in shape -- some return `moments`, some only `evidence`
    -- so both are read rather than assuming one.
    """
    out = []
    for moment in result.get("moments") or []:
        events = {str(s.get("event_id")) for s in (moment.get("said") or [])
                  if s.get("event_id") is not None}
        if events:
            out.append((events, str(moment.get("when") or "")))
    if out:
        return out
    seen = set()
    for e in result.get("evidence") or []:
        eid = str(e.get("event_id"))
        if eid in seen or eid == "None":
            continue
        seen.add(eid)
        out.append(({eid}, str(e.get("day") or "")))
    return out


def load_slice(label, answers_path):
    frozen = os.path.join(HERE, f"slice.{label}.items.json")
    if not os.path.exists(frozen):
        sys.exit(f"no frozen slice at {frozen}; run make_graphrag_slice.py")
    population = json.load(open(frozen, encoding="utf-8"))
    if not os.path.exists(answers_path):
        sys.exit(f"no annotations at {answers_path}; download them from the sheet")
    coded = json.load(open(answers_path, encoding="utf-8"))
    answers = {a["item_id"]: a for a in coded.get("answers") or []}
    items = population["items"]
    missing = [i["item_id"] for i in items if i["item_id"] not in answers]
    return population, items, answers, missing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--slice", dest="label", required=True)
    ap.add_argument("--answers", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--probes", default=os.path.join(
        HERE, "graphrag_negative_probes.json"))
    ap.add_argument("--k", type=int, default=8,
                    help="how many of the in-slice ranking count as returned "
                         "(default 8, what the operations themselves show)")
    ap.add_argument("--depth", type=int, default=200,
                    help="how deep to ask the operation to rank before the "
                         "slice is cut out of it")
    ap.add_argument("--tsv", action="store_true",
                    help="emit the table tab-separated, for build_docx.py")
    args = ap.parse_args(argv)

    answers_path = args.answers if os.path.isabs(args.answers) \
        else os.path.join(HERE, args.answers)
    population, items, answers, missing = load_slice(args.label, answers_path)
    day, since, until = population["day"], population["from"], population["until"]
    print(f"slice: {day} {since}-{until}  "
          f"{len(items)} exchange(s), {len(answers)} annotated")
    if missing:
        # Recall is only defined over a COMPLETE slice. Reporting it with gaps
        # would understate misses as unknowns.
        print(f"\n{len(missing)} exchange(s) unannotated -- recall is not "
              f"defined until the slice is complete:")
        for item_id in missing[:8]:
            print(f"  {item_id}")
        sys.exit(1)

    from graphrag.engine import Graph, execute
    from graphrag.llm import DEFAULT_HOST, DEFAULT_MODEL, narrate, plan
    model = args.model or DEFAULT_MODEL
    host = args.host or DEFAULT_HOST
    graph = Graph.load(since=day, until=day)

    in_slice = {i["item_id"]: set(i["event_ids"]) for i in items}

    def locate(events):
        """Which annotated exchange, if any, a returned item belongs to."""
        for item_id, have in in_slice.items():
            if events & have:
                return item_id
        return None

    results, detail = {}, {}

    # -- event retrieval, and relational retrieval --------------------------
    hits = misses = returned_total = 0
    rel_hits = rel_total = 0
    lines, temporal_ok, temporal_total = [], 0, 0
    for key, operation, question in QUESTIONS:
        relevant = {i for i, a in answers.items() if a.get(key) == "yes"}
        base = len(relevant) / len(items) if items else 0.0
        # The operations rank a whole day and return their sharpest few. Asking
        # for the day's full ranking and then keeping the part that falls in the
        # annotated hour measures the RANKING inside a window where every item
        # is judged -- which is the only place precision and recall both exist.
        # Taking the operation's own top eight instead would score whichever
        # handful of them happened to land in the hour: on this slice that was
        # one, nought and three of eight.
        result = execute(graph, {"operation": operation,
                                 "arguments": {"top_k": args.depth},
                                 "why": "scored against an annotated slice"})
        offered = returned_items(result)
        inside = [(events, when) for events, when in offered
                  if locate(events)][:args.k]
        good = [1 for events, _ in inside if locate(events) in relevant]
        k = len(inside)
        hits += len(good)
        returned_total += k
        misses += len(relevant - {locate(e) for e, _ in inside})
        lift = (len(good) / k - base) if k else 0.0
        lines.append(
            f"  {key:<10} ranked {len(offered):>3} "
            f"(top {k:>2} inside the slice)   "
            f"precision {fmt(len(good), k)}   base {base:.2f}   "
            f"lift {lift:+.2f}{'  AT OR BELOW CHANCE' if k and lift <= 0 else ''}")
        lines.append(
            f"  {'':<10} relevant in slice {len(relevant):>2}   "
            f"recall {fmt(len(good), len(relevant))}")

        # relational: of what it offered, how much was a real dyadic exchange
        for events, _ in inside:
            item_id = locate(events)
            a = answers.get(item_id) or {}
            rel_total += 1
            if a.get(key) == "yes" and a.get("between") not in (
                    None, "alone", "outside", "unclear"):
                rel_hits += 1

        # temporal: does the prose name a day the rows support?
        try:
            answer = narrate(question, result, model, host)
        except Exception as exc:                              # noqa: BLE001
            print(f"  ({operation}: narration failed, {exc})")
            continue
        named = set(DAY_IN_TEXT.findall(answer.replace("\n", " ")))
        named = {n if isinstance(n, str) else n[0] for n in named}
        row_days = {when[:10] for _e, when in offered if when}
        temporal_total += 1
        if not named or all(any(d.startswith(nd[:7]) or nd in answer
                                for d in row_days) for nd in named):
            temporal_ok += 1
        else:
            print(f"  {key}: prose names {sorted(named)}, rows are "
                  f"{sorted(row_days)}")

    print("\nevent retrieval, per question")
    print("\n".join(lines))
    results["event_retrieval"] = (
        f"precision {fmt(hits, returned_total)} over {len(QUESTIONS)} "
        f"questions; {misses} relevant exchange(s) missed")
    results["relational_retrieval"] = (
        f"{fmt(rel_hits, rel_total)} of returned items were a genuine "
        f"dyadic exchange of the kind asked for")
    results["temporal_retrieval"] = (
        f"{temporal_ok} of {temporal_total} answers named only days their "
        f"rows support")

    # -- person attribution -------------------------------------------------
    said_by = {}
    for exchange in graph.exchanges:
        events = {str(e) for e in (exchange.get("source_event_ids") or [])}
        item_id = locate(events)
        if item_id:
            said_by.setdefault(item_id, set()).update(
                p for p in (exchange.get("participants") or []))
    tp = fp = fn = judged = 0
    for item_id, a in answers.items():
        heard = {h for h in (a.get("heard") or []) if h in RESIDENTS}
        if not heard:
            continue                  # annotator could not tell; not evidence
        judged += 1
        named = said_by.get(item_id, set())
        tp += len(named & heard)
        fp += len(named - heard)
        fn += len(heard - named)
    results["person_attribution"] = (
        f"precision {fmt(tp, tp + fp)}, recall {fmt(tp, tp + fn)} "
        f"over {judged} exchange(s) whose voices were identified")
    detail["person_attribution"] = {"tp": tp, "fp": fp, "fn": fn}

    # -- prevalence, which every number above has to be read against --------
    print("\nprevalence in this slice")
    for key, _op, _q in QUESTIONS + [("rebuff", None, None)]:
        counts = Counter(a.get(key) for a in answers.values())
        yes = counts.get("yes", 0)
        print(f"  {key:<10} {yes:>2} of {len(answers)} "
              f"({100 * yes / max(len(answers), 1):.0f}%)   "
              f"no={counts.get('no', 0)} unclear={counts.get('unclear', 0)}")

    # -- the fifth row, if the probes have run ------------------------------
    if os.path.exists(args.probes):
        probes = json.load(open(args.probes, encoding="utf-8"))
        total = probes.get("total_runs") or 0
        line = f"{probes.get('declined', 0)} of {total} runs declined correctly"
        if probes.get("unscored"):
            line += f"; {probes['unscored']} need reading"
        results["negative_query"] = line

    print("\n" + graphrag_axes.render(results))
    out = os.path.join(HERE, f"graphrag_retrieval.{args.label}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"slice": args.label, "day": day, "from": since,
                   "until": until, "n_exchanges": len(items),
                   "axes": results, "detail": detail}, fh,
                  ensure_ascii=False, indent=2)
    print(f"\nwrote {out}")
    if args.tsv:
        tsv = os.path.join(HERE, f"graphrag_retrieval.{args.label}.tsv")
        with open(tsv, "w", encoding="utf-8") as fh:
            fh.write(graphrag_axes.render(results, tsv=True) + "\n")
        print(f"wrote {tsv}  (paste under a 'Table N.' line in a section .txt)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
