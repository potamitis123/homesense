"""Sample-rate conversion between the TTS and the device.

The specification is explicit that TTS output must not be assumed to
match what the BOX3 wants, and it does not: Piper's en_US voices render
at 22050 Hz while the sentinel stream is configured for 16000 Hz mono
16-bit. Sending Piper audio through unconverted plays it slow and pitched
down -- audible immediately, and easy to miss in code review.
"""

from __future__ import annotations

from math import gcd


def resample_pcm16(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """Convert 16-bit mono PCM between sample rates.

    Uses polyphase resampling when SciPy is available (correct filtering,
    no aliasing) and falls back to linear interpolation otherwise, which
    is worse but still far better than misinterpreting the rate.
    """
    if source_rate == target_rate or not pcm:
        return pcm

    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return pcm

    divisor = gcd(int(source_rate), int(target_rate))
    up, down = target_rate // divisor, source_rate // divisor

    try:
        from scipy.signal import resample_poly
        out = resample_poly(samples.astype(np.float32), up, down)
    except Exception:
        length = int(round(samples.size * target_rate / source_rate))
        out = np.interp(
            np.linspace(0, samples.size - 1, length, dtype=np.float64),
            np.arange(samples.size),
            samples.astype(np.float32),
        )

    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()
