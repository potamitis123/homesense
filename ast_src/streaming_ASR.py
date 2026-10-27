import os
import sys
import torch
import numpy as np
import ctypes

# 1. Suppress Windows Error Message Boxes (Entry Point Not Found, etc.)
# SEM_FAILCRITICALERRORS = 0x0001
# SEM_NOGPFAULTERRORBOX  = 0x0002
# SEM_NOOPENFILEERRORBOX = 0x8000
try:
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except:
    pass

# 2. Monkey-patch torchcodec to prevent Diarization Error: name 'AudioDecoder' is not defined
# Even if uninstalled, pyannote 4.0.4 might still have paths that expect it.
try:
    import torchcodec
except ImportError:
    # Create a dummy module
    class DummyAudioDecoder:
        def __init__(self, *args, **kwargs): pass
    
    dummy_torchcodec = type('module', (), {
        'decoders': type('module', (), {'AudioDecoder': DummyAudioDecoder})
    })
    sys.modules['torchcodec'] = dummy_torchcodec
    sys.modules['torchcodec.decoders'] = dummy_torchcodec.decoders

# Monkey-patch NumPy for compatibility with older pyannote-audio versions
if not hasattr(np, "NaN"):
    np.NaN = np.nan

# Restore the torchaudio APIs speechbrain 1.0.3 still calls. Must run before
# speechbrain or pyannote are imported.
from misc.torch_compat import apply as apply_torchaudio_compat
apply_torchaudio_compat()

import re
import unicodedata
import contextlib
from misc.log_test import logger

LOG_D = {'module_name': 'ASR'}

class Silence:
    def __enter__(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = open(os.devnull, 'w')
        sys.stderr = open(os.devnull, 'w')

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout.close()
        sys.stdout = self._stdout
        sys.stderr.close()
        sys.stderr = self._stderr

def normalize_token(token: str) -> str:
    """Lowercase + remove accents for comparison only."""
    token = token.lower()
    token = ''.join(
        c for c in unicodedata.normalize('NFD', token)
        if unicodedata.category(c) != 'Mn'
    )
    return token


def remove_consecutive_repetitions(text: str) -> str:
    """
    Removes immediate repeated words (case & accent insensitive).
    Keeps punctuation attached to the *first* occurrence.
    """
    tokens = re.findall(r"\w+|[^\w\s]", text, re.UNICODE)

    cleaned_tokens = []
    prev_norm = None

    for tok in tokens:
        # Only compare word tokens
        if re.match(r"\w+", tok, re.UNICODE):
            norm = normalize_token(tok)
            if norm == prev_norm:
                continue  # skip repeated word
            cleaned_tokens.append(tok)
            prev_norm = norm
        else:
            # punctuation resets repetition tracking
            cleaned_tokens.append(tok)
            prev_norm = None

    # Rebuild text with proper spacing
    text = " ".join(cleaned_tokens)

    # Fix spacing before punctuation
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)

    return text

def remove_repeated_phrases(text: str, min_words: int = 2) -> str:
    """
    Removes repeated phrases (consecutive or with small gaps).
    E.g. 'I want to go I want to go' -> 'I want to go'
    E.g. 'hello world hello world hello world' -> 'hello world'
    """
    words = text.split()
    if len(words) < min_words * 2:
        return text

    # Try phrase lengths from largest to smallest
    for phrase_len in range(len(words) // 2, min_words - 1, -1):
        i = 0
        result = []
        while i < len(words):
            phrase = words[i:i + phrase_len]
            # Count how many times this phrase repeats consecutively from here
            j = i + phrase_len
            while j + phrase_len <= len(words):
                next_phrase = words[j:j + phrase_len]
                if [normalize_token(w) for w in phrase] == [normalize_token(w) for w in next_phrase]:
                    j += phrase_len  # skip the repeat
                else:
                    break
            if j > i + phrase_len:
                # Repetition found — keep only the first occurrence
                result.extend(phrase)
                i = j
            else:
                result.append(words[i])
                i += 1
        words = result

    return " ".join(words)

# --- STRONGER DLL FIX ---
def force_cuda_paths():
    # Correctly locate the project root
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    
    # Paths for DLLs provided by the user
    user_dll_path = os.path.join(project_root, 'gpu_asr')
    
    # Paths for PyTorch's bundled nvidia libraries
    pytorch_base_path = sys.prefix 
    pytorch_dll_paths = [
        os.path.join(pytorch_base_path, 'Lib', 'site-packages', 'nvidia', 'cublas', 'bin'),
        os.path.join(pytorch_base_path, 'Lib', 'site-packages', 'nvidia', 'cudnn', 'bin')
    ]
    
    paths_to_add = [user_dll_path] + pytorch_dll_paths
    
    resolved = []
    for p in paths_to_add:
        if os.path.exists(p):
            # 1. The modern fix for Python 3.8+ on Windows
            try:
                os.add_dll_directory(p)
            except AttributeError:
                # For older Python or other OS, fallback to PATH modification
                pass
            # 2. The "Old School" fix (Force it into System PATH)
            if p not in os.environ["PATH"]:
                os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]
            resolved.append(p)

    # CTranslate2 needs the CUDA 12 cuBLAS/cuDNN pair. Only complain when none of
    # the candidate folders actually provides it, otherwise the absence of the
    # optional pip-installed nvidia packages looks like an error when gpu_asr
    # already supplies the libraries.
    if any(os.path.exists(os.path.join(p, "cublas64_12.dll")) for p in resolved):
        print(f"[DLL Fix] CUDA libraries for Whisper resolved from: {resolved[0]}")
    else:
        print("[DLL Fix] WARNING: cublas64_12.dll not found in "
              f"{' or '.join(paths_to_add)} - GPU transcription will fail.")

try:
    force_cuda_paths()
except Exception as e:
    print(f"DLL Fix Failed: {e}")
# ------------------------

from faster_whisper import WhisperModel
try:
    from pyannote.audio import Pipeline
    HAS_PYANNOTE = True
except ImportError as e:
    HAS_PYANNOTE = False
    logger.warning(f"Pyannote import failed: {e}", extra=LOG_D)

# Use the globally defined device from the common handler
# This ensures consistency but we still run the DLL fix first.
try:
    from common_files.device_handler import device
    DEVICE = "cuda" if device.type == "cuda" else "cpu"
except ImportError:
    # Fallback if the handler is not available
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

from misc.load_parameters import parameters

