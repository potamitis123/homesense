"""Where does a continuously-spoken stream get lost between mouth and database?

Run a counting test -- read Greek numerals aloud, one a second, without
pausing -- then point this at the window. Numbers are the instrument: the
source is known to be gap-free, so every number missing from the output is
the system's doing, and each one carries its own timestamp in its value.

That last property is what makes the test worth the trouble. A missing
number can be blamed precisely:

  * number N is absent, N-1 closed clip A and N+1 opened clip B
        -> the microphone was not recording. CAPTURE loss.
  * number N is absent but its neighbours sit inside one clip
        -> the audio exists and the transcriber dropped it. ASR loss.

Prose cannot do this. With prose a dropped word and a misheard word look
the same, and there is no way to date the hole.

Usage
    python tests/diagnostics/utterance_loss_test.py --from 15:20 --to 15:30
    python tests/diagnostics/utterance_loss_test.py --date 2026-09-10 \
        --from 15:20 --to 15:30 --device box3-1

Pair it with the device's serial log (idf.py monitor -> file). The firmware
prints why every recording ended -- "silence end ... silence=Yms",
"max_record reached", "CHUNK push failed ... ABORT" -- and there is no MQTT
log topic, so USB is the only way to see it. This script says where the
audio went missing; the serial log says why the device stopped.
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, date as date_cls

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from misc.load_parameters import parameters  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
AUDIO_DIR = os.path.join(PROJECT_ROOT, "assets", "audio_mp3")

# The firmware's own frame geometry, so silence runs are counted the way
# capture_task counts them rather than on some convenient block size.
FRAME_SAMPLES = 1024
FRAME_MS = 64

# g_rms_threshold is adaptive: noise_ema (clamped [150,800]) x 2.5. The SD
# card's RMS_THRESHOLD is only a boot value and is overwritten on the first
# idle frame, so a clip has to be judged against the range, not the setting.
ADAPTIVE_THRESHOLDS = (375, 750, 1250, 2000)


# ── Greek numerals ──────────────────────────────────────────────────
# Additive: "εκατόν είκοσι ένα" = 100 + 20 + 1. Both the formal and the
# colloquial forms appear because Whisper picks whichever it heard.
_WORDS = {
    "ενα": 1, "ένα": 1, "μια": 1, "μία": 1, "εναν": 1, "έναν": 1,
    "δυο": 2, "δύο": 2,
    "τρια": 3, "τρία": 3, "τρεις": 3,
    "τεσσερα": 4, "τέσσερα": 4, "τεσσερις": 4, "τέσσερις": 4,
    "πεντε": 5, "πέντε": 5,
    "εξι": 6, "έξι": 6,
    "εφτα": 7, "εφτά": 7, "επτα": 7, "επτά": 7,
    "οχτω": 8, "οχτώ": 8, "οκτω": 8, "οκτώ": 8,
    "εννια": 9, "εννιά": 9, "εννεα": 9, "εννέα": 9,
    "δεκα": 10, "δέκα": 10,
    "εντεκα": 11, "έντεκα": 11, "ενδεκα": 11, "ένδεκα": 11,
    "δωδεκα": 12, "δώδεκα": 12,
    "δεκατρια": 13, "δεκατρία": 13,
    "δεκατεσσερα": 14, "δεκατέσσερα": 14,
    "δεκαπεντε": 15, "δεκαπέντε": 15,
    "δεκαεξι": 16, "δεκαέξι": 16,
    "δεκαεφτα": 17, "δεκαεφτά": 17, "δεκαεπτα": 17, "δεκαεπτά": 17,
    "δεκαοχτω": 18, "δεκαοχτώ": 18, "δεκαοκτω": 18, "δεκαοκτώ": 18,
    "δεκαεννια": 19, "δεκαεννιά": 19, "δεκαεννεα": 19, "δεκαεννέα": 19,
    "εικοσι": 20, "είκοσι": 20,
    "τριαντα": 30, "τριάντα": 30,
    "σαραντα": 40, "σαράντα": 40,
    "πενηντα": 50, "πενήντα": 50,
    "εξηντα": 60, "εξήντα": 60,
    "εβδομηντα": 70, "εβδομήντα": 70,
    "ογδοντα": 80, "ογδόντα": 80,
    "ενενηντα": 90, "ενενήντα": 90,
    "εκατο": 100, "εκατό": 100, "εκατον": 100, "εκατόν": 100,
    "διακοσια": 200, "διακόσια": 200,
}

# A hundreds/tens word starts a new number; a units word ends one. Tracking
# that is what stops "είκοσι ένα τριάντα" collapsing into a single 51.
_TENS = {20, 30, 40, 50, 60, 70, 80, 90, 100, 200}


def parse_numbers(text):
    """Every numeral in `text`, in the order spoken.

    Handles digits ("47") and words alike -- Whisper emits both, sometimes
    in the same clip, and for this test they mean the same thing.
    """
    if not text:
        return []
    # The non-speech branch writes a diagnostic line rather than words
    # ("No Speech Detected (ASR) | AST: Speech 0.75 | ..."). Its confidences
    # are digits and would otherwise be counted as spoken numbers.
    if text.startswith("No Speech Detected"):
        return []
    # Stored transcripts carry per-segment headers -- "[ 00: 00. 00 - 00:
    # 10. 00 ] Ilyas:" -- whose clock digits are not spoken numbers.
    text = re.sub(r"\[[\d:.\s\-]+\]", " ", text)
    found = []
    tokens = re.findall(r"\d+|[^\W\d_]+", text.lower(), flags=re.UNICODE)
    acc = 0
    for tok in tokens:
        if tok.isdigit():
            if acc:
                found.append(acc)
                acc = 0
            found.append(int(tok))
            continue
        val = _WORDS.get(tok)
        if val is None:
            if acc:
                found.append(acc)
                acc = 0
            continue
        # A second tens-word means the previous number is finished.
        if val in _TENS and acc and acc % 100 == 0 and val >= 100:
            found.append(acc)
            acc = val
        elif val in _TENS and acc and not (acc >= 100 and val < 100):
            found.append(acc)
            acc = val
        else:
            acc += val
            if val < 10 or (10 <= val < 20):
                found.append(acc)
                acc = 0
    if acc:
        found.append(acc)
    return found


# ── clips ───────────────────────────────────────────────────────────
def clip_rows(day, t_from, t_to, device):
    db = os.path.join(PROJECT_ROOT,
                      parameters.get_parameters()["sqlite"]["db_name"])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    where = "date >= ? AND date <= ?"
    args = [f"{day} {t_from}:00", f"{day} {t_to}:59"]
    if device:
        where += " AND device_name = ?"
        args.append(device)
    rows = conn.execute(
        "SELECT id, date, device_name, file_name, transcription"
        f" FROM ast_table WHERE {where} ORDER BY date", args).fetchall()
    conn.close()
    return rows


def rms_frames(path):
    """Per-frame int16 RMS on the firmware's own 1024-sample grid."""
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    nb = len(x) // FRAME_SAMPLES
    if nb == 0:
        return np.array([]), len(x) / sr
    r = np.array([
        np.sqrt(np.mean(x[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES] ** 2))
        for i in range(nb)]) * 32768.0
    return r, len(x) / sr


