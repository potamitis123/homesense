"""Speaker enrollment and identification against the local speakers/ folder.

Used by the live ASR pipeline to map pyannote labels (SPEAKER_00, ...) to
enrolled names (Ilyas, Voula, Rafael, ...). Profiles live in
speakers/speakers_model.pth, beside the recordings they were built from.
Rebuild after adding or replacing voices with:

    python speakers/build_speaker_model.py

That script also reports how well the new profiles separate, which is worth
reading: an enrolment can complete and still produce profiles too alike to
tell apart, and nothing downstream says so.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
from pydub import AudioSegment


def load_audio_as_16k_tensor(file_path: Path) -> torch.Tensor:
    """Load any audio file as a mono float32 tensor at 16 kHz."""
    file_path_str = str(file_path)
    try:
        audio = AudioSegment.from_file(file_path_str)
        audio = audio.set_frame_rate(16000).set_channels(1)

        samples = np.array(audio.get_array_of_samples())
        if audio.sample_width == 2:
            samples = samples.astype(np.float32) / 32768.0
        elif audio.sample_width == 4:
            samples = samples.astype(np.float32) / 2147483648.0
        elif audio.sample_width == 1:
            samples = (samples.astype(np.float32) - 128.0) / 128.0
        else:
            raise ValueError(f"Unusual sample width: {audio.sample_width}")
        return torch.tensor(samples, dtype=torch.float32)
    except Exception as e:
        try:
            from scipy.io.wavfile import read as scipy_read
            sr, data = scipy_read(file_path_str)
            if len(data.shape) > 1:
                data = data.mean(axis=1)
            if data.dtype == np.int16:
                data = data.astype(np.float32) / 32768.0
            elif data.dtype == np.int32:
                data = data.astype(np.float32) / 2147483648.0
            elif data.dtype == np.uint8:
                data = (data.astype(np.float32) - 128.0) / 128.0
            else:
                data = data.astype(np.float32)
            if sr != 16000:
                duration = len(data) / sr
                num_samples = int(duration * 16000)
                data = np.interp(
                    np.linspace(0, len(data), num_samples),
                    np.arange(len(data)),
                    data,
                )
            return torch.tensor(data, dtype=torch.float32)
        except Exception as e2:
            try:
                import soundfile as sf
                data, sr = sf.read(file_path_str)
                if len(data.shape) > 1:
                    data = data.mean(axis=1)
                if sr != 16000:
                    duration = len(data) / sr
                    num_samples = int(duration * 16000)
                    data = np.interp(
                        np.linspace(0, len(data), num_samples),
                        np.arange(len(data)),
                        data,
                    )
                return torch.tensor(data, dtype=torch.float32)
            except Exception as e3:
                raise RuntimeError(
                    f"Failed to load audio file {file_path_str}. "
                    f"pydub: {e}, scipy: {e2}, soundfile: {e3}"
                )


class SpeakerProfileManager:
    def __init__(self, speakers_dir: Path, classifier):
        self.speakers_dir = Path(speakers_dir)
        self.classifier = classifier
        self.speaker_profiles = {}

    def enroll_speakers(self, min_seconds: float = 0.0):
        """Scan speakers/ subfolders and build average embedding profiles.

        `min_seconds` skips recordings too short to carry a voice. It defaults
        to 0 so existing callers are unaffected; speakers/build_speaker_model.py
        passes 1.5, measured leave-one-out over this household's enrolment set:
        every clip under 1.5 s removed, the worst-case margin between the right
        resident and the nearest wrong one went from 0.064 to 0.111. A 0.7 s
        clip in Ilyas's folder had been scoring higher for Rafael outright.

        A short clip is not merely weak evidence -- averaged into a profile it
        pulls that profile toward whatever the recording caught instead of a
        voice.
        """
        if not self.speakers_dir.exists():
            print(f"Error: Speakers directory '{self.speakers_dir}' does not exist.")
            return

        for speaker_folder in sorted(self.speakers_dir.iterdir()):
            if not speaker_folder.is_dir():
                continue
            # __pycache__ and dotfolders are not residents.
            if speaker_folder.name.startswith(('.', '__')):
                continue

            speaker_name = speaker_folder.name
            # Deduplicated by resolved path: NTFS globbing is case-insensitive,
            # so "*.flac" and "*.FLAC" return the *same* files on Windows and
            # every clip was being embedded twice. Averaging duplicated
            # embeddings happens to leave the profile unchanged, so this was
            # invisible in the output -- it just doubled the work and made the
            # file count in the log wrong.
            seen = {}
            for ext in ["*.flac", "*.wav", "*.mp3", "*.m4a"]:
                for pattern in (ext, ext.upper()):
                    for path in speaker_folder.glob(pattern):
                        seen.setdefault(str(path.resolve()).lower(), path)
            audio_files = sorted(seen.values())

            if not audio_files:
                print(f"Speaker '{speaker_name}': No audio files found. Skipping.")
                continue

            print(f"Enrolling '{speaker_name}' with {len(audio_files)} voice files...")
            embeddings = []
            for file_path in audio_files:
                try:
                    signal = load_audio_as_16k_tensor(file_path)
                    seconds = len(signal) / 16000.0
                    if min_seconds and seconds < min_seconds:
                        print(f"  Skipping '{file_path.name}': {seconds:.2f}s, "
                              f"under the {min_seconds:.1f}s floor")
                        continue
                    emb = self.classifier.encode_batch(signal.unsqueeze(0))
                    embeddings.append(emb.squeeze())
                except Exception as e:
                    print(f"  Error loading '{file_path.name}': {e}")

            if embeddings:
                mean_emb = torch.stack(embeddings).mean(dim=0)
                self.speaker_profiles[speaker_name] = mean_emb
                print(f"Speaker '{speaker_name}' enrolled successfully.")

        print(f"Enrollment completed. Enrolled speakers: {list(self.speaker_profiles.keys())}")

    def identify_speaker(self, segment_signal: torch.Tensor, threshold: float = 0.35):
        """Match a segment embedding against enrolled profiles (closed-set)."""
        if not self.speaker_profiles:
            return "Unknown", 0.0, {}

        try:
            emb = self.classifier.encode_batch(segment_signal.unsqueeze(0)).squeeze()
        except Exception as e:
            print(f"Error extracting segment embedding: {e}")
            return "Unknown", 0.0, {}

        best_name = "Unknown"
        best_score = -1.0
        scores = {}
        for name, profile in self.speaker_profiles.items():
            # Profiles may live on a different device than the live embedding.
            profile = profile.to(emb.device)
            sim = torch.nn.functional.cosine_similarity(emb, profile, dim=0).item()
            scores[name] = sim
            if sim > best_score:
                best_score = sim
                best_name = name

        # Closed-set: always return the best match. threshold is kept for callers
        # that want to gate Unknown themselves.
        _ = threshold
        return best_name, best_score, scores

    def save_model(self, path):
        if self.speaker_profiles:
            torch.save(self.speaker_profiles, path)
            print(f"Saved speaker model to: {path}")
            return True
        return False

    def load_model(self, path):
        path = Path(path)
        if not path.exists():
            return False
        try:
            device = next(self.classifier.mods.parameters()).device
        except Exception:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        try:
            self.speaker_profiles = torch.load(
                path, map_location=device, weights_only=False
            )
            return True
        except Exception as e:
            print(f"Error loading speaker model: {e}")
            return False
