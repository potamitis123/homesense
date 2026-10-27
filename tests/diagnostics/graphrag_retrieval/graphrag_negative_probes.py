"""Ask graphrag things the store provably cannot answer, and see if it says so.

    python graphrag_negative_probes.py --since 2026-09-14 --until 2026-09-19
    python graphrag_negative_probes.py --runs 6 --model gpt-4o-mini --kind openai

This fills one row of the five: "Negative query -- did it correctly state that
the evidence is absent?"

Why this is the cheapest measurement available
----------------------------------------------
It needs no annotation at all. Every other axis needs somebody to listen to
audio; this one needs only a question whose answer is absent BY CONSTRUCTION,
and the store itself says which questions those are. So it runs on every new
day, unattended, for the cost of some local inference.

It is also where the system is known to fail. Section 3.4 put one unanswerable
question to it six times over an identical store: the planner chose the same
operation all six times, and five of the six answers asserted an event that
does not exist, disagreeing with each other about when it happened. Retrieval
and planning were stable; the prose was not. A single run therefore measures
nothing, which is why --runs defaults to six.

Absence is verified, not assumed
--------------------------------
A probe is only negative if the store really lacks the answer, so each one is
checked against the loaded graph before it is asked, and a probe that fails its
own check is dropped rather than scored. The check is printed with the result.

Scoring, and what is left to a human
------------------------------------
Two layers, kept apart, because they fail independently:

* the DETERMINISTIC layer -- did the operation return any rows? This is
  exact.
* the NARRATION layer -- does the prose assert something the rows do not
  support? A regular expression is not a judge, so this scores only the two
  unambiguous cases: an answer that declines in so many words and names no
  day, and an answer that names a specific day or opens with "yes" when no
  rows came back. Anything else is left UNSCORED and printed in full for
  someone to read. Counting an unscored answer as a pass would be the same
  confabulation the probe is looking for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
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

# Names chosen to be ordinary for this household's language and plainly not
# residents. Filtered against the store before use, so if one ever moves in
# the probe drops itself instead of quietly becoming answerable.
OUTSIDERS = ["Sofia", "Dimitris", "Eleni", "Kostas", "Maria"]
# Subjects that would be unmistakable in a transcript if they occurred.
ABSENT_TOPICS = ["skiing", "the dentist", "a funeral", "buying a boat"]
DIMENSIONS = ("tension", "warmth", "joy", "withdrawal")

DECLINES = re.compile(
    r"\b(no evidence|not enough evidence|nothing in the (data|record)|"
    r"no record|no indication|no mention|does not (appear|show|contain)|"
    r"there is no|there are no|cannot (tell|say|determine)|"
    r"no data|not supported|i (don't|do not) (have|see|find)|"
    r"was not (observed|recorded)|no such)\b", re.I)
# The ordinal suffix has to be part of the match, not left outside it: with a
# \b anchored straight after the digits, "September 17th" does not match,
# because 7 and t are both word characters. That let exactly the confabulation
# this harness exists to catch -- "Yes, on the day of September 18th" -- score
# as unremarkable unless it also opened with "yes".
ASSERTS_DAY = re.compile(
    r"(\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)\s+of\s+\w+"
    r"|\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b)",
    re.I)
OPENS_YES = re.compile(r"^\s*(yes|indeed|certainly)\b", re.I)


# ---------------------------------------------------------------------------
# building probes the store cannot answer
# ---------------------------------------------------------------------------

def build_probes(graph):
    """Questions with no answer in this store, each with its absence check."""
    probes = []

    people = set(graph.people)
    for name in OUTSIDERS:
        if name in people:
            continue
        probes.append({
            "probe_id": f"absent_person_{name.lower()}",
            "family": "absent person",
            "question": f"When did {name} argue with anyone in the house?",
            "absent_because": f"{name} is not among the people in the store "
                              f"({', '.join(sorted(people))}).",
            "holds": True,
        })
        break                          # one is enough; they test the same thing

    # A day just outside the loaded window. Plausible, and provably unloaded.
    days = sorted(graph.days)
    if days:
        from datetime import datetime, timedelta
        missing = (datetime.strptime(days[0], "%Y-%m-%d")
                   - timedelta(days=1)).strftime("%Y-%m-%d")
        probes.append({
            "probe_id": "absent_day",
            "family": "absent day",
            "question": f"What did the family talk about on {missing}?",
            "absent_because": f"{missing} is not among the {len(days)} loaded "
                              f"days ({days[0]} .. {days[-1]}).",
            "holds": missing not in graph.days,
        })

    # A dimension the reading layer never once scored above zero. Section 3.4's
    # original negative control was exactly this case for withdrawal.
    for dim in DIMENSIONS:
        scores = [x.get("dimensions", {}).get(dim) for x in graph.exchanges]
        real = [s for s in scores if isinstance(s, (int, float))]
        if real and max(real) <= 0.0:
            probes.append({
                "probe_id": f"absent_dimension_{dim}",
                "family": "absent dimension",
                "question": (f"When did someone show {dim} towards another "
                             f"member of the family?"),
                "absent_because": (f"{dim} is scored 0.0 on all "
                                   f"{len(real)} exchanges in the store."),
                "holds": True,
            })

    # A subject nobody mentions. Checked against every utterance, not a sample.
    text = " ".join((s.get("text") or "").lower() for s in graph.segments)
    for topic in ABSENT_TOPICS:
        head = topic.split()[-1].lower()
        if head in text:
            continue
        probes.append({
            "probe_id": f"absent_topic_{head}",
            "family": "absent topic",
            "question": f"What has the family said about {topic}?",
            "absent_because": f"the word '{head}' occurs in none of the "
                              f"{len(graph.segments)} utterances.",
            "holds": True,
        })
        break

    # A room nothing was ever heard in.
    rooms = {x.get("room") for x in graph.exchanges} | {
        s.get("room") for s in graph.segments}
    for candidate in ("Garage", "Basement", "Attic", "Garden"):
        if candidate in rooms:
            continue
        probes.append({
            "probe_id": f"absent_room_{candidate.lower()}",
            "family": "absent room",
            "question": f"What happened in the {candidate}?",
            "absent_because": f"no utterance or exchange is placed in the "
                              f"{candidate}.",
            "holds": True,
        })
        break

    return [p for p in probes if p["holds"]]


# ---------------------------------------------------------------------------
# scoring one answer
# ---------------------------------------------------------------------------

def score_answer(answer, row_count):
    """DECLINED, ASSERTED or UNSCORED -- never a guess dressed as a verdict."""
    text = (answer or "").strip()
    if not text:
        return "UNSCORED", "empty answer"
    declines = bool(DECLINES.search(text))
    names_day = bool(ASSERTS_DAY.search(text))
    says_yes = bool(OPENS_YES.match(text))
    if declines and not names_day and not says_yes:
        return "DECLINED", "declines and names no day"
    if row_count == 0 and (names_day or says_yes):
        return "ASSERTED", ("names a day with no rows behind it" if names_day
                            else "opens with yes and returned no rows")
    return "UNSCORED", "needs reading"


def row_count_of(result):
    """How much the deterministic layer actually found."""
    for key in ("moments", "evidence", "topics", "utterances", "rows", "days"):
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


# ---------------------------------------------------------------------------
# running them
# ---------------------------------------------------------------------------

def run(since, until, runs, model, host, out_path):
    from graphrag.engine import Graph, execute
    from graphrag.llm import DEFAULT_HOST, DEFAULT_MODEL, narrate, plan

    model = model or DEFAULT_MODEL
    host = host or DEFAULT_HOST
    graph = Graph.load(since=since, until=until)
    print(f"store: {len(graph.days)} day(s), {len(graph.exchanges)} exchange(s), "
          f"{len(graph.segments)} utterance(s), people: {', '.join(graph.people)}")

    probes = build_probes(graph)
    if not probes:
        sys.exit("No probe could be verified absent against this store.")
    print(f"probes: {len(probes)} verified absent, {runs} run(s) each\n")

    records, started = [], time.time()
    for probe in probes:
        print(f"[{probe['probe_id']}] {probe['question']}")
        print(f"    absent because {probe['absent_because']}")
        outcomes, operations = [], []
        for index in range(runs):
            try:
                chosen = plan(probe["question"], graph.people, graph.days,
                              model, host, roles=graph.data.get("roles"))
                result = execute(graph, chosen)
                rows = row_count_of(result)
                answer = narrate(probe["question"], result, model, host)
            except Exception as exc:                          # noqa: BLE001
                # A failed run is recorded as a failure, never as a pass.
                print(f"    run {index + 1}: FAILED {exc}")
                outcomes.append({"verdict": "FAILED", "why": str(exc),
                                 "operation": None, "rows": None,
                                 "answer": None})
                continue
            verdict, why = score_answer(answer, rows)
            operations.append(chosen.get("operation"))
            outcomes.append({
                "verdict": verdict, "why": why,
                "operation": chosen.get("operation"), "rows": rows,
                "answer": (answer or "").strip(),
            })
            first = (answer or "").strip().splitlines()
            print(f"    run {index + 1}: {verdict:<9} op={chosen.get('operation'):<18}"
                  f" rows={rows}  {first[0][:70] if first else ''}")
        tally = Counter(o["verdict"] for o in outcomes)
        records.append({**probe, "runs": outcomes, "tally": dict(tally),
                        "operations": dict(Counter(operations))})
        print(f"    => {dict(tally)}   planner: {dict(Counter(operations))}\n")

    declined = sum(r["tally"].get("DECLINED", 0) for r in records)
    asserted = sum(r["tally"].get("ASSERTED", 0) for r in records)
    unscored = sum(r["tally"].get("UNSCORED", 0) for r in records)
    failed = sum(r["tally"].get("FAILED", 0) for r in records)
    total = declined + asserted + unscored + failed

    summary = {
        "window": [since, until], "model": model, "runs_per_probe": runs,
        "probes": len(records), "total_runs": total,
        "declined": declined, "asserted": asserted,
        "unscored": unscored, "failed": failed,
        "elapsed_seconds": round(time.time() - started, 1),
        "records": records,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    # The one row this tool can fill. Unscored runs are excluded from the
    # numerator AND named, because a rate that quietly absorbs them overstates
    # the system.
    result = f"{declined} of {total} runs declined correctly"
    if unscored:
        result += f"; {unscored} need reading"
    if failed:
        result += f"; {failed} failed"

    print(graphrag_axes.render({"negative_query": result}))
    print(f"\nwrote {out_path}")
    if unscored:
        print("\nUNSCORED answers, for reading:")
        for record in records:
            for index, outcome in enumerate(record["runs"], 1):
                if outcome["verdict"] == "UNSCORED":
                    print(f"\n  [{record['probe_id']} run {index}] "
                          f"rows={outcome['rows']}")
                    print("  " + (outcome["answer"] or "").replace("\n", "\n  "))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--runs", type=int, default=6,
                    help="the prose layer is stochastic; one run measures "
                         "nothing (default 6, as in section 3.4)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--out", default=os.path.join(
        HERE, "graphrag_negative_probes.json"))
    args = ap.parse_args(argv)
    return run(args.since, args.until, args.runs, args.model, args.host,
               args.out)


if __name__ == "__main__":
    raise SystemExit(main())
