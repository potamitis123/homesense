# -*- coding: utf-8 -*-
"""Turn a raw recording into a good Chatterbox voice reference.

Cloning quality is dominated by the reference clip, and the usual faults are
mechanical: too long, padded with silence, quiet, or at the wrong sample
rate. This fixes those four.

    prep_voice.bat                                  # my_voice_sample.wav -> *_prepped.wav
    prep_voice.bat raw_recording.wav -o ref.wav
    prep_voice.bat --seconds 8 --keep-gaps

What it cannot fix is missing bandwidth. A 16 kHz recording holds nothing
above 8 kHz, which is where /s/, /f/ and /th/ live -- so the model invents
that band, and invented fricatives are a large part of what an ear reads as
a foreign accent. Re-record at 44.1 or 48 kHz to actually solve that, and
record in the language you intend to speak: cloning copies your phonemes
and prosody, so a Greek reference makes English come out Greek-inflected.
"""

from __future__ import annotations

import argparse
import os

import librosa
import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_SR = 24000  # what Chatterbox works at


def describe(y, sr, label):
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    rms = float(np.sqrt(np.mean(y ** 2))) if y.size else 0.0
    to_db = lambda x: (20 * np.log10(x) if x > 0 else float("-inf"))
    print(f"  {label:9s} {len(y)/sr:6.2f}s  {sr} Hz  "
          f"peak {to_db(peak):6.1f} dBFS  rms {to_db(rms):6.1f} dBFS")


def main():
    parser = argparse.ArgumentParser(description="Prepare a voice reference for cloning.")
    parser.add_argument("input", nargs="?", default=os.path.join(HERE, "my_voice_sample.wav"))
    parser.add_argument("-o", "--output", help="default: <input>_prepped.wav next to this script")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="target reference length (7-12s is the sweet spot)")
    parser.add_argument("--top-db", type=float, default=30.0, dest="top_db",
                        help="threshold below peak treated as silence")
    parser.add_argument("--keep-gaps", action="store_true",
                        help="keep internal pauses instead of closing them up")
    parser.add_argument("--peak-dbfs", type=float, default=-1.0, dest="peak_dbfs")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        raise SystemExit(f"not found: {args.input}")

    output = args.output or os.path.join(
        HERE, os.path.splitext(os.path.basename(args.input))[0] + "_prepped.wav")
    if not os.path.isabs(output):
        output = os.path.join(HERE, output)

    original_sr = librosa.get_samplerate(args.input)
    y, sr = librosa.load(args.input, sr=TARGET_SR, mono=True)
    print(f"\n{os.path.basename(args.input)}")
    describe(y, sr, "original")
    if original_sr < TARGET_SR:
        print(f"  [warn] source is {original_sr} Hz -- upsampling to {TARGET_SR} cannot")
        print(f"         recreate detail above {original_sr // 2} Hz. Re-record for a real fix.")

    # Keep only voiced stretches, so the reference is speech rather than room tone.
    intervals = librosa.effects.split(y, top_db=args.top_db)
    if not len(intervals):
        raise SystemExit("no speech found -- try a higher --top-db")

    target_samples = int(args.seconds * sr)
    if args.keep_gaps:
        # One continuous stretch: start at the first speech, run to the target.
        start = int(intervals[0][0])
        chosen = y[start:start + target_samples]
    else:
        # Concatenate voiced runs, longest first is tempting but order matters
        # for prosody, so keep them in time order and stop at the target.
        pieces, total = [], 0
        for begin, end in intervals:
            piece = y[begin:end]
            pieces.append(piece)
            total += len(piece)
            if total >= target_samples:
                break
        chosen = np.concatenate(pieces)[:target_samples]

    # Normalise to a predictable peak; quiet references clone poorly.
    peak = float(np.max(np.abs(chosen)))
    if peak > 0:
        chosen = chosen * (10 ** (args.peak_dbfs / 20) / peak)

    sf.write(output, chosen, sr, subtype="PCM_16")
    describe(chosen, sr, "prepped")
    print(f"  -> {output}\n")
    print("Use it with:  make_avatar_video.bat --voice "
          f"\"{os.path.basename(output)}\" \"your text here\"")


if __name__ == "__main__":
    main()
