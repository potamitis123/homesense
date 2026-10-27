"""
autopatch.py — fixes pyannote io.py to use librosa instead of torchcodec/torchaudio.
Run once: python autopatch.py
Requires: pip install librosa
"""
import shutil, sys
from pathlib import Path

IO_PATH = Path(r"C:\Users\potam\anaconda3\envs\dash_env\lib\site-packages\pyannote\audio\core\io.py")
BAK     = IO_PATH.with_suffix(".py.bak")

if not IO_PATH.exists():
    sys.exit(f"Not found: {IO_PATH}")

if BAK.exists():
    shutil.copy2(BAK, IO_PATH)
    print("Restored from backup, patching fresh...")
else:
    shutil.copy2(IO_PATH, BAK)
    print("Backup saved.")

content = IO_PATH.read_text(encoding="utf-8")

SHIM = """\
    # ---- torchcodec shim using librosa (no torchcodec/torchaudio backend needed) ----
    import dataclasses as _dc, torch as _torch, librosa as _librosa, soundfile as _sf

    @_dc.dataclass
    class AudioStreamMetadata:
        sample_rate:                  int   = 0
        num_samples:                  int   = 0
        num_channels:                 int   = 0
        duration:                     float = 0.0
        duration_seconds_from_header: float = 0.0
        best_audio_stream_index:      int   = 0
        codec:                        str   = "pcm_s16le"
        bit_rate:                     float = 0.0
        def __getattr__(self, name):
            return 0.0

    @_dc.dataclass
    class AudioSamples:
        data:        object  # torch.Tensor shape (channels, samples)
        sample_rate: int

    def _meta(path):
        info = _sf.info(str(path))
        return info.samplerate, info.channels, info.frames, info.duration

    def _load(path, offset=0.0, duration=None):
        wav, sr = _librosa.load(str(path), sr=None, mono=False,
                                offset=offset, duration=duration)
        if wav.ndim == 1:
            wav = wav[None, :]   # (1, samples)
        return _torch.from_numpy(wav), sr

    class AudioDecoder:
        def __init__(self, path):
            self._path = str(path)

        @property
        def metadata(self):
            sr, nch, nf, dur = _meta(self._path)
            return AudioStreamMetadata(
                sample_rate=sr, num_samples=nf, num_channels=nch,
                duration=dur, duration_seconds_from_header=dur,
                codec="pcm_s16le", bit_rate=float(sr * nch * 16),
            )

        def get_all_samples(self):
            tensor, sr = _load(self._path)
            return AudioSamples(data=tensor, sample_rate=sr)

        def get_samples_played_in_range(self, start_seconds=0.0, stop_seconds=None):
            dur = (stop_seconds - start_seconds) if stop_seconds is not None else None
            tensor, sr = _load(self._path, offset=start_seconds, duration=dur)
            return AudioSamples(data=tensor, sample_rate=sr)

        def __getitem__(self, key):
            return self.get_samples_played_in_range(key.start or 0.0, key.stop)

    class _FakeMod: pass
    torchcodec   = _FakeMod()
    AudioSamples = AudioSamples
    # ---- end shim ----"""

OLD = "    import torchcodec\n    from torchcodec import AudioSamples\n    from torchcodec.decoders import AudioDecoder, AudioStreamMetadata"

if OLD not in content:
    sys.exit("ERROR: could not find torchcodec import block.")

patched = content.replace(OLD, SHIM, 1)
IO_PATH.write_text(patched, encoding="utf-8")
print("Patch applied successfully.")
