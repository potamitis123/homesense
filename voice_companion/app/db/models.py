"""Database schema.

Phase 1 covers the three tables the conversation loop actually writes:
persons, conversation_sessions, conversation_turns. The remaining tables
from the specification (findings, alerts, long_term_memories,
topic_history, ...) arrive with the phases that populate them -- creating
empty tables now would only invite drift.

Note the deliberate separation: LangGraph keeps its own checkpoint
database for graph state (resumability), while these tables are the
durable record a human reads. They serve different purposes and are not
the same data.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Person(Base):
    __tablename__ = "persons"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    age = Column(Integer, nullable=True)
    preferred_language = Column(String(32), default="English")
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    sessions = relationship("ConversationSession", back_populates="person")


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"

    id = Column(String(64), primary_key=True)
    person_id = Column(String(64), ForeignKey("persons.id"), nullable=False)
    sentinel_id = Column(String(64), nullable=False)
    # LangGraph's thread id, kept so a session can be resumed or inspected
    # in the checkpoint store after the fact.
    thread_id = Column(String(64), nullable=True)
    started_at = Column(DateTime, default=datetime.now, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    # running | completed | stopped_by_person | error | timeout
    status = Column(String(32), default="running", nullable=False)
    schedule_theme = Column(String(64), nullable=True)
    end_reason = Column(String(128), nullable=True)

    person = relationship("Person", back_populates="sessions")
    turns = relationship("ConversationTurn", back_populates="session",
                         order_by="ConversationTurn.turn_index")


Index("ix_sessions_person_started", ConversationSession.person_id,
      ConversationSession.started_at)


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), ForeignKey("conversation_sessions.id"),
                        nullable=False)
    turn_index = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=datetime.now, nullable=False)
    # AGENT | HUMAN | UNKNOWN -- in Phase 1 this comes from the state
    # machine, which knows exactly who was given the floor, rather than
    # from voice analysis. See services/speaker.py for why.
    speaker = Column(String(16), nullable=False)
    text = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    audio_duration_seconds = Column(Float, nullable=True)

    session = relationship("ConversationSession", back_populates="turns")


Index("ix_turns_session_index", ConversationTurn.session_id,
      ConversationTurn.turn_index)


class ShortMemoryEvent(Base):
    """One notable thing from a conversation, kept for a few days.

    The counterpart to data/person_ilyas.txt, and deliberately a different
    kind of thing. That file is long memory: who this person is, edited by
    a guardian, never written by this system. This table is short memory:
    what happened lately, written by the system, forgotten on purpose.

    Rows are retired rather than deleted. When the agent decides an event
    is finished ("the tap was fixed") or the buffer rolls over, the row
    stays with retired_at set, so a guardian reading the database later
    can see what the agent knew and when it stopped mentioning it. A row
    that vanished without trace is not debuggable.
    """

    __tablename__ = "short_memory_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    person_id = Column(String(64), ForeignKey("persons.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    # The day the conversation happened. Not the day the event happened:
    # "my daughter came on Tuesday" cannot be dated reliably from speech,
    # and guessing produces an agent that confidently misremembers when.
    occurred_on = Column(DateTime, default=datetime.now, nullable=False)

    # One short line, third person, as it will be read into the prompt:
    # "chased next door's cat out of the garden".
    text = Column(Text, nullable=False)
    # visit | incident | health | plan | mood | other
    category = Column(String(32), default="other", nullable=False)
    # 0..1. How much this is worth raising again. Routine detail scores
    # low and is dropped before it is ever stored.
    salience = Column(Float, default=0.5, nullable=False)
    # Verbatim line from the transcript that supports it, so a wrong
    # memory can be traced back to what was actually said.
    quote = Column(Text, nullable=True)

    source_session_id = Column(String(64), nullable=True)
    retired_at = Column(DateTime, nullable=True)
    # agent | expired | overflow | superseded
    retired_reason = Column(String(32), nullable=True)


Index("ix_short_memory_person_active", ShortMemoryEvent.person_id,
      ShortMemoryEvent.retired_at, ShortMemoryEvent.occurred_on)
