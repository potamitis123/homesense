"""Write the day's listed disputes as a text file for a resident to judge.

    python make_dispute_list.py --day 2026-10-02

The incident layer lists quarrels and gives each a start and an end. Whether a
listed quarrel was a quarrel, and where it really began and ended, is something
only a person who was there can say. This writes each one out with the
transcript around it and blanks to fill in.

Disputes that overlap in time are shown as one stretch with one transcript, so
nothing has to be read twice. The lines the system first pointed at are marked,
because the span it then settled on is the thing being checked: the rule that
follows a quarrel into its neighbouring minutes stretched two mild afternoon
disputes past twenty minutes on 2 October, and a stricter wording did not
shorten them.

The file holds the household's own words. It is gitignored.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from graphrag.incidents import _secs, lines_between                # noqa: E402
from graphrag.ingest import load_day                               # noqa: E402

MARGIN = 120          # seconds of transcript shown either side of a stretch


def clock(seconds: int) -> str:
    seconds = max(0, min(seconds, 24 * 3600 - 1))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def stretches(quarrels: list[dict]) -> list[list[dict]]:
    """Quarrels grouped where their spans, with the margin, overlap."""
    out = []
    for node in sorted(quarrels, key=lambda n: n["lo"]):
        if out and node["lo"] <= max(n["hi"] for n in out[-1]) + MARGIN:
            out[-1].append(node)
        else:
            out.append([node])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", required=True)
    args = ap.parse_args(argv)

    day = load_day(args.day)
    if not day:
        sys.exit(f"{args.day} is not in the store.")
    if not day.get("incident_layer"):
        sys.exit(f"{args.day} has not been read for incidents.")
    segments = [n for n in day["nodes"] if n["kind"] == "segment"]
    incidents = [n for n in day["nodes"] if n["kind"] == "incident"]
    quarrels = [dict(n) for n in incidents if n["type"] == "quarrel"]
    for n in quarrels:
        # The widest the dispute is claimed to be: where it was first seen,
        # and where the span was settled.
        n["lo"] = min(_secs(n["start"][11:19]), _secs(n["listed_start"][11:19]))
        n["hi"] = max(_secs(n["end"][11:19]), _secs(n["listed_end"][11:19]))
    groups = stretches(quarrels)

    w = []
    w.append(f"DISPUTES ON {args.day} -- FOR YOU TO JUDGE")
    w.append("")
    w.append("The system listed these as quarrels and gave each a start and an end.")
    w.append("For each one, say whether it really was a dispute and when it really")
    w.append("began and ended. Type your answers after the blanks and save the file.")
    w.append("")
    w.append("  * Put an x in ONE of the three brackets.")
    w.append("  * Times as HH:MM are enough. Leave them blank if there was no dispute.")
    w.append("  * The transcript is speech recognition and often wrong; lines whose")
    w.append("    speaker the system could not name are not shown.")
    w.append("  * '>>' marks the lines the system first pointed at.")
    w.append("")

    total_lines = 0
    for number, group in enumerate(groups, 1):
        lo = min(n["lo"] for n in group) - MARGIN
        hi = max(n["hi"] for n in group) + MARGIN
        rows = lines_between(segments, lo, hi + 1)
        total_lines += len(rows)
        w.append("=" * 78)
        w.append(f"STRETCH {number} of {len(groups)}        {clock(lo)[:5]} to "
                 f"{clock(hi)[:5]}        {len(rows)} lines")
        w.append("=" * 78)
        w.append("")
        for letter, n in zip("abcdefgh", group):
            w.append(f"  {letter}) {'+'.join(n['participants']) or 'nobody named'}:  {n['what']}")
            w.append(f"     the system's span:   {n['start'][11:19]} to {n['end'][11:19]}"
                     f"      first seen at {n['listed_start'][11:19]}")
            w.append("")
            w.append("     Was it a dispute?    [ ] a real quarrel    [ ] a mild disagreement    [ ] no")
            w.append("     Between whom:        ____")
            w.append("     It began at:         ____          It ended at:  ____")
            w.append("     Notes:               ____")
            w.append("")
        pointed = [(_secs(n["listed_start"][11:19]), _secs(n["listed_end"][11:19]))
                   for n in group]
        w.append("  TRANSCRIPT")
        for time, _line, seg in rows:
            mark = ">>" if any(a <= _secs(time) <= b for a, b in pointed) else "  "
            w.append(f"  {mark} {time}  {seg.get('speaker', ''):<7} {seg.get('text', '')}")
        w.append("")

    w.append("=" * 78)
    w.append("DID THE SYSTEM MISS A DISPUTE?")
    w.append("=" * 78)
    w.append("")
    w.append("  A dispute that day that is not in the list above:")
    w.append("     Between whom: ____      From ____ to ____      About: ____")
    w.append("     Between whom: ____      From ____ to ____      About: ____")
    w.append("")
    w.append("=" * 78)
    w.append("OPTIONAL: THE REST OF WHAT THE SYSTEM LISTED THAT DAY")
    w.append("=" * 78)
    w.append("")
    w.append("  One line each. Put y if it happened roughly as described and at")
    w.append("  roughly that time, n if not, ? if you cannot say.")
    w.append("")
    for n in sorted((n for n in incidents if n["type"] != "quarrel"),
                    key=lambda n: n["start"]):
        w.append(f"  [ ]  {n['start'][11:16]} to {n['end'][11:16]}  {n['type']:<12} "
                 f"{'+'.join(n['participants']) or '-':<14} {n['what']}")
    w.append("")

    path = os.path.join(HERE, f"disputes_{args.day}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(w))
    print(f"{len(quarrels)} listed dispute(s) in {len(groups)} stretch(es), "
          f"{total_lines} transcript lines")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
