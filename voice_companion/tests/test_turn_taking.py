"""Turn taking: whose fault a quiet moment is.

The device uploads on its own sound trigger, and the loudest sound in
the room is the agent's own loudspeaker. So some bursts in every turn
are the agent's voice off the walls, or the codec's power-down click,
and the transcriber rejects them. What the graph does next is the whole
difference between a conversation and three "can you hear me" prompts
delivered to someone sitting there waiting to answer.
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
    """Drive one coroutine to completion.

    The nodes are async; the rest of this suite is not, and the env has
    no pytest-asyncio. One helper is cheaper than a dependency.
    """
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────

class FakeSTT:
    """Returns a scripted verdict per call."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = 0

    async def transcribe(self, audio):
        from app.services.stt import TranscriptionResult
        self.calls += 1
        v = self.verdicts.pop(0) if self.verdicts else None
        if v is None:
            return TranscriptionResult.empty("no_speech", 0.5)
        return TranscriptionResult(text=v, confidence=0.9,
                                   duration_seconds=2.0)


def make_nodes(stt=None, listen_timeout=6.0, attempts=4):
    from app.config import get_settings
    from app.graph.nodes import Nodes
    from app.profile import load_profile

    settings = get_settings()
    settings.listen_timeout_seconds = listen_timeout
    settings.listen_attempts_per_turn = attempts
    return Nodes(audio=None, stt=stt or FakeSTT([]), tts=None, llm=None,
                 profile=load_profile(settings.person_file),
                 settings=settings)


def listening_state(**over):
    from app.graph.state import Phase
    base = {"phase": Phase.LISTENING.value, "started_at": time.time(),
            "consecutive_silences": 0, "listen_attempts": 1,
            "listen_deadline": time.time() + 5.0}
    base.update(over)
    return base


# ── routing ──────────────────────────────────────────────────────────

def test_rejected_burst_with_time_left_listens_again():
    from app.graph.routing import after_listen_attempt
    assert after_listen_attempt(
        {"last_human_text": "", "listen_deadline": time.time() + 3}
    ) == "listen_again"


def test_real_speech_proceeds():
    from app.graph.routing import after_listen_attempt
    assert after_listen_attempt(
        {"last_human_text": "I am fine", "listen_deadline": time.time() + 3}
    ) == "proceed"


def test_spent_window_proceeds():
    from app.graph.routing import after_listen_attempt
    assert after_listen_attempt(
        {"last_human_text": "", "listen_deadline": None}) == "proceed"


def test_error_never_loops():
    """A capture failure must reach close_session, not spin."""
    from app.graph.routing import after_listen_attempt
    assert after_listen_attempt(
        {"last_human_text": "", "listen_deadline": time.time() + 3,
         "error": "boom"}) == "proceed"


# ── the echo must not be scored against the person ───────────────────

def test_echo_does_not_count_as_their_silence():
    nodes = make_nodes(FakeSTT([None]))
    nodes._pending_audio = b"\x00\x00" * 8000
    out = run(nodes.transcribe_input(listening_state()))

    assert out["last_human_text"] == ""
    # The turn stays open ...
    assert out["listen_deadline"] is not None
    # ... and nothing is held against them.
    assert "consecutive_silences" not in out
    assert "should_finish" not in out


def test_silence_still_counts_when_nothing_was_captured():
    """Distinct from the echo case: the device sent nothing at all."""
    nodes = make_nodes()
    nodes._pending_audio = b""
    out = run(nodes.transcribe_input(listening_state()))

    assert out["consecutive_silences"] == 1
    assert out["listen_deadline"] is None


def test_echo_counts_once_the_window_is_spent():
    nodes = make_nodes(FakeSTT([None]))
    nodes._pending_audio = b"\x00\x00" * 8000
    out = run(nodes.transcribe_input(
        listening_state(listen_deadline=time.time() - 0.01)))

    assert out["consecutive_silences"] == 1
    assert out["listen_deadline"] is None


