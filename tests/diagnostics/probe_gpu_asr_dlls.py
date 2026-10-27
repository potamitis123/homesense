"""Report where every CUDA DLL used by the analysis pipeline is resolved from.

Exercises the full GPU analysis path (AST classification, pyannote diarization,
speaker embedding and faster-whisper transcription) and then lists every loaded
CUDA module together with the directory it came from. Used to trim gpu_asr down
to the libraries CTranslate2 genuinely needs, rather than the whole CUDA
redistributable set.
"""

import glob
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GPU_ASR = os.path.normcase(os.path.join(PROJECT_ROOT, "gpu_asr"))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "ast_src"))
os.chdir(PROJECT_ROOT)

import psutil

INTERESTING = ("cublas", "cudnn", "cudart", "cufft", "curand", "cusolver",
               "cusparse", "cupti", "nvrtc", "nvjit", "cuda")


def cuda_modules():
    found = {}
    for mapping in psutil.Process().memory_maps():
        path = mapping.path
        if not path.lower().endswith(".dll"):
            continue
        name = os.path.basename(path).lower()
        if any(name.startswith(prefix) for prefix in INTERESTING):
            found[name] = os.path.normcase(os.path.abspath(path))
    return found


def pick_speech_file():
    candidates = sorted(glob.glob(os.path.join(PROJECT_ROOT, "assets", "audio_mp3", "*_rec.wav")))
    return candidates[-1] if candidates else None


from ast_model import AST
import numpy as np

ast = AST()
ast.inference(waveform=np.random.randn(16000 * 3).astype("float32") * 0.05, sample_rate=16000)

from ast_src.streaming_ASR import ASRTranscriber

transcriber = ASRTranscriber()
target = pick_speech_file()
print(f"\nTranscribing: {os.path.basename(target)}", flush=True)
print("Result:", repr(transcriber.transcribe(target))[:200], flush=True)

modules = cuda_modules()
print("\n" + "=" * 88)
print(f"{'CUDA DLL':<42}RESOLVED FROM")
print("=" * 88)
for name in sorted(modules):
    folder = os.path.dirname(modules[name])
    origin = "gpu_asr" if folder == GPU_ASR else os.path.basename(folder)
    marker = "  <== gpu_asr" if folder == GPU_ASR else ""
    print(f"  {name:<40}{origin}{marker}")

from_gpu_asr = {n for n, p in modules.items() if os.path.dirname(p) == GPU_ASR}
present = {n.lower() for n in os.listdir(os.path.join(PROJECT_ROOT, "gpu_asr"))
           if n.lower().endswith(".dll")}
unused = present - from_gpu_asr
freed = sum(os.path.getsize(os.path.join(PROJECT_ROOT, "gpu_asr", n)) for n in unused) / (1024 * 1024)
print("\n" + "=" * 88)
print(f"  needed from gpu_asr : {sorted(from_gpu_asr)}")
print(f"  unused in gpu_asr   : {len(unused)} files, {freed:.0f} MB")
