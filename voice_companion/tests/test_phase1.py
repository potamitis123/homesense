"""Phase 1 tests.

Covers the decisions that must not silently regress: honouring a request
to stop, duration routing, the state machine's illegal transitions,
hallucination filtering, and placeholder suppression in the profile.

No network, no models, no device.

    run_tests.bat
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.graph import routing                                   # noqa: E402
from app.graph.nodes import _asked_to_stop                      # noqa: E402
from app.graph.state import (                                   # noqa: E402
    ALLOWED, IllegalTransition, Phase, transition,
)
from app.profile import load_profile, profile_to_prompt_block   # noqa: E402
from app.services.stt import looks_hallucinated                 # noqa: E402


# ── explicit stop ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Goodbye for now.", "I want to stop", "That's enough for today",
    "I'm tired, let's talk tomorrow", "no more please", "GOODBYE",
])
def test_stop_requests_are_honoured(text):
    assert _asked_to_stop(text)


@pytest.mark.parametrize("text", [
    # The case that matters: recounting a farewell is not making one.
    # Hanging up on someone describing their day would be exactly the kind
    # of small betrayal that stops people trusting the thing.
    "I said goodbye to my neighbour this morning",
    "She told me goodbye and then drove off to the shops",
    "The garden is looking good", "Yes, I slept well", "",
])
def test_ordinary_speech_is_not_a_stop_request(text):
    assert not _asked_to_stop(text)


@pytest.mark.parametrize("text", [
    "That was lovely. Goodbye.",   # farewell ending a longer turn
    "Bye",                         # bare farewell
])
def test_genuine_farewells_still_stop(text):
    assert _asked_to_stop(text)


# ── duration and routing ─────────────────────────────────────────────

def test_routing_closes_when_finish_flagged():
    assert routing.after_turn({"should_finish": True}) == "close"
    assert routing.after_turn({"error": "boom"}) == "close"


def test_routing_continues_by_default():
    assert routing.after_turn({"should_finish": False}) == "listen"


def test_routing_closes_after_repeated_silence():
    state = {"consecutive_silences": routing.MAX_CONSECUTIVE_SILENCES}
    assert routing.after_transcription(state) == "close"


def test_routing_answers_while_silences_are_tolerable():
    assert routing.after_transcription({"consecutive_silences": 1}) == "respond"


# ── state machine ────────────────────────────────────────────────────

def test_legal_transition():
    assert transition(Phase.IDLE.value, Phase.STARTING) == "STARTING"


def test_illegal_transition_raises():
    # Speaking straight from listening would mean talking over someone.
    with pytest.raises(IllegalTransition):
        transition(Phase.LISTENING.value, Phase.SPEAKING)


def test_finished_is_terminal():
    assert ALLOWED[Phase.FINISHED] == set()
    with pytest.raises(IllegalTransition):
        transition(Phase.FINISHED.value, Phase.LISTENING)


def test_barge_in_path_exists():
    # Phase 1 never takes it, but the edge must be there for Phase 2.
    assert transition(Phase.SPEAKING.value, Phase.INTERRUPTED) == "INTERRUPTED"
    assert transition(Phase.INTERRUPTED.value, Phase.LISTENING) == "LISTENING"


def test_cannot_speak_twice_without_listening():
    with pytest.raises(IllegalTransition):
        transition(Phase.SPEAKING.value, Phase.SPEAKING)


# ── hallucination filtering ──────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Υπότιτλοι AUTHORWAVE",          # seen on real silent sentinel clips
    "Subtitles by the Amara.org community",
    "Thanks for watching!",
    "   ", "",
])
def test_known_hallucinations_are_rejected(text):
    assert looks_hallucinated(text)


@pytest.mark.parametrize("text", [
    "I slept well, thank you.",
    "The roses need water again.",
    "Ευχαριστώ, είμαι καλά.",
])
def test_real_speech_survives(text):
    assert not looks_hallucinated(text)


# ── profile ──────────────────────────────────────────────────────────

def test_placeholders_never_reach_the_prompt(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text(
        "person_id: person_test\n"
        "name: Test\n"
        "age: 70\n"
        "interests:\n"
        "  - real gardening\n"
        "  - [PLACEHOLDER] e.g. something invented\n"
        "health_context:\n"
        "  - [PLACEHOLDER] invented condition\n",
        encoding="utf-8")
    profile = load_profile(f)
    assert profile.get("interests") == ["real gardening"]
    assert profile.get("health_context") == []

    block = profile_to_prompt_block(profile)
    assert "PLACEHOLDER" not in block
    assert "invented" not in block
    assert "real gardening" in block


def test_shipped_profile_has_no_placeholders_in_prompt():
    from app.config import get_settings
    profile = load_profile(get_settings().person_file)
    assert "PLACEHOLDER" not in profile_to_prompt_block(profile)
