"""Graph assembly.

The shape follows the specification, with the Phase 1 caveat that the
analysis tail (summarize / extract_findings / update_memory /
final_safety_review) is not wired yet -- those nodes arrive in Phase 3
and attach between close_session and END without disturbing the loop.

    START -> load_person_context -> prepare_daily_plan
          -> respond (agent speaks first; it placed the call)
          -> listen -> transcribe -> persist_turn
             ^-------------'  (not speech, and their window is not up)
          -> retrieve_memory -> choose_goal -> respond ...
          -> close_session -> END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from . import routing
from .nodes import Nodes
from .state import ConversationState


def build_graph(nodes: Nodes, checkpointer=None):
    g = StateGraph(ConversationState)

    g.add_node("load_person_context", nodes.load_person_context)
    g.add_node("prepare_daily_plan", nodes.prepare_daily_plan)
    g.add_node("receive_human_input", nodes.receive_human_input)
    g.add_node("transcribe_input", nodes.transcribe_input)
    g.add_node("persist_turn", nodes.persist_turn)
    g.add_node("assess_safety", nodes.assess_safety)
    g.add_node("retrieve_relevant_memory", nodes.retrieve_relevant_memory)
    g.add_node("choose_next_goal", nodes.choose_next_goal)
    g.add_node("generate_agent_response", nodes.generate_agent_response)
    g.add_node("persist_agent_turn", nodes.persist_agent_turn)
    g.add_node("speak", nodes.speak)
    g.add_node("continue_or_finish", nodes.continue_or_finish)
    g.add_node("final_safety_review", nodes.final_safety_review)
    g.add_node("update_short_memory", nodes.update_short_memory)
    g.add_node("close_session", nodes.close_session)

    g.add_edge(START, "load_person_context")
    g.add_edge("load_person_context", "prepare_daily_plan")

    # The agent opens: it initiated the call, so silence at the start is
    # expected rather than a failure.
    g.add_edge("prepare_daily_plan", "generate_agent_response")

    g.add_edge("generate_agent_response", "persist_agent_turn")
    g.add_edge("persist_agent_turn", "speak")
    g.add_edge("speak", "continue_or_finish")

    g.add_conditional_edges("continue_or_finish", routing.after_turn,
                            {"listen": "receive_human_input",
                             "close": "final_safety_review"})

    g.add_edge("receive_human_input", "transcribe_input")

    # A burst that reached the transcriber and turned out not to be
    # speech sends the turn back to listening rather than back to the
    # agent. The device triggers its upload on sound, and its own
    # loudspeaker is the loudest sound in the room, so some bursts in
    # every turn are the agent's own voice. Answering those -- or worse,
    # counting them as the person's silence -- is what ended
    # conversations while the person was still waiting to speak.
    g.add_conditional_edges("transcribe_input", routing.after_listen_attempt,
                            {"listen_again": "receive_human_input",
                             "proceed": "persist_turn"})
    g.add_edge("persist_turn", "assess_safety")
    g.add_edge("assess_safety", "retrieve_relevant_memory")
    g.add_edge("retrieve_relevant_memory", "choose_next_goal")

    g.add_conditional_edges("choose_next_goal", routing.after_transcription,
                            {"respond": "generate_agent_response",
                             "close": "final_safety_review"})

    # Safety first, then the diary. If a conversation contained something
    # urgent, the guardian is told before the agent spends a second model
    # call deciding what to remember about it.
    g.add_edge("final_safety_review", "update_short_memory")
    g.add_edge("update_short_memory", "close_session")
    g.add_edge("close_session", END)

    return g.compile(checkpointer=checkpointer)
