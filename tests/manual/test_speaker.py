#!/usr/bin/env python3
"""
Standalone Speaker Identification, Diarization, and Transcription Script.
Takes an audio file, transcribes it, and attributes segments to real speaker names
based on the speaker enrollment files in the 'speakers' folder.

Requirements:
    pip install pyannote.audio pydub torch torchaudio speechbrain soundfile
"""

import os
import sys
import json
import glob
import tempfile
import argparse
import types
from pathlib import Path

# ── 1. COMPATIBILITY PATCHES ──
# Run BEFORE importing pyannote/speechbrain/torchaudio to avoid import & compatibility errors

# Patch torchaudio for modern version compatibility
import torchaudio as _ta
if not hasattr(_ta, "list_audio_backends"):
    _ta.list_audio_backends = lambda: []
if not hasattr(_ta, "set_audio_backend"):
    _ta.set_audio_backend = lambda x: None

# Mock torchcodec to prevent import-time failures on systems where it's not installed
for _m in ["torchcodec", "torchcodec.decoders", "torchcodec.decoders._core"]:
    if _m not in sys.modules:
        sys.modules[_m] = types.ModuleType(_m)

# Suppress Windows Error Dialogs
try:
    import ctypes
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except AttributeError:
    pass

import torch
import numpy as np
import soundfile as sf
from pydub import AudioSegment
from faster_whisper import WhisperModel
from pyannote.audio import Pipeline
from speechbrain.inference import EncoderClassifier

