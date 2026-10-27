"""
Speaker Diarization Script
Reads the MP3 from C:\\Users\\potam\\ast2\\audio_examples and tags speakers using pyannote.

Requirements:
    pip install pyannote.audio pydub torch torchaudio
    ffmpeg on PATH: https://ffmpeg.org/download.html
    HuggingFace token with access to:
      https://huggingface.co/pyannote/speaker-diarization-3.1
      https://huggingface.co/pyannote/segmentation-3.0
"""

import sys
import os
import glob
import tempfile

# ── Compatibility patches (must run before any pyannote/speechbrain import) ───

# 1. Stub torchcodec (not installed) — use plain module objects, NOT MagicMock,
#    to avoid MagicMock leaking into numeric comparisons inside pyannote.
import types
_tc = types.ModuleType("torchcodec")
_tc_dec = types.ModuleType("torchcodec.decoders")
_tc_core = types.ModuleType("torchcodec.decoders._core")
sys.modules.setdefault("torchcodec", _tc)
sys.modules.setdefault("torchcodec.decoders", _tc_dec)
sys.modules.setdefault("torchcodec.decoders._core", _tc_core)

# 2. speechbrain calls torchaudio.list_audio_backends() at import time;
#    the function was removed in newer torchaudio — patch it in.
import torchaudio as _ta
if not hasattr(_ta, "list_audio_backends"):
    _ta.list_audio_backends = lambda: []

# ─────────────────────────────────────────────────────────────────────────────

from pathlib import Path


def find_mp3(directory: str) -> str:
    matches = glob.glob(os.path.join(directory, "*.mp3"))
    if not matches:
        raise FileNotFoundError(f"No MP3 files found in: {directory}")
    if len(matches) > 1:
        raise ValueError(f"Expected exactly one MP3, found {len(matches)}: {matches}")
    return matches[0]


def mp3_to_wav(mp3_path: str, wav_path: str) -> None:
    try:
        from pydub import AudioSegment
    except ImportError:
        sys.exit("pydub not installed. Run: pip install pydub  (also needs ffmpeg on PATH)")
    print(f"Converting {mp3_path}\n      -->  {wav_path} ...")
    audio = AudioSegment.from_mp3(mp3_path)
    audio = audio.set_channels(1).set_frame_rate(16000)
    audio.export(wav_path, format="wav")
    duration = len(audio) / 1000
    print(f"Conversion complete. Duration: {duration:.1f}s ({duration/60:.1f} min)")


def load_pipeline(hf_token: str):
    from pyannote.audio import Pipeline
    print("Loading pyannote/speaker-diarization-3.1 ...")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )
    print("Pipeline loaded.")
    return pipeline


def diarize(wav_path: str, pipeline, num_speakers: int = None):
    kwargs = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers

    print("Running diarization ...", flush=True)
    try:
        from pyannote.audio.pipelines.utils.hook import ProgressHook
        with ProgressHook() as hook:
            result = pipeline(wav_path, hook=hook, **kwargs)
    except Exception:
        result = pipeline(wav_path, **kwargs)

    print("\nDiarization complete.")
    return result


def print_results(diarization, speaker_mapping=None, output_rttm: str = None) -> None:
    print("\n── Speaker turns ──────────────────────")
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        start_m, start_s = divmod(turn.start, 60)
        end_m,   end_s   = divmod(turn.end,   60)
        spk_name = speaker_mapping.get(speaker, speaker) if speaker_mapping else speaker
        print(f"{int(start_m):02d}:{start_s:05.2f} - {int(end_m):02d}:{end_s:05.2f}  {spk_name}")

    speakers = {lbl for _, _, lbl in diarization.itertracks(yield_label=True)}
    mapped_speakers = sorted([speaker_mapping.get(lbl, lbl) if speaker_mapping else lbl for lbl in speakers])
    print(f"\nSpeakers detected: {len(speakers)}  ({', '.join(mapped_speakers)})")

    if output_rttm:
        with open(output_rttm, "w") as f:
            diarization.write_rttm(f)
        print(f"RTTM saved: {output_rttm}")