class ASRTranscriber:
    def __init__(self, model_size="large-v3", compute_type=None):
        # On GPU, float16 is standard. On CPU, int8 provides a good speed/accuracy trade-off.
        if compute_type is None:
            compute_type = "float16" if DEVICE == "cuda" else "int8"
            
        logger.info(f"Loading Whisper ASR (device: {DEVICE}, compute: {compute_type})...", extra=LOG_D)
        try:
            # The device string is passed to faster-whisper
            self.model = WhisperModel(model_size, device=DEVICE, compute_type=compute_type)
        except Exception as e:
            logger.error(f"Error loading Whisper: {e}", extra=LOG_D)
            self.model = None

        # --- Diarization Setup ---
        self.diarization_pipeline = None
        diar_params = parameters.get_parameters().get("diarization", {})
        if diar_params.get("enabled", False) and HAS_PYANNOTE:
            hf_token = diar_params.get("hf_token", "")
            if hf_token and hf_token != "YOUR_HF_TOKEN_HERE":
                logger.info(f"Loading pyannote diarization pipeline (device: {DEVICE})...", extra=LOG_D)
                try:
                    self.diarization_pipeline = Pipeline.from_pretrained(
                        diar_params.get("model", "pyannote/speaker-diarization-3.1"),
                        token=hf_token
                    )
                    if self.diarization_pipeline:
                        self.diarization_pipeline.to(torch.device(DEVICE))
                        logger.info("Pyannote Diarization Pipeline loaded successfully.", extra=LOG_D)
                except Exception as e:
                    logger.error(f"Error loading Diarization pipeline: {e}", extra=LOG_D)
            else:
                logger.warning("Diarization enabled but HF token is missing or default. Skipping.", extra=LOG_D)
        else:
            logger.info(f"Diarization skipped (Enabled: {diar_params.get('enabled', False)}, HasPyannote: {HAS_PYANNOTE})", extra=LOG_D)

    def get_speaker_labels(self, file_path):
        if not self.diarization_pipeline:
            # "Did anybody speak at all" is this pipeline's main defence against
            # Whisper inventing words over a bark or a knock. With no diarizer
            # the defence is simply absent, and nothing downstream says so --
            # worth one loud line per run rather than silence. It nearly
            # happened on 18-09-2026: speechbrain 1.0.3 calls torchaudio APIs
            # that 2.9 removed, and only misc/torch_compat keeps the import
            # alive.
            if parameters.get_parameters().get("diarization", {}).get("enabled", False) \
                    and not getattr(self, "_warned_no_diarisation", False):
                self._warned_no_diarisation = True
                logger.warning(
                    "Diarisation is enabled in the settings but no pipeline is "
                    "loaded: clips are being transcribed with nothing checking "
                    "that a person spoke.", extra=LOG_D)
            return None
        logger.info(f"Processing diarization for {os.path.basename(file_path)}...", extra=LOG_D)
        try:
            # pyannote 3.1+ might return a DiarizeOutput object wrapping the annotation
            result = self.diarization_pipeline(file_path)
            diarization = getattr(result, "speaker_diarization", result)
            
            labels = diarization.labels()
            logger.info(f"Diarization found {len(labels)} speakers: {labels}", extra=LOG_D)
            return diarization
        except Exception as e:
            logger.error(f"Diarization Error: {e}", extra=LOG_D)
            return None

    def transcribe(self, file_path, language=None, speech_regions=None):
        """Transcribe one file in a single decoding pass.

        `speech_regions` is an optional list of (start, end) seconds marking
        where VAD heard speech. It is used only as a fallback, when the clip
        as a whole decodes to nothing -- see the retry near the end.

        The language comes from parameters.json (asr.language) unless the
        caller names one:

            "el"   force Greek
            "en"   force English
            "auto" let Whisper decide

        Forcing the wrong language is worse than leaving it to detection:
        English speech decoded as Greek does not fail loudly, it produces
        fluent Greek that is simply wrong -- "It's a bit lonely" was being
        stored as "Eina lígo monadikó" ("it's a bit unique"). Only ever one
        pass, whichever setting is used.
        """
        # Cleared per clip: a stale value would attribute this recording to
        # whoever spoke in the previous one.
        self.last_speakers = []

        if not self.model:
            return ""

        if language is None:
            language = parameters.get_parameters().get('asr', {}).get('language', 'auto')
        language = (language or 'auto').strip().lower()
        # faster-whisper takes None to mean "detect".
        whisper_language = None if language in ('auto', '', 'detect') else language

        # Household proper nouns, biasing the decode toward names it would
        # otherwise never produce. None (not "") when unset: faster-whisper
        # treats an empty string as a prompt and prepends a stray token.
        asr_initial_prompt = (parameters.get_parameters()
                              .get('asr', {}).get('initial_prompt') or '').strip() or None

        # Cosine similarity below which the closed set is not trusted to name
        # anyone. Enrolled profiles of *different* residents sit at 0.17-0.40,
        # so a match under ~0.45 carries no identity information. Set
        # speaker_id.min_similarity to 0 in parameters.json to restore the old
        # always-name-the-best-candidate behaviour.
        min_speaker_similarity = float(
            parameters.get_parameters().get('speaker_id', {}).get('min_similarity', 0.45))

        # Whisper decodes each diarised turn separately when there is more than
        # one speaker, so a short turn is not swallowed by a longer overlapping
        # one. Turns below this are too short to decode reliably -- they are the
        # length at which the model starts inventing subtitle text.
        min_turn_seconds = float(
            parameters.get_parameters().get('asr', {}).get('min_turn_seconds', 0.7))

        # Turn length below which a per-turn decode is run WITHOUT the names
        # prompt.
        #
        # The prompt is a clear win on a whole clip -- it supplies proper nouns
        # the decoder cannot otherwise reach. Handed a single short turn it
        # takes over instead: the 0.87 s turn on clip 44210 decoded as "Οχι το
        # Ραφ" with the prompt and "Οχι τωρα" without it, "Ραφ" being a
        # fragment of Ραφαηλ lifted out of the prompt itself. Same failure the
        # zero-turn gate below already guards against, reached by a different
        # route: little enough audio that the prompt outweighs it.
        #
        # Only the per-turn path consults this. The whole-file decode keeps the
        # prompt regardless, which is where it was measured to help.
        prompt_min_turn_seconds = float(
            parameters.get_parameters().get('asr', {}).get('prompt_min_turn_seconds', 2.0))

        # How far the transcript's time span may exceed the speech the diarizer
        # actually found before the whole transcript is treated as invented.
        # Genuine speech sits near 1x; the observed hallucinations were 10x and
        # above. Set high (e.g. 999) to disable the guard.
        max_speech_span_ratio = float(
            parameters.get_parameters().get('asr', {}).get('max_speech_span_ratio', 4.0))

        # Per-segment attribution (see the block that fills segment_names).
        #
        # The gate is the gap between the best and second-best profile, not the
        # best score on its own, because raw similarity does not separate a real
        # match from a lucky one at segment length: over the annotated corpus
        # the correct name scored a median 0.223 and a wrong name 0.118, which
        # overlap almost completely. The margin does separate them -- across 87
        # clips of one session every override above 0.12 was correct by ear and
        # every one below 0.05 was noise, nearly all of it naming Rafael on a
        # short segment.
        segment_margin_floor = float(
            parameters.get_parameters().get('speaker_id', {}).get('segment_margin', 0.12))

        # Set speaker_id.per_segment to false to go back to naming whole
        # clusters only.
        per_segment_enabled = bool(
            parameters.get_parameters().get('speaker_id', {}).get('per_segment', True))

        # A segment shorter than this carries too little voice to embed, and is
        # left with whatever its cluster was called.
        min_segment_seconds = float(
            parameters.get_parameters().get('speaker_id', {}).get('min_segment_seconds', 0.4))

        # Floor for naming a segment the diarizer gave no cluster for, further
        # down. min_similarity cannot serve: it is calibrated for a whole
        # concatenated cluster, while one segment of real speech scores far
        # lower -- a median 0.223 for the correct name against 0.118 for a
        # wrong one over the annotated corpus -- so the cluster figure would
        # silence legitimate naming. Below this there is no identity in the
        # number at all: on 18-09-2026 the neighbours' dog and a cracking noise
        # were stored as Ilyas, Rafael and Voula at 0.016 to 0.095.
        min_segment_similarity = float(
            parameters.get_parameters().get('speaker_id', {}).get('min_segment_similarity', 0.15))

        # How well the enrolled voices must match before a retry is allowed to
        # speak for a clip the diarizer heard nobody in (see the retries far
        # below). Measured over the 180 such clips in one day's log: what the
        # retries produced there matched a profile at 0.016-0.175 when it was
        # junk ("Ωραία!", "Υποτιτλισμός", stock subtitle lines, a dog barking
        # next door) and at 0.378-0.574 when it was a real sentence.
        retry_voice_similarity = float(
            parameters.get_parameters().get('speaker_id', {}).get('retry_voice_similarity', 0.30))

        try:
            # Run Diarization if enabled
            diarization_result = self.get_speaker_labels(file_path)

            # Map pyannote SPEAKER_00/01/... labels to enrolled names via speakers_model.pth
            speaker_mapping = {}
            speaker_scores = {}
            # Kept outside the block below so the per-segment loop can fall back
            # to identifying a speaker itself when diarisation covered nothing.
            profile_manager = None
            full_audio_signal = None
            from pathlib import Path
            project_root = Path(__file__).resolve().parent.parent
            # The model lives beside the voices it was built from, in
            # speakers/, written there by speakers/build_speaker_model.py.
            # The old project-root location is still accepted so an older
            # checkout keeps working, but it says so: running months-old
            # profiles while believing they were just rebuilt is exactly the
            # kind of mistake that shows up as an ordinary-looking wrong name.
            model_path = project_root / "speakers" / "speakers_model.pth"
            if not model_path.exists():
                legacy = project_root / "speakers_model.pth"
                if legacy.exists():
                    logger.warning(
                        f"Using the speaker model at {legacy}. The current "
                        f"location is {model_path}; rebuild it with "
                        f"speakers/build_speaker_model.py", extra=LOG_D)
                    model_path = legacy
            # Loaded whenever the model exists, not only when diarisation
            # produced turns: a clip the diarizer said nothing about still has
            # speech to attribute, and that is exactly the case that used to
            # come out as "Unknown".
            if model_path.exists():
                try:
                    from speechbrain.inference import EncoderClassifier
                    from ast_src.speaker_id import (
                        SpeakerProfileManager,
                        load_audio_as_16k_tensor,
                    )

                    # Load the ECAPA classifier once and keep it on GPU (or CPU fallback).
                    # First call pays the load cost; later clips reuse the cache.
                    if not hasattr(self, "speaker_classifier") or self.speaker_classifier is None:
                        device = "cuda" if torch.cuda.is_available() else "cpu"
                        logger.info(
                            f"Loading speechbrain/spkrec-ecapa-voxceleb on {device}...",
                            extra=LOG_D,
                        )
                        self.speaker_classifier = EncoderClassifier.from_hparams(
                            source="speechbrain/spkrec-ecapa-voxceleb",
                            run_opts={"device": device},
                        )

                    if not hasattr(self, "speaker_profiles") or self.speaker_profiles is None:
                        profile_manager = SpeakerProfileManager(
                            project_root / "speakers", self.speaker_classifier
                        )
                        if not profile_manager.load_model(model_path):
                            raise RuntimeError(f"Failed to load {model_path}")
                        self.speaker_profiles = profile_manager
                        logger.info(
                            f"Loaded speaker profiles: {list(profile_manager.speaker_profiles.keys())}",
                            extra=LOG_D,
                        )
                    else:
                        profile_manager = self.speaker_profiles

                    full_audio_signal = load_audio_as_16k_tensor(Path(file_path))

                    # Only meaningful when the diarizer produced turns; without
                    # them there is nothing to map, and the per-segment
                    # fallback below does the attribution instead.
                    raw_speaker_signals = {}
                    if diarization_result:
                        diarization = getattr(
                            diarization_result, "speaker_diarization", diarization_result
                        )
                        for turn, _, raw_speaker in diarization.itertracks(yield_label=True):
                            if raw_speaker not in raw_speaker_signals:
                                raw_speaker_signals[raw_speaker] = []
                            start_sample = int(turn.start * 16000)
                            end_sample = int(turn.end * 16000)
                            start_sample = max(0, min(start_sample, len(full_audio_signal)))
                            end_sample = max(0, min(end_sample, len(full_audio_signal)))
                            if end_sample > start_sample:
                                raw_speaker_signals[raw_speaker].append(
                                    full_audio_signal[start_sample:end_sample]
                                )

                    for raw_speaker, chunks in raw_speaker_signals.items():
                        if not chunks:
                            continue
                        concat_signal = torch.cat(chunks)
                        name, score, _scores = profile_manager.identify_speaker(concat_signal)
                        if name != "Unknown" and score >= min_speaker_similarity:
                            speaker_scores[raw_speaker] = score
                            logger.info(
                                f"Matched {raw_speaker} to '{name}' with similarity {score:.3f}",
                                extra=LOG_D,
                            )
                            speaker_mapping[raw_speaker] = name
                        else:
                            # Below the floor the closed set has no useful
                            # answer. Naming the best candidate anyway is worse
                            # than saying nothing: clip 37755 matched a second
                            # speaker to 'Ilyas' at 0.083 -- lower than the
                            # similarity between two *different* residents'
                            # enrolled profiles (0.17-0.40), i.e. pure noise
                            # presented as an identification.
                            logger.info(
                                f"{raw_speaker} left unidentified: best was "
                                f"'{name}' at {score:.3f}, below the "
                                f"{min_speaker_similarity:.2f} floor", extra=LOG_D)
                except Exception as e:
                    logger.error(f"Error in on-the-fly speaker identification: {e}", extra=LOG_D)

            # Diarised turns, if any, as plain (start, end, label) tuples.
            # Read before the decode options are built: whether the diarizer
            # found any speech at all decides whether the names prompt is safe
            # to use below.
            turns = []
            if diarization_result is not None:
                try:
                    _diar = getattr(diarization_result, "speaker_diarization",
                                    diarization_result)
                    turns = [(t.start, t.end, spk)
                             for t, _, spk in _diar.itertracks(yield_label=True)]
                except Exception as e:
                    logger.error(f"Could not read diarisation turns: {e}", extra=LOG_D)

            # ---- Per-segment speaker attribution -------------------------
            #
            # pyannote both FINDS segments and CLUSTERS them, and it is the
            # clustering half that fails. On clip 44210 it returned exactly the
            # right three turns -- 0.94-3.07, 4.81-5.68, 6.26-7.59 -- and then
            # merged them into a single speaker, so Ilyas's 0.87 s reply
            # reached the dashboard as Voula. Asking for two speakers does not
            # help: num_speakers=2, min_speakers=2 and min/max together all
            # return that same one cluster, because min_cluster_size prunes the
            # short turn before clustering can keep it.
            #
            # So the boundaries are kept -- they were never wrong -- and only
            # the clustering DECISION is replaced. The mapping built above
            # concatenates each cluster's audio and embeds it once, which
            # blends two voices into an embedding dominated by whoever spoke
            # longer: the shorter turn is erased arithmetically, before the
            # enrolled profiles are ever consulted. Embedding each segment on
            # its own cannot do that.
            #
            # The cluster's name remains the default and a segment overrules it
            # only when its own margin clears the floor, so this can add a
            # speaker who was missing but cannot rename one who was already
            # right. Over the 87 clips of one recording session four clips
            # gained a speaker, all four confirmed correct by ear, and no clip
            # lost one at any floor from 0.00 upward.
            #
            # min_speaker_similarity is deliberately NOT applied here. It is
            # calibrated for a whole concatenated cluster, which scores far
            # higher than one segment: raising it to its 0.45 default would
            # reject all four of the verified-correct overrides above, silently
            # turning this off. The margin is the gate at segment length.
            segment_names = {}
            if per_segment_enabled and turns and speaker_mapping \
                    and profile_manager is not None and full_audio_signal is not None:
                for t_start, t_end, raw_speaker in turns:
                    if (t_end - t_start) < min_segment_seconds:
                        continue
                    a = max(0, min(int(t_start * 16000), len(full_audio_signal)))
                    b = max(0, min(int(t_end * 16000), len(full_audio_signal)))
                    if b <= a:
                        continue
                    try:
                        name, score, all_scores = profile_manager.identify_speaker(
                            full_audio_signal[a:b])
                    except Exception as e:
                        logger.error(
                            f"Per-segment identification failed for "
                            f"[{t_start:.2f}s -> {t_end:.2f}s]: {e}", extra=LOG_D)
                        continue
                    base = speaker_mapping.get(raw_speaker)
                    if base is not None:
                        # How far the segment's own best name beats the name
                        # its CLUSTER carries -- which is the decision being
                        # made here: is this segment a different person from
                        # the cluster it sits in? Whether that name also beats
                        # the runner-up is a different question and not this
                        # one. It used to be measured that way, and the new
                        # speaker profiles exposed the difference: on clip
                        # 44210's Ilyas turn the runner-up is now Rafael at
                        # 0.118 rather than 0.016, so the old reading fell
                        # from 0.176 to 0.071 and the override went silent on
                        # the very clip it was built for, while the gap over
                        # the cluster stayed decisive at 0.184.
                        #
                        # Recalibrated over 64 clips of one session against
                        # the four exchanges confirmed by ear: this measure
                        # gets all four right at every floor up to 0.12, where
                        # the old one held only to 0.06. A wider plateau means
                        # the threshold matters less.
                        strength = all_scores.get(name, 0.0) - all_scores.get(base, 0.0)
                    else:
                        # No name on the cluster to compare against -- the
                        # closed set did not recognise it. Fall back to the
                        # runner-up gap, which is all there is.
                        ranked = sorted(all_scores.values(), reverse=True)
                        strength = (ranked[0] - ranked[1]) if len(ranked) > 1 else 0.0
                    if (name != "Unknown" and name != base
                            and strength >= segment_margin_floor):
                        segment_names[(t_start, t_end, raw_speaker)] = name
                        logger.info(
                            f"Segment [{t_start:.2f}s -> {t_end:.2f}s] "
                            f"reassigned from '{base}' to '{name}' "
                            f"(similarity {score:.3f}, {strength:.3f} above "
                            f"'{base}' on this segment, floor "
                            f"{segment_margin_floor:.2f})", extra=LOG_D)

            # Who diarisation and the profiles say was talking, kept on the
            # transcriber so the caller can still see it when no words survive
            # decoding. Otherwise a clip where two detectors heard speech and
            # the recogniser named the speaker reaches the database as bare
            # "No Speech Detected", throwing away an identification that was
            # made successfully.
            self.last_speakers = sorted(
                {n for n in speaker_mapping.values() if n and n != "Unknown"}
                | {n for n in segment_names.values() if n and n != "Unknown"})

            # The names prompt is applied ONLY when the diarizer actually found
            # speech.
            #
            # It supplies proper nouns Whisper cannot guess, and on real speech
            # it is a clear win (WER 0.83 -> 0.33 and 2.50 -> 1.00, recovering
            # "Ραφαήλ" where the plain decode gave "Να φαίνεται"). But it also
            # biases the model toward *finding* speech: on clip 39572, a drawer
            # sound with no diarised turns, the prompt drove Whisper's own
            # no_speech_prob from 0.757 down to 0.102 and made it emit
            # "Ραφαήλ" -- a name taken straight from the prompt. Withholding it
            # when nothing was diarised keeps the benefit and removes that.
            use_prompt = bool(turns) and asr_initial_prompt is not None
            if asr_initial_prompt is not None and not turns:
                logger.info(
                    "Diarisation found no speech; decoding without the names "
                    "prompt to avoid seeding a name into non-speech audio",
                    extra=LOG_D)

            # Decoding options, built once so the whole-file and per-turn paths
            # cannot drift apart.
            decode_kwargs = dict(
                task='transcribe',
                beam_size=5,
                language=whisper_language,
                initial_prompt=asr_initial_prompt if use_prompt else None,
                condition_on_previous_text=False,
                # Disable built-in VAD to avoid double-filtering
                vad_filter=False,
                # Suppress common hallucinations
                suppress_blank=True,
                suppress_tokens=[-1],
                # Lower temperature can reduce hallucinations
                temperature=0.0,
                # Require higher log probability to accept results
                compression_ratio_threshold=3, #used to be 2.4
                log_prob_threshold=None,
                no_speech_threshold=0.85,  # Default is 1 or 0.6, can increase to 0.7-0.8 for stricter
            )

            # Whisper decodes the whole clip as one pass by default, which means
            # a short turn overlapping a longer one is simply lost: on clip
            # 37755 it produced ONE segment spanning both speakers, so Voula's
            # reply was never transcribed at all and the words that were
            # transcribed all went to Ilyas. When the diarizer reports more than
            # one speaker, decode each turn's own audio instead.
            #
            # The cap matters: this costs one Whisper pass per turn, and a
            # heavily diarised clip would otherwise multiply GPU time without
            # improving anything.
            distinct_speakers = {spk for _, _, spk in turns}
            # A non-empty segment_names means a segment was confidently
            # assigned to a different resident than its cluster, i.e. more than
            # one person is present even though the clustering said otherwise.
            # Decoding per turn is what lets each of them carry their own
            # words: without it clip 44210 stays one Whisper segment spanning
            # all three turns, and there is only a single line of text to put a
            # name on, however good the attribution is.
            per_turn = ((len(distinct_speakers) > 1 or bool(segment_names))
                        and full_audio_signal is not None
                        and len(turns) <= 12)

            banned_phrases = [
                "Υπότιτλοι AUTHORWAVE",
                "AUTHORWAVE",
                "Υπότιτλοι",
                "Subtitles by",
                "Amara.org",
                # "ευχαριστώ" is both a stock Whisper hallucination and one of
                # the commonest words in Greek, so neither keeping nor deleting
                # it was right. It is safe again now only because matching is
                # whole-segment (see below): a segment that is *nothing but*
                # "Ευχαριστώ." is dropped, while the same word inside a real
                # sentence is left completely alone.
                "ευχαριστώ",
                "ευχαριστώ πολύ",
                "σας ευχαριστώ",
                "Σας .",
		"που παρακολουθήσατε το βίντεο",
		"Ευχαριστούμε για την ευκαιρία μας.",
		"Ευχαριστούμε.",
                "Φίλοι μας, ευχαριστούμε",
		"Φτιάξτε το βίντεο και εγγραφείτε στο κανάλι μας",
                # Written without their closing full stop: strip_banned already
                # eats any trailing punctuation, and with the stop spelled in,
                # "Ευχαριστώ που παρακολουθήσατε!" missed by the one character
                # and was stored as speech by Voula over a cracking noise
                # (clip 65092, 18-09-2026).
		"που παρακολουθήσατε",
                # The singular of the AUTHORWAVE line, which arrives on its own
                # too: clip 65049, a dog barking next door, reached the
                # database as "Ilyas: Υποτιτλισμός".
                "Υποτιτλισμός"
            ]

            def strip_banned(value):
                """Only used to TEST a segment, never to rewrite one.

                Deleting a banned phrase out of the middle of a real sentence
                is what produced the orphaned "Σε," rows. The residue below
                decides whether the whole segment was boilerplate; text that
                survives is emitted exactly as Whisper produced it.
                """
                # Longest first. Applied in list order, "ευχαριστώ" would strip
                # out of "ευχαριστώ πολύ" and leave " πολύ.", which is not empty
                # -- so the segment survived as a hallucination. Removing the
                # longest match first makes the residue genuinely empty.
                #
                # A phrase's own closing punctuation is dropped before matching,
                # because Whisper varies it and one character used to be enough
                # to slip through: "Ευχαριστώ που παρακολουθήσατε!" against a
                # list holding "που παρακολουθήσατε.", and "Ευχαριστούμε που
                # παρακολουθήσατε." against "Ευχαριστούμε." (18 and 19-09-2026).
                for phrase in sorted(banned_phrases, key=len, reverse=True):
                    value = re.sub(re.escape(phrase.rstrip(' .,!?;:')) + r'[.,!?;:]*',
                                   '', value, flags=re.IGNORECASE)
                return value

            def is_empty_after_cleaning(value):
                """True when nothing but punctuation/whitespace survives."""
                return not re.sub(r'[\s.,;:!?·…\-"\'«»]', '', value)

            def _flatten(value):
                """Lowercase, strip accents and punctuation, collapse spaces."""
                value = unicodedata.normalize('NFD', (value or '').lower())
                value = ''.join(c for c in value
                                if unicodedata.category(c) != 'Mn')
                return ' '.join(re.sub(r'[^\w\s]', ' ', value).split())

            def is_repetition_loop(value):
                """One phrase repeated to fill the clip, not a transcript.

                The failure mode of the relaxed retry below, and only of that
                retry: sampling at a non-zero temperature over a mostly quiet
                clip settles into a loop. Clip 45814 came back as "Pame."
                twelve times and 45699 repeated one sentence about beans. Both
                read as fluent Greek, so nothing else here would stop them.

                A third of the words distinct, measured over the 47 clips
                where the first retry failed and this one produced text. The
                two genuine loops there scored 0.08 and 0.16 and the next
                lowest was 0.65, so anything from 0.34 to 0.60 separates them
                identically -- a third sits mid-band rather than on an edge.

                Short segments are exempt: "Ochi. Ochi." is a person
                insisting, not a decoder looping.
                """
                words = _flatten(value).split()
                if len(words) < 6:
                    return False
                return len(set(words)) * 3 <= len(words)

            _flat_prompt = _flatten(asr_initial_prompt) if asr_initial_prompt else ''

            def is_prompt_echo(value):
                """True when the segment is just the initial_prompt read back.

                Whisper's best-known initial_prompt failure is to emit the
                prompt itself as the transcript when it cannot decode the
                audio. On clip 39616 it produced "Τα ονόματα είναι Ραφαήλ,
                Ηλίας, Βούλα, Άννα." -- the prompt's second sentence, word for
                word, stored as speech by Voula. Without the prompt that clip
                yields "Υπότιτλοι AUTHORWAVE", so there the audio really was
                undecodable and nothing was lost by dropping it.

                That is not the general case, and reading it as one cost real
                speech. Clip 46446 echoed the prompt over a 21 s turn that was
                loud and clear the whole way; without the prompt the same audio
                returned eight turns of dialogue. So an echo means "ask again
                without the prompt", not "there is nothing here" -- the retry
                is at the per-turn decode, and this stays the detector.

                The length floor matters: the prompt contains the residents'
                names, and a genuine one-word "Ραφαήλ" is a substring of it.
                Only a long run of prompt text counts as an echo.
                """
                if not _flat_prompt:
                    return False
                flat = _flatten(value)
                if len(flat.split()) < 4 or len(flat) < 15:
                    return False
                return flat in _flat_prompt or _flat_prompt in flat

            # (abs_start, abs_end, text, raw_speaker or None, avg_logprob,
            #  no_speech_prob, resolved_name or None) -- avg_logprob and
            # no_speech_prob carry Whisper's own quality signals through so the
            # filters below still see them; resolved_name carries a per-segment
            # override so the attribution loop does not have to re-derive it.
            decoded = []

            def _row(start, end, seg, raw_speaker, resolved=None):
                return (start, end, seg.text, raw_speaker,
                        getattr(seg, 'avg_logprob', None),
                        getattr(seg, 'no_speech_prob', None),
                        resolved)

            if per_turn:
                logger.info(
                    f"Decoding {len(turns)} diarised turns separately "
                    f"({len(distinct_speakers)} speakers)", extra=LOG_D)
                for t_start, t_end, raw_speaker in sorted(turns):
                    if (t_end - t_start) < min_turn_seconds:
                        logger.info(
                            f"Skipping {raw_speaker} turn "
                            f"[{t_start:.2f}s -> {t_end:.2f}s]: shorter than "
                            f"{min_turn_seconds:.2f}s", extra=LOG_D)
                        continue
                    a = max(0, min(int(t_start * 16000), len(full_audio_signal)))
                    b = max(0, min(int(t_end * 16000), len(full_audio_signal)))
                    if b <= a:
                        continue
                    chunk = full_audio_signal[a:b].numpy().astype(np.float32)
                    resolved = segment_names.get((t_start, t_end, raw_speaker))
                    turn_kwargs = decode_kwargs
                    if (decode_kwargs.get('initial_prompt')
                            and (t_end - t_start) < prompt_min_turn_seconds):
                        turn_kwargs = dict(decode_kwargs, initial_prompt=None)
                        logger.info(
                            f"Decoding {raw_speaker} turn "
                            f"[{t_start:.2f}s -> {t_end:.2f}s] without the "
                            f"names prompt: shorter than "
                            f"{prompt_min_turn_seconds:.2f}s", extra=LOG_D)
                    try:
                        segs, _info = self.model.transcribe(chunk, **turn_kwargs)
                        rows = [_row(t_start + s.start, t_start + s.end, s,
                                     raw_speaker, resolved) for s in segs]
                        # Asked to decode a hard turn, Whisper sometimes recites
                        # the prompt back instead of transcribing, and the guard
                        # below then drops the segment -- so the turn is lost
                        # entirely. On clip 46446 that cost 21 s and eight turns
                        # of dialogue: the audio was loud and clean throughout,
                        # and decoded again with no prompt it came back in full.
                        # The prompt is an aid, not evidence; when it is the only
                        # thing that came back, take it away and ask again.
                        if (turn_kwargs.get('initial_prompt') and rows
                                and all(is_prompt_echo(r[2])
                                        or is_empty_after_cleaning(
                                            strip_banned(r[2] or ''))
                                        for r in rows)):
                            logger.warning(
                                f"{raw_speaker} turn [{t_start:.2f}s -> "
                                f"{t_end:.2f}s] came back as the prompt or "
                                f"boilerplate; re-decoding without the names "
                                f"prompt", extra=LOG_D)
                            segs, _info = self.model.transcribe(
                                chunk, **dict(turn_kwargs, initial_prompt=None))
                            retried = [_row(t_start + s.start, t_start + s.end,
                                            s, raw_speaker, resolved)
                                       for s in segs]
                            # Only if it actually did better. A turn that is
                            # boilerplate both ways keeps its first reading and
                            # is dropped downstream as before.
                            if any(not (is_prompt_echo(r[2])
                                        or is_empty_after_cleaning(
                                            strip_banned(r[2] or '')))
                                   for r in retried):
                                rows = retried
                        decoded.extend(rows)
                    except Exception as e:
                        logger.error(
                            f"Per-turn decode failed for {raw_speaker} "
                            f"[{t_start:.2f}s -> {t_end:.2f}s]: {e}", extra=LOG_D)

                # Overlapping turns get decoded twice, so the same words come
                # back under two turns. Matching on exact text was not enough:
                # the two decodes see slightly different audio and so word
                # slightly differently. Clip 40043 produced
                #   [0.98-3.10] "Αυτό έχει τώρα ξεσαλώσει, ε!"
                #   [1.28-3.00] "Αυτό έχει τώρα εξασσαλώσει."
                # -- one utterance, stored twice, because the strings differ by
                # a word. Two segments are the same utterance when they overlap
                # in time AND read alike, so both are required here.
                #
                # The earlier (and so usually longer-context) decode is kept:
                # above, it is the one that produced a real Greek word.
                from difflib import SequenceMatcher

                def _words(value):
                    return re.sub(r'[^\w\s]', ' ', (value or '').lower()).split()

                def _time_overlap(a, b):
                    span = min(a[1], b[1]) - max(a[0], b[0])
                    shorter = min(a[1] - a[0], b[1] - b[0])
                    return span / shorter if shorter > 0 else 0.0

                deduped = []
                # Sorted on the times alone. Comparing whole rows would fall
                # through to the trailing fields when two segments share a
                # start and end, and those can hold None beside a string.
                for item in sorted(decoded, key=lambda r: (r[0], r[1])):
                    words = _words(item[2])
                    duplicate = False
                    for kept_item in deduped:
                        if _time_overlap(item, kept_item) < 0.5:
                            continue
                        ratio = SequenceMatcher(
                            None, words, _words(kept_item[2])).ratio()
                        if ratio >= 0.6:
                            logger.info(
                                f"Dropping near-duplicate [{item[0]:.2f}s -> "
                                f"{item[1]:.2f}s] {(item[2] or '').strip()[:40]!r} "
                                f"({ratio:.0%} match with the overlapping turn "
                                f"already kept)", extra=LOG_D)
                            duplicate = True
                            break
                    if not duplicate:
                        deduped.append(item)
                decoded = deduped
            else:
                segments, info = self.model.transcribe(file_path, **decode_kwargs)
                decoded = [_row(s.start, s.end, s, None) for s in segments]

            # Collect segments with filtering and speaker mapping
            valid_segments = []

            def format_timestamp(seconds):
                m, s = divmod(seconds, 60)
                return f"{int(m):02d}:{s:05.2f}"


            # Boilerplate segments are removed BEFORE the duration guard below,
            # and the ordering is not cosmetic: a hallucinated segment carries a
            # wild end time (one spanned 8.96s -> 38.94s on a 14.75s clip), so
            # leaving it in stretches the measured span and makes the guard
            # reject the real speech sitting next to it. Filter first, measure
            # what is left.
            def drop_hallucinations(rows):
                """Boilerplate and prompt echoes out. Used by the retry below
                too, so both decodes are judged by exactly the same rules."""
                kept = []
                for row in rows:
                    if is_empty_after_cleaning(strip_banned(row[2] or '')):
                        logger.info(
                            f"Dropping segment [{row[0]:.2f}s -> {row[1]:.2f}s] "
                            f"{(row[2] or '').strip()[:40]!r}: entirely a known "
                            f"hallucination phrase", extra=LOG_D)
                        continue
                    if is_prompt_echo(row[2]):
                        logger.warning(
                            f"Dropping segment [{row[0]:.2f}s -> {row[1]:.2f}s] "
                            f"{(row[2] or '').strip()[:60]!r}: this is the "
                            f"initial_prompt read back, not speech", extra=LOG_D)
                        continue
                    kept.append(row)
                return kept

            decoded = drop_hallucinations(decoded)

            # Duration-mismatch guard.
            #
            # Whisper's signature failure is to emit fluent subtitle text over
            # audio that contains almost no speech, with perfectly round
            # segment boundaries and no warning in its own confidence numbers.
            # The diarizer, run on the same audio, is the independent witness:
            # on clip 39439 it found 0.38 s of speech across five fragments
            # while Whisper produced 4.00 s of "Ευχαριστώ." -- a 10x mismatch.
            #
            # Turn spans are summed rather than unioned, which over-counts when
            # turns overlap and so makes the guard harder to trip, not easier.
            #
            # The zero-turn case is checked FIRST and separately. The ratio test
            # below needs turns to divide by, so an empty turn list skipped the
            # guard entirely -- and "the diarizer found no speech whatsoever,
            # yet Whisper produced words" is the strongest hallucination signal
            # there is, not the weakest. Clip 39572 (a drawer) reached the
            # database as "Voula: Ραφαήλ" through exactly this hole.
            diariser_heard_nothing = diarization_result is not None and not turns
            if decoded and diariser_heard_nothing:
                logger.warning(
                    "Rejecting transcript: diarisation found no speech at all, "
                    f"but Whisper produced "
                    f"{[t.strip()[:40] for _a, _b, t, *_ in decoded]}",
                    extra=LOG_D)
                decoded = []

            if decoded and turns:
                detected_speech = sum(max(0.0, e - s) for s, e, _ in turns)
                transcript_span = max(e for _s, e, *_ in decoded) - \
                    min(s for s, *_ in decoded)
                if detected_speech > 0 and transcript_span > 0:
                    ratio = transcript_span / detected_speech
                    if ratio > max_speech_span_ratio:
                        logger.warning(
                            f"Rejecting transcript: {transcript_span:.2f}s of text "
                            f"over {detected_speech:.2f}s of detected speech "
                            f"({ratio:.1f}x, limit {max_speech_span_ratio:.1f}x). "
                            f"Dropped: "
                            f"{[t.strip()[:40] for _a, _b, t, *_ in decoded]}",
                            extra=LOG_D)
                        decoded = []

            # Nothing survived: retry on the speech alone.
            #
            # Handed a clip that is mostly silence, Whisper does not return an
            # empty string -- it returns the initial_prompt, or a stock
            # subtitle line, and the filters above rightly throw those away,
            # leaving the event with no transcript at all. Clip 45817 was 15 s
            # holding 9.8 s of speech in three pieces: as a whole it decoded to
            # the names prompt read back, while the same audio with the silence
            # removed decoded as "Boula, bges sto banio", which is what was
            # said, word for word.
            #
            # The regions come from the VAD pass main.py has already run, so
            # this costs one extra decode and only on a clip that would
            # otherwise store nothing. Trimming to the speech SPAN was not
            # enough (it misread one word) and dropping the internal gap by
            # hand was worse than useless (the prompt echo came back), so the
            # regions are used exactly as VAD reported them, padding included.
            #
            # Timestamps are mapped back to the original recording, because the
            # diarised turns, the span guard and the per-segment speaker
            # attribution all index the audio as it was heard, not as it was
            # spliced for this second attempt.
            # Both retries below rescue an utterance the first decode mangled.
            # On a clip the diarizer heard nobody in there is usually nothing to
            # rescue, and asking again just fetches another hallucination --
            # which then reaches the database, because the rejection above is
            # applied to the first decode alone. That is how the neighbours' dog
            # became "Ilyas: Υποτιτλισμός", "Rafael: Ωραία!" and "Ilyas:
            # Σταθμός!", and a cracking noise "Voula: Ευχαριστώ που
            # παρακολουθήσατε!" on 18-09-2026.
            #
            # Refusing every such retry would also lose the real speech pyannote
            # misses on this household's quieter, far-microphone clips, so the
            # enrolled voices decide instead: the retry runs only when the
            # speech VAD found actually matches somebody who lives here.
            retry_allowed = not diariser_heard_nothing
            if diariser_heard_nothing and not decoded and speech_regions \
                    and profile_manager is not None and full_audio_signal is not None:
                try:
                    voiced = [full_audio_signal[int(a * 16000):int(b * 16000)]
                              for a, b in speech_regions]
                    voiced = [piece for piece in voiced if len(piece)]
                    if voiced:
                        who, voice_score, _all = profile_manager.identify_speaker(
                            torch.cat(voiced))
                        retry_allowed = voice_score >= retry_voice_similarity
                        logger.info(
                            f"Diarisation heard nobody; the speech VAD found "
                            f"matches '{who}' at {voice_score:.3f} "
                            f"({'retrying' if retry_allowed else 'no retry'}, "
                            f"floor {retry_voice_similarity:.2f})", extra=LOG_D)
                except Exception as e:                       # noqa: BLE001
                    logger.error(f"Voice check before retry failed: {e}", extra=LOG_D)
                    retry_allowed = False

            if not decoded and retry_allowed \
                    and speech_regions and full_audio_signal is not None:
                pieces, spans, cursor = [], [], 0.0
                for region_start, region_end in speech_regions:
                    a = max(0, min(int(region_start * 16000), len(full_audio_signal)))
                    b = max(0, min(int(region_end * 16000), len(full_audio_signal)))
                    if b <= a:
                        continue
                    pieces.append(full_audio_signal[a:b].numpy().astype(np.float32))
                    length = (b - a) / 16000.0
                    # Region bounds taken from the clamped sample indices, so
                    # they describe the audio actually spliced in.
                    spans.append((cursor, cursor + length,
                                  a / 16000.0, b / 16000.0))
                    cursor += length

                if pieces:
                    def to_original(moment):
                        """Spliced-timeline second -> original-timeline second.

                        Clamped to the region the moment lands in. A segment
                        boundary sitting exactly on a splice is ambiguous to
                        within a rounding error, and without the clamp it can
                        resolve into the silence that was cut out -- landing
                        the timestamp in a gap where, by definition, nothing
                        was said.
                        """
                        for begins, ends, origin, finish in spans:
                            if moment <= ends:
                                return min(origin + max(0.0, moment - begins),
                                           finish)
                        begins, _ends, origin, finish = spans[-1]
                        return min(origin + max(0.0, moment - begins), finish)

                    try:
                        segs, _info = self.model.transcribe(
                            np.concatenate(pieces), **decode_kwargs)
                        retry = [_row(to_original(s.start), to_original(s.end),
                                      s, None) for s in segs]
                    except Exception as e:
                        logger.error(f"Speech-only retry failed: {e}", extra=LOG_D)
                        retry = []

                    retry = drop_hallucinations(retry)
                    if retry:
                        logger.info(
                            f"Recovered {len(retry)} segment(s) by decoding the "
                            f"{cursor:.2f}s of speech VAD found inside "
                            f"{len(full_audio_signal) / 16000:.2f}s of audio",
                            extra=LOG_D)
                        decoded = retry

            # Still nothing: the whole clip once more, without the names
            # prompt and with sampling instead of greedy decoding.
            #
            # This is a different failure from the one above. There the words
            # were buried in silence and removing it was enough. Here the clip
            # is mostly speech and fluent, and the prompt is what pulls the
            # decoder to boilerplate: on the reading session of 7 September,
            # dropping it recovered the passage the household was reading,
            # matching -- almost word for word -- the clean enrolment
            # recordings of the same sentences made minutes earlier.
            #
            # Measured over 132 clips of this branch, both retries run on each:
            #
            #     speech-only retry alone     16 recovered
            #     relaxed retry alone         30
            #     speech-only, then relaxed   35
            #
            # So it runs SECOND rather than instead. Replacing the first would
            # gain 19 clips and lose 5, and those 5 are the household's own
            # short utterances -- "Πατερ, τελειωσες!", "Ενα, δυο, τρια απο εδω"
            # -- which matter more to the reports than the read-aloud passages
            # making up most of the gain. Ordered this way costs a second
            # decode only on a clip that would otherwise store nothing.
            #
            # Retention was tried as a way to pick one retry instead of
            # running both and does not work: the five clips only the first
            # retry rescues sit at 0.26, 0.28, 0.54, 0.80 and 1.00, across the
            # whole range.
            if not decoded and retry_allowed and full_audio_signal is not None:
                # beam_size is not dropped because faster-whisper ignores it
                # once temperature > 0 -- it forces beam_size=1 and samples
                # best_of hypotheses instead. Raising best_of past 5 only
                # re-rolls the dice: same content, different wording.
                relaxed_kwargs = dict(decode_kwargs)
                relaxed_kwargs.update(initial_prompt=None, temperature=0.4,
                                      best_of=5, no_speech_threshold=0.6)
                try:
                    segs, _info = self.model.transcribe(
                        full_audio_signal.numpy().astype(np.float32),
                        **relaxed_kwargs)
                    relaxed = [_row(s.start, s.end, s, None) for s in segs]
                except Exception as e:
                    logger.error(f"Relaxed retry failed: {e}", extra=LOG_D)
                    relaxed = []

                relaxed = [row for row in drop_hallucinations(relaxed)
                           if not is_repetition_loop(row[2])]
                if relaxed:
                    logger.info(
                        f"Recovered {len(relaxed)} segment(s) on a relaxed "
                        f"decode of the whole clip, without the names prompt",
                        extra=LOG_D)
                    decoded = relaxed

            for (seg_start, seg_end, seg_text, turn_speaker,
                 seg_logprob, seg_no_speech, seg_resolved) in decoded:
                # Skip very short segments (likely noise/hallucination)
                if len((seg_text or '').strip()) < 3:
                    continue

                # Skip if avg_logprob is too low (uncertain transcription)
                if seg_logprob is not None and seg_logprob < -2:
                    continue

                # Skip if no_speech_prob is too high
                if seg_no_speech is not None and seg_no_speech > 0.9:
                    continue

                text = seg_text.strip()
                logger.info(f"Whisper Segment: [{seg_start:.2f}s -> {seg_end:.2f}s] Text: '{text}'", extra=LOG_D)

                # Banned phrases are stripped here, per segment, rather than
                # from the joined blob further down. Doing it after the join
                # left the speaker prefix behind when the whole utterance was
                # a banned phrase -- that is where the "Unknown:." rows came
                # from: "ευχαριστώ." passed the 3-character gate, then lost
                # its only word and left an empty labelled line.
                # Boilerplate-only segments were already removed above, before
                # the duration guard measured the span. Nothing to redo here.

                # Attribute the segment, most reliable evidence first:
                #   0. the segment's own embedding, when it beat its cluster's
                #      name by more than the margin floor -- the only evidence
                #      here that survives a cluster holding two people;
                #   1. the diarised turn this text was decoded FROM, when the
                #      per-turn path ran -- there is no overlap ambiguity there;
                #   2. the turn with the most overlap, for the whole-file path;
                #   3. the segment's own audio, when diarisation covered nothing.
                current_speaker = None
                speaker_score = None

                if seg_resolved is not None:
                    current_speaker = seg_resolved

                if current_speaker is None and turn_speaker is not None \
                        and turn_speaker in speaker_mapping:
                    current_speaker = speaker_mapping[turn_speaker]
                    speaker_score = speaker_scores.get(turn_speaker)

                if current_speaker is None and turn_speaker is None and diarization_result:
                    from pyannote.core import Segment as PySegment
                    whisper_seg = PySegment(seg_start, seg_end)

                    speaker_counts = {}
                    for s in diarization_result.crop(whisper_seg).itertracks(yield_label=True):
                        overlap = s[0].duration
                        speaker_counts[s[2]] = speaker_counts.get(s[2], 0) + overlap

                    if speaker_counts:
                        raw_speaker = max(speaker_counts, key=speaker_counts.get)
                        if raw_speaker in speaker_mapping:
                            current_speaker = speaker_mapping[raw_speaker]
                            speaker_score = speaker_scores.get(raw_speaker)

                if current_speaker is None and profile_manager is not None \
                        and full_audio_signal is not None:
                    try:
                        start_sample = max(0, min(int(seg_start * 16000),
                                                  len(full_audio_signal)))
                        end_sample = max(0, min(int(seg_end * 16000),
                                                len(full_audio_signal)))
                        if end_sample > start_sample:
                            name, score, _all = profile_manager.identify_speaker(
                                full_audio_signal[start_sample:end_sample])
                            if score >= min_segment_similarity:
                                current_speaker, speaker_score = name, score
                                logger.info(
                                    f"Segment [{seg_start:.2f}s -> {seg_end:.2f}s] "
                                    f"matched '{name}' directly with similarity "
                                    f"{score:.3f}", extra=LOG_D)
                            else:
                                logger.info(
                                    f"Segment [{seg_start:.2f}s -> {seg_end:.2f}s] "
                                    f"left unidentified: best was '{name}' at "
                                    f"{score:.3f}, below the "
                                    f"{min_speaker_similarity:.2f} floor",
                                    extra=LOG_D)
                    except Exception as e:
                        logger.error(
                            f"Per-segment speaker identification failed: {e}",
                            extra=LOG_D)

                if current_speaker is None:
                    # Reached when no candidate cleared the similarity floor, or
                    # when the speaker model is unavailable. "Unknown" here is an
                    # answer -- "the closed set does not recognise this voice" --
                    # not the old "nobody asked".
                    current_speaker = "Unknown"

                # The label stays a bare name: tests/diagnostics and the
                # dynamics pipeline both match it against the resident list by
                # exact string, so decorating it with the score here would drop
                # every segment out of the speaker confusion matrix. The score
                # goes to the log instead.
                if speaker_score is not None:
                    logger.info(
                        f"Segment speaker '{current_speaker}' similarity "
                        f"{speaker_score:.3f}", extra=LOG_D)

                # Format: [00:00.55 - 00:06.33] Speaker_0: Text
                timestamp_str = f"[{format_timestamp(seg_start)} - {format_timestamp(seg_end)}]"
                valid_segments.append(f"{timestamp_str} {current_speaker}: {text}")

            # Join segments with newlines for clear display in the dashboard
            clean_text = "\n".join(valid_segments)

            # Final cleanup: if result is just punctuation or very short, return empty
            final_text = clean_text.strip()
            final_text = remove_consecutive_repetitions(final_text)
            final_text = remove_repeated_phrases(final_text)
            if len(final_text) <= 2 or final_text in [".", ",", "!", "?"]:
                return ""
                
            return final_text
            
        except Exception as e:
            logger.error(f"ASR Error: {e}", extra=LOG_D)
            return ""

if __name__ == "__main__":
    # Test
    print("\n--- ASRTranscriber Test ---")
    asr = ASRTranscriber()
    # Find a test file
    test_audio_path = os.path.join(os.path.dirname(__file__), '..', 'assets', 'audio_mp3', '20260114_124616_esp32s3-3_rec.mp3')
    if os.path.exists(test_audio_path) and asr.model:
        print(f"\nTranscribing test file: {test_audio_path}")
        transcription = asr.transcribe(test_audio_path)
        print(f"\nTranscription Result: '{transcription}'")
    elif not asr.model:
        print("\nSkipping transcription test because model failed to load.")
    else:
        print(f"\nTest audio file not found at: {test_audio_path}")
