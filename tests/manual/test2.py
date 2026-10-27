import sys, os, glob, tempfile, types
import torchaudio as _ta
from pathlib import Path

if not hasattr(_ta, "list_audio_backends"):
    _ta.list_audio_backends = lambda: []
for _m in ["torchcodec", "torchcodec.decoders", "torchcodec.decoders._core"]:
    sys.modules.setdefault(_m, types.ModuleType(_m))


def find_mp3(directory):
    matches = glob.glob(os.path.join(directory, "*.mp3"))
    if not matches: raise FileNotFoundError(f"No MP3 in {directory}")
    if len(matches) > 1: raise ValueError(f"Multiple MP3s: {matches}")
    return matches[0]


def mp3_to_wav(mp3_path, wav_path):
    from pydub import AudioSegment
    print(f"Converting {mp3_path} ...")
    audio = AudioSegment.from_mp3(mp3_path).set_channels(1).set_frame_rate(16000)
    audio.export(wav_path, format="wav")
    print(f"Done. Duration: {len(audio)/1000:.1f}s")


def load_pipeline(hf_token):
    from pyannote.audio import Pipeline
    print("Loading pipeline ...")
    p = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=hf_token)
    print("Pipeline loaded.")
    return p


def diarize(wav_path, pipeline, num_speakers=None):
    kwargs = {"num_speakers": num_speakers} if num_speakers else {}
    print("Running diarization ...", flush=True)
    result = pipeline(wav_path, **kwargs)
    print("Done.")
    # DiarizeOutput wraps the actual Annotation in .speaker_diarization
    return result.speaker_diarization if hasattr(result, "speaker_diarization") else result


def print_results(annotation, output_rttm=None):
    print("\n── Speaker turns ──────────────────────")
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        sm, ss = divmod(turn.start, 60)
        em, es = divmod(turn.end,   60)
        print(f"{int(sm):02d}:{ss:05.2f} - {int(em):02d}:{es:05.2f}  {speaker}")
    speakers = {s for _, _, s in annotation.itertracks(yield_label=True)}
    print(f"\nSpeakers detected: {len(speakers)}  ({', '.join(sorted(speakers))})")
    if output_rttm:
        with open(output_rttm, "w") as f:
            annotation.write_rttm(f)
        print(f"RTTM saved: {output_rttm}")


def main():
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from misc.secrets import hf_token

    AUDIO_DIR    = os.path.join(project_root, "audio_examples")
    HF_TOKEN     = hf_token()
    NUM_SPEAKERS = None

    mp3_path = find_mp3(AUDIO_DIR)
    print(f"Found: {mp3_path}")

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_path = tmp.name
    tmp.close()

    mp3_to_wav(mp3_path, wav_path)
    pipeline = load_pipeline(HF_TOKEN)
    try:
        diarization = diarize(wav_path, pipeline, num_speakers=NUM_SPEAKERS)
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)

    print_results(diarization, output_rttm=str(Path(mp3_path).with_suffix(".rttm")))


if __name__ == "__main__":
    main()
