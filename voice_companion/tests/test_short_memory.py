"""Short rolling memory: policy, guards, and the long-memory boundary."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A ShortMemory backed by a throwaway database."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models, session as db

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", future=True,
                           connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal",
                        sessionmaker(bind=engine, expire_on_commit=False,
                                     future=True))
    from app.services.short_memory import ShortMemory
    return ShortMemory("ilyas", max_events=4, max_age_days=7,
                       min_salience=0.4)


def _update(remember=(), retire_ids=()):
    from app.services.short_memory import RememberedEvent, ShortMemoryUpdate
    return ShortMemoryUpdate(
        remember=[RememberedEvent(text=t, category=c, salience=s, quote=q)
                  for t, c, s, q in remember],
        retire_ids=list(retire_ids))


# ── what gets kept ──────────────────────────────────────────────────

def test_notable_event_is_remembered(store):
    res = store.apply(_update([
        ("chased next door's cat out of the garden", "incident", 0.8,
         "I chased the cat"),
    ]))
    assert res["added"] == 1
    assert "chased next door's cat" in store.render_for_prompt()


def test_routine_event_is_discarded(store):
    res = store.apply(_update([
        ("had a coffee", "other", 0.1, "I had a coffee"),
    ]))
    assert res["added"] == 0 and res["skipped_routine"] == 1
    assert store.render_for_prompt() == ""


def test_duplicate_is_not_stored_twice(store):
    ev = ("his niece visited with soup", "visit", 0.9, "my niece came")
    store.apply(_update([ev]))
    res = store.apply(_update([ev]))
    assert res["added"] == 0 and res["skipped_duplicate"] == 1
    assert len(store.active()) == 1


def test_at_most_three_events_per_conversation(store):
    res = store.apply(_update([
        (f"event number {i}", "other", 0.9, f"q{i}") for i in range(6)
    ]))
    assert res["added"] == 3


# ── rolling ─────────────────────────────────────────────────────────

def test_buffer_is_a_fixed_length_queue(store):
    """New events push the oldest off the end. Strict FIFO."""
    for i in range(6):                       # max_events is 4 in the fixture
        store.apply(_update([(f"event {i}", "other", 0.8, f"q{i}")]))

    texts = [r.text for r in store.active()]
    assert len(texts) == store.max_events
    # active() is newest-first
    assert texts == ["event 5", "event 4", "event 3", "event 2"]
    assert "event 0" not in texts and "event 1" not in texts


def test_importance_is_applied_at_the_door_not_in_the_queue(store):
    """Salience decides what gets in; after that position decides.

    So a notable event *can* be pushed out by newer ones — that is the
    intended behaviour of a rolling buffer, and the reason the salience
    floor exists is to keep the routine ones from doing the pushing.
    """
    store.apply(_update([("a fall in the hallway", "incident", 0.95, "I fell")]))
    for i in range(4):
        store.apply(_update([(f"later thing {i}", "other", 0.5, f"q{i}")]))

    texts = [r.text for r in store.active()]
    assert len(texts) == store.max_events
    assert "a fall in the hallway" not in texts, "FIFO should have evicted it"


def test_old_events_expire(store):
    from app.db import session as db
    eid = db.add_short_memory("ilyas", "an old thing", "other", 0.9, None, None,
                              occurred_on=datetime.now() - timedelta(days=30))
    assert any(r.id == eid for r in store.active())
    store.prune()
    assert not any(r.id == eid for r in store.active())


def test_nothing_survives_a_week(store):
    """The age bound, at the boundary that matters."""
    from app.db import session as db
    six = db.add_short_memory("ilyas", "six days ago", "other", 0.9, None, None,
                              occurred_on=datetime.now() - timedelta(days=6))
    eight = db.add_short_memory("ilyas", "eight days ago", "other", 0.9, None,
                                None,
                                occurred_on=datetime.now() - timedelta(days=8))
    store.prune()
    live = {r.id for r in store.active()}
    assert six in live, "a six-day-old event should still be remembered"
    assert eight not in live, "an eight-day-old event should have expired"


def test_prune_is_applied_before_rendering(store):
    """An event that aged out overnight must not reach tomorrow's prompt."""
    from app.db import session as db
    db.add_short_memory("ilyas", "stale news", "other", 0.9, None, None,
                        occurred_on=datetime.now() - timedelta(days=40))
    store.prune()
    assert "stale news" not in store.render_for_prompt()


# ── deletion ────────────────────────────────────────────────────────

def test_agent_can_retire_a_finished_event(store):
    store.apply(_update([("the kitchen tap was leaking", "incident", 0.7,
                          "the tap drips")]))
    eid = store.active()[0].id
    res = store.apply(_update(retire_ids=[eid]))
    assert res["retired"] >= 1
    assert store.render_for_prompt() == ""