def main():
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from misc.secrets import hf_token

    AUDIO_DIR    = os.path.join(project_root, "audio_examples")
    HF_TOKEN     = hf_token()
    NUM_SPEAKERS = None   # set e.g. to 2 if you know the speaker count

    if not HF_TOKEN:
        sys.exit(
            "HF_TOKEN not set.\n"
            "Run:  set HF_TOKEN=hf_your_token_here\n"
            "Token page:  https://huggingface.co/settings/tokens\n"
            "Accept licences at:\n"
            "  https://huggingface.co/pyannote/speaker-diarization-3.1\n"
            "  https://huggingface.co/pyannote/segmentation-3.0"
        )

    mp3_path = find_mp3(AUDIO_DIR)
    print(f"Found MP3: {mp3_path}")

    # Convert MP3 -> WAV
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_path = tmp.name
    tmp.close()

    mp3_to_wav(mp3_path, wav_path)

    # Load pipeline (slow - outside try/finally so wav isn't deleted on failure)
    pipeline = load_pipeline(HF_TOKEN)

    # Diarize, then clean up wav regardless of outcome
    speaker_mapping = {}
    try:
        diarization_raw = diarize(wav_path, pipeline, num_speakers=NUM_SPEAKERS)
        diarization = getattr(diarization_raw, "speaker_diarization", diarization_raw)
        
        # Slicing & Speaker matching
        try:
            print("\n── Speaker Recognition ────────────────")
            import torch
            from speechbrain.inference import EncoderClassifier
            
            # Load SpeechBrain model
            device = "cuda" if torch.cuda.is_available() else "cpu"
            classifier = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                run_opts={"device": device}
            )
            
            # Use modular SpeakerProfileManager and load_audio_as_16k_tensor
            from ast_src.speaker_id import SpeakerProfileManager, load_audio_as_16k_tensor
            
            speakers_dir = Path(__file__).resolve().parent / "speakers"
            profile_manager = SpeakerProfileManager(speakers_dir, classifier)
            profile_manager.enroll_speakers()
            
            # Load raw WAV data
            full_audio_signal = load_audio_as_16k_tensor(Path(wav_path))
            
            # Group chunks by raw speaker label
            raw_speaker_signals = {}
            for turn, _, raw_speaker in diarization.itertracks(yield_label=True):
                if raw_speaker not in raw_speaker_signals:
                    raw_speaker_signals[raw_speaker] = []
                start_sample = int(turn.start * 16000)
                end_sample = int(turn.end * 16000)
                start_sample = max(0, min(start_sample, len(full_audio_signal)))
                end_sample = max(0, min(end_sample, len(full_audio_signal)))
                
                if end_sample > start_sample:
                    raw_speaker_signals[raw_speaker].append(full_audio_signal[start_sample:end_sample])
                    
            for raw_speaker, chunks in raw_speaker_signals.items():
                if not chunks:
                    speaker_mapping[raw_speaker] = raw_speaker
                    continue
                concat_signal = torch.cat(chunks)
                name, score, scores = profile_manager.identify_speaker(concat_signal, threshold=0.35)
                score_details = ", ".join([f"{n}: {s:.3f}" for n, s in scores.items()])
                if name != "Unknown":
                    print(f"🎯 Matched {raw_speaker} to '{name}' with similarity {score:.3f} (Details: {score_details})")
                    speaker_mapping[raw_speaker] = name
                else:
                    closest_spk = max(scores, key=scores.get) if scores else "None"
                    closest_score = scores.get(closest_spk, 0.0) if scores else 0.0
                    print(f"❓ {raw_speaker} could not be matched. Closest is '{closest_spk}' with score {closest_score:.3f} (Details: {score_details})")
                    speaker_mapping[raw_speaker] = raw_speaker
            print("───────────────────────────────────────\n")
        except Exception as e:
            print(f"Error during speaker identification: {e}")
            
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)

    rttm_out = str(Path(mp3_path).with_suffix(".rttm"))
    print_results(diarization, speaker_mapping=speaker_mapping, output_rttm=rttm_out)


if __name__ == "__main__":
    main()


