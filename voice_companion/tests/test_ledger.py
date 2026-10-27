"""The agent leads, and it leads with something intimate.

Every rejected question below was said on a live call to a man with
stage-2 dementia. Every one was answered with "not really", "I don't
know", or silence -- which is the evidence for what this module assumes:
an open question does not merely go unanswered, it never starts the
search at all. Nothing in it catches on anything.

The accepted questions are the same act done properly. A name lands
first and drags something up by itself; after that an open tail is
welcome, and wanted -- "do you remember your sister Ann, or any
favourite memory of those days?" is the shape being aimed for, not a
compromise with it.
"""

import re

import pytest

from app.graph.nodes import _OPEN_FIELD_RE
from app.services.ledger import (
    Anchor, FAMILIAR, INTIMATE, LIGHT, TopicLedger,
)


class _Profile:
    """Enough of a profile to build a ledger from."""
    def __init__(self, lists):
        self.lists = lists
        self.address_as = "Ilyas"


PROFILE = _Profile({
    "close_relatives": [
        "name: Voula | relationship: [wife]",
        "name: Rafael | relationship: [son]",
        "name: Ann | relationship: [sister]",
    ],
    "open_follow_ups": [
        "[He has visits from his cousin Rini]",
        "[He has visits from hairdresser Giorgos]",
    ],
    "conversation_preferences": [
        "[likes talking about his past, his mother, the good old days]",
    ],
    "interests": ["[old Greek music] e.g."],
})

RECENT = "- yesterday: he talked about paddling in Crete as a boy"


@pytest.fixture
def ledger():
    return TopicLedger.build(PROFILE, RECENT)


def _rejected(ledger, text):
    """What the agent applies to its own turn before speaking it."""
    return (bool(_OPEN_FIELD_RE.search(text))
            or ("?" in text and ledger.find_in(text) is None))


# ── the inventory ────────────────────────────────────────────────────

def test_close_family_ranks_highest(ledger):
    top = {a.label for a in ledger.anchors if a.intimacy == INTIMATE}
    assert "your wife Voula" in top
    assert "your sister Ann" in top
    assert "your mother" in top, "a relative named by role, never by name"


def test_home_is_intimate(ledger):
    """For someone who cannot leave it, the house is not scenery."""
    assert any(a.kind == "place" and a.intimacy == INTIMATE
               for a in ledger.anchors)


def test_interests_rank_lowest(ledger):
    music = next(a for a in ledger.anchors if "music" in a.label)
    assert music.intimacy == LIGHT


def test_yesterday_is_worth_leading_with(ledger):
    """Something they raised themselves is already connected."""
    recent = [a for a in ledger.anchors if a.kind == "recent"]
    assert recent and recent[0].intimacy == FAMILIAR


def test_candidates_lead_with_the_closest(ledger):
    assert all(a.intimacy == INTIMATE for a in ledger.candidates(3))


def test_rotation_moves_on(ledger):
    first = ledger.candidates(1)[0]
    ledger.mark_used(f"Have you heard from {first.label} lately?")
    assert first.key not in {a.key for a in ledger.candidates(3)}


def test_rotation_comes_round_rather_than_stalling():
    """A long call must not run out of things to say."""
    led = TopicLedger(anchors=[Anchor("your wife Voula", "person", INTIMATE)])
    led.mark_used("Voula")
    assert led.candidates(), "an exhausted ledger still offers something"


# ── the check applied to each generated turn ─────────────────────────

REJECT = [
    # Handing back the lead.
    "Is there anything else on your mind today?",
    "Is there anything specific you'd like to talk about today?",
    "Is there anything else you'd like to share or ask about?",
    "If there's anything specific you'd like to talk about, feel free "
    "to let me know.",
    "What would you like to talk about today?",
    # Asking for a memory with no thread attached.
    "Do you have a favorite childhood memory you enjoy thinking about?",
    "Is there a favorite memory with your friends you'd like to share?",
    "Do you remember any particular festival that stood out to you?",
    "Do you remember any particular story or image that stood out to you?",
    "What do you recall enjoying about those stories?",
    "Is there a particular book or story you remember fondly?",
]

ACCEPT = [
    # A name, then a door left open -- the shape being aimed for.
    "Do you remember your sister Ann, or any favourite memory of those days?",
    "Has your cousin Rini been to see you this week?",
    "Do you remember your mother's cooking?",
    "Was it Giorgos who came to cut your hair?",
    "You mentioned paddling in Crete. Was it the sea or a river?",
    # A remark rather than a question is always allowed.
    "Voula would have liked this weather.",
]


@pytest.mark.parametrize("turn", REJECT)
def test_turns_that_must_be_sent_back(ledger, turn):
    assert _rejected(ledger, turn)


@pytest.mark.parametrize("turn", ACCEPT)
def test_turns_that_must_be_spoken(ledger, turn):
    assert not _rejected(ledger, turn)


def test_open_tail_survives_once_a_name_has_landed(ledger):
    """The open half is the point, not the problem.

    An earlier version rejected this sentence -- the exact shape it
    exists to produce -- because it matched on "favourite memory".
    """
    turn = ("Do you remember your sister Ann -- or any favourite memory "
            "of those days?")
    assert not _rejected(ledger, turn)
    assert ledger.find_in(turn).label == "your sister Ann"


def test_a_name_not_in_the_notes_is_not_an_anchor(ledger):
    """The check is only as good as the inventory, and should be.

    "Do you remember your friend George?" reads perfectly, but nobody
    called George is written down anywhere, so the agent would be
    inventing him. Add the friends to the profile and this passes.
    """
    assert _rejected(ledger, "Do you remember your friend George?")


def test_one_ordinary_shared_word_is_not_a_match(ledger):
    """Diary anchors are sentences and share common words with anything."""
    assert ledger.find_in("What do you recall enjoying about those?") is None
