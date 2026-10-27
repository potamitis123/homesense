import torchaudio
print("torchaudio version:", torchaudio.__version__)
print("Has info:", hasattr(torchaudio, "info"))
print("Has load:", hasattr(torchaudio, "load"))
print("Has backend:", hasattr(torchaudio, "get_audio_backend"))
print("Has sox_io:", hasattr(torchaudio, "sox_io"))
print()
print("All top-level attributes:")
print([x for x in dir(torchaudio) if not x.startswith("_")])