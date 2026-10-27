"""Pushes the spoken acknowledgement to a sentinel's loudspeaker.

Reuses the existing production Push-to-Talk MQTT transport
(ast_src.audio_transport) exactly as the PTT button does -- no new
transport code, just decode-and-send. Verified working end-to-end against
a real device (box3-1) before this feature was wired up.
"""

import os
import time

from pydub import AudioSegment

from ast_src.audio_transport import AudioSessionManager
from ast_src.audio_transport.base import (
    AudioCodec,
    AudioFrame,
    AudioSessionConfig,
    SessionMode,
    TransportDirection,
)
from ast_src.audio_transport.constants import MQTT_CHUNK_SIZE, INTER_CHUNK_DELAY_S

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _default_wav():
    """Kokora_stop.wav if it has been recorded, else the older Talos one.

    The wake word changed to "Kokora" but the acknowledgement clip is a
    recording, not text -- it still says "Talos" until someone records a
    replacement. Preferring the new name means dropping Kokora_stop.wav into
    the project root is the whole migration; until then the sentinel keeps
    acknowledging with the old clip rather than falling silent.
    """
    for name in ('Kokora_stop.wav', 'Talos_stop.wav'):
        path = os.path.join(_PROJECT_ROOT, name)
        if os.path.exists(path):
            return path
    return os.path.join(_PROJECT_ROOT, 'Kokora_stop.wav')


DEFAULT_WAV_PATH = _default_wav()


def announce(device_id, wav_path=DEFAULT_WAV_PATH):
    """Push wav_path to device_id's loudspeaker. Returns True if sent."""
    if not os.path.exists(wav_path):
        print(f"[Voice_commands.broadcaster] {wav_path} not found -- skipping announce")
        return False

    try:
        audio = AudioSegment.from_file(wav_path)
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        pcm_bytes = audio.raw_data

        mgr = AudioSessionManager.get_instance()
        transport = mgr.get_transport("mqtt_ptt")

        config = AudioSessionConfig(
            device_ids=[device_id],
            direction=TransportDirection.TX,
            mode=SessionMode.PTT,
            codec=AudioCodec.PCM_WAV,
        )
        session_id = mgr.start_session("mqtt_ptt", config)
        frame = AudioFrame(
            pcm=pcm_bytes, is_last=True, codec=AudioCodec.PCM_RAW,
            metadata={"source": "kokora_stop_announce"},
        )
        ok = transport.send_audio_frame(frame, session_id)

        # Publish is async (background sender thread); wait long enough for
        # it to finish before releasing the session.
        num_chunks = (len(pcm_bytes) + MQTT_CHUNK_SIZE - 1) // MQTT_CHUNK_SIZE
        time.sleep(1.0 + num_chunks * INTER_CHUNK_DELAY_S + 1.5)
        mgr.stop_session(session_id)

        print(f"[Voice_commands.broadcaster] announced {wav_path} -> {device_id}: {ok}")
        return ok
    except Exception as exc:
        print(f"[Voice_commands.broadcaster] announce to {device_id} failed: {exc}")
        return False
