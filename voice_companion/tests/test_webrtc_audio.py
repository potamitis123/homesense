"""The two pieces of the WebRTC transport that can be checked without a
device: the SDP rewrite that decides whether the device answers at all,
and the endpointer that decides where a turn ends.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.webrtc_sentinel import (          # noqa: E402
    FRAME_MS, Endpointer, _force_pcma,
)

RATE = 16000
FRAME = RATE * FRAME_MS // 1000          # samples per 20 ms frame


def _frame(amplitude: int) -> bytes:
    if amplitude == 0:
        return bytes(FRAME * 2)
    rng = np.random.default_rng(0)
    a = rng.normal(0, amplitude, FRAME).clip(-32000, 32000)
    return a.astype("<i2").tobytes()


def _real_offer_sdp() -> str:
    """An offer from aiortc itself, not a handwritten fixture."""
    import asyncio

    from aiortc import RTCPeerConnection

    async def make():
        pc = RTCPeerConnection()
        pc.addTransceiver("audio", direction="sendrecv")
        offer = await pc.createOffer()
        await pc.close()
        return offer.sdp
    return asyncio.run(make())


# ── SDP ──────────────────────────────────────────────────────────────

def test_offer_is_reduced_to_pcma_alone():
    """esp_peer cannot answer an offer carrying codecs it lacks."""
    sdp = _real_offer_sdp()
    assert "opus" in sdp.lower(), "precondition: aiortc offers opus by default"

    out = _force_pcma(sdp)
    audio = [l for l in out.replace("\r\n", "\n").split("\n")
             if l.startswith("m=audio ")][0]
    payloads = audio.split(" ")[3:]
    assert len(payloads) == 1, f"expected one payload type, got {payloads}"

    rtpmaps = [l for l in out.replace("\r\n", "\n").split("\n")
               if l.startswith("a=rtpmap:")]
    assert len(rtpmaps) == 1
    assert "pcma" in rtpmaps[0].lower()
    assert payloads[0] == rtpmaps[0][len("a=rtpmap:"):].split(" ")[0]


def test_attributes_for_dropped_codecs_are_removed():
    sdp = _force_pcma(_real_offer_sdp()).replace("\r\n", "\n")
    assert "opus" not in sdp.lower()
    assert "telephone-event" not in sdp.lower()


def test_session_level_lines_survive():
    """Stripping codecs must not strip the connection or ICE lines."""
    sdp = _force_pcma(_real_offer_sdp()).replace("\r\n", "\n")
    for needed in ("v=0", "a=ice-ufrag:", "a=fingerprint:", "m=audio "):
        assert any(l.startswith(needed) for l in sdp.split("\n")), needed


def test_sdp_without_pcma_is_left_alone():
    sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\n"
    assert _force_pcma(sdp) == sdp


def test_sdp_without_audio_is_left_alone():
    sdp = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    assert _force_pcma(sdp) == sdp


# ── endpointing ──────────────────────────────────────────────────────

def _endpointer(**over):
    kw = dict(silence_ms=700, threshold=0.5, min_utterance_seconds=0.4,
              max_utterance_seconds=45.0, sample_rate=RATE)
    kw.update(over)
    return Endpointer(**kw)


def test_silence_produces_nothing():
    ep = _endpointer()
    assert all(ep.feed(_frame(0)) is None for _ in range(100))


def test_speech_then_silence_produces_one_utterance():
    ep = _endpointer()
    for _ in range(50):                      # settle the noise floor
        ep.feed(_frame(0))
    out = None
    for _ in range(50):                      # 1 s of speech
        assert ep.feed(_frame(4000)) is None
    for _ in range(40):                      # silence closes it
        out = ep.feed(_frame(0)) or out
    assert out is not None
    assert 1.0 <= len(out) / (RATE * 2) <= 2.0


def test_utterance_shorter_than_the_minimum_is_dropped():
    """A cough is not a turn."""
    ep = _endpointer(min_utterance_seconds=1.0)
    for _ in range(50):
        ep.feed(_frame(0))
    for _ in range(5):                       # 100 ms
        ep.feed(_frame(4000))
    out = None
    for _ in range(40):
        out = ep.feed(_frame(0)) or out
    assert out is None


def test_silence_inside_a_sentence_does_not_end_it():
    """Older speakers pause mid-thought; that is not the end of a turn."""
    ep = _endpointer(silence_ms=700)
    for _ in range(50):
        ep.feed(_frame(0))
    for _ in range(25):
        ep.feed(_frame(4000))
    for _ in range(10):                      # 200 ms pause, under the 700
        assert ep.feed(_frame(0)) is None
    for _ in range(25):
        assert ep.feed(_frame(4000)) is None


def test_a_very_long_utterance_is_capped():
    ep = _endpointer(max_utterance_seconds=1.0)
    for _ in range(50):
        ep.feed(_frame(0))
    out = None
    for _ in range(200):
        out = ep.feed(_frame(4000)) or out
        if out:
            break
    assert out is not None
    assert len(out) / (RATE * 2) <= 1.1


def test_reset_discards_a_partial_utterance():
    """Used when the agent starts speaking: whatever was mid-capture is
    ours, not theirs."""
    ep = _endpointer()
    for _ in range(50):
        ep.feed(_frame(0))
    for _ in range(25):
        ep.feed(_frame(4000))
    ep.reset()
    for _ in range(40):
        assert ep.feed(_frame(0)) is None


def test_loud_rooms_do_not_trigger_forever():
    """The floor adapts, so constant hum stops counting as speech."""
    ep = _endpointer()
    for _ in range(200):
        ep.feed(_frame(600))                 # steady hum
    assert all(ep.feed(_frame(600)) is None for _ in range(50))


# ── the outbound track ───────────────────────────────────────────────

def _drain(track, frames=400):
    """Pull frames until the track is empty, or give up."""
    import asyncio

    async def run():
        for _ in range(frames):
            await track.recv()
            if track.pending_seconds() == 0.0:
                return True
        return False
    return asyncio.run(run())


def test_a_partial_final_frame_still_leaves_the_track():
    """An utterance rarely divides evenly into 20 ms frames.

    Stranding the remainder means pending_seconds() never reaches zero,
    which is what hung the first real call to a P4: send_audio waited
    for a drain that could not finish.
    """
    from app.services.webrtc_sentinel import WIRE_FRAME_BYTES, _build_agent_track

    track = _build_agent_track()
    track.queue(b"" * ((WIRE_FRAME_BYTES // 2) * 2 + 17))
    assert _drain(track), "the track never emptied"
    assert track.pending_seconds() == 0.0


def test_an_exact_number_of_frames_also_drains():
    from app.services.webrtc_sentinel import WIRE_FRAME_BYTES, _build_agent_track

    track = _build_agent_track()
    track.queue(bytes(WIRE_FRAME_BYTES * 3))
    assert _drain(track)
    assert track.pending_seconds() == 0.0


def test_an_idle_track_keeps_producing_silence():
    """A track that stops yielding stalls the connection."""
    import asyncio

    from app.services.webrtc_sentinel import WIRE_FRAME_SAMPLES, _build_agent_track

    track = _build_agent_track()

    async def run():
        return [await track.recv() for _ in range(3)]
    frames = asyncio.run(run())
    assert [f.samples for f in frames] == [WIRE_FRAME_SAMPLES] * 3
    assert [f.pts for f in frames] == [0, WIRE_FRAME_SAMPLES,
                                       WIRE_FRAME_SAMPLES * 2]
