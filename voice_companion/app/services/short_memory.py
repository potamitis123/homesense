"""Short-term rolling memory.

Two memories, and the difference between them is the point:

    data/person_ilyas.txt   LONG.  Who this person is — family, health
                            context, interests, what not to raise. Edited
                            by a guardian, by hand. **This system never
                            writes to it**, and nothing in this module
                            opens it at all.

    short_memory_events     SHORT. What happened lately — a visit, an
                            incident, something out of the ordinary. The
                            agent writes it, and it rolls over and is
                            forgotten within days.

Long memory is identity and changes slowly. Short memory is news and is
supposed to expire. Mixing them would mean either an agent that forgets
who someone's daughter is, or a profile file that quietly fills up with
"chased the cat" — which is exactly the failure this split avoids.

WHAT GOES IN

Only what is worth raising again tomorrow. A visit from a neighbour, a
fall, a plan for the week, an argument, a lost cat. Not "had a coffee",
not "watched the news" — routine detail crowds out the notable, and an
agent that recites yesterday's beverages is worse company than one that
says nothing. The judgement is made by the model against SHORT_MEMORY_SYSTEM,
then filtered again here by salience, so a talkative model cannot fill
the buffer on its own.

WHY NOT langchain.memory

LangChain's memory classes (ConversationBufferMemory and friends) are for
keeping *within* one conversation, and are legacy in LangChain 1.x. This
is the other problem: carrying a handful of facts *between* conversations
that happen a day apart. LangGraph's checkpointer already handles the
first — it is what makes a conversation resumable — and it is the wrong
tool for the second, because a checkpoint is a machine state to resume,
not a small set of facts to reason about. So short memory is ordinary
application state in the database this project already owns, rendered
into the system prompt at the start of a conversation and revised by a
structured-output pass at the end.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from ..db import session as db

log = logging.getLogger(__name__)

VALID_CATEGORIES = {"visit", "incident", "health", "plan", "mood", "other"}


# ── what the analyst returns ────────────────────────────────────────

class RememberedEvent(BaseModel):
    """One thing worth carrying into tomorrow's conversation."""

    text: str = Field(
        description="One short sentence, third person, past tense, as it "
                    "would be read aloud. For example: 'chased next door's "
                    "cat out of the garden'.")
    category: str = Field(
        description="One of: visit, incident, health, plan, mood, other")
    salience: float = Field(
        description="0.0 to 1.0. How much this is worth raising again in a "
                    "later conversation. Routine daily detail is below 0.3.")
    quote: str = Field(
        description="The exact line from the transcript that supports this, "
                    "copied verbatim.")


class ShortMemoryUpdate(BaseModel):
    """The changes one conversation makes to short memory."""

    remember: list[RememberedEvent] = Field(
        default_factory=list,
        description="New events worth keeping. Usually none, one or two. "
                    "Never more than three.")
    retire_ids: list[int] = Field(
        default_factory=list,
        description="Ids of existing events that are finished or resolved "
                    "and should no longer be mentioned.")
    retire_reason: str = Field(
        default="",
        description="Why those events were retired, in a few words.")


