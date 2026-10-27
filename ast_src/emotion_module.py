import os
os.environ["TORCH_HUB_OFFLINE"] = "1"

import torch
import torch.nn as nn
import librosa
import numpy as np
from transformers import Wav2Vec2FeatureExtractor, HubertForSequenceClassification
import logging
from common_files.device_handler import device

# ---------------------------------------------------------------------------
# Which emotion model backs recognize().
#
#   'dimensional' : audeering wav2vec2-large-robust fine-tuned on MSP-Podcast.
#                   Predicts arousal / dominance / valence, mapped back to the
#                   four labels the dashboard already renders.
#   'categorical' : the original superb/hubert-large-superb-er classifier.
#
# Flip this back to 'categorical' to restore the previous behaviour exactly --
# no other edit, no git needed.
#
# Why the change: HuBERT-superb is fine-tuned on IEMOCAP (acted studio English),
# where 'sad' acts as the low-energy catch-all. On this household's ambient
# audio it labelled 74.7% of 4,473 events 'sad' (avg confidence 0.76) and found
# anger 17 times -- useless for the household-dynamics work. The dimensional
# model was calibrated against 532 of those same recordings; see
# tests/diagnostics/compare_emotion_models.py for the harness and the numbers.
# ---------------------------------------------------------------------------
EMOTION_MODEL_MODE = 'dimensional'

DIMENSIONAL_MODEL_ID = "audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim"
CATEGORICAL_MODEL_ID = "superb/hubert-large-superb-er"

# Circumplex cut points fitted on 532 labelled recordings from this house, to
# the household's own estimate of reality (75% neutral / 20% happy / 3% angry /
# 2% sad). Re-fit with:
#   python tests/diagnostics/compare_emotion_models.py --recalibrate
VALENCE_LOW = 0.26
VALENCE_HIGH = 0.58
AROUSAL_HIGH = 0.49


class RegressionHead(nn.Module):
    """Head for the audeering checkpoint (declared here: the published model
    ships no remote code and its architecture is not a stock transformers
    class, so it cannot be loaded with AutoModelForAudioClassification)."""

    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.dropout = nn.Dropout(config.final_dropout)
        self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

    def forward(self, features, **kwargs):
        x = self.dropout(features)
        x = self.dense(x)
        x = torch.tanh(x)
        x = self.dropout(x)
        return self.out_proj(x)


def _build_dimensional_model():
    # Imported lazily so the 'categorical' rollback path never pays for it.
    from transformers.models.wav2vec2.modeling_wav2vec2 import (
        Wav2Vec2Model,
        Wav2Vec2PreTrainedModel,
    )

    class EmotionModel(Wav2Vec2PreTrainedModel):
        """Outputs three regression values: arousal, dominance, valence."""

        def __init__(self, config):
            super().__init__(config)
            self.config = config
            self.wav2vec2 = Wav2Vec2Model(config)
            self.classifier = RegressionHead(config)
            self.init_weights()

        def forward(self, input_values):
            hidden_states = self.wav2vec2(input_values)[0]
            hidden_states = torch.mean(hidden_states, dim=1)
            return hidden_states, self.classifier(hidden_states)

    return EmotionModel


def map_dimensions_to_label(arousal, valence):
    """Arousal/valence -> one of neu / hap / ang / sad.

    Low valence splits on arousal: agitated is anger, subdued is sadness.
    Kept to these four labels deliberately -- the Statistics radar charts
    exactly HAPPY/SAD/NEUTRAL/ANGRY, so a fifth label would silently vanish
    from it.
    """
    if valence <= VALENCE_LOW:
        return "ang" if arousal >= AROUSAL_HIGH else "sad"
    if valence >= VALENCE_HIGH:
        return "hap"
    return "neu"


def _dimensional_confidence(label, arousal, valence):
    """How far inside its own region the prediction sits.

    Reported on a 0.5-1.0 scale: a point sitting on a decision boundary is a
    coin flip rather than zero confidence, which also keeps these numbers in
    the same range as the softmax scores the classifier used to emit.
    """
    if label == "hap":
        margin = (valence - VALENCE_HIGH) / max(1e-6, 1.0 - VALENCE_HIGH)
    elif label == "ang":
        margin = min((VALENCE_LOW - valence) / max(1e-6, VALENCE_LOW),
                     (arousal - AROUSAL_HIGH) / max(1e-6, 1.0 - AROUSAL_HIGH))
    elif label == "sad":
        margin = min((VALENCE_LOW - valence) / max(1e-6, VALENCE_LOW),
                     (AROUSAL_HIGH - arousal) / max(1e-6, AROUSAL_HIGH))
    else:  # neu -- confidence peaks mid-band, not at the edges
        centre = (VALENCE_LOW + VALENCE_HIGH) / 2
        half_width = max(1e-6, (VALENCE_HIGH - VALENCE_LOW) / 2)
        margin = 1.0 - abs(valence - centre) / half_width
    return round(0.5 + 0.5 * min(1.0, max(0.0, margin)), 2)


def dimensions_tag(dimensions) -> str:
    """The raw regression values, as a suffix for the stored emotion string.

    The model predicts arousal, dominance and valence and then
    map_dimensions_to_label collapses them to one of four codes. Only the code
    was ever stored, and the four-class label was measured against a listener
    on 149 clips at Cohen's kappa of -0.02 -- chance. That condemns the
    discretisation; it says nothing about the three numbers underneath it,
    because nobody had kept them.

    Appended rather than given columns of their own so that no schema changes
    and no writer breaks: both readers of this field match the label and
    confidence from the start of the string and ignore what follows.

        SAD (0.76) [a=0.31 d=0.42 v=0.28]
    """
    if not dimensions:
        return ""
    arousal, dominance, valence = dimensions
    return f" [a={arousal:.2f} d={dominance:.2f} v={valence:.2f}]"


