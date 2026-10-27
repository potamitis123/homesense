"""Smoke test: confirm faster-whisper can actually transcribe on the GPU.

Verifies the CUDA 12 cuBLAS/cuDNN libraries in gpu_asr are being picked up,
i.e. that transcription no longer fails with 'cublas64_12.dll is not found'.
"""

import glob
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "ast_src"))
os.chdir(PROJECT_ROOT)

from ast_src.streaming_ASR import ASRTranscriber

candidates = sorted(glob.glob(os.path.join(PROJECT_ROOT, "assets", "audio_mp3", "*_rec.wav")))[-3:]
if not candidates:
    candidates = sorted(glob.glob(os.path.join(PROJECT_ROOT, "test_sounds", "*.wav")))[:3]

print(f"Candidates: {[os.path.basename(c) for c in candidates]}", flush=True)

transcriber = ASRTranscriber()
for path in candidates:
    try:
        text = transcriber.transcribe(path)
        print(f"OK    {os.path.basename(path)} -> {text!r}", flush=True)
    except Exception as exc:
        print(f"ERROR {os.path.basename(path)} -> {type(exc).__name__}: {exc}", flush=True)