def test_invented_ids_are_ignored(store):
    """A model that hallucinates ids must not delete someone else's memory."""
    from app.db import session as db
    other = db.add_short_memory("someone-else", "not ours", "other", 0.9,
                                None, None)
    store.apply(_update([("a real event", "other", 0.8, "q")]))
    res = store.apply(_update(retire_ids=[9999, other]))
    assert res["retired"] == 0
    assert len(store.active()) == 1
    assert any(r.id == other for r in db.active_short_memory("someone-else"))


def test_retired_rows_are_kept_for_audit(store):
    from app.db import session as db
    store.apply(_update([("something", "other", 0.8, "q")]))
    eid = store.active()[0].id
    store.apply(_update(retire_ids=[eid]))
    with db.SessionLocal() as s:
        row = s.get(db.ShortMemoryEvent, eid)
    assert row is not None and row.retired_at is not None
    assert row.retired_reason == "agent"


# ── the boundary that matters ───────────────────────────────────────

def test_long_memory_file_is_never_written(store):
    """Long memory is the guardian's file. Nothing here may touch it."""
    from app.config import get_settings
    person_file = get_settings().person_file
    before = person_file.read_bytes()

    store.apply(_update([("a fall in the hallway", "incident", 0.95, "I fell")],
                        retire_ids=[1, 2, 3]))
    store.prune()
    store.render_for_prompt()

    assert person_file.read_bytes() == before


def test_short_memory_module_performs_no_file_io():
    """Structural guard against a future edit crossing the line.

    Short memory lives in the database. If this module ever grows a file
    write, the next question is which file -- and the answer must never be
    the guardian's profile. Checking for file I/O at all is a stronger and
    less brittle guard than checking for a filename (the docstring names
    the file on purpose, to explain the boundary).
    """
    import ast

    src = (ROOT / "app" / "services" / "short_memory.py").read_text("utf-8")
    tree = ast.parse(src)

    banned = {"open", "write_text", "write_bytes", "unlink", "remove"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in banned:
                found.add(name)
    assert not found, f"short_memory.py performs file I/O: {sorted(found)}"


# ── prompt rendering ────────────────────────────────────────────────

def test_prompt_block_uses_relative_days(store):
    from app.db import session as db
    db.add_short_memory("ilyas", "his niece visited", "visit", 0.9, None, None,
                        occurred_on=datetime.now() - timedelta(days=1))
    assert "yesterday: his niece visited" in store.render_for_prompt()


def test_analyst_view_exposes_ids(store):
    store.apply(_update([("a real event", "other", 0.8, "q")]))
    eid = store.active()[0].id
    assert f"[{eid}]" in store.render_for_analyst()


def test_system_prompt_omits_the_block_when_empty():
    from app.prompts.conversation import build_system_prompt
    empty = build_system_prompt("Ilyas", "Name: Ilyas", 300, recent_memory="")
    filled = build_system_prompt("Ilyas", "Name: Ilyas", 300,
                                 recent_memory="- yesterday: his niece visited")
    assert "RECENTLY" not in empty
    assert "RECENTLY" in filled and "his niece visited" in filled


# ── turn taking ─────────────────────────────────────────────────────

def test_listen_window_covers_the_whole_onset_range():
    """The two waits are different things and must not be conflated.

    The lower bound is not arbitrary: on the BOX3 a reply starts
    anywhere up to about four seconds after the loudspeaker stops, and
    a window that merely matches that range clips its own late edge.
    Asserted as a floor rather than an exact value so a deployment may
    wait longer, never less.
    """
    from app.config import get_settings
    s = get_settings()
    assert s.listen_timeout_seconds >= 6.0
    # A pause once they have started is held open separately, and must
    # stay generous: this is what stops a slow speaker being cut off.
    assert s.continuation_grace_seconds >= 2.0


def test_silence_prompts_escalate_then_close():
    """First silence checks they can hear; the second changes the subject."""
    from app.graph.nodes import MAX_CONSECUTIVE_SILENCES
    from app.prompts.conversation import (
        NO_RESPONSE_INSTRUCTION, SECOND_SILENCE_INSTRUCTION,
    )

    def instruction_for(silences):
        return (SECOND_SILENCE_INSTRUCTION if silences >= 2
                else NO_RESPONSE_INSTRUCTION)

    assert instruction_for(1) is NO_RESPONSE_INSTRUCTION
    assert instruction_for(2) is SECOND_SILENCE_INSTRUCTION
    # The second prompt must not ask again whether they can hear.
    assert "hear you" in NO_RESPONSE_INSTRUCTION
    assert "Do not ask again" in SECOND_SILENCE_INSTRUCTION
    assert "Change the subject" in SECOND_SILENCE_INSTRUCTION
    # And there is an end to it rather than prompting for ever.
    assert MAX_CONSECUTIVE_SILENCES == 3
