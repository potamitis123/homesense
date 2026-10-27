"""Were the clips the prefilter shelved really duplicates?

The prefilter decides before transcription, so its evidence is a correlation
score and nothing anyone can read. That is fine while it is right and
invisible when it is wrong -- a shelved clip that carried different words is
lost content, and the only trace is a number in skipped.jsonl.

This transcribes the shelved audio (kept, not deleted, exactly so this is
possible) and puts it beside the winner's stored transcript. Same words means
the prefilter was right; different words means it discarded speech.

    python tests/diagnostics/verify_shelved.py --date 2026-09-09 --sample 10
"""

import argparse
import difflib
import glob
import json
import os
import re
import sqlite3
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "ast_src"))
os.chdir(PROJECT_ROOT)

from ast_src.streaming_ASR import ASRTranscriber  # noqa: E402
from misc.load_parameters import parameters  # noqa: E402

SKIPPED_DIR = os.path.join(PROJECT_ROOT, "assets", "audio_skipped")
AUDIO_DIR = os.path.join(PROJECT_ROOT, "assets", "audio_mp3")


def strip_headers(text):
    """Drop the "[ 00: 00. 00 - ... ] Name:" segment headers."""
    if not text:
        return ""
    if text.startswith("No Speech Detected"):
        return ""
    text = re.sub(r"\[[\d:.\s\-]+\]", " ", text)
    text = re.sub(r"\b[A-ZΑ-ΩΆΈΉΊΌΎΏ][\wΑ-Ωα-ωάέήίόύώϊϋΐΰ]*:", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def words(text):
    return re.findall(r"[^\W\d_]+", (text or "").lower(), flags=re.UNICODE)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--sample", type=int, default=10,
                    help="how many shelved clips to check")
    args = ap.parse_args()

    log = os.path.join(SKIPPED_DIR, "skipped.jsonl")
    rows = [json.loads(l) for l in open(log, encoding="utf-8") if l.strip()]
    day = [r for r in rows if str(r["when"])[:10] == args.date]
    if not day:
        print(f"Nothing shelved on {args.date}.")
        return

    # Spread the sample across the correlation range: a check that only looks
    # at the confident end would not find the marginal mistakes.
    day.sort(key=lambda r: r["correlation"])
    step = max(1, len(day) // args.sample)
    sample = day[::step][:args.sample]

    db = os.path.join(PROJECT_ROOT,
                      parameters.get_parameters()["sqlite"]["db_name"])
    conn = sqlite3.connect(db)

    print(f"{len(day)} shelved on {args.date}; checking {len(sample)} "
          f"across correlation {sample[0]['correlation']:.2f}"
          f"-{sample[-1]['correlation']:.2f}\n", flush=True)

    transcriber = ASRTranscriber()
    same = diff = missing = 0

    for r in sample:
        shelved = os.path.join(SKIPPED_DIR, os.path.basename(r["file"]))
        if not os.path.exists(shelved):
            hits = glob.glob(os.path.join(SKIPPED_DIR,
                                          os.path.basename(r["file"])))
            if not hits:
                print(f"-- {os.path.basename(r['file'])}: audio gone\n")
                missing += 1
                continue
            shelved = hits[0]

        row = conn.execute(
            "SELECT transcription FROM ast_table WHERE file_name = ?",
            (os.path.basename(r["winner"]),)).fetchone()
        winner_text = strip_headers(row[0]) if row else None

        try:
            loser_text = strip_headers(transcriber.transcribe(shelved))
        except Exception as exc:
            print(f"-- {os.path.basename(r['file'])}: ASR failed "
                  f"({type(exc).__name__}: {exc})\n")
            missing += 1
            continue

        lw, ww = words(loser_text), words(winner_text)
        ratio = (difflib.SequenceMatcher(None, lw, ww).ratio()
                 if lw and ww else 0.0)
        verdict = ("SAME" if ratio >= 0.6 else
                   "DIFFERENT" if lw and ww else
                   "one side silent")
        if verdict == "SAME":
            same += 1
        elif verdict == "DIFFERENT":
            diff += 1

        print(f"corr={r['correlation']:.2f}  snr {r['snr']:.1f} -> "
              f"{r['snr_winner']:.1f} dB   overlap={ratio:.2f}  {verdict}")
        print(f"   shelved  {os.path.basename(r['file'])}")
        print(f"      {loser_text or '(no words)'}")
        print(f"   winner   {os.path.basename(r['winner'])}")
        print(f"      {winner_text if winner_text is not None else '(not in db)'}\n",
              flush=True)

    print(f"\n{same} same, {diff} DIFFERENT, {missing} unchecked "
          f"of {len(sample)}")
    if diff:
        print("A 'DIFFERENT' pair means the prefilter discarded speech that "
              "the winner does not contain.")


if __name__ == "__main__":
    main()
