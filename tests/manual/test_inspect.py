# Run after diarization to inspect the DiarizeOutput object
# Add this temporarily at the end of your diarize() call

import sys, types
import torchaudio as _ta
if not hasattr(_ta, "list_audio_backends"):
    _ta.list_audio_backends = lambda: []
_tc = types.ModuleType("torchcodec")
_tc_dec = types.ModuleType("torchcodec.decoders")
sys.modules.setdefault("torchcodec", _tc)
sys.modules.setdefault("torchcodec.decoders", _tc_dec)

import os, glob, tempfile
from pydub import AudioSegment
from pyannote.audio import Pipeline

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
from misc.secrets import hf_token

AUDIO_DIR = os.path.join(project_root, "audio_examples")
HF_TOKEN  = hf_token()

mp3 = glob.glob(os.path.join(AUDIO_DIR, "*.mp3"))[0]
tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
wav = tmp.name; tmp.close()
audio = AudioSegment.from_mp3(mp3).set_channels(1).set_frame_rate(16000)
audio.export(wav, format="wav")

pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=HF_TOKEN)
result = pipeline(wav)

print("type:", type(result))
print("dir:", [x for x in dir(result) if not x.startswith("__")])