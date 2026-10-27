import os
import sys
import torch
import torchaudio
import numpy as np
import ctypes
import types
import time
import argparse
from pathlib import Path
from pydub import AudioSegment

# --- 1. PRE-IMPORT COMPATIBILITY SHIMS ---
# These must run BEFORE importing pyannote or other heavy libs

# Suppress Windows Error Dialogs
try:
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except AttributeError:
    pass

# Patch torchaudio for modern version compatibility
if not hasattr(torchaudio, "list_audio_backends"):
    torchaudio.list_audio_backends = lambda: []
if not hasattr(torchaudio, "set_audio_backend"):
    torchaudio.set_audio_backend = lambda x: None

# Mock torchcodec if it doesn't exist
for _m in ["torchcodec", "torchcodec.decoders", "torchcodec.decoders._core"]:
    if _m not in sys.modules:
        sys.modules[_m] = types.ModuleType(_m)

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- 2. IMPORTS ---
from ast_src.streaming_ASR import ASRTranscriber
from misc.load_parameters import parameters

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
# Beside the voices it is built from; see speakers/build_speaker_model.py
MODEL_PATH = os.path.join("speakers", "speakers_model.pth")


class IdentifiedASRTranscriber(ASRTranscriber):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_diarization_result = None

    def get_speaker_labels(self, file_path):
        diarization_result = super().get_speaker_labels(file_path)
        self.last_diarization_result = diarization_result
        return diarization_result


def save_speaker_model(speakers_dir: str, classifier, output_model_path: str) -> bool:
    """
    Builds and saves the speaker profiles model as a PyTorch .pth serialized checkpoint.
    """
    print("\n── Creating Speaker Model (Enrollment) ──")
    speakers_dir = Path(speakers_dir)
    speaker_profiles = {}
    
    if not speakers_dir.exists():
        print(f"Error: Speakers directory '{speakers_dir}' does not exist.")
        return False
        
    from ast_src.speaker_id import load_audio_as_16k_tensor
    
    for speaker_folder in sorted(speakers_dir.iterdir()):
        if not speaker_folder.is_dir():
            continue
            
        speaker_name = speaker_folder.name
        audio_files = []
        for ext in ["*.flac", "*.wav", "*.mp3", "*.m4a"]:
            audio_files.extend(list(speaker_folder.glob(ext)))
            audio_files.extend(list(speaker_folder.glob(ext.upper())))
            
        if not audio_files:
            print(f"Speaker '{speaker_name}': No audio files found. Skipping.")
            continue
            
        print(f"Processing '{speaker_name}' with {len(audio_files)} files...")
        embeddings = []
        for file_path in audio_files:
            try:
                signal = load_audio_as_16k_tensor(file_path)
                emb = classifier.encode_batch(signal.unsqueeze(0))
                embeddings.append(emb.squeeze())
            except Exception as e:
                print(f"  Error processing '{file_path.name}': {e}")
                
        if embeddings:
            mean_emb = torch.stack(embeddings).mean(dim=0)
            speaker_profiles[speaker_name] = mean_emb
            print(f"✅ Speaker '{speaker_name}' profiles compiled.")
            
    if speaker_profiles:
        torch.save(speaker_profiles, output_model_path)
        print(f"🎉 Saved speaker model to: {output_model_path}")
        print(f"Enrolled speakers: {list(speaker_profiles.keys())}")
        print("────────────────────────────────────────\n")
        return True
    else:
        print("❌ Enrollment failed: No profiles were created.")
        return False


def load_speaker_model(model_path: str):
    """
    Loads pre-compiled speaker profiles from a PyTorch .pth checkpoint.
    """
    if not os.path.exists(model_path):
        return None
    try:
        profiles = torch.load(model_path, map_location=torch.device(DEVICE))
        return profiles
    except Exception as e:
        print(f"Error loading speaker model {model_path}: {e}")
        return None


