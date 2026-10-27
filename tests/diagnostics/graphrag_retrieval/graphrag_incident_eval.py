"""Section 3.4: GraphRag asked dated questions, scored against the residents.

    python tests/diagnostics/graphrag_retrieval/graphrag_incident_eval.py --model gpt-4o-mini

The residents have confirmed or rejected every incident the system listed on
the days in the window (graphrag/judgements.py). That makes a ground truth the
questions below can be scored against without anyone judging an answer by eye:

  event      "Was there a <kind> on <date>?"            every day x every kind
  person     "Was <person> involved in a quarrel ..."   every day x every resident
  relational "Did <A> and <B> quarrel on <date>?"       every day x every pair
  unread     the event question on days never read      must not be answered

A question is POSITIVE when at least one incident of that kind (with that
person, or that pair) was confirmed on that day, and NEGATIVE otherwise -- so
the negative queries are not invented: they are the days on which the thing
asked about did not happen.

Each question goes through the whole system: the planner (CLASSIFY), the
engine (EXECUTE) and the narrator (INTERPRET). Recorded per question: the plan,
the engine's verdict, the narrator's verdict, and whether every clock time and
every resident the prose names is one the retrieved rows contain.

What is sent to the model: the question, and the retrieved incident rows with
a few of their lines. Nothing is written to the store.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from graphrag import judgements                                    # noqa: E402
from graphrag.engine import Graph, execute                         # noqa: E402
from graphrag.llm import DEFAULT_HOST, narrate, plan               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
KINDS = {"quarrel": "a quarrel", "complaint": "a complaint", "worry": "a worry voiced",
         "warm_moment": "a warm moment", "reconciliation": "a reconciliation",
         "rebuff": "a rebuff"}
PEOPLE = ["Ilyas", "Voula", "Rafael"]


def said(day):
    when = datetime.strptime(day, "%Y-%m-%d")
    return f"{when.day} {when:%B %Y}"


def truth():
    """day -> kind -> [(participants, verdict)] from the standing judgements."""
    out = defaultdict(lambda: defaultdict(list))
    for line in judgements.standing().values():
        inc = line["incident"]
        out[inc["day"]][inc["kind"]].append(
            (set(inc["participants"] or []), line["verdict"], inc["start"], inc["end"]))
    return out


def questions(days, unread, gold):
    qs = []
    for day in days:
        for kind, phrase in KINDS.items():
            hits = [h for h in gold[day][kind] if h[1] == "confirmed"]
            qs.append({"axis": "event", "day": day, "kind": kind, "person": None,
                       "pair": None, "truth": "yes" if hits else "no",
                       "q": f"Was there {phrase} on {said(day)}? When did it start "
                            f"and finish, and between whom?"})
        quarrels = [h for h in gold[day]["quarrel"] if h[1] == "confirmed"]
        for person in PEOPLE:
            qs.append({"axis": "person", "day": day, "kind": "quarrel", "person": person,
                       "pair": None,
                       "truth": "yes" if any(person in h[0] for h in quarrels) else "no",
                       "q": f"Was {person} involved in a quarrel on {said(day)}?"})
        for a, b in itertools.combinations(PEOPLE, 2):
            qs.append({"axis": "relational", "day": day, "kind": "quarrel", "person": None,
                       "pair": [a, b],
                       "truth": "yes" if any({a, b} <= h[0] for h in quarrels) else "no",
                       "q": f"Did {a} and {b} quarrel with each other on {said(day)}?"})
    for day in unread:
        qs.append({"axis": "unread", "day": day, "kind": "quarrel", "person": None,
                   "pair": None, "truth": "unknown",
                   "q": f"Was there a quarrel on {said(day)}? When did it start and finish?"})
    return qs


def verdict_of(text):
    """yes / no / unknown / unclear, from how an answer or an assessment opens."""
    head = " ".join((text or "").strip().split())[:260].lower()
    if re.match(r"(nothing can be said|it cannot be said|cannot be determined|"
                r"there is no way to (say|know)|.{0,80}(never|not) (been )?read)", head):
        return "unknown"
    if re.match(r"yes\b", head):
        return "yes"
    if re.match(r"no\b", head) or re.search(
            r"\b(no|not any) (recorded |evidence of |record of )?"
            r"(quarrel|complaint|worry|worries|warm moment|reconciliation|rebuff|incident)",
            head) or re.search(r"\b(was|were|is|are) (not|no)\b|did not\b|there (was|is|were) no\b", head):
        return "no"
    if re.search(r"\b(was|were) (involved|a |an )|\bquarrel(led|ed)\b|occurred|took place", head):
        return "yes"
    return "unclear"


def run_one(g, item, model, host):
    chosen = plan(item["q"], g.people, g.days, model, host, roles=g.data.get("roles"))
    result = execute(g, chosen)
    rows = result.get("incidents") or []
    answer = narrate(item["q"], result, model, host)

    args = chosen.get("arguments") or {}
    plan_ok = (chosen.get("operation") == "incidents" and args.get("kind") == item["kind"]
               and args.get("day") == item["day"]
               and (item["person"] is None or args.get("person") == item["person"]))
    # What the engine's rows say, read for the question that was asked: a pair
    # question is answered by a row only if both of them are in it.
    scoped = [r for r in rows
              if (not item["pair"] or set(item["pair"]) <= set(r["participants"]))
              and (not item["person"] or item["person"] in r["participants"])]
    never_read = bool(result.get("days_never_read_for_incidents")) and \
        not result.get("days_read_for_incidents")
    engine = "unknown" if never_read else ("yes" if scoped else "no")

    clocks = set(re.findall(r"\b([01]\d|2[0-3]):([0-5]\d)", answer))
    known = {(r[k][:2], r[k][3:5]) for r in rows for k in ("start", "end")}
    named = {p for p in PEOPLE if re.search(rf"\b{p}\b", answer)}
    allowed = set().union(*[set(r["participants"]) for r in rows]) if rows else set()
    allowed |= set(filter(None, [item["person"]])) | set(item["pair"] or [])
    return {**item, "operation": chosen.get("operation"), "arguments": args,
            "plan_ok": plan_ok, "rows": len(rows), "rows_in_scope": len(scoped),
            "engine": engine, "narrator": verdict_of(answer), "answer": answer,
            "assessment": result.get("assessment"),
            "times_named": len(clocks), "times_ungrounded": len(clocks - known),
            "people_ungrounded": sorted(named - allowed)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--unread", nargs="*", default=["2026-09-26", "2026-09-27", "2026-09-28"],
                    help="days with no incident list, for the questions that must not be answered")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(HERE, "graphrag_incident_eval.json"))
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    gold = truth()
    days = sorted(gold)
    g = Graph.load(days=sorted(set(days) | set(args.unread)))
    unread = [d for d in args.unread if d not in g.incident_layer]
    items = questions(days, unread, gold)
    print(f"{len(days)} judged day(s) {days[0]} .. {days[-1]}, {len(unread)} unread, "
          f"{len(items)} questions, model {args.model}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda it: run_one(g, it, args.model, args.host), items))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"model": args.model, "days": days, "unread": unread,
                   "run_at": datetime.now().isoformat(timespec="seconds"),
                   "results": results}, fh, ensure_ascii=False, indent=1)
    for axis in ("event", "person", "relational", "unread"):
        rs = [r for r in results if r["axis"] == axis]
        if rs:
            print(f"{axis:<11} n={len(rs):>3}  plan ok {sum(r['plan_ok'] for r in rs):>3}  "
                  f"engine right {sum(r['engine'] == r['truth'] for r in rs):>3}  "
                  f"narrator right {sum(r['narrator'] == r['truth'] for r in rs):>3}  "
                  f"unclear {sum(r['narrator'] == 'unclear' for r in rs)}")
    print("wrote", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
