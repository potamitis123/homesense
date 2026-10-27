"""Typed graph state and the conversation state machine.

Two separate things live here on purpose:

  ConversationState -- the LangGraph state dict, checkpointed to SQLite
  Phase             -- the explicit state machine from the specification

The machine is not decoration. It is what prevents the agent speaking
over itself, treating its own voice as a human turn, or sending audio
after the session has closed. Every transition is checked, so an illegal
one raises rather than silently corrupting a conversation.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages


class Phase(str, enum.Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    AGENT_THINKING = "AGENT_THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    CLOSING = "CLOSING"
    FINISHED = "FINISHED"
    ERROR = "ERROR"


# Which phases may follow which. Barge-in is already expressed
# (SPEAKING -> INTERRUPTED -> LISTENING) even though Phase 1 never takes
# that edge, so enabling it later is a behaviour change, not a redesign.
ALLOWED: dict[Phase, set[Phase]] = {
    Phase.IDLE: {Phase.STARTING, Phase.ERROR},
    Phase.STARTING: {Phase.AGENT_THINKING, Phase.LISTENING, Phase.ERROR, Phase.CLOSING},
    Phase.LISTENING: {Phase.PROCESSING, Phase.CLOSING, Phase.ERROR, Phase.LISTENING},
    Phase.PROCESSING: {Phase.AGENT_THINKING, Phase.LISTENING, Phase.CLOSING, Phase.ERROR},
    Phase.AGENT_THINKING: {Phase.SPEAKING, Phase.CLOSING, Phase.ERROR},
    Phase.SPEAKING: {Phase.LISTENING, Phase.INTERRUPTED, Phase.CLOSING, Phase.ERROR},
    Phase.INTERRUPTED: {Phase.LISTENING, Phase.CLOSING, Phase.ERROR},
    Phase.CLOSING: {Phase.FINISHED, Phase.ERROR},
    Phase.FINISHED: set(),
    Phase.ERROR: {Phase.FINISHED},
}


class IllegalTransition(RuntimeError):
    pass


def transition(current: str, target: Phase) -> str:
    now = Phase(current)
    if target not in ALLOWED[now]:
        raise IllegalTransition(f"{now.value} -> {target.value} is not allowed")
    return target.value


class Turn(TypedDict):
    index: int
    speaker: Literal["AGENT", "HUMAN", "UNKNOWN"]
    text: str
    timestamp: str
    audio_seconds: float
    confidence: float | None


class ConversationState(TypedDict, total=False):
    # identity
    person_id: str
    sentinel_id: str
    session_id: str
    thread_id: str

    # dialogue
    messages: Annotated[list, add_messages]
    transcript: list[Turn]
    turn_index: int

    # timing
    started_at: float
    elapsed_seconds: float

    # control
    phase: str
    conversation_phase: str      # opening | main | closing (spec's wording)
    should_finish: bool
    end_reason: str | None
    consecutive_silences: int
    # When the person's opportunity to start speaking runs out. Set once
    # per turn and carried across re-listens, so a burst that turned out
    # not to be speech costs them a couple of seconds rather than the
    # whole turn. None between turns.
    listen_deadline: float | None
    # Re-listens used inside the current turn. A bound on the loop that
    # does not depend on the clock, for the case where the device is
    # emitting bursts faster than the deadline can expire.
    listen_attempts: int

    # context (Phase 2 fills these out properly)
    profile_block: str
    person_name: str
    language: str
    current_goal: str | None
    daily_plan: list[str]
    recent_topics: list[str]
    retrieved_memories: list[str]
    findings: list[dict[str, Any]]
    safety_state: str
    # What the closing short-memory pass changed, for the log and the
    # report. The diary itself lives in the database, not in graph state:
    # it outlives this conversation by days.
    short_memory_updated: dict[str, Any] | None

    # last turn's working values
    last_human_text: str
    last_agent_text: str
    last_confidence: float | None
    last_audio_seconds: float
    last_spoken_seconds: float
    error: str | None
