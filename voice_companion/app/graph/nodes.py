"""Graph nodes.

Each node does one thing and returns a state delta. Services (audio, STT,
TTS, LLM) are injected once at build time rather than constructed here,
so the graph stays testable with mocks and the expensive models load
exactly once per process.

Phase 1 keeps the thin nodes thin -- choose_next_goal and
retrieve_relevant_memory exist as real nodes with placeholder bodies, so
Phase 2 fills them in rather than reshaping the graph.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..db import session as db
from ..prompts.conversation import (
    ANCHORS_BLOCK, CLOSING_INSTRUCTION, CONCERNS_BLOCK,
    NO_RESPONSE_INSTRUCTION,
    OPENING_INSTRUCTION, SECOND_SILENCE_INSTRUCTION, build_system_prompt,
)
from .state import ConversationState, Phase, transition

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_SILENCES = 3

# Above this many words an utterance is a sentence of the person's own,
# whatever it happens to share with what the agent just said.
ECHO_MAX_WORDS = 8

# The shortest listening slice worth asking the transport for. Below
# this a re-listen cannot catch anyone starting to speak and would only
# burn an attempt.
MIN_LISTEN_SLICE = 1.0

# Handing the lead back, in the wordings actually observed on live calls.
# Every one of these was said to somebody with stage-2 dementia, and
# every one was answered with "not really", "I don't know", or silence.
#
# Only the lead-handback phrasings belong here, not open recall. "Any
# favourite memory of those days?" is a perfectly good tail once a name
# has already landed -- "do you remember your friend George, or any
# favourite memory of those days?" is the shape being aimed for, and an
# earlier version of this list rejected it. What makes the difference is
# not the open half; it is whether anything was named first. That test
# is the anchor check, and it is the one doing the real work here.
_OPEN_FIELD_RE = re.compile(
    r"anything else|anything on your mind|anything specific you|"
    r"anything (?:you'?d|you would) like to (?:talk|share|discuss)|"
    r"what (?:would|do) you (?:like|want) to (?:talk|chat|discuss)|"
    r"feel free to (?:share|let me know|tell|ask)|"
    r"(?:anything|something) (?:on your mind|you want to)", re.I)


class Nodes:
    def __init__(self, *, audio, stt, tts, llm, profile, settings,
                 analyst=None, notifier=None, short_memory=None):
        self.audio = audio
        self.stt = stt
        self.tts = tts
        self.llm = llm
        self.profile = profile
        self.settings = settings
        self.analyst = analyst
        self.notifier = notifier
        # Rolling diary of the last few days. None disables it entirely,
        # which is what --mock does: a test run should not write events
        # into the real person's memory.
        self.short_memory = short_memory
        # Rule hits accumulated across the conversation, so the closing
        # report can cite them even if the moment has passed.
        self._rule_hits = []
        # Built once the profile and diary are loaded; the agent
        # has nothing to lead with before that.
        self.ledger = None
        # Captured PCM is handed between receive and transcribe here rather
        # than through graph state: every state update is serialised into
        # the checkpoint database, and a five-minute conversation would
        # write hundreds of megabytes of raw audio into it. The two nodes
        # always run back to back in one process, so an attribute is safe.
        self._pending_audio: bytes = b""

    # ── setup ────────────────────────────────────────────────────────

    async def load_person_context(self, state: ConversationState) -> dict:
        from ..profile import profile_to_prompt_block

        db.upsert_person(self.profile.person_id, self.profile.name,
                         self.profile.age, self.profile.preferred_language)
        db.create_session(state["session_id"], self.profile.person_id,
                          state["sentinel_id"], state["thread_id"])

        if self.profile.unfilled:
            log.warning("profile fields still unfilled (agent will not use "
                        "them): %s", ", ".join(self.profile.unfilled))

        # Both memories reach the prompt here, and only here: the durable
        # profile from the guardian's file, and the rolling diary of what
        # happened lately. Pruning on the way in means an event that aged
        # out overnight is gone before it can be mentioned.
        recent = ""
        if self.short_memory is not None:
            try:
                self.short_memory.prune()
                recent = self.short_memory.render_for_prompt()
                if recent:
                    log.info("short memory: %d event(s) in context",
                             len(recent.splitlines()))
            except Exception:
                # A companion that cannot remember yesterday is still worth
                # having; one that will not start is not.
                log.exception("short memory unavailable for this conversation")

        # The household's own standing instructions. Read fresh every
        # conversation so an edit takes effect on the next call rather
        # than the next restart -- whoever looks after this person
        # should not need us to deploy anything.
        from ..house_rules import load_house_rules
        rules = load_house_rules(self.settings.house_rules_file)

        from ..services.ledger import TopicLedger
        self.ledger = TopicLedger.build(self.profile, recent)

        system = build_system_prompt(
            self.profile.address_as,
            profile_to_prompt_block(self.profile),
            self.settings.target_conversation_seconds,
            recent_memory=recent,
            house_rules=rules,
            agent_name=self.settings.agent_name)

        return {
            "phase": transition(state.get("phase", Phase.IDLE.value), Phase.STARTING),
            "conversation_phase": "opening",
            "person_id": self.profile.person_id,
            "person_name": self.profile.address_as,
            "language": self.profile.preferred_language,
            "profile_block": profile_to_prompt_block(self.profile),
            "messages": [SystemMessage(content=system)],
            "transcript": [],
            "turn_index": 0,
            "started_at": time.time(),
            "elapsed_seconds": 0.0,
            "should_finish": False,
            "end_reason": None,
            "consecutive_silences": 0,
            "daily_plan": [],
            "recent_topics": [],
            "retrieved_memories": [],
            "findings": [],
            "safety_state": "normal",
            "error": None,
        }

    async def prepare_daily_plan(self, state: ConversationState) -> dict:
        # Phase 2: schedule themes, topic cooldowns, diversity scoring.
        return {"daily_plan": ["warm check-in"], "current_goal": "warm check-in"}

    # ── listening ────────────────────────────────────────────────────

    async def receive_human_input(self, state: ConversationState) -> dict:
        """Wait for the person to say something.

        Entered once per turn normally, and again for each burst that
        reached the transcriber but turned out not to be speech. The
        deadline is what makes the difference between the two: it is the
        person's opportunity to begin, and it survives a re-listen, so
        the loop cannot give them less time than one attempt would have.
        """
        phase = transition(state["phase"], Phase.LISTENING)
        now = time.time()

        deadline = state.get("listen_deadline") or 0.0
        attempts = state.get("listen_attempts", 0)
        if deadline <= now:
            deadline = now + self.settings.listen_timeout_seconds
            attempts = 0

        # Never ask for a sliver. Every attempt gets at least
        # min_listen_seconds, even when the turn's own deadline has
        # nearly run out: the floor belongs to the person, and a
        # re-listen offering them two seconds is not an opportunity to
        # answer, it is a formality before giving up on them.
        remaining = max(self.settings.min_listen_seconds, deadline - now)

        try:
            audio = await self.audio.receive_utterance(timeout=remaining)
        except Exception as exc:
            log.exception("audio capture failed")
            return {"phase": Phase.ERROR.value, "error": str(exc),
                    "should_finish": True, "end_reason": "audio_error"}

        self._pending_audio = audio or b""
        if not audio:
            silences = state.get("consecutive_silences", 0) + 1
            log.info("no speech captured (%d in a row)", silences)

        return {"phase": phase,
                "listen_deadline": deadline,
                "listen_attempts": attempts + 1,
                "elapsed_seconds": time.time() - state["started_at"]}

    async def transcribe_input(self, state: ConversationState) -> dict:
        # Always leave LISTENING, even with nothing to decode: silence is a
        # normal outcome of a turn, not an error, and the phase must still
        # advance or the next node has no legal transition.
        phase = transition(state["phase"], Phase.PROCESSING)
        audio, self._pending_audio = self._pending_audio, b""

        if not audio:
            # The window expired with nothing captured at all. That is a
            # real silence: the person did not answer.
            return {"phase": phase, "last_human_text": "",
                    "listen_deadline": None, "listen_attempts": 0,
                    **self._count_silence(state)}

        started = time.time()
        result = await self.stt.transcribe(audio)
        stt_seconds = time.time() - started

        if result.is_empty:
            log.info("transcription discarded (%s)", result.rejected_reason)

            # Something was captured, and it was not the person speaking:
            # the agent's own voice off the walls, the loudspeaker's
            # power-down click, a door. The device uploads on its own
            # sound trigger and cannot tell those from speech, so this is
            # ordinary, not exceptional.
            #
            # What must not happen is scoring it as the person's silence.
            # Doing so is how a conversation ends with three "can you
            # hear me" prompts while they are sitting there waiting to
            # answer -- the agent counting its own echo against them.
            # Keep the turn open instead.
            #
            # The seconds spent in the transcriber are handed back to the
            # deadline -- they were the machine's, not the person's --
            # but only up to a cap. An uncapped hand-back turns one slow
            # transcription into a turn that looks frozen.
            given_back = min(stt_seconds, self.settings.listen_extension_cap_seconds)
            if stt_seconds > given_back:
                log.warning("transcription took %.1fs; giving back only %.1fs "
                            "of it so the turn does not stall",
                            stt_seconds, given_back)
            deadline = (state.get("listen_deadline") or time.time()) + given_back
            attempts = state.get("listen_attempts", 0)
            if (attempts < self.settings.listen_attempts_per_turn
                    and time.time() < deadline):
                log.info("not speech; still %.1fs of their window left "
                         "(attempt %d/%d)", deadline - time.time(), attempts,
                         self.settings.listen_attempts_per_turn)
                return {"phase": phase, "last_human_text": "",
                        "listen_deadline": deadline}

            log.info("not speech, and the window is spent (%d attempt(s))",
                     attempts)
            return {"phase": phase, "last_human_text": "",
                    "listen_deadline": None, "listen_attempts": 0,
                    **self._count_silence(state)}

        agent_said = state.get("last_agent_text", "")
        since = getattr(self.audio, "seconds_since_agent_spoke", None)
        if (agent_said and since is not None
                and since <= self.settings.echo_repeat_window_seconds
                and sounds_like_the_agent(result.text, agent_said,
                                          self.settings.echo_similarity_max)):
            log.warning("discarded %r -- that is the agent's own voice back "
                        "through the room, %.1fs after it spoke",
                        result.text, since)
            # Treated exactly like any other burst that was not speech:
            # the person's turn stays open rather than being spent on it.
            deadline = state.get("listen_deadline")
            if deadline and time.time() < deadline:
                return {"phase": phase, "last_human_text": "",
                        "listen_deadline": deadline}
            return {"phase": phase, "last_human_text": "",
                    "listen_deadline": None, "listen_attempts": 0,
                    **self._count_silence(state)}

        log.info("HUMAN: %s", result.text)
        return {"phase": phase, "last_human_text": result.text,
                "listen_deadline": None, "listen_attempts": 0,
                "consecutive_silences": 0,
                "last_confidence": result.confidence,
                "last_audio_seconds": result.duration_seconds}

    def _count_silence(self, state: ConversationState) -> dict:
        """Record another unanswered turn, and give up once there are
        enough of them.

        The reason is set here rather than in continue_or_finish because
        routing sends a silent turn straight to close_session, which would
        otherwise record a give-up as a normal completion.
        """
        silences = state.get("consecutive_silences", 0) + 1
        update: dict = {"consecutive_silences": silences}
        if silences >= MAX_CONSECUTIVE_SILENCES:
            update.update(should_finish=True, end_reason="no_response")
        return update

    async def persist_turn(self, state: ConversationState) -> dict:
        text = state.get("last_human_text", "")
        if not text:
            return {}
        index = state.get("turn_index", 0)
        db.add_turn(state["session_id"], index, "HUMAN", text,
                    state.get("last_confidence"), state.get("last_audio_seconds"))
        turn = {"index": index, "speaker": "HUMAN", "text": text,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "audio_seconds": state.get("last_audio_seconds", 0.0),
                "confidence": state.get("last_confidence")}
        return {"turn_index": index + 1,
                "transcript": state.get("transcript", []) + [turn],
                "messages": [HumanMessage(content=text)]}

    async def assess_safety(self, state: ConversationState) -> dict:
        """Deterministic check on what was just said.

        Runs every turn because it is free and cannot hallucinate. The
        LLM assessment happens once at the end over the whole transcript;
        doing it per turn would triple the cost for little gain.
        """
        from ..services.safety import check_rules

        text = state.get("last_human_text", "")
        if not text:
            return {}

        rules = check_rules(text)
        if not rules.any_hit:
            return {}

        self._rule_hits.extend(rules.hits)
        categories = sorted({h.category for h in rules.hits})
        log.info("safety rules matched %s in: %r", categories, text[:80])

        if rules.urgent:
            # Do not wait for the conversation to end.
            log.warning("URGENT phrase detected; notifying guardian now")
            await self._notify_guardian(state, urgent=True)
            return {"safety_state": "urgent"}

        return {"safety_state": "concern"}

    async def _notify_guardian(self, state, urgent: bool = False) -> dict | None:
        """Build a factual report and hand it to the notifier."""
        from datetime import datetime
        from ..prompts.analysis import REPORT_SYSTEM, transcript_text
        from ..services.safety import (
            GuardianReport, RuleResult, check_rules, severity_of,
        )

        if self.notifier is None:
            return None

        turns = state.get("transcript", [])
        rules = RuleResult(hits=list(self._rule_hits))
        assessment = state.get("_assessment")

        report = None
        if self.analyst is not None:
            report = await self.analyst.structured(
                GuardianReport,
                REPORT_SYSTEM.format(name=self.profile.address_as),
                transcript_text(turns))

        if report is None:
            # The analyst failed or is disabled: report the rule hits
            # verbatim rather than sending nothing at all.
            quotes = [h.quote for h in rules.hits][:6]
            report = GuardianReport(
                subject="conversation flagged for review",
                summary=("Automatic phrase matching flagged part of this "
                         "conversation. The exact lines are below."),
                reported_items=[f"{h.category}: {h.quote}" for h in rules.hits][:6],
                quotes=quotes)

        severity = "URGENT" if urgent else severity_of(rules, assessment)
        started = state.get("started_at")
        started_dt = datetime.fromtimestamp(started) if started else datetime.now()

        result = self.notifier.send(
            person_name=self.profile.address_as,
            severity=severity,
            report=report,
            session_id=state.get("session_id", ""),
            started_at=started_dt,
            sentinel_id=state.get("sentinel_id", ""),
            room=self.profile.room,
            transcript=turns)
        log.info("guardian notification: sent=%s dry_run=%s error=%s",
                 result.sent, result.dry_run, result.error)
        return {"_notified": True}

    async def final_safety_review(self, state: ConversationState) -> dict:
        """One structured pass over the finished conversation."""
        from ..prompts.analysis import SAFETY_SYSTEM, transcript_text
        from ..services.safety import (
            RuleResult, SafetyAssessment, should_notify,
        )

        turns = state.get("transcript", [])
        if not turns:
            return {}

        assessment = None
        if self.analyst is not None:
            assessment = await self.analyst.structured(
                SafetyAssessment,
                SAFETY_SYSTEM.format(name=self.profile.address_as),
                transcript_text(turns))
        if assessment is not None:
            log.info("safety assessment: level=%s notify=%s reason=%s",
                     assessment.level, assessment.guardian_notification_recommended,
                     assessment.reason)

        rules = RuleResult(hits=list(self._rule_hits))
        if state.get("safety_state") == "urgent":
            return {}          # already sent during the conversation

        if should_notify(rules, assessment):
            await self._notify_guardian({**state, "_assessment": assessment})
            return {"safety_state": "notified"}

        log.info("nothing to report to the guardian")
        return {}

    async def update_short_memory(self, state: ConversationState) -> dict:
        """Decide what tomorrow should know about today.

        Runs once, after the conversation. Never during: judging what is
        worth remembering needs the whole conversation, and a model call
        between turns is latency the person hears.

        The long-term profile is untouched by this and by everything else
        in this file. It is the guardian's document; the agent only ever
        reads it.
        """
        from ..prompts.analysis import transcript_text
        from ..prompts.short_memory import build_short_memory_prompt
        from ..services.short_memory import ShortMemoryUpdate

        if self.short_memory is None or self.analyst is None:
            return {}

        turns = state.get("transcript", [])
        # Nothing was said: there is nothing to remember, and asking the
        # model anyway invites it to invent a day.
        if not any((t.get("text") or "").strip() for t in turns
                   if t.get("speaker") != "AGENT"):
            log.info("short memory: nothing said, leaving the diary alone")
            return {}

        try:
            update = await self.analyst.structured(
                ShortMemoryUpdate,
                build_short_memory_prompt(self.profile.address_as,
                                          self.short_memory.render_for_analyst()),
                transcript_text(turns))
            if update is None:
                log.warning("short memory: analyst returned nothing")
                return {}
            result = self.short_memory.apply(
                update, session_id=state.get("session_id"))
            return {"short_memory_updated": result}
        except Exception:
            # The conversation has already happened and been reported on.
            # Failing to update the diary must not fail the session.
            log.exception("short memory update failed")
            return {}

    # ── thinking ─────────────────────────────────────────────────────

    async def retrieve_relevant_memory(self, state: ConversationState) -> dict:
        # Phase 2: bounded retrieval of durable memories.
        return {}

    async def choose_next_goal(self, state: ConversationState) -> dict:
        # Phase 2: diversity scoring against topic history.
        elapsed = state.get("elapsed_seconds", 0.0)
        if elapsed >= self.settings.target_conversation_seconds:
            return {"conversation_phase": "closing"}
        if state.get("conversation_phase") == "opening" and state.get("turn_index", 0) > 1:
            return {"conversation_phase": "main"}
        return {}

    async def _reanchor(self, messages: list, reply: str) -> str:
        """Send the turn back once if it asks without naming anything.

        The prompt asks for this in prose and the prose does not hold.
        Measured: the rule listing "is there anything else on your mind?"
        as forbidden went in, and the next call said that sentence word
        for word, along with six open memory questions in five minutes.

        So the reply is read before it is spoken. One retry, never two:
        a second failure is answered by taking the question away
        entirely, because a warm remark with no question in it is always
        better than a third attempt at the same bad one.
        """
        if self.ledger is None:
            return reply

        open_field = _OPEN_FIELD_RE.search(reply)
        unanchored = "?" in reply and self.ledger.find_in(reply) is None
        if not (open_field or unanchored):
            self.ledger.mark_used(reply)
            return reply

        why = ("That turn hands the choice of subject back to them"
               if open_field else
               "That question names nobody and nothing they can catch on")
        log.info("re-anchoring: %s -- %r", why.lower(), reply[:70])

        names = ", ".join(a.label for a in self.ledger.candidates(3))
        try:
            retry = await self.llm.reply(messages + [SystemMessage(content=(
                f"{why}, so it cannot be answered from a standing start. "
                f"Say it again, naming one of these out loud: {names}. "
                "Keep the warmth and keep it short. You may leave the "
                "question open-ended after the name, but the name must "
                "come first."))])
        except Exception:
            log.exception("re-anchor failed; keeping the original turn")
            return reply

        # Only prefer the retry if it actually fixed the thing.
        if self.ledger.find_in(retry) and not _OPEN_FIELD_RE.search(retry):
            self.ledger.mark_used(retry)
            return retry

        log.info("re-anchor did not land either; keeping the original")
        self.ledger.mark_used(reply)
        return reply

    def _concerns_block(self) -> str:
        """The person's own words for anything flagged this call.

        Their wording, not a summary of it: "my legs won't hold me up"
        tells the model what to do about a walk, where "mobility concern"
        does not. Deduplicated and capped, because this goes into every
        remaining turn and a long list stops being read.
        """
        if not self._rule_hits:
            return ""

        seen, quotes = set(), []
        for hit in self._rule_hits:
            if hit.category == "message":
                continue          # requests are relayed, not held in mind
            quote = hit.quote.strip()
            key = quote.lower()
            if quote and key not in seen:
                seen.add(key)
                quotes.append(f'- they said: "{quote}"')

        if not quotes:
            return ""
        return CONCERNS_BLOCK.format(concerns="\n".join(quotes[-6:]))

    async def generate_agent_response(self, state: ConversationState) -> dict:
        phase = transition(state["phase"], Phase.AGENT_THINKING)
        messages = list(state["messages"])

        closing = (state.get("conversation_phase") == "closing"
                   or state.get("should_finish"))
        if not state.get("transcript"):
            messages.append(SystemMessage(content=OPENING_INSTRUCTION))
        elif closing:
            messages.append(SystemMessage(content=CLOSING_INSTRUCTION))
        elif state.get("consecutive_silences", 0) > 0:
            # Graduated: the first silence is treated as "did you hear
            # me", the second as "that question was hard to answer". Asking
            # twice whether someone can hear you is what a machine does.
            silences = state.get("consecutive_silences", 0)
            messages.append(SystemMessage(
                content=SECOND_SILENCE_INSTRUCTION if silences >= 2
                else NO_RESPONSE_INSTRUCTION))

        # Anything flagged so far, restated on every turn. The transcript
        # already contains it, but six turns later it carries no more
        # weight than the weather, and a question about a walk does not
        # look dangerous on its own.
        concerns = self._concerns_block()
        if concerns:
            messages.append(SystemMessage(content=concerns))

        # What to lead with. Not on the opening (which introduces itself
        # and asks nothing personal) or the close (which asks nothing at
        # all) -- only on the turns that actually carry a question.
        ordinary = bool(state.get("transcript")) and not closing
        if ordinary and self.ledger is not None:
            candidates = self.ledger.candidates()
            if candidates:
                messages.append(SystemMessage(content=ANCHORS_BLOCK.format(
                    name=self.profile.address_as,
                    anchors="\n".join(f"- {a.label}" for a in candidates))))

        try:
            reply = await self.llm.reply(messages)
        except Exception as exc:
            log.exception("LLM failed")
            return {"phase": Phase.ERROR.value, "error": str(exc),
                    "should_finish": True, "end_reason": "llm_error"}

        if ordinary:
            reply = await self._reanchor(messages, reply)

        log.info("AGENT: %s", reply)
        return {"phase": phase, "last_agent_text": reply}

    async def persist_agent_turn(self, state: ConversationState) -> dict:
        text = state.get("last_agent_text", "")
        if not text:
            return {}
        index = state.get("turn_index", 0)
        db.add_turn(state["session_id"], index, "AGENT", text, 1.0, None)
        turn = {"index": index, "speaker": "AGENT", "text": text,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "audio_seconds": 0.0, "confidence": 1.0}
        return {"turn_index": index + 1,
                "transcript": state.get("transcript", []) + [turn],
                "messages": [AIMessage(content=text)]}

    # ── speaking ─────────────────────────────────────────────────────

    async def speak(self, state: ConversationState) -> dict:
        text = state.get("last_agent_text", "")
        if not text:
            return {}
        phase = transition(state["phase"], Phase.SPEAKING)
        try:
            chunks = (c.pcm for c in self.tts.stream(text, state.get("language", "en")))
            seconds = await self.audio.send_audio(
                chunks, source_rate=getattr(self.tts, "sample_rate", None))
        except Exception as exc:
            log.exception("speech output failed")
            return {"phase": Phase.ERROR.value, "error": str(exc),
                    "should_finish": True, "end_reason": "tts_error"}
        return {"phase": phase, "last_spoken_seconds": seconds,
                "elapsed_seconds": time.time() - state["started_at"]}

    # ── closing ──────────────────────────────────────────────────────

    async def continue_or_finish(self, state: ConversationState) -> dict:
        elapsed = time.time() - state["started_at"]
        updates: dict = {"elapsed_seconds": elapsed}

        if state.get("should_finish"):
            return updates

        if state.get("consecutive_silences", 0) >= MAX_CONSECUTIVE_SILENCES:
            updates.update(should_finish=True, end_reason="no_response")
        elif _asked_to_stop(state.get("last_human_text", "")):
            updates.update(should_finish=True, end_reason="person_asked_to_stop",
                           conversation_phase="closing")
        elif elapsed >= self.settings.max_conversation_seconds:
            updates.update(should_finish=True, end_reason="max_duration")
        elif (elapsed >= self.settings.target_conversation_seconds
              and state.get("conversation_phase") == "closing"):
            updates.update(should_finish=True, end_reason="target_reached")
        elif elapsed >= self.settings.target_conversation_seconds:
            updates["conversation_phase"] = "closing"
        return updates

    async def close_session(self, state: ConversationState) -> dict:
        elapsed = time.time() - state["started_at"]
        reason = state.get("end_reason") or "completed"
        status = {"person_asked_to_stop": "stopped_by_person",
                  "no_response": "timeout",
                  "audio_error": "error", "llm_error": "error",
                  "tts_error": "error"}.get(reason, "completed")
        db.finish_session(state["session_id"], status, reason, elapsed)
        log.info("session %s finished after %.0fs (%s)",
                 state["session_id"], elapsed, reason)
        try:
            phase = transition(state["phase"], Phase.CLOSING)
        except Exception:
            phase = Phase.CLOSING.value
        return {"phase": transition(phase, Phase.FINISHED),
                "elapsed_seconds": elapsed, "end_reason": reason}


# Unambiguous: these state an intention about this conversation and are
# honoured wherever they appear.
STRONG_STOP_PHRASES = (
    "i want to stop", "i'd like to stop", "let's stop", "lets stop",
    "stop talking", "that's enough", "thats enough", "no more",
    "leave me alone", "go away", "i'm tired", "im tired",
    "not now", "another time",
)

# Ambiguous in isolation: a farewell can equally be part of a story about
# someone else. Only counted when the utterance is plainly a farewell.
WEAK_STOP_PHRASES = (
    "goodbye", "good bye", "bye for now", "bye", "talk later",
    "talk tomorrow", "speak tomorrow",
)

# Markers that the person is recounting a farewell rather than making one.
NARRATIVE_MARKERS = ("said", "says", "saying", "told", "waved", "wished")

SHORT_UTTERANCE_WORDS = 6
TAIL_WORDS = 4


def _normalise(text: str) -> list[str]:
    keep = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    return keep.split()


def sounds_like_the_agent(human: str, agent: str, threshold: float) -> bool:
    """Is this the agent's own voice coming back through the room?

    The device's loudspeaker and microphone share a room, so a phrase
    the agent has just spoken can be captured, transcribed, and answered
    as though the person said it. Measured on p4-door-1: the agent asked
    "is there something on your mind today?" and was told, by itself,
    "mind today" -- then answered that. Four turns in a row went this
    way, and one of them invented a person out of the agent mishearing
    its own use of the person's name.

    Compared fuzzily against every window of the agent's last sentence,
    because what comes back has been through a loudspeaker, a room and a
    transcriber: "chat later" returned as "that later".

    Deliberately narrow. It only ever fires on a short utterance, and
    the caller only consults it within seconds of the agent speaking,
    because a person repeating the agent's words back to them is
    ordinary conversation and must not be discarded as noise.
    """
    from difflib import SequenceMatcher

    said = _normalise(human)
    spoke = _normalise(agent)
    if not said or not spoke or len(said) > ECHO_MAX_WORDS:
        return False

    window = len(said)
    best = 0.0
    for i in range(0, max(1, len(spoke) - window + 1)):
        candidate = " ".join(spoke[i:i + window])
        best = max(best, SequenceMatcher(None, " ".join(said), candidate).ratio())
        if best >= threshold:
            return True
    return best >= threshold


def _asked_to_stop(text: str) -> bool:
    """Deterministic stop detection.

    Deliberately not left to the model: a request to stop must always be
    honoured, and a rule cannot be argued out of it the way a prompt can.

    The subtlety is that a farewell word is not always a farewell. "I said
    goodbye to my neighbour this morning" is someone telling you about
    their day, and hanging up on them for it would be exactly the kind of
    small betrayal that stops people trusting the thing.
    """
    lowered = " ".join(text.lower().split())
    if not lowered:
        return False

    if any(phrase in lowered for phrase in STRONG_STOP_PHRASES):
        return True

    words = lowered.replace(",", " ").replace(".", " ").split()
    if any(marker in words for marker in NARRATIVE_MARKERS):
        return False

    if len(words) <= SHORT_UTTERANCE_WORDS:
        return any(phrase in lowered for phrase in WEAK_STOP_PHRASES)

    # A farewell at the very end still counts: "...that was nice. Goodbye."
    tail = " ".join(words[-TAIL_WORDS:])
    return any(phrase in tail for phrase in WEAK_STOP_PHRASES)
