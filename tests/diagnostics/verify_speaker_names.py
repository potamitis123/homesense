"""Quick check that enrolled names replace SPEAKER_00 after the speaker_id fix."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from ast_src.streaming_ASR import ASRTranscriber

candidates = [
    os.path.join(ROOT, "assets", "audio_mp3", "20260803_114214_esp32s3-1_rec.wav"),
    os.path.join(ROOT, "assets", "audio_mp3", "20260803_114331_esp32s3-1_rec.wav"),
    os.path.join(ROOT, "assets", "audio_mp3", "20260803_113539_box3-2_rec.wav"),
]
path = next((p for p in candidates if os.path.exists(p)), None)
if not path:
    # fall back to newest wav
    folder = os.path.join(ROOT, "assets", "audio_mp3")
    wavs = sorted(
        (os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".wav")),
        key=os.path.getmtime,
        reverse=True,
    )
    path = wavs[0] if wavs else None

print(f"Testing: {path}")
asr = ASRTranscriber()
text = asr.transcribe(path)
print("--- result ---")
print(text)
print("---")
if "Speaker_" in text:
    print("FAIL: still showing Speaker_N (name mapping did not apply)")
    sys.exit(1)
if any(n in text for n in ("Ilyas", "Voula", "Rafael", "Unknown")):
    print("PASS: named speaker attribution is working")
else:
    print("WARN: no named label and no Speaker_ - empty/short clip?")
