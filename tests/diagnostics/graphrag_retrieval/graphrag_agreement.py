"""Do two coders agree about what is in the slice?

    python graphrag_agreement.py graphrag_slice_20260917_1300_A.json \\
                                 graphrag_slice_20260917_1300_lab1.json

This is the number a reviewer asks for when they write that a study lacks
independent human assessment. Everything else in this folder measures the
system; this measures whether the yardstick itself is stable when somebody who
did not build the system picks it up.

It answers a narrower question than the dynamics study's score.py, because the
judgements here are binary rather than a five-point scale. Cohen's kappa on
yes/no/unclear is the right statistic and a weighted kappa is not: "cannot
tell" is not half way between yes and no.

Two things are reported besides agreement, and both matter more than they look:

* each coder's PREVALENCE. Two coders can agree often and still disagree about
  how common quarrels are, and every precision figure in this folder is read
  against the base rate. A second coder who finds half as many quarrels moves
  every number that depends on it.
* the items they disagree on, listed by time, so they can be listened to again
  rather than argued about in the abstract.

The agreement statistics are imported from the dynamics study rather than
rewritten: that implementation is validated against Krippendorff's published
worked example, and a second copy would be a second thing to get wrong.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PILOT = os.path.join(PROJECT_ROOT, "tests", "diagnostics", "dynamics_pilot")
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, PILOT)
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # pragma: no cover
    pass

from score import cohen_kappa, krippendorff_alpha, bootstrap_ci, jaccard  # noqa: E402

QUESTIONS = ["quarrel", "warm", "complaint", "rebuff"]


def verdict(kappa):
    """Landis and Koch, named rather than implied."""
    if kappa is None:
        return "no variance"
    if kappa < 0.0:
        return "worse than chance"
    if kappa < 0.21:
        return "slight"
    if kappa < 0.41:
        return "fair"
    if kappa < 0.61:
        return "moderate"
    if kappa < 0.81:
        return "substantial"
    return "almost perfect"


def load(path):
    full = path if os.path.isabs(path) else os.path.join(HERE, path)
    if not os.path.exists(full):
        sys.exit(f"no such annotation file: {full}")
    data = json.load(open(full, encoding="utf-8"))
    coder = data.get("coder") or os.path.basename(full)
    answers = {a["item_id"]: a for a in data.get("answers") or []}
    return coder, data.get("slice"), answers


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("first")
    ap.add_argument("second")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    coder_a, slice_a, a = load(args.first)
    coder_b, slice_b, b = load(args.second)
    if slice_a and slice_b and slice_a != slice_b:
        sys.exit(f"different slices: {slice_a} and {slice_b}")

    shared = sorted(set(a) & set(b))
    if not shared:
        sys.exit("the two coders share no items")
    print(f"slice {slice_a or '?'}   coders {coder_a} and {coder_b}   "
          f"{len(shared)} item(s) coded by both "
          f"({len(a)} and {len(b)} in total)\n")

    report = {"slice": slice_a, "coders": [coder_a, coder_b],
              "n_shared": len(shared), "questions": {}}

    print(f"{'question':<11}{'kappa':>7}{'95% CI':>16}{'agree':>8}"
          f"{'  ' + coder_a[:8]:>11}{'  ' + coder_b[:8]:>11}   reading")
    print("-" * 78)
    for key in QUESTIONS:
        pairs, units = [], []
        for item in shared:
            x, y = a[item].get(key), b[item].get(key)
            if x is None or y is None:
                continue
            pairs.append((x, y))
            units.append([x, y])
        if not pairs:
            print(f"{key:<11}{'-':>7}{'':>16}{'-':>8}{'':>11}{'':>11}   not coded")
            continue
        kappa = cohen_kappa(pairs)
        alpha = krippendorff_alpha(units, ordinal=False)
        lo, hi = bootstrap_ci(units) if len(units) > 4 else (None, None)
        raw = sum(1 for x, y in pairs if x == y) / len(pairs)
        yes_a = sum(1 for x, _ in pairs if x == "yes") / len(pairs)
        yes_b = sum(1 for _, y in pairs if y == "yes") / len(pairs)
        band = f"[{lo:.2f}, {hi:.2f}]" if lo is not None else ""
        print(f"{key:<11}"
              f"{(f'{kappa:.2f}' if kappa is not None else 'n/a'):>7}"
              f"{band:>16}{raw:>8.2f}{yes_a:>11.0%}{yes_b:>11.0%}   "
              f"{verdict(kappa)}")
        report["questions"][key] = {
            "n": len(pairs), "cohen_kappa": kappa, "alpha_nominal": alpha,
            "alpha_ci": [lo, hi], "raw_agreement": raw,
            "prevalence": {coder_a: yes_a, coder_b: yes_b},
        }

    # Who was in the room, and who it was between. Not kappa: one is a set and
    # the other a single choice out of many, so they are reported plainly.
    heard = [jaccard(a[i].get("heard") or [], b[i].get("heard") or [])
             for i in shared
             if (a[i].get("heard") or b[i].get("heard"))]
    between = [(a[i].get("between"), b[i].get("between")) for i in shared
               if a[i].get("between") and b[i].get("between")]
    same = sum(1 for x, y in between if x == y)
    print()
    if heard:
        print(f"voices heard   mean Jaccard {sum(heard)/len(heard):.2f} "
              f"over {len(heard)} exchange(s)")
    if between:
        print(f"who it is between   {same}/{len(between)} = "
              f"{same/len(between):.2f} exact agreement")
    report["heard_jaccard"] = (sum(heard) / len(heard)) if heard else None
    report["between_exact"] = (same / len(between)) if between else None

    # What to listen to again.
    print("\ndisagreements, to be listened to again")
    rows = 0
    for item in shared:
        differing = [k for k in QUESTIONS
                     if a[item].get(k) and b[item].get(k)
                     and a[item][k] != b[item][k]]
        if not differing:
            continue
        rows += 1
        detail = "  ".join(
            f"{k}: {coder_a}={a[item][k]} {coder_b}={b[item][k]}"
            for k in differing)
        print(f"  {item}   {detail}")
    if not rows:
        print("  none")
    report["disagreement_items"] = rows

    out = args.out or os.path.join(
        HERE, f"graphrag_agreement.{slice_a or 'slice'}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
