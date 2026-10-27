"""Settings for the voice companion.

Everything tunable lives here, read from voice_companion/.env (see
.env.example). Secrets never appear in code or in the person profile.

Phase 1 note: only the settings the current slice actually uses are
defined. Later phases add the safety, Brevo and scheduler blocks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# voice_companion/
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
# ast_home/ -- the existing Sentinel checkout we import (never modify)
SENTINEL_ROOT = PACKAGE_ROOT.parent

DATA_DIR = PACKAGE_ROOT / "data"
VOICES_DIR = DATA_DIR / "voices"
REPORTS_DIR = PACKAGE_ROOT / "reports"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PACKAGE_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── storage ──────────────────────────────────────────────────────
    # SQLite by design: one household, and it matches the existing stack.
    # Swapping to Postgres later is a URL change plus a driver install.
    database_url: str = Field(
        default=f"sqlite:///{(DATA_DIR / 'voice_companion.db').as_posix()}")
    checkpoint_db: str = Field(
        default=str(DATA_DIR / "checkpoints.sqlite"))

    # ── LLM ──────────────────────────────────────────────────────────
    # Conversation and analysis are deliberately separate so they can be
    # tuned, priced and *hosted* apart. The common case for this household
    # is a small local model doing the talking (it is spoken aloud, so
    # latency is felt) and a cloud model doing the analysis afterwards,
    # where a slow careful answer costs nothing.
    #
    # LLM_PROVIDER is the default for both roles; CONVERSATION_PROVIDER
    # and ANALYSIS_PROVIDER override it individually. Leave the per-role
    # ones blank to follow LLM_PROVIDER.
    # What the companion calls itself. Spoken aloud in the opening line,
    # so it wants to be easy to hear and easy to say back.
    agent_name: str = "Nausicaa"
    # How much the wording varies. High on purpose: this speaks to the
    # same person every day, and a greeting delivered identically each
    # morning stops sounding like somebody and starts sounding like a
    # machine reading a card. Variation is not decoration here.
    conversation_temperature: float = 0.9

    llm_provider: str = "openai"            # openai | ollama | scripted
    conversation_provider: str = ""         # blank -> llm_provider
    analysis_provider: str = ""             # blank -> llm_provider

    # Explicit model names, whatever the provider. Blank means "use the
    # provider's default below", which is what keeps a plain
    # LLM_PROVIDER=ollama switch working with no other edits.
    conversation_model: str = ""
    analysis_model: str = ""

    # ── OpenAI ───────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_conversation_model: str = "gpt-4o"
    openai_analysis_model: str = "gpt-4o"

    # ── Ollama ───────────────────────────────────────────────────────
    # llama3.2 for both by default: the card here is 6 GB and already
    # holds Whisper large-v3, so a 9B alongside it spills out of VRAM and
    # the spoken reply arrives late. Point ANALYSIS_MODEL at a larger
    # pulled model (gemma2:9b, qwen3.5:4b) if the analysis pass is worth
    # the load time — it runs after the call, not during it.
    ollama_base_url: str = "http://localhost:11434"
    ollama_conversation_model: str = "llama3.2:latest"
    ollama_analysis_model: str = "llama3.2:latest"

    llm_timeout_seconds: float = 20.0
    analysis_timeout_seconds: float = 60.0
    # Local models need their own budgets, and the two roles need very
    # different ones. Talking is quick: llama3.2 answers a conversational
    # turn in a couple of seconds here. Structured extraction is not —
    # Ollama constrains generation to the JSON schema, and a 60 s budget
    # was measured failing outright (httpx.ReadTimeout) on the diary pass.
    # A slow diary costs nothing, since it runs after the goodbye; a slow
    # spoken reply is heard, so the two must not share a number.
    ollama_timeout_seconds: float = 45.0
    ollama_analysis_timeout_seconds: float = 240.0
    llm_max_tokens: int = 160

    # ── speech ───────────────────────────────────────────────────────
    stt_model: str = "large-v3"
    # "auto", "el" or "en". Left blank it follows asr.language from the
    # dashboard's Settings tab, so the household language is configured in
    # one place rather than two. Forcing the wrong one does not fail
    # loudly -- it produces fluent, confident nonsense in the forced
    # language, which is worse than an empty transcript.
    stt_language: str = ""
    # Greedy decoding for the live turn: latency matters more than the
    # last word of accuracy. The report re-transcribes with beam search.
    stt_beam_size_live: int = 1
    stt_beam_size_report: int = 5

    # ── where speech recognition runs ────────────────────────────────
    # "auto" takes the GPU when the model actually fits in what is free
    # and falls back to the CPU when it does not. The fallback keeps the
    # SAME model: a companion that mishears is worse than one that
    # answers a second later, so accuracy is not what gets traded away
    # when the card is busy.
    #
    # This matters on this machine specifically. The Sentinel
    # application holds roughly 4.4 GB of the 6 GB card for CLAP and the
    # emotion models and does not release it between events, which
    # leaves too little for large-v3. Loading anyway does not fail --
    # it thrashes, and measured transcription of a 0.6 s clip went from
    # 3 s to 22 s, which reads to the person in the room as the
    # companion having frozen. Refusing the GPU is the better outcome:
    # slow is recoverable, unpredictable is not.
    #
    # "cuda" and "cpu" force the choice and skip the check.
    stt_device: str = "auto"            # auto | cuda | cpu
    # Free VRAM required before "auto" will use the GPU. large-v3 in
    # float16 is ~3.1 GB of weights; the rest is decoding headroom.
    stt_min_free_vram_mb: int = 3600
    # 0 lets CTranslate2 choose. Worth setting explicitly on the CPU
    # path, where it is the difference between slow and unusable.
    stt_cpu_threads: int = 0

    local_tts_provider: str = "piper"
    local_tts_model_path: str = str(VOICES_DIR / "en_US-lessac-medium.onnx")
    # >1 speaks more slowly. 1.15 is noticeably gentler without sounding odd.
    tts_length_scale: float = 1.15

    # ── turn taking ──────────────────────────────────────────────────
    # How long a pause means "they have finished speaking". Too short and
    # the agent interrupts an elderly speaker mid-thought, which is worse
    # than waiting; measured guidance was ~0.5-0.8s.
    # How long a pause means "they have finished speaking".
    #
    # Raised from 700 ms after watching it end a turn mid-sentence: "I
    # woke up in the morning" was taken as a complete answer while the
    # person was still drawing breath to continue.
    #
    # Three seconds, because the pauses are inside the sentence, not
    # only at the end of it: "yes I need ... some water" has a two
    # second gap in the middle and is one thought. Anything that cuts
    # there produces a stump, an answer to the stump, and a person who
    # learns that this thing talks over them.
    #
    # It is paid for in latency -- the agent now waits three seconds
    # after the last word before it replies. That is the right trade for
    # this audience: being interrupted is worse than being answered
    # slowly.
    vad_silence_ms: int = 3000
    vad_threshold: float = 0.5
    # How long to wait for them to *begin* replying before treating the
    # turn as unanswered. Not a cap on how long they may speak, and not a
    # cap on pauses once they have started -- a pause mid-sentence is held
    # open by continuation_grace_seconds below, which is the one that
    # protects a slow speaker from being cut off.
    #
    # Six seconds, and the sixth matters. Measured on the BOX3, a reply
    # begins anywhere from the instant the loudspeaker stops to about
    # four seconds after it -- the person has to notice the question
    # ended, decide, and start. A four-second window therefore cuts off
    # the late end of the ordinary range rather than catching outliers,
    # and every one it cuts is scored as silence against them.
    #
    # It is still not generous. Reaching it is mild -- the agent says
    # one short line giving them an easy opening, and only closes after
    # MAX_CONSECUTIVE_SILENCES of them -- but waiting far longer leaves
    # an elderly person sitting in silence wondering whether the thing
    # is still on, which is its own kind of rude.
    # Raised from 6.0 after a call where "do you enjoy his music?" was
    # scored as silence, the agent asked whether he could still hear it,
    # and only then did the answer arrive -- "once upon a time in the
    # 60s". He had not gone quiet. He was reaching back sixty years, and
    # the window closed while he was still doing it.
    #
    # That is the cost asymmetry here. Waiting too long looks like a
    # pause; cutting too early asks somebody whether they can hear you
    # while they are in the middle of remembering, and their answer then
    # lands two turns late against a question nobody is asking any more.
    listen_timeout_seconds: float = 9.0
    # No listening window is ever shorter than this, however much of the
    # turn has already been spent rejecting bursts that were not speech.
    #
    # Four seconds is a floor set by the person, not by the machine: a
    # re-listen that hands back 2.9 seconds is not a chance to answer,
    # and being cut off while gathering a thought is what makes somebody
    # anxious about talking to it at all.
    min_listen_seconds: float = 4.0
    # After the device says a burst ended, wait this long for another one
    # before treating the turn as finished. Mid-sentence pauses are common
    # and the device cannot tell them from the end of a thought.
    continuation_grace_seconds: float = 2.5
    # A single answer may genuinely run this long. 45 s is the safety
    # cap on a stuck detector, not a target -- 15 s of continuous speech
    # in one turn is ordinary for someone telling you about their day.
    max_utterance_seconds: float = 45.0
    # How long after our last frame the far end is still playing it.
    #
    # A WebRTC peer has a jitter buffer, and the device's loudspeaker is
    # in the same room as its microphone, so audio we have finished
    # sending is still audible for some time afterwards. Measured on
    # p4-door-1 the agent's own voice came back about 1.2 s after the
    # last frame left, was transcribed, and was answered as though the
    # person had said it -- a self-sustaining loop that spent every turn
    # before the person could take it.
    #
    # This is the mic staying shut for that long. It is charged directly
    # to the person's response time, so it should be as small as the
    # room allows and no smaller.
    echo_guard_seconds: float = 1.2
    # Second net, for whatever survives the guard. A transcript that
    # closely matches what the agent just said, and arrives soon after
    # it said it, is the room -- not an answer. Only applied inside
    # echo_repeat_window_seconds, because a person genuinely repeating
    # the agent's words back is ordinary conversation.
    echo_similarity_max: float = 0.72
    echo_repeat_window_seconds: float = 3.0
    # Log a level bar while listening, so it is visible from the console
    # whether the person's voice is arriving at all.
    show_input_level: bool = True
    # How many times one turn may re-listen after capturing something
    # that turned out not to be speech. The device uploads on its own
    # sound trigger, and its own loudspeaker sets that trigger off, so
    # some bursts in every turn are the agent's voice off the walls or
    # the codec's power-down click. Rejecting one of those must not end
    # the person's turn -- but it must not be able to loop forever
    # either, hence a count as well as a deadline.
    listen_attempts_per_turn: int = 4
    # Ceiling on how much of the transcriber's time is handed back to
    # the person's listening window.
    #
    # Giving it back at all is right: seconds spent deciding that a
    # burst was not speech were the machine's, not theirs. Giving it
    # back without a ceiling is not. When recognition is slow -- a
    # contended GPU, or the CPU fallback -- a single slow transcription
    # would extend one turn by its whole duration, and a turn that
    # stretches to most of a minute reads as the companion having
    # frozen. A companion for an elderly person may be slow; it may not
    # appear to have stopped working.
    listen_extension_cap_seconds: float = 2.5

    # ── conversation length ──────────────────────────────────────────
    min_conversation_seconds: int = 240
    target_conversation_seconds: int = 300
    max_conversation_seconds: int = 360

    # ── short memory ─────────────────────────────────────────────────
    # The rolling diary of the last few days, injected into every system
    # prompt. Long memory (data/person_ilyas.txt) is the guardian's file
    # and is never written by this system; these bounds apply only to the
    # short one.
    #
    # A fixed-length queue: a new event pushes the oldest off the end, and
    # nothing survives a week. Small on purpose — every active event costs
    # tokens on every turn, and more than a dozen buries the two that
    # matter.
    short_memory_enabled: bool = True
    short_memory_max_events: int = 12
    short_memory_max_age_days: int = 7
    # Below this, an event is routine and is discarded rather than stored.
    # The prompt already teaches the distinction; this is the backstop for
    # a model that is feeling generous.
    short_memory_min_salience: float = 0.4

    # ── sentinel ─────────────────────────────────────────────────────
    # Standing household instructions, plain text, re-read at the start
    # of every conversation. Separate from the prompt in code because it
    # belongs to whoever is responsible for the person, not to whoever
    # maintains this package. Missing or empty is legal -- the built-in
    # prompt already stands on its own.
    house_rules_file: str = str(DATA_DIR / "house_rules.txt")

    default_sentinel_id: str = "box3-2"
    # How the companion reaches the device. auto | mqtt | webrtc.
    # "auto" reads the device's retained capability announcement and
    # falls back to mqtt whenever it learns nothing, so a BOX-3 -- whose
    # firmware predates that topic -- keeps working untouched.
    audio_transport: str = "auto"
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    audio_sample_width: int = 2

    # ── guardian notification ────────────────────────────────────────
    # Credentials and the recipient come from the dashboard's Settings tab
    # (Alert Communication / Brevo SMTP), not from here.
    enable_guardian_email: bool = True
    enable_dry_run_alerts: bool = False
    # Whole transcripts are not mailed by default: an alert should carry
    # the relevant lines, not a recording of someone's private afternoon.
    alert_include_transcript: bool = False

    log_level: str = "INFO"

    @property
    def person_file(self) -> Path:
        return DATA_DIR / "person_ilyas.txt"

    # ── LLM resolution ───────────────────────────────────────────────

    def resolve_llm(self, role: str) -> "LLMChoice":
        """Which provider, model and budget a role should use.

        One place decides, so `llm.py` never has to reason about defaults
        and a future provider is a branch here plus a builder there.

        Precedence, most specific first:
            CONVERSATION_MODEL / ANALYSIS_MODEL      explicit, any provider
            OPENAI_* / OLLAMA_* per-role model       the provider's default
        and independently:
            CONVERSATION_PROVIDER / ANALYSIS_PROVIDER
            LLM_PROVIDER
        """
        if role not in ("conversation", "analysis"):
            raise ValueError(f"unknown LLM role: {role}")

        per_role = (self.conversation_provider if role == "conversation"
                    else self.analysis_provider)
        provider = (per_role or self.llm_provider or "openai").strip().lower()

        explicit = (self.conversation_model if role == "conversation"
                    else self.analysis_model).strip()

        if provider == "openai":
            model = explicit or (self.openai_conversation_model
                                 if role == "conversation"
                                 else self.openai_analysis_model)
            timeout = (self.llm_timeout_seconds if role == "conversation"
                       else self.analysis_timeout_seconds)
        elif provider == "ollama":
            model = explicit or (self.ollama_conversation_model
                                 if role == "conversation"
                                 else self.ollama_analysis_model)
            timeout = (self.ollama_timeout_seconds if role == "conversation"
                       else self.ollama_analysis_timeout_seconds)
        elif provider == "scripted":
            model, timeout = "scripted", self.llm_timeout_seconds
        else:
            raise ValueError(
                f"Unknown LLM provider '{provider}' for {role}. "
                "Use openai, ollama or scripted.")

        return LLMChoice(role=role, provider=provider, model=model,
                         timeout=timeout, max_tokens=self.llm_max_tokens,
                         base_url=self.ollama_base_url,
                         api_key=self.openai_api_key)


@dataclass(frozen=True)
class LLMChoice:
    """A fully resolved model choice, ready for llm.py to build."""

    role: str
    provider: str
    model: str
    timeout: float
    max_tokens: int
    base_url: str
    api_key: str

    def describe(self) -> str:
        return f"{self.role}: {self.provider}/{self.model}"


def _sentinel_asr_language(default: str = "auto") -> str:
    """asr.language from the Sentinel app's parameters.json, if readable."""
    try:
        import sys
        if str(SENTINEL_ROOT) not in sys.path:
            sys.path.insert(0, str(SENTINEL_ROOT))
        from misc.load_parameters import parameters
        return (parameters.get_parameters().get("asr", {}).get("language")
                or default)
    except Exception:
        return default


def get_settings() -> Settings:
    settings = Settings()
    if not settings.openai_api_key:
        # Fall back to the Sentinel checkout's own secret resolution so a
        # working install does not need the key written twice.
        try:
            import sys
            if str(SENTINEL_ROOT) not in sys.path:
                sys.path.insert(0, str(SENTINEL_ROOT))
            from misc.secrets import openai_api_key
            settings.openai_api_key = openai_api_key() or ""
        except Exception:
            pass
    if not settings.stt_language:
        settings.stt_language = _sentinel_asr_language()
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    return settings
