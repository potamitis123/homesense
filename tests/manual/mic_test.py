#!/usr/bin/env python3
"""
mic_test.py  –  Records 5 s from device 1 (Realtek) and saves to mic_test.wav
Open mic_test.wav in any audio player to hear what the BOX3 is receiving.
"""
import io
import sounddevice as sd
import numpy as np
from pydub import AudioSegment

SAMPLE_RATE  = 16000
CHANNELS     = 1
SAMPLE_WIDTH = 2
DEVICE       = 1          # change if needed
DURATION     = 5.0

print(f"Recording {DURATION}s from device {DEVICE}…  SPEAK NOW")
sd.default.device = (DEVICE, sd.default.device[1])

pcm = sd.rec(int(SAMPLE_RATE * DURATION), samplerate=SAMPLE_RATE,
             channels=CHANNELS, dtype="int16", blocking=True)

flat = pcm[:, 0] if pcm.ndim == 2 else pcm.flatten()
rms  = int(np.sqrt(np.mean(flat.astype(np.float32) ** 2)))
print(f"RMS = {rms}  (voice should be 2000+, silence is < 200)")

audio = AudioSegment(
    data=flat.tobytes(),
    sample_width=SAMPLE_WIDTH,
    frame_rate=SAMPLE_RATE,
    channels=CHANNELS,
)
out = "mic_test.wav"
audio.export(out, format="wav")
print(f"Saved → {out}   open it and listen")