def longest_quiet_run(rms, thr):
    best = cur = 0
    for v in rms:
        cur = cur + 1 if v < thr else 0
        best = max(best, cur)
    return best


def main():
    # Transcripts are Greek and the console here is cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date_cls.today().isoformat())
    ap.add_argument("--from", dest="t_from", required=True,
                    help="HH:MM window start")
    ap.add_argument("--to", dest="t_to", required=True, help="HH:MM window end")
    ap.add_argument("--device", default=None,
                    help="restrict to one sentinel, e.g. box3-1")
    args = ap.parse_args()

    rows = clip_rows(args.date, args.t_from, args.t_to, args.device)
    if not rows:
        print("No clips in that window.")
        return

    print(f"{len(rows)} clips  {args.date} {args.t_from}-{args.t_to}"
          f"{'  ' + args.device if args.device else ''}\n")

    clips = []
    for r in rows:
        path = os.path.join(AUDIO_DIR, r["file_name"] or "")
        rms, dur = (rms_frames(path) if r["file_name"] and os.path.exists(path)
                    else (np.array([]), None))
        start = datetime.strptime(str(r["date"])[:19], "%Y-%m-%d %H:%M:%S")
        clips.append({
            "id": r["id"], "start": start, "dur": dur,
            "device": r["device_name"], "rms": rms,
            "nums": parse_numbers(r["transcription"]),
            "text": (r["transcription"] or "").strip(),
        })

    # ── per clip, and the gap that follows it ──────────────────────
    print("clip      start      dur     ends     gap→next   numbers")
    print("-" * 78)
    for i, c in enumerate(clips):
        end = (c["start"].timestamp() + c["dur"]) if c["dur"] else None
        gap = ""
        if end and i + 1 < len(clips):
            g = clips[i + 1]["start"].timestamp() - end
            gap = f"{g:+6.2f}s"
            if g > 0.0:
                gap += " DEAF" if g > 1.28 else ""
        nums = c["nums"]
        span = f"{nums[0]}-{nums[-1]}" if nums else "-"
        print(f"#{c['id']:<7} {c['start'].strftime('%H:%M:%S')}"
              f" {c['dur'] or 0:6.2f}s"
              f" {datetime.fromtimestamp(end).strftime('%H:%M:%S') if end else '  --  '}"
              f" {gap:>11}   {len(nums):>3} [{span}]")

    # ── what went missing, and whose fault it was ──────────────────
    spoken = sorted({n for c in clips for n in c["nums"]})
    if not spoken:
        print("\nNo numerals found -- was this a counting run?")
        return
    lo, hi = spoken[0], spoken[-1]
    missing = [n for n in range(lo, hi + 1) if n not in set(spoken)]

    print(f"\nheard {len(spoken)} of {hi - lo + 1} numbers in {lo}..{hi}"
          f"   missing {len(missing)}")
    if not missing:
        print("Nothing lost in this window.")
        return

    # Attribute each hole to a clip boundary or to a clip's interior.
    last_of = {}
    first_of = {}
    for i, c in enumerate(clips):
        if c["nums"]:
            last_of[c["nums"][-1]] = i
            first_of[c["nums"][0]] = i

    capture, asr, unclear = [], [], []
    for n in missing:
        before, after = n - 1, n + 1
        bi = last_of.get(before)
        ai = first_of.get(after)
        if bi is not None and ai is not None and ai == bi + 1:
            capture.append((n, clips[bi]["id"], clips[ai]["id"]))
        elif bi is None and ai is None:
            # both neighbours sit inside a clip -> the audio was captured
            inside = [c for c in clips
                      if before in c["nums"] and after in c["nums"]]
            (asr if inside else unclear).append(n)
        else:
            unclear.append(n)

    if capture:
        print(f"\nCAPTURE loss -- lost between two clips ({len(capture)}):")
        for n, a, b in capture:
            print(f"   {n:>4}  fell in the gap after #{a}, before #{b}")
    if asr:
        print(f"\nASR loss -- audio exists, words did not survive ({len(asr)}):")
        print("   " + ", ".join(str(n) for n in asr))
    if unclear:
        print(f"\nUnattributed ({len(unclear)}): "
              + ", ".join(str(n) for n in unclear))

    # ── would the firmware's SAD have cut these clips? ─────────────
    print("\nSilence-timeout check (firmware grid, adaptive threshold range)")
    print("clip      frames  medRMS   longest quiet run at threshold")
    print("-" * 78)
    need = int(np.ceil(3000 / FRAME_MS))  # SILENCE_TIMEOUT_MS now 3000
    for c in clips:
        if not len(c["rms"]):
            continue
        runs = " ".join(
            f"{t}:{longest_quiet_run(c['rms'], t) * FRAME_MS:>5}ms"
            + ("*" if longest_quiet_run(c["rms"], t) >= need else " ")
            for t in ADAPTIVE_THRESHOLDS)
        print(f"#{c['id']:<7} {len(c['rms']):>6} {np.median(c['rms']):>7.0f}"
              f"   {runs}")
    print("\n  * = a run long enough to trip SILENCE_TIMEOUT_MS at that "
          "threshold,\n    i.e. the recording would have been cut there.")


if __name__ == "__main__":
    main()
