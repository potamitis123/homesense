"""Offline A/B of the current emotion model against audeering's dimensional one.

Nothing here touches the app: it reads existing WAVs from assets/audio_mp3
that already carry an emotion label in the DB, runs both models over the
*same* preprocessed audio, and reports how each one's label distribution
compares with the informal ground truth for this household.

Why two phases: wav2vec2-large inference is the slow part, so raw model
output is cached to JSON and the threshold calibration re-runs off that
cache instantly. Re-running with different thresholds costs nothing.

    python tests/diagnostics/compare_emotion_models.py                # infer (cached) + calibrate
    python tests/diagnostics/compare_emotion_models.py --limit 100    # quick smoke run
    python tests/diagnostics/compare_emotion_models.py --device cuda  # only if the app is NOT running
    python tests/diagnostics/compare_emotion_models.py --recalibrate  # skip inference, reuse cache

The audeering model predicts arousal / dominance / valence rather than
labels, so a quadrant mapping turns it back into the four labels the
dashboard, Statistics radar and derive_home_dynamics already understand.
The mapping's cut points are fitted here, not guessed.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json
import os
import re
import sqlite3
import sys
import time

# Must precede numpy/torch: mixed BLAS runtimes otherwise abort mid-matmul.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TORCH_HUB_OFFLINE", "1")

import numpy as np
import torch
import torch.nn as nn

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

AUDIO_DIR = os.path.join(PROJECT_ROOT, "assets", "audio_mp3")
DB_PATH = os.path.join(PROJECT_ROOT, "ast_audio_db", "ast_audio.db")
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "emotion_model_comparison_cache.json")

AUDEERING_ID = "audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim"

LABELS = ("neu", "hap", "ang", "sad")

# The household's own estimate of reality, used as the calibration target.
TARGET = {"neu": 0.75, "hap": 0.20, "ang": 0.03, "sad": 0.02}


# ── audeering model definition ───────────────────────────────────────
# The published checkpoint has no remote code, and its architecture
# (Wav2Vec2ForSpeechClassification) is not a stock transformers class, so
# the regression head is declared here exactly as on the model card.

from transformers import Wav2Vec2FeatureExtractor  # noqa: E402
from transformers.models.wav2vec2.modeling_wav2vec2 import (  # noqa: E402
    Wav2Vec2Model,
    Wav2Vec2PreTrainedModel,
)


class RegressionHead(nn.Module):
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


# ── shared preprocessing (mirrors production) ────────────────────────

def build_preprocessor():
    """Peak-normalise + VAD-concatenate, exactly as EmotionRecognizer does.

    Production hands the emotion model a VAD-filtered segment, while the
    files on disk are full recordings -- so the same filtering has to be
    reproduced here or the comparison would not reflect what the app sees.
    """
    import librosa

    local_vad = os.path.join(os.path.expanduser("~"), ".cache", "torch", "hub",
                             "snakers4_silero-vad_master")
    if os.path.exists(local_vad):
        vad_model, utils = torch.hub.load(repo_or_dir=local_vad, model="silero_vad",
                                          source="local", onnx=False)
    else:
        vad_model, utils = torch.hub.load(repo_or_dir="snakers4/silero-vad",
                                          model="silero_vad", onnx=False, trust_repo=True)
    get_speech_timestamps = utils[0]

    def preprocess(path, sr=16000):
        y, _ = librosa.load(path, sr=sr)
        peak = np.max(np.abs(y)) if y.size else 0.0
        if peak > 0:
            y = y / peak
        tensor = torch.from_numpy(y).float()

        stamps = get_speech_timestamps(tensor, vad_model, sampling_rate=sr,
                                       min_speech_duration_ms=100)
        if stamps:
            silence = torch.zeros(int(0.1 * sr))
            segments = [tensor[s["start"]:s["end"]] for s in stamps]
            joined = torch.cat([x for seg in segments for x in (seg, silence)])
            tensor = joined[:-len(silence)] if len(joined) > len(silence) else joined
        if tensor.shape[0] < 1600:  # under 0.1s -- same floor as production
            return None
        return tensor

    return preprocess


# ── inference ────────────────────────────────────────────────────────

def load_rows(limit=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT file_name, emotion FROM ast_table "
                "WHERE emotion IS NOT NULL AND emotion != 'None' AND file_name IS NOT NULL "
                "ORDER BY date DESC")
    rows = []
    for file_name, emotion in cur.fetchall():
        path = os.path.join(AUDIO_DIR, file_name)
        if not os.path.exists(path):
            continue
        match = re.match(r"([A-Za-z/ ]+)", emotion or "")
        rows.append({
            "file_name": file_name,
            "db_label": (match.group(1).strip().lower()[:3] if match else "?"),
        })
    conn.close()

    # Sample across the whole period rather than taking the newest N: a
    # recent slice can miss whole labels (the last 8 files contain no 'sad'
    # at all), which would make a limited run misleading. Fixed seed so
    # repeated runs compare like for like.
    if limit and len(rows) > limit:
        import random
        random.Random(20260819).shuffle(rows)
        rows = rows[:limit]
    return rows


def run_inference(rows, device, with_baseline):
    preprocess = build_preprocessor()

    print(f"Loading {AUDEERING_ID} on {device} ...", flush=True)
    extractor = Wav2Vec2FeatureExtractor.from_pretrained(AUDEERING_ID)
    model = EmotionModel.from_pretrained(AUDEERING_ID).to(device).eval()

    baseline = None
    if with_baseline:
        # The live model, re-run here so "does our preprocessing match
        # production?" is answerable rather than assumed.
        from ast_src.emotion_module import EmotionRecognizer
        print("Loading current model (superb/hubert-large-superb-er) ...", flush=True)
        baseline = EmotionRecognizer()

    results, started, skipped = [], time.time(), 0
    for i, row in enumerate(rows, 1):
        path = os.path.join(AUDIO_DIR, row["file_name"])
        try:
            audio = preprocess(path)
        except Exception as exc:
            print(f"  [{i}] {row['file_name']}: preprocess failed ({exc})")
            skipped += 1
            continue
        if audio is None:
            skipped += 1
            continue

        inputs = extractor(audio.numpy(), sampling_rate=16000, return_tensors="pt")
        with torch.no_grad():
            _, logits = model(inputs["input_values"].to(device))
        arousal, dominance, valence = [float(x) for x in logits[0].cpu().numpy()]

        record = {**row, "arousal": arousal, "dominance": dominance, "valence": valence}

        if baseline is not None:
            label, conf = baseline.recognize(audio, sampling_rate=16000, apply_vad=False)
            record["baseline_label"] = label.strip().lower()[:3]
            record["baseline_conf"] = float(conf)

        results.append(record)
        if i % 25 == 0 or i == len(rows):
            rate = i / max(1e-9, time.time() - started)
            print(f"  {i}/{len(rows)}  ({rate:.1f} files/s)", flush=True)

    print(f"inference done: {len(results)} usable, {skipped} skipped")
    return results


# ── quadrant mapping + calibration ───────────────────────────────────

def map_label(arousal, valence, v_low, v_high, a_high):
    """Circumplex quadrants -> the four labels the app already renders."""
    if valence <= v_low:
        return "ang" if arousal >= a_high else "sad"
    if valence >= v_high:
        return "hap"
    return "neu"


def confidence(arousal, valence):
    """How far from the neutral centre -- stands in for a softmax score."""
    return round(min(1.0, max(abs(valence - 0.5), abs(arousal - 0.5)) * 2), 2)


def distribution(labels):
    counts = collections.Counter(labels)
    total = max(1, sum(counts.values()))
    return {lab: counts.get(lab, 0) / total for lab in LABELS}, counts


def fit_thresholds(records):
    """Grid-search cut points that best reproduce the household's estimate."""
    best, best_err = None, float("inf")
    grid_v = [round(x, 3) for x in np.arange(0.20, 0.75, 0.01)]
    grid_a = [round(x, 3) for x in np.arange(0.35, 0.85, 0.01)]
    for v_low, v_high in itertools.product(grid_v, grid_v):
        if v_high <= v_low:
            continue
        for a_high in grid_a:
            labels = [map_label(r["arousal"], r["valence"], v_low, v_high, a_high)
                      for r in records]
            dist, _ = distribution(labels)
            err = sum(abs(dist[k] - TARGET[k]) for k in LABELS)
            if err < best_err:
                best, best_err = (v_low, v_high, a_high), err
    return best, best_err


