"""Things said early in a call that must still matter later.

Both cases here were observed live, in the same conversation.

The first: "my legs won't hold me up" matched no safety pattern at all.
It contains no pain, no hurt, no fall -- it is a plain description of
being unable to stand, in the words somebody would actually use, and the
whole health list walked past it.

The second: four minutes and six turns after saying it, the same person
asked "or maybe go out for a walk?" and was told to make sure he felt
safe and comfortable, and that perhaps someone could accompany him. A
qualified yes, to a man who could not stand up. The rule against
encouraging exercise was intact and had been followed correctly earlier
in the same call; the disclosure had simply gone stale in the
transcript.
"""

import pytest

from app.graph.nodes import Nodes
from app.services.safety import check_rules


# Phrasings for not being able to bear weight. None of these say pain,
# hurt, fall or weak, which is exactly why they were missed.
MOBILITY = [
    "My legs won't hold me up.",
    "my legs will not hold me",
    "my knees give way when I stand",
    "they won't carry me any more",
    "I can't stand for long",
    "I can't manage the stairs",
    "I've gone all wobbly",
    "I am a bit unsteady today",
    "my legs are weak",
    "I'm short of breath",
]


@pytest.mark.parametrize("utterance", MOBILITY)
def test_mobility_is_flagged(utterance):
    assert check_rules(utterance).any_hit, (
        f"not flagged, so nothing carries it forward: {utterance!r}")


@pytest.mark.parametrize("utterance", [
    "the dog won't hold still",
    "the gate won't stay up",
    "I feel fine today, the sun is out",
    "she can't stand that programme",
    "I can't stand the noise from next door",
])
def test_ordinary_talk_is_not_flagged(utterance):
    """The patterns are loose, but not so loose they flag the weather."""
    assert not check_rules(utterance).any_hit, f"false positive: {utterance!r}"


def _nodes_with(*utterances):
    n = Nodes.__new__(Nodes)
    n._rule_hits = []
    for u in utterances:
        n._rule_hits.extend(check_rules(u).hits)
    return n


def test_no_block_before_anything_is_flagged():
    assert _nodes_with("good morning, the sun is out")._concerns_block() == ""


def test_concern_is_carried_in_their_own_words():
    block = _nodes_with("My legs won't hold me up.")._concerns_block()
    assert "hold me up" in block, "a summary does not tell the model what to do"
    assert "do not encourage" in block


def test_requests_are_not_carried_forward():
    """Groceries get relayed once. They are not a standing worry."""
    block = _nodes_with("I think I need some groceries. Can you arrange "
                        "that?")._concerns_block()
    assert block == ""


def test_repeated_concern_appears_once():
    block = _nodes_with("My legs won't hold me up.",
                        "My legs won't hold me up.")._concerns_block()
    assert block.count("hold me up") == 1


def test_block_stays_short():
    """A long list stops being read, and this goes into every turn."""
    n = _nodes_with(*[f"my legs are weak and it hurts, day {i}"
                      for i in range(20)])
    assert len(n._concerns_block().splitlines()) < 20
