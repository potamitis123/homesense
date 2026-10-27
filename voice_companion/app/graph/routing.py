"""Conditional-edge decisions.

Separate functions rather than lambdas inside the graph builder, so each
routing rule can be unit tested on a plain dict without constructing a
graph or any services.
"""

from __future__ import annotations

from .state import ConversationState

MAX_CONSECUTIVE_SILENCES = 3


def after_transcription(state: ConversationState) -> str:
    """Heard something worth answering, or listen again?"""
    if state.get("should_finish"):
        return "close"
    if state.get("error"):
        return "close"
    if state.get("consecutive_silences", 0) >= MAX_CONSECUTIVE_SILENCES:
        return "close"
    if not state.get("last_human_text"):
        # Silence, but not yet enough of it to give up: the agent speaks
        # again rather than both parties waiting in silence.
        return "respond"
    return "respond"


def after_listen_attempt(state: ConversationState) -> str:
    """Was that the person, or should the turn keep listening?

    "listen_again" is chosen only when the transcriber rejected what it
    was given *and* transcribe_input left a deadline standing, which it
    does only while the person still has time to begin. Everything
    else -- real speech, a spent window, an error -- goes on.
    """
    if state.get("error") or state.get("should_finish"):
        return "proceed"
    if state.get("last_human_text"):
        return "proceed"
    if state.get("listen_deadline"):
        return "listen_again"
    return "proceed"


def after_turn(state: ConversationState) -> str:
    """Another exchange, or wrap up?"""
    if state.get("should_finish") or state.get("error"):
        return "close"
    return "listen"


def should_open_with_agent(state: ConversationState) -> str:
    """The agent always speaks first -- it placed the call."""
    return "respond"
