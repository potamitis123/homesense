"""Check pyannote/speechbrain availability the way the application loads them.

speechbrain 1.0.3 calls torchaudio.list_audio_backends() at import time, which
torchaudio 2.9 removed. ast_src/streaming_ASR.py installs a shim for it, so a
bare "import speechbrain" fails while the application path works. Each scenario
runs in its own interpreter, because a failed import leaves a broken partial
module behind that would poison anything tried afterwards.
"""

import os
import subprocess
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

BARE = """
import torch, torchaudio
print(f"    torch {torch.__version__}  cuda={torch.cuda.is_available()}")
print(f"    torchaudio {torchaudio.__version__}")
print(f"    torchaudio.list_audio_backends present: {hasattr(torchaudio,'list_audio_backends')}")
try:
    import speechbrain
    print("    import speechbrain   OK")
except Exception as exc:
    print(f"    import speechbrain   {type(exc).__name__}: {exc}")
"""

VIA_APP = """
import sys, os
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "ast_src"))
os.chdir(PROJECT_ROOT)
import torchaudio
from ast_src.streaming_ASR import ASRTranscriber
print(f"    torchaudio.list_audio_backends after shim: {hasattr(torchaudio,'list_audio_backends')}")
import speechbrain
print("    import speechbrain                OK")
from speechbrain.inference import EncoderClassifier
print("    speechbrain.inference.EncoderClassifier OK")
from pyannote.audio import Pipeline
print("    pyannote.audio.Pipeline           OK")
"""


def run(title, code):
    print(f"\n{title}")
    source = f"PROJECT_ROOT = {PROJECT_ROOT!r}\n" + code
    result = subprocess.run([sys.executable, "-u", "-c", source],
                            cwd=PROJECT_ROOT, capture_output=True, text=True)
    for line in (result.stdout or "").splitlines():
        if line.startswith("    "):
            print(line)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        print(f"    FAILED (exit {result.returncode}): {tail[-1] if tail else ''}")


print(f"executable    : {sys.executable}")
print(f"CONDA_PREFIX  : {os.environ.get('CONDA_PREFIX', '<unset>')}")
print(f"CONDA_DEFAULT : {os.environ.get('CONDA_DEFAULT_ENV', '<unset>')}")

run("[1] bare import, without the application shim:", BARE)
run("[2] via the application path (ast_src.streaming_ASR installs the shim):", VIA_APP)
