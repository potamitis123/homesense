#!/usr/bin/env python3
"""Copy the clips the assessment read into reports/journal_2026-09-10/audio.

    python tests/diagnostics/journal_collect_audio.py

Every clip the database holds inside a take's window, from any sentinel, is a
clip the assessment read; those are the recordings copied here, under their
original names. index.csv records the take, sentinel and timestamp of each,
so the folder can be checked against Table 1 of the report and any clip can
be traced back to the line it carries.

The windows come from takes_fixed.txt rather than takes.txt: six of the forty
windows logged by hand are unusable as written (an end before the start, a
zero duration, transposed digits) and were repaired from the clip timestamps.
Scoring used the repaired manifest, so collecting audio from anything else
would hand over a different corpus from the one the report describes.
"""
import csv
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
for _p in (HERE, os.path.join(ROOT, "ast_dash", "llm_report"), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import score_recorded as R                                         # noqa: E402

OUT = os.path.join(ROOT, "reports", "journal_2026-09-10")
AUDIO = os.path.join(OUT, "audio")
SOURCE = os.path.join(ROOT, "assets", "audio_mp3")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    takes = sorted(R.load_manifest(os.path.join(HERE, "takes_fixed.txt")),
                   key=lambda t: t["scenario"])
    conn = sqlite3.connect(f"file:{R.SOURCE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    os.makedirs(AUDIO, exist_ok=True)

    index, seen = [], {}
    copied = no_name = not_on_disk = duplicate = 0
    bytes_copied = 0
    for take in takes:
        rows = conn.execute(
            "SELECT id, date, device_name, room, file_name, transcription"
            " FROM ast_table WHERE date >= ? AND date <= ? ORDER BY date",
            (take["since"], take["until"])).fetchall()
        for row in rows:
            text = (row["transcription"] or "").strip()
            name = row["file_name"]
            if not name:
                status = "no audio file recorded"
                no_name += 1
            elif name in seen:
                status = f"already copied for take {seen[name]}"
                duplicate += 1
            elif not os.path.exists(os.path.join(SOURCE, name)):
                status = "audio no longer on disk"
                not_on_disk += 1
            else:
                shutil.copy2(os.path.join(SOURCE, name), os.path.join(AUDIO, name))
                bytes_copied += os.path.getsize(os.path.join(SOURCE, name))
                seen[name] = take["scenario"]
                status = "copied"
                copied += 1
            index.append({
                "take": take["scenario"], "window_start": take["since"],
                "window_end": take["until"], "event_id": row["id"],
                "timestamp": row["date"], "sentinel": row["device_name"],
                "room": row["room"], "file": name or "",
                "transcribed_speech": bool(text) and not text.startswith("No Speech"),
                "status": status})
    conn.close()

    with open(os.path.join(AUDIO, "index.csv"), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(index[0]))
        writer.writeheader()
        writer.writerows(index)

    clips = len(index)
    speech = sum(1 for r in index if r["transcribed_speech"])
    print(f"takes                    : {len(takes)}")
    print(f"clips in the take windows: {clips}")
    print(f"  with transcribed speech: {speech}")
    print(f"copied                   : {copied}"
          f"   ({bytes_copied / 1e6:.1f} MB)")
    if duplicate:
        print(f"  in two windows         : {duplicate} (copied once)")
    if no_name:
        print(f"  no audio file recorded : {no_name}")
    if not_on_disk:
        print(f"  audio no longer on disk: {not_on_disk}")
    per_device = {}
    for r in index:
        per_device.setdefault(r["sentinel"], [0, 0])
        per_device[r["sentinel"]][0] += 1
        per_device[r["sentinel"]][1] += r["status"] == "copied"
    for device, (total, got) in sorted(per_device.items()):
        print(f"    {device:<12} {got:>3} of {total:>3} clips")

    # The report's Table 1 counts the same windows; they must agree.
    results = os.path.join(OUT, "data", "results.json")
    if os.path.exists(results):
        with open(results, encoding="utf-8") as fh:
            corpus = json.load(fh)["corpus"]
        expected = sum(corpus["clips_by_device"].values())
        expected_speech = sum(corpus["speech_clips_by_device"].values())
        print(f"\nTable 1 of the report    : {expected} clips, {expected_speech} with speech")
        print("match" if (clips, speech) == (expected, expected_speech)
              else f"MISMATCH: collected {clips}/{speech}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
