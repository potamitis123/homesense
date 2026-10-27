# -*- coding: utf-8 -*-
"""Text in, talking-head video out: your face (avatar.jpg) saying the text in
your cloned voice (my_voice_sample.wav).

Standalone on purpose -- nothing here is imported by the Sentinel app, and it
installs nothing. Both stages run in the existing `audioapp` conda env, which
already carries Chatterbox and every SadTalker dependency. Use the .bat
wrapper so the env is activated for you:

    make_avatar_video.bat "Good evening. The front door has been open for ten minutes."
    make_avatar_video.bat --text-file speech.txt -o briefing.mp4 --enhancer gfpgan

Two stages, deliberately run as separate processes:

  1. Chatterbox TTS  -> a wav in your voice
  2. SadTalker       -> lip-synced mp4 from avatar.jpg + that wav

Separate processes because this laptop has 6 GB of VRAM and the Sentinel app
often holds ~4.4 GB of it; running both models in one process risks an OOM
that would take the running app down with it. A subprocess boundary returns
every byte between stages.

SadTalker itself is left untouched on disk. It predates NumPy 2 and
torchvision 0.17, so the two incompatibilities are shimmed in-process below
rather than by editing a third-party checkout.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SADTALKER_ROOT = r"C:\Users\potam\SadTalker"

DEFAULT_IMAGE = os.path.join(HERE, "avatar.jpg")
DEFAULT_VOICE = os.path.join(HERE, "my_voice_sample.wav")

# Which reference recording to clone for a given language. Cloning copies
# your phonemes and prosody, so an English reference makes Greek come out
# English-inflected and vice versa -- one sample per language you actually
# speak is what keeps both sounding native. Add more as you record them;
# anything unlisted falls back to DEFAULT_VOICE.
VOICE_BY_LANGUAGE = {
    "en": "my_voice_sample.wav",
    "el": "my_voice_sample_GR.wav",
}


def resolve_voice(requested, language):
    """Pick the reference recording, honouring an explicit --voice."""
    if requested and requested != "auto":
        path = requested if os.path.isabs(requested) else os.path.join(HERE, requested)
        if not os.path.exists(path):
            raise SystemExit(f"[tts] voice sample not found: {path}")
        return path

    mapped = VOICE_BY_LANGUAGE.get(language)
    if mapped:
        path = os.path.join(HERE, mapped)
        if os.path.exists(path):
            return path
        print(f"[tts] no {mapped} for '{language}' -- falling back to "
              f"{os.path.basename(DEFAULT_VOICE)}")

    if not os.path.exists(DEFAULT_VOICE):
        raise SystemExit(f"[tts] no voice sample found at {DEFAULT_VOICE}")
    return DEFAULT_VOICE


# ── compatibility shims ──────────────────────────────────────────────

def apply_sadtalker_shims():
    """Make a NumPy-1 / torchvision-0.16 era codebase import under current libs.

    Applied in-process, before any SadTalker import, so the checkout stays
    pristine and nothing here leaks into other environments.
    """
    import numpy as np

    # Aliases NumPy removed in 1.24/2.0. SadTalker still references them.
    for name, value in (
        ("VisibleDeprecationWarning", DeprecationWarning),
        ("float", float), ("int", int), ("bool", bool),
        ("object", object), ("str", str),
    ):
        if not hasattr(np, name):
            setattr(np, name, value)

    # torchvision >= 0.17 dropped transforms.functional_tensor, which basicsr
    # (pulled in by the gfpgan enhancer) imports by name.
    import torchvision.transforms.functional as tv_functional
    sys.modules.setdefault("torchvision.transforms.functional_tensor", tv_functional)


def patch_align_img():
    """Repair align_img's ragged np.array call under NumPy 2.

    align_img ends with:

        trans_params = np.array([w0, h0, s, t[0], t[1]])

    where s and t[*] are 1-element arrays, not scalars. NumPy 1.x built a
    ragged object array (emitting the VisibleDeprecationWarning that this
    very file suppresses); NumPy 2 raises instead. The only consumer,
    src/utils/preprocess.py, immediately does

        [float(item) for item in np.hsplit(trans_params, 5)]

    so five plain floats is what the value was always reduced to -- casting
    here is behaviour-preserving, not a guess.

    Patched at runtime, and on both modules: src/utils/preprocess.py binds
    align_img by name at import time, so replacing it only on the defining
    module would leave that copy untouched.

    Must be called after SADTALKER_ROOT is on sys.path.
    """
    import numpy as np
    import src.face3d.util.preprocess as face3d_preprocess
    import src.utils.preprocess as utils_preprocess

    original_pos = face3d_preprocess.POS
    original_resize = face3d_preprocess.resize_n_crop_img
    original_extract_5p = face3d_preprocess.extract_5p

    def align_img(img, lm, lm3D, mask=None, target_size=224., rescale_factor=102.):
        w0, h0 = img.size
        lm5p = lm if lm.shape[0] == 5 else original_extract_5p(lm)

        t, s = original_pos(lm5p.transpose(), lm3D.transpose())
        s = rescale_factor / s

        img_new, lm_new, mask_new = original_resize(
            img, lm, t, s, target_size=target_size, mask=mask)
        trans_params = np.array([w0, h0, float(s), float(t[0]), float(t[1])])
        return trans_params, img_new, lm_new, mask_new

    face3d_preprocess.align_img = align_img
    utils_preprocess.align_img = align_img


def detect_language(text):
    """Guess a language id from the script the text is written in.

    Deliberately script-based rather than a statistical detector: it needs no
    extra dependency, never mis-fires on a two-word sentence, and the case
    that actually matters here (Greek vs English) is a clean script split.

    Its limit is real though -- every Latin-script language looks like
    English to this function, so Spanish, French, German and the rest need
    --lang. Scripts that map to exactly one supported language are safe.
    """
    ranges = (
        ("el", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),   # Greek, Greek Extended
        ("ru", ((0x0400, 0x04FF),)),                     # Cyrillic
        ("he", ((0x0590, 0x05FF),)),                     # Hebrew
        ("ar", ((0x0600, 0x06FF),)),                     # Arabic
        ("hi", ((0x0900, 0x097F),)),                     # Devanagari
        ("ko", ((0xAC00, 0xD7AF), (0x1100, 0x11FF))),    # Hangul
        ("ja", ((0x3040, 0x309F), (0x30A0, 0x30FF))),    # Hiragana, Katakana
        ("zh", ((0x4E00, 0x9FFF),)),                     # CJK ideographs
    )
    counts = {lang: 0 for lang, _ in ranges}
    for char in text:
        code = ord(char)
        for lang, spans in ranges:
            if any(low <= code <= high for low, high in spans):
                counts[lang] += 1
                break

    best = max(counts, key=counts.get)
    return best if counts[best] else "en"


def resolve_device(requested):
    import torch

    if requested != "auto":
        return requested
    if not torch.cuda.is_available():
        return "cpu"
    try:
        free, _total = torch.cuda.mem_get_info()
        if free < 2 * 1024 ** 3:
            print(f"[warn] only {free / 1024 ** 3:.1f} GB VRAM free -- "
                  f"is the Sentinel app running? Falling back to CPU (slower).")
            return "cpu"
    except Exception:
        pass
    return "cuda"


# ── stage 1: speech ──────────────────────────────────────────────────

def stage_tts(args):
    import torchaudio as ta

    device = resolve_device(args.device)

    language = args.lang if args.lang != "auto" else detect_language(args.text)

    # The English-only checkpoint is the better-sounding one for English and
    # is the path already proven here, so it stays the default for 'en'.
    # Anything else goes to the multilingual checkpoint -- feeding Greek to
    # the English model produces fluent-sounding nonsense, not an error.
    use_multilingual = (args.tts_model == "multilingual"
                        or (args.tts_model == "auto" and language != "en"))

    voice = resolve_voice(args.voice, language)
    generate_kwargs = {"audio_prompt_path": voice}
    if args.exaggeration is not None:
        generate_kwargs["exaggeration"] = args.exaggeration
    if args.cfg_weight is not None:
        generate_kwargs["cfg_weight"] = args.cfg_weight

    if use_multilingual:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS, SUPPORTED_LANGUAGES

        if language not in SUPPORTED_LANGUAGES:
            raise SystemExit(
                f"[tts] language '{language}' is not supported. Choose one of: "
                + ", ".join(sorted(SUPPORTED_LANGUAGES)))
        print(f"[tts] Chatterbox multilingual on {device} "
              f"[{language} = {SUPPORTED_LANGUAGES[language]}]")
        model = ChatterboxMultilingualTTS.from_pretrained(device=device)
        generate_kwargs["language_id"] = language
    else:
        from chatterbox.tts import ChatterboxTTS

        print(f"[tts] Chatterbox English on {device}")
        model = ChatterboxTTS.from_pretrained(device=device)

    print(f"[tts] cloning voice from {os.path.basename(voice)}")
    wav = model.generate(args.text, **generate_kwargs)
    ta.save(args.wav_out, wav, model.sr)
    seconds = wav.shape[-1] / model.sr
    print(f"[tts] wrote {args.wav_out} ({seconds:.1f}s @ {model.sr} Hz)")


# ── stage 2: face ────────────────────────────────────────────────────

def stage_render(args):
    import runpy

    apply_sadtalker_shims()

    inference_py = os.path.join(SADTALKER_ROOT, "inference.py")
    if not os.path.exists(inference_py):
        raise SystemExit(f"[render] SadTalker not found at {SADTALKER_ROOT}")

    result_dir = tempfile.mkdtemp(prefix="sadtalker_", dir=args.work_dir)

    # inference.py derives its config root from sys.argv[0], and several of
    # its defaults (the BFM folder in particular) are cwd-relative -- so it
    # needs both argv[0] and the working directory pointed at the checkout.
    argv = [
        inference_py,
        "--driven_audio", args.wav_out,
        "--source_image", args.image,
        "--result_dir", result_dir,
        "--checkpoint_dir", os.path.join(SADTALKER_ROOT, "checkpoints"),
        "--size", str(args.size),
        "--preprocess", args.preprocess,
        "--batch_size", str(args.batch_size),
        "--pose_style", str(args.pose_style),
    ]
    if args.still:
        argv.append("--still")
    if args.enhancer:
        argv += ["--enhancer", args.enhancer]
    if resolve_device(args.device) == "cpu":
        argv.append("--cpu")

    previous_cwd = os.getcwd()
    previous_argv = sys.argv
    sys.path.insert(0, SADTALKER_ROOT)
    os.chdir(SADTALKER_ROOT)
    sys.argv = argv
    patch_align_img()  # needs SadTalker importable, so not in apply_sadtalker_shims
    try:
        print(f"[render] SadTalker size={args.size} preprocess={args.preprocess} "
              f"enhancer={args.enhancer or 'none'}")
        runpy.run_path(inference_py, run_name="__main__")
    finally:
        sys.argv = previous_argv
        os.chdir(previous_cwd)

    # inference.py writes <result_dir>/<timestamp>.mp4 next to its scratch dir.
    produced = sorted(glob.glob(os.path.join(result_dir, "*.mp4")),
                      key=os.path.getmtime)
    if not produced:
        raise SystemExit(f"[render] SadTalker produced no mp4 under {result_dir}")

    shutil.move(produced[-1], args.output)
    shutil.rmtree(result_dir, ignore_errors=True)
    print(f"[render] wrote {args.output}")


# ── orchestration ────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        description="Turn text into an mp4 of your avatar speaking it in your voice.")
    parser.add_argument("text", nargs="*", help="what the avatar should say")
    parser.add_argument("--text-file", help="read the text from a UTF-8 file instead")
    parser.add_argument("-o", "--output", help="output .mp4 (default: avatar_<timestamp>.mp4 here)")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="portrait to animate")
    parser.add_argument("--voice", default="auto",
                        help="voice sample to clone. 'auto' picks by language "
                             f"({', '.join(f'{k}->{v}' for k, v in VOICE_BY_LANGUAGE.items())})")
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])

    # Quality-first defaults: 512 + gfpgan is roughly 7x slower than 256 with
    # no enhancer, and it is the difference between invented mush and
    # recognisable teeth. Use --fast when you only need to check the wording.
    parser.add_argument("--size", type=int, default=512, choices=[256, 512],
                        help="face render size (default 512)")
    parser.add_argument("--preprocess", default="crop",
                        choices=["crop", "extcrop", "resize", "full", "extfull"])
    parser.add_argument("--enhancer", default="gfpgan",
                        choices=["none", "gfpgan", "RestoreFormer"],
                        help="face restoration pass (default gfpgan)")
    parser.add_argument("--fast", action="store_true",
                        help="draft preset: 256, no enhancer, ~7x faster")
    parser.add_argument("--still", action="store_true",
                        help="minimal head motion; steadier for a straight-on portrait")
    parser.add_argument("--batch-size", type=int, default=2, dest="batch_size")
    parser.add_argument("--pose-style", type=int, default=0, dest="pose_style")

    parser.add_argument("--lang", default="auto",
                        help="language id (el, en, es, fr, de, ...). 'auto' infers it "
                             "from the script the text is written in -- reliable for "
                             "Greek, but every Latin-script language reads as English, "
                             "so name it explicitly for those.")
    parser.add_argument("--tts-model", default="auto", dest="tts_model",
                        choices=["auto", "english", "multilingual"],
                        help="auto: English checkpoint for English, multilingual otherwise")
    parser.add_argument("--exaggeration", type=float, default=None,
                        help="Chatterbox emotion intensity (default: model's own)")
    parser.add_argument("--cfg-weight", type=float, default=None, dest="cfg_weight")
    parser.add_argument("--keep-wav", action="store_true", help="keep the intermediate speech wav")

    # Internal: which half of the job this process is running.
    parser.add_argument("--_stage", choices=["tts", "render"], help=argparse.SUPPRESS)
    parser.add_argument("--_wav", dest="wav_out", help=argparse.SUPPRESS)
    parser.add_argument("--_work", dest="work_dir", help=argparse.SUPPRESS)
    return parser


def run_stage_subprocess(stage, args, wav_out, work_dir):
    """Re-invoke this script for one stage, so its VRAM is freed on exit."""
    argv = [sys.executable, os.path.abspath(__file__), "--_stage", stage,
            "--_wav", wav_out, "--_work", work_dir,
            "--device", args.device, "--image", args.image, "--voice", args.voice,
            "--size", str(args.size), "--preprocess", args.preprocess,
            "--batch-size", str(args.batch_size), "--pose-style", str(args.pose_style),
            "--lang", args.lang, "--tts-model", args.tts_model,
            "-o", args.output]
    if args.still:
        argv.append("--still")
    # Always explicit: the child defaults to gfpgan, so omitting this would
    # silently re-enable the enhancer that --fast just turned off.
    argv += ["--enhancer", args.enhancer or "none"]
    if args.exaggeration is not None:
        argv += ["--exaggeration", str(args.exaggeration)]
    if args.cfg_weight is not None:
        argv += ["--cfg-weight", str(args.cfg_weight)]
    argv.append(args.text)

    result = subprocess.run(argv)
    if result.returncode != 0:
        raise SystemExit(f"[{stage}] stage failed with exit code {result.returncode}")


def main():
    args = build_parser().parse_args()

    # "none" is how the CLI spells "no enhancer"; SadTalker wants it absent.
    if args.enhancer == "none":
        args.enhancer = None
    if args.fast:
        args.size, args.enhancer = 256, None

    if args.text_file:
        with open(args.text_file, encoding="utf-8") as handle:
            args.text = handle.read().strip()
    else:
        args.text = " ".join(args.text).strip()

    if not args.text:
        raise SystemExit("nothing to say: pass text, or --text-file")

    if not os.path.exists(args.image):
        raise SystemExit(f"portrait not found: {args.image}")

    # Videos land next to this script, not in whatever directory the .bat
    # happened to be launched from. An absolute --output still wins.
    if not args.output:
        args.output = os.path.join(HERE, f"avatar_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
    elif not os.path.isabs(args.output):
        args.output = os.path.join(HERE, args.output)
    args.output = os.path.abspath(args.output)

    # Child process: do one stage and stop.
    if args._stage == "tts":
        return stage_tts(args)
    if args._stage == "render":
        return stage_render(args)

    # Parent: run both stages, each in its own process.
    work_dir = tempfile.mkdtemp(prefix="avatar_")
    wav_out = args.wav_out or os.path.join(work_dir, "speech.wav")
    started = time.time()
    try:
        run_stage_subprocess("tts", args, wav_out, work_dir)
        run_stage_subprocess("render", args, wav_out, work_dir)
        if args.keep_wav:
            kept = os.path.splitext(args.output)[0] + ".wav"
            shutil.copy2(wav_out, kept)
            print(f"kept speech audio -> {kept}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\nDone in {time.time() - started:.0f}s -> {args.output}")


if __name__ == "__main__":
    main()