def test_attempts_are_bounded():
    """A device emitting bursts faster than the clock cannot loop forever."""
    nodes = make_nodes(FakeSTT([None]), attempts=2)
    nodes._pending_audio = b"\x00\x00" * 8000
    out = run(nodes.transcribe_input(
        listening_state(listen_attempts=2,
                        listen_deadline=time.time() + 60)))

    assert out["listen_deadline"] is None
    assert out["consecutive_silences"] == 1


def test_speech_clears_the_turn_state():
    nodes = make_nodes(FakeSTT(["I can hear you"]))
    nodes._pending_audio = b"\x00\x00" * 8000
    out = run(nodes.transcribe_input(
        listening_state(consecutive_silences=2)))

    assert out["last_human_text"] == "I can hear you"
    assert out["consecutive_silences"] == 0
    assert out["listen_deadline"] is None
    assert out["listen_attempts"] == 0


# ── the deadline is the person's, not the machine's ──────────────────

def test_slow_transcription_cannot_stretch_the_turn():
    """A 30 s transcription may not buy 30 s more listening.

    The uncapped version of this turned one contended-GPU transcription
    into a turn approaching a minute long, which reads as a hang.
    """
    class GlacialSTT(FakeSTT):
        async def transcribe(self, audio):
            time.sleep(0.3)
            return await super().transcribe(audio)

    nodes = make_nodes(GlacialSTT([None]))
    nodes.settings.listen_extension_cap_seconds = 0.1
    nodes._pending_audio = b"\x00\x00" * 8000
    before = time.time() + 5.0
    out = run(nodes.transcribe_input(listening_state(listen_deadline=before)))

    assert out["listen_deadline"] <= before + 0.1 + 0.01


def test_transcriber_time_is_given_back():
    """Whisper taking two seconds must not cost them two seconds."""
    class SlowSTT(FakeSTT):
        async def transcribe(self, audio):
            time.sleep(0.15)
            return await super().transcribe(audio)

    nodes = make_nodes(SlowSTT([None]))
    nodes._pending_audio = b"\x00\x00" * 8000
    before = time.time() + 5.0
    out = run(nodes.transcribe_input(listening_state(listen_deadline=before)))

    assert out["listen_deadline"] > before


def test_relisten_keeps_the_original_deadline():
    """A re-listen may not hand them a fresh window, nor a shorter one."""
    class Recorder:
        def __init__(self): self.timeouts = []
        async def receive_utterance(self, timeout):
            self.timeouts.append(timeout)
            return b""

    audio = Recorder()
    nodes = make_nodes()
    nodes.audio = audio
    deadline = time.time() + 2.0
    out = run(nodes.receive_human_input(
        listening_state(listen_deadline=deadline, listen_attempts=1)))

    assert out["listen_deadline"] == pytest.approx(deadline, abs=0.05)
    assert out["listen_attempts"] == 2
    # The turn's own deadline is preserved, but the window handed to the
    # transport never drops below the floor: two seconds is not a chance
    # to answer, and being given one is what teaches somebody that this
    # thing cuts them off.
    assert audio.timeouts[0] >= nodes.settings.min_listen_seconds


def test_a_fresh_turn_opens_a_full_window():
    class Recorder:
        def __init__(self): self.timeouts = []
        async def receive_utterance(self, timeout):
            self.timeouts.append(timeout)
            return b""

    audio = Recorder()
    nodes = make_nodes(listen_timeout=6.0)
    nodes.audio = audio
    run(nodes.receive_human_input(
        listening_state(listen_deadline=None, listen_attempts=0)))

    assert audio.timeouts[0] == pytest.approx(6.0, abs=0.05)


# ── the agent hearing itself ─────────────────────────────────────────

# Every pair below is transcribed verbatim from the p4-door-1 call of
# 2026-08-22, where the loudspeaker's output reached the microphone and
# four consecutive turns were spent answering the agent's own voice.
REAL_ECHOES = [
    ("mind today", "I'm right here, Ilyas. Is there something on your mind today?"),
    ("in the kitchen", "Do you have something in mind to eat, or just going to "
                       "see what's in the kitchen?"),
    ("me know", "If you need a hand with anything, feel free to let me know."),
    ("that later", "feel free to let me know if you'd like to chat later."),
    ("talk later", "Sounds good, Ilyas. Take care, and we'll talk later."),
]


