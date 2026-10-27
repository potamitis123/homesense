"""Compatibility shims for torchaudio 2.9 against speechbrain 1.0.3.

torchaudio cannot be dropped: pyannote.audio imports it in `core/io.py` and
`pipelines/speaker_verification.py`, and speechbrain imports it in 20 modules.
Diarization and speaker identification both depend on it.

What is stale is speechbrain's use of two APIs that torchaudio 2.1+ removed:

* ``torchaudio.list_audio_backends()`` - speechbrain's ``check_torchaudio_backend()``
  calls it purely to warn when no backend is available, so reporting the
  soundfile backend (which this environment does have) is accurate.
* ``torchaudio.set_audio_backend()`` - only reached on the "old torchaudio"
  branch that this version never takes.

Without the shim, ``import speechbrain`` raises
``AttributeError: module 'torchaudio' has no attribute 'list_audio_backends'``.

Call :func:`apply` before importing speechbrain or pyannote.
"""


def apply():
    """Install the torchaudio shims. Safe to call more than once."""
    import torchaudio

    if not hasattr(torchaudio, "list_audio_backends"):
        torchaudio.list_audio_backends = lambda: ["soundfile"]

    if not hasattr(torchaudio, "set_audio_backend"):
        torchaudio.set_audio_backend = lambda backend: None

    return torchaudio