def process_audio_file(audio_path: str, asr: IdentifiedASRTranscriber, classifier, speaker_profiles, language: str = 'el'):
    """
    Processes a single audio file: running transcription, diarization, and speaker verification.
    """
    print(f"\nProcessing file: {audio_path}")
    if not os.path.exists(audio_path):
        print(f"!!! Error: File '{audio_path}' does not exist.")
        return
        
    start_time = time.time()
    result = asr.transcribe(audio_path, language=language)
    duration = time.time() - start_time
    
    # Run speaker identification and post-process results
    if result and asr.last_diarization_result and speaker_profiles:
        try:
            from ast_src.speaker_id import load_audio_as_16k_tensor
            
            # Load raw audio signal for slicing speaker turns
            full_audio_signal = load_audio_as_16k_tensor(Path(audio_path))
            
            # Group chunks by raw speaker label
            raw_speaker_signals = {}
            diarization = getattr(asr.last_diarization_result, "speaker_diarization", asr.last_diarization_result)
            for turn, _, raw_speaker in diarization.itertracks(yield_label=True):
                if raw_speaker not in raw_speaker_signals:
                    raw_speaker_signals[raw_speaker] = []
                start_sample = int(turn.start * 16000)
                end_sample = int(turn.end * 16000)
                start_sample = max(0, min(start_sample, len(full_audio_signal)))
                end_sample = max(0, min(end_sample, len(full_audio_signal)))
                
                if end_sample > start_sample:
                    raw_speaker_signals[raw_speaker].append(full_audio_signal[start_sample:end_sample])
            
            # Identify each speaker
            for raw_speaker, chunks in raw_speaker_signals.items():
                if not chunks:
                    continue
                concat_signal = torch.cat(chunks)
                
                try:
                    emb = classifier.encode_batch(concat_signal.unsqueeze(0)).squeeze()
                except Exception as e:
                    print(f"Error extracting segment embedding: {e}")
                    continue
                    
                best_name = "Unknown"
                best_score = -1.0
                scores = {}
                
                for name, profile in speaker_profiles.items():
                    sim = torch.nn.functional.cosine_similarity(emb, profile, dim=0).item()
                    scores[name] = sim
                    if sim > best_score:
                        best_score = sim
                        best_name = name
                
                try:
                    spk_num = int(raw_speaker.split('_')[-1])
                    speaker_label = f"Speaker_{spk_num}"
                except Exception:
                    speaker_label = raw_speaker.capitalize()
                    
                score_details = ", ".join([f"{n}: {s:.3f}" for n, s in scores.items()])
                print(f"🎯 Matched {raw_speaker} to '{best_name}' with similarity {best_score:.3f} (Details: {score_details})")
                
                # Replace in transcription result
                result = result.replace(f" {speaker_label}:", f" {best_name}:")
                if result.startswith(f"{speaker_label}:"):
                    result = result.replace(f"{speaker_label}:", f"{best_name}:", 1)
                    
        except Exception as e:
            print(f"Error matching speakers: {e}")
            
    # Print results
    print("\n" + "="*50)
    print(f"TRANSCRIPTION RESULT FOR: {os.path.basename(audio_path)}")
    print(f"({duration:.1f}s processing time)")
    print("-" * 50)
    if result:
        print(result)
    else:
        print("(No speech detected or transcription failed)")
    print("="*50 + "\n")

    # Write/Append to transcript.txt in the script's folder
    output_txt_path = Path(__file__).resolve().parent / "transcript.txt"
    with open(output_txt_path, "a", encoding="utf-8") as f_out:
        f_out.write(f"=== TRANSCRIPT FOR: {os.path.basename(audio_path)} ===\n")
        f_out.write(f"({duration:.1f}s processing time)\n")
        f_out.write("-" * 50 + "\n")
        if result:
            f_out.write(result + "\n")
        else:
            f_out.write("(No speech detected or transcription failed)\n")
        f_out.write("=" * 50 + "\n\n")


