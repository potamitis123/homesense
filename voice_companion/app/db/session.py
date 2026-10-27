"""Engine, session factory, and the few writes Phase 1 performs.

Kept synchronous: SQLite plus a single household is not a concurrency
problem, and sync SQLAlchemy inside async nodes is far easier to reason
about than mixing two async stacks. If this ever moves to Postgres the
repository functions below are the only place that has to change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from .models import (
    Base, ConversationSession, ConversationTurn, Person, ShortMemoryEvent,
)

_settings = get_settings()

_engine = create_engine(
    _settings.database_url,
    future=True,
    # SQLite refuses cross-thread use by default; the transport delivers
    # audio on its own thread, so writes can originate off the main one.
    connect_args={"check_same_thread": False}
    if _settings.database_url.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(_engine)


def upsert_person(person_id: str, name: str, age: int | None,
                  language: str) -> None:
    with SessionLocal() as db:  # type: Session
        row = db.get(Person, person_id)
        if row is None:
            db.add(Person(id=person_id, name=name, age=age,
                          preferred_language=language))
        else:
            row.name, row.age, row.preferred_language = name, age, language
        db.commit()


def create_session(session_id: str, person_id: str, sentinel_id: str,
                   thread_id: str, theme: str | None = None) -> None:
    with SessionLocal() as db:
        db.add(ConversationSession(
            id=session_id, person_id=person_id, sentinel_id=sentinel_id,
            thread_id=thread_id, schedule_theme=theme, status="running"))
        db.commit()


def add_turn(session_id: str, turn_index: int, speaker: str, text: str | None,
             confidence: float | None = None,
             audio_seconds: float | None = None) -> None:
    with SessionLocal() as db:
        db.add(ConversationTurn(
            session_id=session_id, turn_index=turn_index, speaker=speaker,
            text=text, confidence=confidence,
            audio_duration_seconds=audio_seconds))
        db.commit()


def finish_session(session_id: str, status: str, reason: str | None,
                   duration_seconds: float) -> None:
    with SessionLocal() as db:
        row = db.get(ConversationSession, session_id)
        if row is None:
            return
        row.ended_at = datetime.now()
        row.status = status
        row.end_reason = reason
        row.duration_seconds = duration_seconds
        db.commit()


# ── short memory ────────────────────────────────────────────────────
#
# Rolling, bounded, and owned by the system. The long-term profile file is
# never touched from here — see services/short_memory.py.

def add_short_memory(person_id: str, text: str, category: str,
                     salience: float, quote: str | None,
                     session_id: str | None,
                     occurred_on: datetime | None = None) -> int:
    with SessionLocal() as db:
        row = ShortMemoryEvent(
            person_id=person_id, text=text, category=category,
            salience=salience, quote=quote, source_session_id=session_id,
            occurred_on=occurred_on or datetime.now())
        db.add(row)
        db.commit()
        return row.id


def active_short_memory(person_id: str) -> list[ShortMemoryEvent]:
    """Live events, newest first."""
    with SessionLocal() as db:
        return list(
            db.query(ShortMemoryEvent)
            .filter(ShortMemoryEvent.person_id == person_id,
                    ShortMemoryEvent.retired_at.is_(None))
            .order_by(ShortMemoryEvent.occurred_on.desc(),
                      ShortMemoryEvent.id.desc())
            .all())


def retire_short_memory(event_ids: list[int], reason: str,
                        person_id: str | None = None) -> int:
    """Retire events, returning how many were actually retired.

    `person_id` is not optional in practice: ids come back from a language
    model, which can invent them, so the caller passes the person and this
    refuses to touch anything belonging to anyone else.
    """
    if not event_ids:
        return 0
    with SessionLocal() as db:
        q = db.query(ShortMemoryEvent).filter(
            ShortMemoryEvent.id.in_(event_ids),
            ShortMemoryEvent.retired_at.is_(None))
        if person_id is not None:
            q = q.filter(ShortMemoryEvent.person_id == person_id)
        rows = list(q.all())
        for row in rows:
            row.retired_at = datetime.now()
            row.retired_reason = reason
        db.commit()
        return len(rows)


def load_session_with_turns(session_id: str):
    """Session row plus its turns, for the HTML report."""
    with SessionLocal() as db:
        row = db.get(ConversationSession, session_id)
        if row is None:
            return None, [], None
        turns = list(row.turns)
        person = db.get(Person, row.person_id)
        return row, turns, person