# ── reporting ────────────────────────────────────────────────────────

def pct_table(title, dist, counts):
    print(f"\n{title}")
    for lab in LABELS:
        bar = "#" * int(round(dist[lab] * 40))
        print(f"  {lab:4s} {counts.get(lab,0):5d}  {100*dist[lab]:5.1f}%  {bar}")


def report(records):
    n = len(records)
    print("\n" + "=" * 68)
    print(f"EMOTION MODEL COMPARISON   n={n} files")
    print("=" * 68)

    print("\nRaw audeering output (0-1 scale):")
    for key in ("arousal", "dominance", "valence"):
        vals = np.array([r[key] for r in records])
        print(f"  {key:10s} mean={vals.mean():.3f}  p10={np.percentile(vals,10):.3f}  "
              f"p50={np.percentile(vals,50):.3f}  p90={np.percentile(vals,90):.3f}")

    dist, counts = distribution([r["db_label"] for r in records])
    pct_table("CURRENT model, as stored in the DB:", dist, counts)

    if "baseline_label" in records[0]:
        dist_b, counts_b = distribution([r["baseline_label"] for r in records])
        pct_table("CURRENT model, re-run here (preprocessing fidelity check):", dist_b, counts_b)
        agree = sum(1 for r in records if r["baseline_label"] == r["db_label"])
        print(f"  agreement with stored labels: {100*agree/n:.1f}%")

    target_counts = {k: int(round(v * n)) for k, v in TARGET.items()}
    pct_table("TARGET (household estimate):", TARGET, target_counts)

    (v_low, v_high, a_high), err = fit_thresholds(records)
    labels = [map_label(r["arousal"], r["valence"], v_low, v_high, a_high) for r in records]
    dist_new, counts_new = distribution(labels)
    pct_table(f"AUDEERING mapped  (valence<={v_low}, valence>={v_high}, arousal>={a_high}):",
              dist_new, counts_new)
    print(f"  total absolute deviation from target: {err:.3f}")

    print("\nWhere the current model's labels move to (rows=current, cols=audeering):")
    header = "".join(f"{lab:>7s}" for lab in LABELS)
    print(f"  {'':5s}{header}")
    for old in LABELS:
        cells = collections.Counter(new for r, new in zip(records, labels)
                                    if r["db_label"] == old)
        row = "".join(f"{cells.get(new,0):7d}" for new in LABELS)
        print(f"  {old:5s}{row}")

    print("\nSuggested mapping for emotion_module.py:")
    print(f"  v_low={v_low}  v_high={v_high}  a_high={a_high}")
    print("=" * 68)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="only process the N most recent labelled files")
    parser.add_argument("--device", default="cpu",
                        help="cpu (default) or cuda -- cuda only if the app is not running")
    parser.add_argument("--no-baseline", action="store_true",
                        help="skip re-running the current model (faster, less rigorous)")
    parser.add_argument("--recalibrate", action="store_true",
                        help="reuse the cached inference and only redo the calibration")
    args = parser.parse_args()

    # A smoke run's cache must not be mistaken for the full one later.
    global CACHE_PATH
    if args.limit:
        CACHE_PATH = CACHE_PATH.replace(".json", f"_limit{args.limit}.json")

    if args.recalibrate or os.path.exists(CACHE_PATH):
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, encoding="utf-8") as fh:
                records = json.load(fh)
            print(f"loaded {len(records)} cached results from {CACHE_PATH}")
            if records:
                report(records)
                return
        if args.recalibrate:
            print(f"no cache at {CACHE_PATH} -- run without --recalibrate first")
            return

    rows = load_rows(args.limit)
    print(f"{len(rows)} labelled files still on disk")
    if not rows:
        print("nothing to compare")
        return

    records = run_inference(rows, args.device, not args.no_baseline)
    if not records:
        print("no usable results")
        return

    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=1)
    print(f"cached raw results -> {CACHE_PATH}")
    report(records)


if __name__ == "__main__":
    main()