class EmotionRecognizer:
    def __init__(self, model_id=None):
        # Force CPU to save VRAM for Whisper/Diarization
        self.device = torch.device("cpu")
        self.mode = EMOTION_MODEL_MODE
        # Last raw (arousal, dominance, valence), for debugging / re-calibration.
        self.last_dimensions = None

        if self.mode == 'dimensional':
            model_id = model_id or DIMENSIONAL_MODEL_ID
            logging.info(f"Initializing dimensional emotion model {model_id} on {self.device}")
            self.feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(model_id)
            self.model = _build_dimensional_model().from_pretrained(model_id).to(self.device)
        else:
            model_id = model_id or CATEGORICAL_MODEL_ID
            logging.info(f"Initializing HuBERT Emotion Recognizer on {self.device} (Forced CPU for VRAM management)")
            # Set do_normalize=True explicitly to ensure standard scaling
            self.feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(model_id, do_normalize=True)
            self.model = HubertForSequenceClassification.from_pretrained(model_id).to(self.device)
        self.model.eval()

        # 2. Load Silero VAD - Load from local cache offline if available
        local_vad_path = os.path.join(os.path.expanduser("~"), ".cache", "torch", "hub", "snakers4_silero-vad_master")
        if os.path.exists(local_vad_path):
            self.vad_model, utils = torch.hub.load(repo_or_dir=local_vad_path,
                                                  model='silero_vad',
                                                  source='local',
                                                  onnx=False)
        else:
            self.vad_model, utils = torch.hub.load(repo_or_dir='snakers4/silero-vad',
                                                  model='silero_vad',
                                                  force_reload=False,
                                                  onnx=False,
                                                  trust_repo=True)
        self.get_speech_timestamps = utils[0]

    def predict(self, file_path, apply_vad=True):
        # Load audio (the emotion model and Silero both need 16kHz)
        y, sr = librosa.load(file_path, sr=16000)

        # -- FIX 1: Peak Normalization --
        # Emotion models are sensitive to volume. If audio is quiet, they predict "Sad".
        # We normalize the entire file to -1.0 to 1.0 range before processing.
        if np.max(np.abs(y)) > 0:
            y = y / np.max(np.abs(y))

        audio_tensor = torch.from_numpy(y).float()
        return self.recognize(audio_tensor, sampling_rate=16000, apply_vad=apply_vad)

    def recognize(self, audio_tensor, sampling_rate=16000, apply_vad=True):
        if not self.model:
            return "Model Error", 0.0

        clean_audio = audio_tensor

        if apply_vad:
            vad_input = audio_tensor.cpu() if audio_tensor.device.type != 'cpu' else audio_tensor

            # Get all speech chunks
            speech_timestamps = self.get_speech_timestamps(
                vad_input,
                self.vad_model,
                sampling_rate=sampling_rate,
                min_speech_duration_ms=100  # Lowered to catch short bursts (e.g., "Stop!")
            )

            if len(speech_timestamps) > 0:
                # -- FIX 2: Concatenate All Speech --
                # Previously, you took only the `max` (longest) segment.
                # This often discarded the emotional peak if it was short.
                # Now we join all valid speech parts together.
                segments = []
                for ts in speech_timestamps:
                    start = ts['start']
                    end = ts['end']
                    segments.append(audio_tensor[start:end])

                # Combine segments with a tiny bit of silence (100ms) in between so words don't merge
                silence = torch.zeros(int(0.1 * sampling_rate))
                clean_audio = torch.cat([seg for pair in zip(segments, [silence]*len(segments)) for seg in pair])

                # Remove the last trailing silence
                if len(clean_audio) > len(silence):
                    clean_audio = clean_audio[:-len(silence)]
            else:
                # If VAD fails to find speech, fallback to original audio instead of returning error
                # (Sometimes VAD is too strict on emotional shouting/crying)
                logging.warning("VAD found no speech, using original audio.")
                clean_audio = audio_tensor

        # Cleared before every inference. These are read by the caller AFTER
        # predict() returns, so a value left over from the previous clip would
        # be written into this clip's row -- and an early return below would
        # leave exactly that.
        self.last_dimensions = None

        # -- FIX 3: Minimum Length Check --
        # The model needs roughly 0.25s+ to form a valid embedding.
        if clean_audio.shape[0] < 1600: # Less than 0.1s
            return "Silence/Too Short", 0.0

        # Inference
        inputs = self.feature_extractor(
            clean_audio.numpy(),
            sampling_rate=sampling_rate,
            return_tensors="pt",
            padding=True
        ).to(self.device)

        if self.mode == 'dimensional':
            with torch.no_grad():
                _, logits = self.model(inputs["input_values"])
            arousal, dominance, valence = [float(x) for x in logits[0].cpu().numpy()]
            self.last_dimensions = (arousal, dominance, valence)
            label = map_dimensions_to_label(arousal, valence)
            confidence = _dimensional_confidence(label, arousal, valence)
            # Raw values logged so the cut points above can be re-fitted later
            # from production audio without re-running the whole harness.
            logging.info(
                f"Emotion dims: arousal={arousal:.3f} dominance={dominance:.3f} "
                f"valence={valence:.3f} -> {label} ({confidence:.2f})"
            )
            return label, confidence

        with torch.no_grad():
            logits = self.model(**inputs).logits
            predicted_ids = torch.argmax(logits, dim=-1)
            probabilities = torch.nn.functional.softmax(logits, dim=-1)
            confidence = torch.max(probabilities).item()

        label = self.model.config.id2label[predicted_ids.item()]

        # Map rare labels if necessary (HuBERT-Large-Superb output: neu, hap, ang, sad)
        return label, confidence
