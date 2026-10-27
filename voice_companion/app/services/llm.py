"""LLM access.

A real factory, not a hardwired model. Two things vary independently:

  * the **role** — conversation is spoken aloud, so it is short, warm and
    latency-sensitive; analysis runs after the call, is parsed rather than
    heard, and can afford to be slow and careful;
  * the **provider** — OpenAI or a local Ollama, chosen per role.

That second axis is the point of this module. The common arrangement for
this household is a small local model doing the talking and a cloud model
doing the analysis, because a late spoken reply is felt by the person in
the room and a late analysis is not. `Settings.resolve_llm()` decides;
everything here just builds what it is told.

Switching is an .env edit and nothing else:

    LLM_PROVIDER=ollama                 # both roles local
    CONVERSATION_PROVIDER=ollama        # ...or split them
    ANALYSIS_PROVIDER=openai
    CONVERSATION_MODEL=llama3.2:latest  # optional, overrides the default
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

log = logging.getLogger(__name__)


class ConversationLLM(Protocol):
    async def reply(self, messages: list) -> str: ...


# ── conversation ────────────────────────────────────────────────────

class ChatConversationLLM:
    """Any LangChain chat model, wrapped for the spoken turn.

    Provider-agnostic on purpose: the timeout and the graceful-failure
    text are identical whoever is answering, and duplicating them per
    provider is how they drift apart.
    """

    def __init__(self, chat_model, model_name: str, timeout: float):
        self._llm = chat_model
        self.model_name = model_name
        self._timeout = timeout

    async def reply(self, messages: list) -> str:
        try:
            result = await asyncio.wait_for(self._llm.ainvoke(messages),
                                            timeout=self._timeout)
        except asyncio.TimeoutError:
            # A companion that goes silent is worse than one that stumbles;
            # say something harmless and keep the conversation alive.
            log.warning("LLM (%s) timed out after %.0fs", self.model_name,
                        self._timeout)
            return "Sorry, I lost my thread for a moment. What were you saying?"
        except Exception:
            # Local models add failure modes the cloud does not have: the
            # daemon may be down, or the model not pulled. Same contract —
            # never leave the person in silence.
            log.exception("LLM (%s) call failed", self.model_name)
            return "Sorry, I lost my thread for a moment. What were you saying?"
        text = (getattr(result, "content", "") or "").strip()
        return text or "Sorry, I did not catch that."


class ScriptedLLM:
    """Fixed replies, for tests and for exercising the graph offline."""

    def __init__(self, replies: list[str] | None = None):
        self._replies = replies or [
            "Good morning. How are you feeling today?",
            "That is good to hear. Have you been outside at all?",
            "It has been lovely weather for it. I will let you get on. Talk tomorrow.",
        ]
        self._i = 0
        self.model_name = "scripted"

    async def reply(self, messages: list) -> str:
        text = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return text


# ── provider builders ───────────────────────────────────────────────
#
# Imported lazily inside each builder so that running with one provider
# never requires the other's package to be installed.

def _build_openai_chat(choice, *, temperature: float, max_tokens: int | None):
    from langchain_openai import ChatOpenAI

    if not choice.api_key:
        raise RuntimeError(
            "No OpenAI key. Set OPENAI_API_KEY in voice_companion/.env, or "
            "switch provider with LLM_PROVIDER=ollama.")
    kwargs = dict(model=choice.model, api_key=choice.api_key,
                  timeout=choice.timeout, temperature=temperature)
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


def _build_ollama_chat(choice, *, temperature: float, max_tokens: int | None):
    from langchain_ollama import ChatOllama

    # Ollama's knobs are not OpenAI's: the reply cap is num_predict, and
    # ChatOllama has no timeout field at all — the httpx client takes it
    # through client_kwargs, and asyncio.wait_for enforces the rest.
    kwargs = dict(model=choice.model, base_url=choice.base_url,
                  temperature=temperature,
                  client_kwargs={"timeout": choice.timeout})
    if max_tokens is not None:
        kwargs["num_predict"] = max_tokens
    return ChatOllama(**kwargs)


_BUILDERS = {"openai": _build_openai_chat, "ollama": _build_ollama_chat}


def build_llm(settings, scripted: bool = False) -> ConversationLLM:
    """The conversation model, per CONVERSATION_PROVIDER / LLM_PROVIDER."""
    if scripted:
        return ScriptedLLM()

    choice = settings.resolve_llm("conversation")
    if choice.provider == "scripted":
        return ScriptedLLM()

    builder = _BUILDERS.get(choice.provider)
    if builder is None:
        raise RuntimeError(f"No builder for provider '{choice.provider}'")

    temperature = getattr(settings, "conversation_temperature", 0.8)
    chat = builder(choice, temperature=temperature, max_tokens=choice.max_tokens)
    log.info("conversation LLM: %s (timeout %.0fs)", choice.describe(),
             choice.timeout)
    return ChatConversationLLM(chat, choice.model, choice.timeout)


# ── analysis ────────────────────────────────────────────────────────

class Analyst:
    """Structured post-conversation analysis.

    Separate model and separate client from the conversation: this one is
    allowed to be slower and more careful, and its output is parsed rather
    than spoken.
    """

    def __init__(self, chat_model, model_name: str, structured_method: str | None):
        self._llm = chat_model
        self.model_name = model_name
        self._method = structured_method

    async def structured(self, schema, system: str, user: str):
        """Return an instance of `schema`, or None if the model fails.

        A malformed structured output must not take down a conversation
        that has already happened, so failure is None and the caller falls
        back to the deterministic layer.
        """
        from langchain_core.messages import HumanMessage, SystemMessage
        try:
            if self._method:
                model = self._llm.with_structured_output(schema,
                                                         method=self._method)
            else:
                model = self._llm.with_structured_output(schema)
            return await model.ainvoke([SystemMessage(content=system),
                                        HumanMessage(content=user)])
        except Exception:
            log.exception("structured analysis failed (%s via %s)",
                          schema.__name__, self.model_name)
            return None


def build_analyst(settings):
    """The analysis model, per ANALYSIS_PROVIDER / LLM_PROVIDER."""
    choice = settings.resolve_llm("analysis")
    if choice.provider == "scripted":
        return None

    builder = _BUILDERS.get(choice.provider)
    if builder is None:
        raise RuntimeError(f"No builder for provider '{choice.provider}'")

    # Temperature 0 and no token cap: this output is parsed, and a reply
    # truncated at the conversation's 160-token budget is a parse failure,
    # not a shorter answer.
    chat = builder(choice, temperature=0.0, max_tokens=None)

    # How structured output is obtained differs by provider. OpenAI models
    # tool-call, which LangChain picks by itself. Ollama has to be told to
    # constrain generation to the JSON schema instead — several pulled
    # models (gemma2 among them) support no tool calling at all, and the
    # default path would fail for them at every call.
    method = "json_schema" if choice.provider == "ollama" else None

    log.info("analysis LLM: %s (timeout %.0fs%s)", choice.describe(),
             choice.timeout, f", {method}" if method else "")
    return Analyst(chat, choice.model, method)