def main():
    parser = argparse.ArgumentParser(description="Integrated Diarized Transcription and Speaker Verification CLI")
    parser.add_argument("--enroll", action="store_true", help="Compile and save speaker profiles to model checkpoint")
    parser.add_argument("--input", type=str, help="Path to a single audio file or a folder of audio files to process")
    parser.add_argument("--language", type=str, default="el", help="Transcription language, e.g., 'el' or 'en' (default: 'el')")
    args = parser.parse_args()

    # Clear previous transcript.txt
    output_txt_path = Path(__file__).resolve().parent / "transcript.txt"
    if output_txt_path.exists():
        output_txt_path.unlink()

    # Default fallback: if no CLI arguments are supplied, run standard MP3 integration test
    if not args.enroll and not args.input:
        AUDIO_DIR = os.path.join(project_root, "audio_examples")
        test_files = [f for f in os.listdir(AUDIO_DIR) if f.endswith(".mp3") or f.endswith(".wav")]
        if test_files:
            args.input = os.path.join(AUDIO_DIR, test_files[0])
            print(f"No arguments provided. Defaulting to: {args.input}")
        else:
            parser.print_help()
            return

    # Load HF token from the single credential source (misc/secrets.py)
    from misc.secrets import hf_token as _hf_token
    hf_token = _hf_token()

    # Initialize SpeechBrain Classifier (shared for both enrollment and inference)
    from speechbrain.inference import EncoderClassifier
    print(f"Initializing SpeechBrain EncoderClassifier on {DEVICE}...")
    classifier = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        run_opts={"device": DEVICE}
    )

    # 1. Action: Enroll Speakers
    if args.enroll:
        speakers_dir = os.path.join(project_root, "speakers")
        success = save_speaker_model(speakers_dir, classifier, MODEL_PATH)
        if not success:
            sys.exit("Enrollment failed.")
        return

    # 2. Action: Run Inference / Process Inputs
    if args.input:
        # Load pre-built speaker model
        speaker_profiles = load_speaker_model(MODEL_PATH)
        if speaker_profiles is None:
            print(f"⚠️ Pre-built model '{MODEL_PATH}' not found. Automatically enrolling speakers...")
            speakers_dir = os.path.join(project_root, "speakers")
            save_speaker_model(speakers_dir, classifier, MODEL_PATH)
            speaker_profiles = load_speaker_model(MODEL_PATH)

        # Initialize IdentifiedASRTranscriber
        print("\nInitializing IdentifiedASRTranscriber (Whisper + Pyannote)...")
        orig_diar_enabled = parameters.get_parameters().get("diarization", {}).get("enabled", False)
        parameters.get_parameters()["diarization"]["enabled"] = True

        try:
            asr = IdentifiedASRTranscriber()
            if asr.diarization_pipeline is None:
                sys.exit("!!! FAILURE: Diarization pipeline failed to load in ASRTranscriber.")

            input_path = Path(args.input)
            if input_path.is_dir():
                print(f"Scanning directory: {input_path}")
                audio_extensions = [".wav", ".mp3", ".flac", ".m4a"]
                audio_files = []
                for ext in audio_extensions:
                    audio_files.extend(list(input_path.rglob(f"*{ext}")))
                    audio_files.extend(list(input_path.rglob(f"*{ext.upper()}")))
                
                audio_files = sorted(list(set(audio_files)))
                if not audio_files:
                    sys.exit(f"No audio files found in directory '{input_path}' with extensions {audio_extensions}")
                
                print(f"Processing {len(audio_files)} files...")
                for f in audio_files:
                    process_audio_file(str(f), asr, classifier, speaker_profiles, language=args.language)
            else:
                process_audio_file(str(input_path), asr, classifier, speaker_profiles, language=args.language)
        finally:
            # Restore original parameters state
            parameters.get_parameters()["diarization"]["enabled"] = orig_diar_enabled


if __name__ == "__main__":
    main()
