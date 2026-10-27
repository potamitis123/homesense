#!/usr/bin/env python3
"""Ask the household graph a question.

    python -m graphrag.ask "What does Rafael talk about?"
    python -m graphrag.ask "How do Ilyas and Rafael get on?" --show-plan
    python -m graphrag.ask --op person_topics --arg person=Rafael

The three stages are printed separately on purpose. Seeing which operation was
chosen, and the numbers it returned, is what lets you tell a wrong answer from
a wrongly-planned one.
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import Graph, execute, focus_incidents
from .llm import (DEFAULT_HOST, DEFAULT_MODEL, names_a_subject, narrate, plan,
                  select_incidents)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # pragma: no cover
    pass


def _rule(title: str) -> None:
    print(f"\n{title}\n" + "-" * max(24, len(title)))


def _print_incidents(result: dict) -> None:
    """The incidents an answer rests on, from the record and not the model.

    Printed whatever the narrator went on to say, for the same reason the
    evidence is: a start and an end are the whole point of this operation, and
    they must reach the reader exactly as stored.
    """
    rows = result.get("incidents") or []
    if not rows:
        return
    _rule("INCIDENTS (from the record)")
    for r in rows:
        tension = "" if r.get("tension") is None else f"  tension {r['tension']}"
        print(f"  {r['day']}  {r['start']} to {r['end']}  {r['kind']:<14} "
              f"{'+'.join(r.get('participants') or []) or '-':<20} "
              f"[{', '.join(r.get('rooms') or [])}]{tension}")
        print(f"      {r.get('what') or r.get('about') or ''}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="?", default=None)
    ap.add_argument("--op", default=None,
                    help="skip the planner and run this operation directly")
    ap.add_argument("--arg", action="append", default=[],
                    metavar="K=V", help="argument for --op, repeatable")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--since", default=None,
                    help="only load days from this date onward")
    ap.add_argument("--until", default=None,
                    help="only load days up to this date")
    ap.add_argument("--show-plan", action="store_true")
    ap.add_argument("--show-result", action="store_true",
                    help="print the full operation output as JSON")
    ap.add_argument("--no-narrate", action="store_true",
                    help="stop after the deterministic result")
    args = ap.parse_args(argv)

    if not args.question and not args.op:
        ap.error("give a question, or --op")

    g = Graph.load(since=args.since, until=args.until)
    window = g.data["window"]
    print(f"graph: {len(g.days)} day(s) {window['start']} .. {window['end']}  |  "
          f"{len(g.segments)} utterances, {g.data['counts']['topics']} topics, "
          f"{window['event_count']} events, people: {', '.join(g.people)}")

    if args.op:
        parsed = {}
        for item in args.arg:
            k, _, v = item.partition("=")
            parsed[k] = int(v) if v.isdigit() else v
        chosen = {"operation": args.op, "arguments": parsed,
                  "why": "specified on the command line"}
    else:
        chosen = plan(args.question, g.people, g.days, args.model,
                      args.host, roles=g.data.get('roles'))

    if args.show_plan or args.op:
        _rule("PLAN")
        print(json.dumps(chosen, ensure_ascii=False, indent=2))

    result = execute(g, chosen)

    # SELECT: of the incidents in scope, which does the question ask about?
    # A model's judgement, so it is skipped with --no-narrate and when the
    # operation was given by hand with no question to judge against.
    #
    # It runs only when the question names a SUBJECT -- "complain about work".
    # Asked "was there a quarrel", every quarrel in scope is an answer and the
    # sharpest already comes first, so a model has nothing to add and can only
    # take away: the local model, given the day's eight quarrels, dropped the
    # one real one and kept six minor disputes.
    if (chosen["operation"] == "incidents" and args.question
            and names_a_subject(args.question)
            and not args.no_narrate and result.get("incidents")):
        # The whole memory, where the reader of a day is given only part of
        # it (incidents.READER_ENTRIES). A line about what tends to happen
        # cannot add an incident here, only bear on the choice among those
        # already listed, and on five questions the choice was the same with
        # twelve lines as with all of them.
        from .incidents import load_household_memory
        picked = select_incidents(args.question, result["incidents"],
                                  args.model, args.host,
                                  memory=load_household_memory())
        if picked is None:
            print("  [select] the model did not answer; every incident in "
                  "scope is kept")
        else:
            considered = len(result["incidents"])
            result = focus_incidents(g, result, picked)
            _rule("SELECTED")
            print(f"  {len(picked)} of {considered} incident(s) in scope "
                  f"judged to answer the question")

    if args.show_result:
        _rule("RESULT")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _rule("RESULT (summary)")
        for key, value in result.items():
            if key.startswith("_") or key == "evidence":
                continue
            rendered = json.dumps(value, ensure_ascii=False, default=str)
            print(f"  {key}: {rendered[:300]}"
                  + ("…" if len(rendered) > 300 else ""))
        print(f"  evidence: {len(result.get('evidence') or [])} utterance(s)")

    if args.no_narrate:
        _print_incidents(result)
        return 0

    _rule("ANSWER")
    print(narrate(args.question or f"Describe {chosen['operation']}",
                  result, args.model, args.host))

    _print_incidents(result)

    # Printed from the record, not from the model. This is the only place the
    # utterances appear, so what the reader sees is exactly what was said.
    evidence = result.get("evidence") or []
    if evidence:
        _rule("EVIDENCE (verbatim from the record)")
        for i, e in enumerate(evidence, 1):
            print(f'  [E{i}] "{e["quote"]}"')
            print(f'       {e["speaker"]} · {e["day"]} · {e.get("room", "?")} '
                  f'· event {e.get("event_id")} · {e.get("audio_file") or "-"}')
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