@pytest.mark.parametrize("heard,spoken", REAL_ECHOES)
def test_real_echoes_are_recognised(heard, spoken):
    from app.graph.nodes import sounds_like_the_agent
    assert sounds_like_the_agent(heard, spoken, 0.72)


def test_a_genuine_answer_is_not_an_echo():
    from app.graph.nodes import sounds_like_the_agent
    for reply in ("I'm a bit busy and I'm a bit hungry",
                  "yes please", "no not today",
                  "my niece came round with some soup"):
        assert not sounds_like_the_agent(
            reply, "Is there something on your mind today?", 0.72), reply


def test_a_long_utterance_is_never_an_echo():
    """Above a few words it is the person's own sentence, whatever it
    happens to share with the agent's."""
    from app.graph.nodes import sounds_like_the_agent
    agent = "Is there something on your mind today?"
    assert not sounds_like_the_agent(
        "is there something on your mind today I was wondering that myself "
        "actually", agent, 0.72)


def test_echo_check_needs_something_to_compare():
    from app.graph.nodes import sounds_like_the_agent
    assert not sounds_like_the_agent("", "anything", 0.72)
    assert not sounds_like_the_agent("anything", "", 0.72)


def test_echo_does_not_spend_the_turn(monkeypatch):
    """Recognising an echo must leave the person's window open, exactly
    as any other not-speech burst does."""
    nodes = make_nodes(FakeSTT(["mind today"]))
    nodes.audio = type("A", (), {"seconds_since_agent_spoke": 0.5})()
    nodes._pending_audio = b"\x00\x00" * 8000
    out = run(nodes.transcribe_input(
        listening_state(last_agent_text="Is there something on your mind today?")))

    assert out["last_human_text"] == ""
    assert out["listen_deadline"] is not None
    assert "consecutive_silences" not in out


def test_the_same_words_much_later_are_the_person():
    """Outside the window a repeat is ordinary conversation."""
    nodes = make_nodes(FakeSTT(["in the kitchen"]))
    nodes.audio = type("A", (), {"seconds_since_agent_spoke": 30.0})()
    nodes._pending_audio = b"\x00\x00" * 8000
    out = run(nodes.transcribe_input(
        listening_state(last_agent_text="what's in the kitchen?")))

    assert out["last_human_text"] == "in the kitchen"


def test_a_transport_without_timing_never_false_positives():
    """The MQTT adapter exposes no capture timing; it must be unaffected."""
    nodes = make_nodes(FakeSTT(["mind today"]))
    nodes.audio = object()
    nodes._pending_audio = b"\x00\x00" * 8000
    out = run(nodes.transcribe_input(
        listening_state(last_agent_text="something on your mind today?")))

    assert out["last_human_text"] == "mind today"


# ── the floor belongs to the person ──────────────────────────────────

def test_every_relisten_still_offers_the_floor():
    """However much of the turn has gone on rejecting bursts that were
    not speech, the next attempt is still a real opportunity."""
    class Recorder:
        def __init__(self): self.timeouts = []
        async def receive_utterance(self, timeout):
            self.timeouts.append(timeout)
            return b""

    audio = Recorder()
    nodes = make_nodes()
    nodes.audio = audio
    # A deadline that expired a moment ago -- the worst case.
    run(nodes.receive_human_input(
        listening_state(listen_deadline=time.time() - 0.5, listen_attempts=3)))
    assert audio.timeouts[0] >= nodes.settings.min_listen_seconds


def test_a_pause_mid_sentence_does_not_end_the_turn():
    """Measured: "I woke up in the morning" was taken as a finished
    answer while the person was still drawing breath."""
    from app.config import get_settings
    s = get_settings()
    assert s.vad_silence_ms >= 1200, (
        "700 ms ends a turn between clauses for an older speaker")


def test_a_fifteen_second_answer_is_within_the_cap():
    """Fifteen seconds of continuous speech in one turn is ordinary."""
    from app.config import get_settings
    s = get_settings()
    assert s.max_utterance_seconds >= 15.0