# Ensure project root is in path
project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from misc.load_parameters import parameters
from misc.secrets import hf_token
from ast_src.speaker_id import SpeakerProfileManager, load_audio_as_16k_tensor

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def find_default_mp3() -> Path:
    AUDIO_DIR = project_root / "audio_examples"
    matches = list(AUDIO_DIR.glob("*.mp3"))
    if not matches:
        raise FileNotFoundError(f"No MP3 files found in: {AUDIO_DIR}")
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description="Standalone Speaker Diarization, Transcription, and Identification Script")
    parser.add_argument("audio_path", nargs="?", help="Path to the audio file to analyze")
    parser.add_argument("--enroll", action="store_true", help="Compile and save speaker profiles to model checkpoint")
    parser.add_argument("--threshold", type=float, default=0.35, help="Cosine similarity threshold for speaker recognition (default: 0.35)")
    parser.add_argument("--language", type=str, default="el", help="Transcription language, e.g., 'el' or 'en' (default: 'el')")
    args = parser.parse_args()

    # Clear previous transcript.txt
    output_txt_path = Path(__file__).resolve().parent / "transcript.txt"
    if output_txt_path.exists():
        output_txt_path.unlink()

    # Determine input path only if we are not enrolling
    if not args.enroll:
        if args.audio_path:
            audio_path = Path(args.audio_path)
        else:
            try:
                audio_path = find_default_mp3()
                print(f"No audio file specified. Defaulting to: {audio_path}")
            except FileNotFoundError as e:
                sys.exit(f"Error: {e}. Please specify an audio file.")

        if not audio_path.exists():
            sys.exit(f"Error: Input audio file '{audio_path}' does not exist.")

    # 1. Initialize SpeechBrain EncoderClassifier
    print("\n── [Step 2/5] Initializing SpeechBrain EncoderClassifier ─")
    print(f"Loading speechbrain/spkrec-ecapa-voxceleb on {DEVICE}...")
    classifier = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        run_opts={"device": DEVICE}
    )
    print("✅ SpeechBrain model loaded.")

    # 2. Enroll speakers or load pre-existing model
    speakers_dir = project_root / "speakers"
    profile_manager = SpeakerProfileManager(speakers_dir, classifier)
    
    # Beside the voices it is built from; see speakers/build_speaker_model.py
    MODEL_PATH = project_root / "speakers" / "speakers_model.pth"
    
    if args.enroll:
        profile_manager.enroll_speakers()
        profile_manager.save_model(MODEL_PATH)
        return
        
    # Loading model or enrolling on the fly
    if not profile_manager.load_model(MODEL_PATH):
        print(f"⚠️ Pre-built model '{MODEL_PATH}' not found. Automatically enrolling speakers...")
        profile_manager.enroll_speakers()
        profile_manager.save_model(MODEL_PATH)

    # 3. Load PyAnnote Diarization Pipeline
    print("── [Step 3/5] Loading PyAnnote Diarization Pipeline ────")
    try:
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=hf_token()
        )
        pipeline.to(torch.device(DEVICE))
        print("✅ PyAnnote pipeline loaded.")
    except Exception as e:
        sys.exit(f"❌ Failed to load PyAnnote pipeline: {e}\nCheck your HF_TOKEN and network connection.")

    # 4. Prepare WAV version of input file (needed for PyAnnote)
    print("\nConverting input file to 16kHz mono WAV...")
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_path = Path(tmp_wav.name)
    tmp_wav.close()

    try:
        # Convert audio
        audio = AudioSegment.from_file(str(audio_path))
        audio = audio.set_channels(1).set_frame_rate(16000)
        audio.export(str(wav_path), format="wav")
        
        # Load full audio tensor for slicing speaker turns
        full_audio_signal = load_audio_as_16k_tensor(wav_path)
        
        # Run Diarization
        print("\n── [Step 4/5] Running Diarization & Speaker Matching ──")
        print("Running pyannote speaker-diarization-3.1...")
        diarization_result = pipeline(str(wav_path))
        diarization = getattr(diarization_result, "speaker_diarization", diarization_result)
        
        # Group audio chunks by pyannote speaker label
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

        # Identify each pyannote speaker label
        speaker_mapping = {}
        for raw_speaker, chunks in raw_speaker_signals.items():
            if not chunks:
                speaker_mapping[raw_speaker] = f"Unknown ({raw_speaker})"
                continue
                
            concat_signal = torch.cat(chunks)
            name, score, scores = profile_manager.identify_speaker(concat_signal, threshold=args.threshold)
            
            score_details = ", ".join([f"{n}: {s:.3f}" for n, s in scores.items()])
            if name != "Unknown":
                print(f"🎯 Matched {raw_speaker} to '{name}' with cosine similarity: {score:.3f} (Details: {score_details})")
                speaker_mapping[raw_speaker] = name
            else:
                closest_speaker = max(scores, key=scores.get) if scores else "None"
                closest_score = scores.get(closest_speaker, 0.0) if scores else 0.0
                print(f"❓ {raw_speaker} could not be matched. Closest is '{closest_speaker}' with score {closest_score:.3f} (Details: {score_details})")
                speaker_mapping[raw_speaker] = raw_speaker

        # 5. Whisper Transcription and Alignment
        print("\n── [Step 5/5] Running Whisper Transcription ───────────")
        print("Loading Whisper model (large-v3)...")
        try:
            whisper_model = WhisperModel("large-v3", device=DEVICE, compute_type="float16" if DEVICE == "cuda" else "int8")
        except Exception as e:
            if DEVICE == "cuda":
                print(f"⚠️ GPU Out of Memory or loading error: {e}")
                print("Falling back to CPU for Whisper transcription...")
                torch.cuda.empty_cache()
                whisper_model = WhisperModel("large-v3", device="cpu", compute_type="int8")
            else:
                raise e
                
        print("Transcribing audio...")
        
        segments, _ = whisper_model.transcribe(
            str(wav_path),
            task='transcribe',
            beam_size=5,
            language=args.language,
            vad_filter=False,
            suppress_blank=True,
            suppress_tokens=[-1],
            temperature=0.0
        )

        def format_timestamp(seconds):
            m, s = divmod(seconds, 60)
            return f"{int(m):02d}:{s:05.2f}"

        print("\n" + "="*60)
        print("FINAL SPEAKER-ATTRIBUTED TRANSCRIPT:")
        print("="*60)
        
        has_segments = False
        transcript_lines = []
        for segment in segments:
            if len(segment.text.strip()) < 3:
                continue
                
            has_segments = True
            text = segment.text.strip()
            
            # Find which PyAnnote speaker had the highest overlap with this Whisper segment
            from pyannote.core import Segment as PySegment
            whisper_seg = PySegment(segment.start, segment.end)
            
            speaker_counts = {}
            for s in diarization.crop(whisper_seg).itertracks(yield_label=True):
                overlap = s[0].duration
                speaker_counts[s[2]] = speaker_counts.get(s[2], 0) + overlap
            
            current_speaker = "Unknown"
            if speaker_counts:
                raw_speaker = max(speaker_counts, key=speaker_counts.get)
                current_speaker = speaker_mapping.get(raw_speaker, raw_speaker)
                
            timestamp_str = f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}]"
            line_str = f"{timestamp_str} {current_speaker}: {text}"
            print(line_str)
            transcript_lines.append(line_str)
            
        if not has_segments:
            print("(No speech detected in the audio file)")
            transcript_lines.append("(No speech detected in the audio file)")
            
        print("="*60 + "\n")

        # Write to transcript.txt in the script's folder
        with open(output_txt_path, "w", encoding="utf-8") as f_out:
            f_out.write(f"=== TRANSCRIPT FOR: {audio_path.name} ===\n")
            f_out.write("\n".join(transcript_lines) + "\n")
            f_out.write("=" * 60 + "\n")
        print(f"🎉 Saved transcript text to: {output_txt_path}\n")

    finally:
        # Clean up temporary WAV file
        if wav_path.exists():
            wav_path.unlink()


if __name__ == "__main__":
    main()