# ── the store ───────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def _relative_day(when: datetime, today: datetime) -> str:
    """'yesterday' rather than '2026-08-20'.

    The block is read by a model whose output is spoken aloud, and an
    agent that says "on the twentieth of August" about last Tuesday
    sounds like it is reading a file, which it is.
    """
    days = (today.date() - when.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "last week"
    return f"{days // 7} weeks ago"


class ShortMemory:
    """A fixed-length rolling queue, per person.

    Two independent ways out, and both are meant to happen:

        age      nothing is kept longer than max_age_days. A week is about
                 how long an event stays worth raising; past that, asking
                 about it is odd rather than attentive.
        overflow a new event pushes the oldest off the end once the queue
                 is full. Strict FIFO — position, not importance.

    Importance is applied at the door instead: apply() drops anything
    below min_salience, so everything in the queue has already earned its
    place and the queue itself can stay simple.

    The bound is not decoration. Everything here is injected into every
    system prompt, so an unbounded buffer costs tokens on every turn of
    every conversation and, worse, buries the two things that matter
    under twenty that do not.
    """

    def __init__(self, person_id: str, *, max_events: int = 12,
                 max_age_days: int = 7, min_salience: float = 0.4):
        self.person_id = person_id
        self.max_events = max_events
        self.max_age_days = max_age_days
        self.min_salience = min_salience

    # ── reading ─────────────────────────────────────────────────────

    def active(self) -> list:
        return db.active_short_memory(self.person_id)

    def render_for_prompt(self, now: datetime | None = None) -> str:
        """The block that goes into the conversation system prompt."""
        now = now or datetime.now()
        rows = self.active()
        if not rows:
            return ""
        lines = []
        for row in rows:
            lines.append(f"- {_relative_day(row.occurred_on, now)}: {row.text}")
        return "\n".join(lines)

    def render_for_analyst(self) -> str:
        """The same events, with ids, so the analyst can retire them."""
        rows = self.active()
        if not rows:
            return "(nothing remembered yet)"
        now = datetime.now()
        return "\n".join(
            f"[{row.id}] {_relative_day(row.occurred_on, now)}: {row.text}"
            for row in rows)

    # ── writing ─────────────────────────────────────────────────────

    def prune(self, now: datetime | None = None) -> int:
        """Expire by age, then by count. Returns how many were retired."""
        now = now or datetime.now()
        rows = self.active()
        retired = 0

        cutoff = now - timedelta(days=self.max_age_days)
        stale = [r.id for r in rows if r.occurred_on < cutoff]
        if stale:
            retired += db.retire_short_memory(stale, "expired", self.person_id)
            rows = [r for r in rows if r.id not in set(stale)]

        if len(rows) > self.max_events:
            # Strict FIFO: the newest push the oldest off the end, whatever
            # they are about. Salience decides what gets *in* — it is the
            # floor in apply() — and after that the buffer is just a queue.
            #
            # active() is newest-first, so the tail is the oldest.
            overflow = [r.id for r in rows[self.max_events:]]
            retired += db.retire_short_memory(overflow, "overflow",
                                              self.person_id)
        if retired:
            log.info("short memory: retired %d event(s) on prune", retired)
        return retired

    def apply(self, update: ShortMemoryUpdate, session_id: str | None = None,
              now: datetime | None = None) -> dict:
        """Apply an analyst's proposed changes, with the guards.

        The model proposes; this decides. It can be wrong in three ways
        that matter, and each is checked here rather than trusted:
        ids that do not exist or belong to someone else, events too
        routine to keep, and events already remembered.
        """
        now = now or datetime.now()
        result = {"added": 0, "retired": 0, "skipped_routine": 0,
                  "skipped_duplicate": 0}
        if update is None:
            return result

        # Retire first, so a superseded event does not block its successor
        # as a duplicate.
        if update.retire_ids:
            result["retired"] = db.retire_short_memory(
                list(update.retire_ids), "agent", self.person_id)
            if result["retired"] != len(update.retire_ids):
                # Not an error worth failing on: a model that invents an id
                # is common, and the guard did its job.
                log.info("short memory: %d of %d retire ids were valid",
                         result["retired"], len(update.retire_ids))

        existing = {_normalise(r.text) for r in self.active()}

        for event in update.remember[:3]:
            text = (event.text or "").strip()
            if not text:
                continue
            if event.salience < self.min_salience:
                log.debug("short memory: dropping routine '%s' (%.2f)",
                          text, event.salience)
                result["skipped_routine"] += 1
                continue
            key = _normalise(text)
            if key in existing:
                result["skipped_duplicate"] += 1
                continue
            category = (event.category or "other").strip().lower()
            if category not in VALID_CATEGORIES:
                category = "other"
            db.add_short_memory(
                person_id=self.person_id, text=text, category=category,
                salience=max(0.0, min(1.0, float(event.salience))),
                quote=(event.quote or "").strip() or None,
                session_id=session_id, occurred_on=now)
            existing.add(key)
            result["added"] += 1

        result["retired"] += self.prune(now)
        log.info("short memory: +%d added, %d retired, %d routine, %d duplicate",
                 result["added"], result["retired"], result["skipped_routine"],
                 result["skipped_duplicate"])
        return result


def build_short_memory(settings, person_id: str) -> ShortMemory:
    return ShortMemory(
        person_id,
        max_events=settings.short_memory_max_events,
        max_age_days=settings.short_memory_max_age_days,
        min_salience=settings.short_memory_min_salience)
