"""Letting someone finish, and believing them when they do.

Both behaviours here come from the same complaint after the p4-door-1
calls of 2026-08-22: "sometimes it does not hear me and asks if I am
there", and "it should wait and take all the answer while I talk".
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(coro):
    return asyncio.run(coro)


# ── the window must not expire mid-sentence ──────────────────────────

def _adapter(in_speech_for: float):
    """A WebRTC adapter whose endpointer reports speech for a while."""
    from app.services.sentinel import UtteranceConfig
    from app.services.webrtc_sentinel import WebRtcSentinelAdapter

    a = WebRtcSentinelAdapter.__new__(WebRtcSentinelAdapter)
    a.cfg = UtteranceConfig(max_utterance_seconds=10.0)
    a._t0 = time.monotonic()
    a._spoke_at = 0.0
    a.seconds_since_agent_spoke = None
    a.utterances = asyncio.Queue()

    class Fake:
        def __init__(self): self.until = time.monotonic() + in_speech_for
        @property
        def in_speech(self): return time.monotonic() < self.until
    a._endpointer = Fake()
    return a


def test_a_long_answer_is_not_cut_off():
    """The complaint exactly: a 6 s answer into a 6 s window used to
    arrive just after the window closed, costing a 'can you hear me'."""
    a = _adapter(in_speech_for=0.6)

    async def scenario():
        async def finish_late():
            await asyncio.sleep(0.5)          # still talking past the window
            a.utterances.put_nowait(b"\x00\x00" * 16000)
        asyncio.get_event_loop().create_task(finish_late())
        return await a.receive_utterance(timeout=0.2)

    heard = run(scenario())
    assert heard, "the window closed while they were still speaking"


def test_silence_still_times_out_promptly():
    """Holding the window open must not mean never giving up."""
    a = _adapter(in_speech_for=0.0)
    started = time.monotonic()
    assert run(a.receive_utterance(timeout=0.3)) == b""
    assert time.monotonic() - started < 1.0


def test_a_stuck_detector_cannot_hold_a_turn_forever():
    """If in_speech never goes false, the hard stop must still fire."""
    a = _adapter(in_speech_for=9999.0)
    a.cfg.max_utterance_seconds = 0.2          # hard stop = timeout + 0.2 + 5
    started = time.monotonic()
    assert run(a.receive_utterance(timeout=0.1)) == b""
    assert time.monotonic() - started < 8.0


def test_speech_already_queued_is_returned_at_once():
    a = _adapter(in_speech_for=0.0)
    a.utterances.put_nowait(b"\x00\x00" * 16000)
    assert run(a.receive_utterance(timeout=5.0))


# ── believing a quiet speaker ────────────────────────────────────────

def test_a_long_quiet_sentence_is_kept():
    """Measured live: 'I don't remember I think' scored 0.71 and was
    thrown away. It was real, and the person was not asked again."""
    from app.services.stt import (
        LONG_AUDIO_SECONDS, NO_SPEECH_PROB_MAX, NO_SPEECH_PROB_MAX_LONG,
    )
    assert 0.71 > NO_SPEECH_PROB_MAX, "precondition: strict rule rejected it"
    assert 0.71 < NO_SPEECH_PROB_MAX_LONG
    assert LONG_AUDIO_SECONDS < 2.5


def test_short_bursts_are_still_judged_strictly():
    """Room tone arrives in short bursts; that is where the invented
    subtitle credits come from."""
    from app.services.stt import NO_SPEECH_PROB_MAX
    assert NO_SPEECH_PROB_MAX <= 0.6


# ── decoder loops ────────────────────────────────────────────────────

def test_the_real_looping_transcript_is_caught():
    """Verbatim from the 2026-08-22 call."""
    from app.services.stt import looks_repetitive
    assert looks_repetitive("  ".join(["I'm sorry."] * 29))


def test_a_short_loop_is_caught():
    from app.services.stt import looks_repetitive
    assert looks_repetitive("Thank you. Thank you. Thank you. Thank you.")


def test_ordinary_speech_is_not_a_loop():
    from app.services.stt import looks_repetitive
    for real in ("I'm a little bit hungry and I have not seen my children "
                 "for one month.",
                 "It's ok, today I was chasing my cat, but my cat was very fast.",
                 "Yes. No. Maybe. I am not sure.",
                 "I'm sorry. I did not hear you."):
        assert not looks_repetitive(real), real


def test_repetition_needs_a_short_phrase():
    """A long sentence repeated is odd but not the decoder loop shape."""
    from app.services.stt import looks_repetitive
    long_one = ("the quick brown fox jumped over the lazy dog by the river. "
                * 5)
    assert not looks_repetitive(long_one)
