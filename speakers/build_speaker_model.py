"""Build speakers_model.pth from the voice files in this folder.

Each subfolder here is one resident, and every audio file inside it is a
recording of that person. Enrolment embeds each file with ECAPA and averages
them into one profile per speaker; the profiles are what the pipeline matches
live audio against to put a name on a turn.

Run it from anywhere:

    python speakers/build_speaker_model.py

The model is written beside this script, as speakers/speakers_model.pth, and
that is where ast_src/streaming_ASR.py loads it from.

WHY IT PRINTS A SEPARATION TABLE

An enrolment that completes is not necessarily an enrolment that works. The
profiles it produces can be too alike to tell apart, and nothing downstream
says so -- the pipeline simply names the wrong resident with an ordinary
looking score. The previous model had Ilyas and Rafael sitting 0.398 apart
while a correct far-field match scores only 0.20-0.45, so on that axis the
identification was close to a coin toss and had been for months.

So the last thing this does is measure the profiles against each other and
against the individual files. Read that table before trusting the model:

  * every file should score highest for its own speaker. One that does not
    is either mislabelled or has someone else talking in it.
  * off-diagonal similarity is how alike two DIFFERENT people look. Lower is
    better. Anything approaching the scores real matches earn (0.2-0.45) is
    a pair the system cannot reliably separate.
  * a speaker whose own files disagree with each other more than they differ
    from another resident cannot be recognised. Usually that means one
    recording was made in very different conditions from the rest.
"""

import argparse
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# torchaudio 2.9 dropped list_audio_backends, which speechbrain still calls on
# import. Same shim the diagnostics scripts use.
import torchaudio  # noqa: E402

if not hasattr(torchaudio, "list_audio_backends"):
    torchaudio.list_audio_backends = lambda: ["soundfile"]

import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402

MODEL_PATH = HERE / "speakers_model.pth"


def report(manager, classifier):
    """Print what the new profiles can and cannot tell apart."""
    from ast_src.speaker_id import load_audio_as_16k_tensor

    profiles = manager.speaker_profiles
    names = list(profiles)
    if len(names) < 2:
        print("\nOnly one speaker enrolled; nothing to separate.")
        return

    print("\n" + "=" * 70)
    print("HOW ALIKE ARE THE PROFILES?  (off-diagonal: two DIFFERENT people)")
    print("=" * 70)
    print("     " + "".join(f"{n:>10}" for n in names))
    worst = (0.0, None)
    for a in names:
        row = ""
        for b in names:
            value = F.cosine_similarity(
                profiles[a].flatten(), profiles[b].flatten(), dim=0).item()
            row += f"{value:>10.3f}"
            if a != b and value > worst[0]:
                worst = (value, f"{a}/{b}")
        print(f"{a:<5}{row}")
    print(f"\n  closest pair: {worst[1]} at {worst[0]:.3f}")
    if worst[0] >= 0.35:
        print("  WARNING: a real far-field match scores about 0.20-0.45, so these")
        print("  two are about as alike as a correct identification is confident.")
        print("  Expect them to be confused. More varied recordings of each, made")
        print("  through the sentinels rather than close-mic, is what separates them.")
    elif worst[0] >= 0.25:
        print("  Workable, but this pair is the one to watch in the diagnostics.")
    else:
        print("  Good separation.")

    print("\n" + "=" * 70)
    print("EACH FILE AGAINST EVERY PROFILE  (a file must win for its own folder)")
    print("=" * 70)
    device = next(classifier.mods.parameters()).device
    problems = 0
    for speaker in names:
        folder = HERE / speaker
        seen = {}
        for ext in ("*.flac", "*.wav", "*.mp3", "*.m4a"):
            for pattern in (ext, ext.upper()):
                for path in folder.glob(pattern):
                    seen.setdefault(str(path.resolve()).lower(), path)
        print(f"\n  {speaker}/")
        for path in sorted(seen.values()):
            try:
                signal = load_audio_as_16k_tensor(path)
                with torch.no_grad():
                    emb = classifier.encode_batch(signal.unsqueeze(0)).squeeze()
                scores = {n: F.cosine_similarity(
                    emb, profiles[n].flatten().to(emb.device), dim=0).item()
                    for n in names}
            except Exception as e:
                print(f"    {path.name[:40]:<42} could not be read: {e}")
                continue
            best = max(scores, key=scores.get)
            flag = "" if best == speaker else f"   <-- scores as {best}"
            if best != speaker:
                problems += 1
            secs = len(signal) / 16000.0
            print(f"    {path.name[:40]:<42}{secs:>7.1f}s  "
                  + "  ".join(f"{n}={scores[n]:.3f}" for n in names) + flag)
    if problems:
        print(f"\n  {problems} file(s) score higher for someone else. Check whether "
              f"the folder is right,\n  and whether another person is audible in the "
              f"recording.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(MODEL_PATH),
                    help="where to write the model (default: beside this script)")
    ap.add_argument("--no-report", action="store_true",
                    help="skip the separation table")
    # Measured leave-one-out over this household's enrolment set: with every
    # clip under 1.5 s dropped, the worst-case margin between the right
    # resident and the nearest wrong one rose from 0.064 to 0.111. Set 0 to
    # enrol everything regardless of length.
    ap.add_argument("--min-seconds", type=float, default=1.5,
                    help="skip recordings shorter than this (default 1.5)")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    from speechbrain.inference import EncoderClassifier
    from ast_src.speaker_id import SpeakerProfileManager

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"speakers folder : {HERE}")
    print(f"model output    : {args.out}")
    print(f"device          : {device}\n")

    folders = sorted(p.name for p in HERE.iterdir() if p.is_dir())
    print(f"speakers found  : {folders or '(none -- nothing to enrol)'}")
    if not folders:
        return 1

    classifier = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb", run_opts={"device": device})

    manager = SpeakerProfileManager(HERE, classifier)
    manager.enroll_speakers(min_seconds=args.min_seconds)
    if not manager.speaker_profiles:
        print("\nNothing was enrolled; the existing model is left alone.")
        return 1

    # Written only after enrolment succeeds, so a failed run cannot leave the
    # pipeline with a half-built model.
    out = Path(args.out)
    if out.exists():
        backup = out.with_suffix(out.suffix + ".bak")
        try:
            os.replace(out, backup)
            print(f"\nprevious model kept as {backup.name}")
        except Exception as e:
            print(f"\ncould not back up the previous model: {e}")
    manager.save_model(out)

    if not args.no_report:
        report(manager, classifier)

    print(f"\nWritten: {out}")
    print("Restart the recogniser (ast_src/main.py) to pick it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
