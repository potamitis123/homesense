"""The household's own standing instructions.

Kept in a text file the household owns, not in the prompt in code, so
whoever is responsible for the person can change what the companion says
about shopping or symptoms without editing Python.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── loading ──────────────────────────────────────────────────────────

def test_comments_never_reach_the_agent(tmp_path):
    from app.house_rules import load_house_rules
    f = tmp_path / "r.txt"
    f.write_text("# a note to the carer\nNever give medical advice.\n"
                 "  # indented comment\nSay it is noted.\n", encoding="utf-8")
    rules = load_house_rules(f)
    assert "carer" not in rules and "indented" not in rules
    assert "Never give medical advice." in rules
    assert "Say it is noted." in rules


def test_a_missing_file_is_not_an_error(tmp_path):
    """A companion that will not start because a rules file is absent is
    worse than one running on its defaults."""
    from app.house_rules import load_house_rules
    assert load_house_rules(tmp_path / "nope.txt") == ""


def test_a_directory_is_not_an_error(tmp_path):
    from app.house_rules import load_house_rules
    assert load_house_rules(tmp_path) == ""


def test_paragraph_breaks_survive(tmp_path):
    """Single blank lines separate one rule from the next."""
    from app.house_rules import load_house_rules
    f = tmp_path / "r.txt"
    f.write_text("MEDICAL\n\nNever advise.\n\n\n\nREQUESTS\n\nSay noted.\n",
                 encoding="utf-8")
    rules = load_house_rules(f)
    assert "\n\n\n" not in rules
    assert rules.startswith("MEDICAL")
    assert rules.endswith("Say noted.")


def test_an_overlong_file_is_truncated_loudly(tmp_path, caplog):
    """Every line costs tokens on every turn, so an over-long file must
    be trimmed -- and the trimming must be noticed, not silent."""
    import logging

    from app.house_rules import MAX_CHARACTERS, load_house_rules
    f = tmp_path / "r.txt"
    f.write_text("\n".join(["a rule that goes on"] * 900), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.house_rules"):
        rules = load_house_rules(f)
    assert len(rules) <= MAX_CHARACTERS
    assert any(r.levelno >= logging.WARNING for r in caplog.records), \
        "truncation happened silently"


# ── the shipped file ─────────────────────────────────────────────────

@pytest.fixture()
def shipped():
    from app.config import get_settings
    from app.house_rules import load_house_rules
    return load_house_rules(get_settings().house_rules_file)


def test_the_shipped_rules_load(shipped):
    assert shipped, "data/house_rules.txt did not load"
    assert not shipped.startswith("#")


@pytest.mark.parametrize("topic", [
    "medical advice", "noted", "guardian", "email", "dementia",
])
def test_the_shipped_rules_cover_what_they_should(shipped, topic):
    assert topic.lower() in shipped.lower(), f"house rules do not mention {topic}"


# ── reaching the prompt ──────────────────────────────────────────────

def test_rules_appear_in_the_system_prompt(shipped):
    from app.prompts.conversation import build_system_prompt
    prompt = build_system_prompt("Ilyas", "(notes)", 300, house_rules=shipped)
    assert "HOUSE RULES" in prompt
    assert "Never give medical advice" in prompt


def test_the_household_wins_a_conflict(shipped):
    """They are introduced as overriding; that is the point of the file."""
    from app.prompts.conversation import build_system_prompt
    prompt = build_system_prompt("Ilyas", "(notes)", 300, house_rules=shipped)
    intro = prompt.split("HOUSE RULES", 1)[1][:200].lower()
    assert "conflicts" in intro or "follow these" in intro


def test_no_rules_leaves_the_prompt_intact():
    from app.prompts.conversation import build_system_prompt
    prompt = build_system_prompt("Ilyas", "(notes)", 300)
    assert "HOUSE RULES" not in prompt
    assert "HOW TO SPEAK" in prompt


# ── the promise the program has to keep ──────────────────────────────

# Every one of these was said to the companion during the live calls of
# 2026-08-21/22. The agent is told to answer them with "it is noted, and
# the guardian will be notified by email" -- so each must actually reach
# the guardian, or the sentence is a lie.
REAL_REQUESTS = [
    "I would like a pizza.",
    "You can arrange it tomorrow with my son to bring it.",
    "Tell my son I need some groceries.",
    "I have run out of milk",
    "I need my prescription collected",
    "there is nothing in the fridge",
    "Can you order me a takeaway?",
]

REAL_CHAT = [
    "It's ok, today I was chasing my cat, but my cat was very fast.",
    "I feel a little bit grumpy",
    "How are you?",
    "Yes, as a matter of fact, I did.",
    "I hear you. Do you hear me?",
]


@pytest.mark.parametrize("said", REAL_REQUESTS)
def test_a_request_reaches_the_guardian(said):
    from app.services.safety import check_rules, should_notify
    assert should_notify(check_rules(said), None), said


@pytest.mark.parametrize("said", REAL_CHAT)
def test_ordinary_chat_does_not_email_anyone(said):
    from app.services.safety import check_rules, should_notify
    assert not should_notify(check_rules(said), None), said


# ── self-harm ────────────────────────────────────────────────────────

# "I would like to take some pills with alcohol" was said to the
# companion on 2026-08-23. It scored only `health`, so the agent replied
# "it's important to be careful" and changed the subject, and nothing
# left the machine until the call had ended. Urgent means the guardian
# is told while the person is still on the line.
SELF_HARM = [
    "I would like to take some pills with alcohol.",
    "I might take all my pills",
    "I don't want to wake up tomorrow",
    "there is nothing to live for",
    "I want to die",
    "I could hurt myself",
    "I would be better off dead",
    "I have no reason to go on",
]

# Ordinary life, which must not summon an emergency.
NOT_SELF_HARM = [
    "I take my pills every morning",
    "I had a glass of wine with dinner",
    "my knee hurts a bit",
    "I would like a pizza",
    "the doctor changed my medication",
    "I did not sleep well",
]


@pytest.mark.parametrize("said", SELF_HARM)
def test_self_harm_is_urgent_not_merely_noted(said):
    from app.services.safety import check_rules, severity_of
    rules = check_rules(said)
    assert rules.urgent, f"{said!r} would have waited for the call to end"
    assert severity_of(rules, None) == "URGENT"


@pytest.mark.parametrize("said", NOT_SELF_HARM)
def test_ordinary_life_is_not_an_emergency(said):
    from app.services.safety import check_rules
    assert not check_rules(said).urgent, said


def test_the_rules_tell_the_agent_not_to_change_the_subject(shipped):
    """The failure was not the detection alone: the agent noted it and
    moved on, because that is what the file told it to do."""
    lowered = shipped.lower()
    assert "harming themselves" in lowered
    assert "change the subject" in lowered
    assert "overrides everything else" in lowered
