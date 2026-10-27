"""Deciding when the guardian needs to be told.

Two layers, deliberately, because neither is trustworthy alone:

  Layer 1  deterministic phrase rules. Cannot be talked out of it, cannot
           hallucinate, fires the instant the words are heard. Blunt, and
           will produce false alarms.
  Layer 2  a structured LLM assessment over the whole conversation.
           Catches what no word list anticipates -- someone describing a
           fall without ever saying "fall" -- but can be wrong in both
           directions.

The layers are combined so that either can raise a concern; only both
staying quiet keeps the guardian un-notified. False alarms here cost an
unnecessary email. False negatives cost something that cannot be undone,
so the asymmetry is intentional.

Nothing in this file diagnoses anything. It reports what was said, with
the words that were actually said, and leaves judgement to a human.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# ── Layer 1: phrase rules ────────────────────────────────────────────
# Urgent: something may be happening right now.
URGENT_PATTERNS = [
    r"\bi(?:'ve| have)? fallen\b", r"\bi fell\b", r"\bfell down\b",
    r"can(?:'t|not) get up", r"can(?:'t|not) breathe", r"can(?:'t|not) move",
    r"\bchest pain\b", r"\bpain in my chest\b",
    r"\bhelp me\b", r"\bcall an ambulance\b", r"\bcall a doctor\b",
    r"\bi(?:'m| am) bleeding\b", r"\bblood\b",
    r"\bi(?:'m| am) trapped\b", r"\bstuck\b",
    r"\bfire\b", r"\bsmoke\b", r"\bgas\b",
    r"\bnobody can reach me\b", r"\bno one can reach me\b",
    # Self-harm. Urgent rather than "health", and the distinction is the
    # whole point: health waits for the conversation to end, and this
    # cannot. Measured live -- "I would like to take some pills with
    # alcohol" scored only health, so the agent said "it's important to
    # be careful" and changed the subject, and nothing left the machine
    # until the call was over.
    #
    # Loose, and unapologetically so. Every other list here trades a
    # false positive against a missed report; this one trades it against
    # somebody saying the most serious thing they will ever say to this
    # system and being answered with a change of subject.
    r"\bkill myself\b", r"\bend (my life|it all|it)\b",
    r"\btake (all|the rest of) (my|the) (pills|tablets|medication)\b",
    r"\b(pills|tablets|medication)\b[^.?!]{0,20}\b(with|and)\b[^.?!]{0,10}"
    r"\b(alcohol|drink|whisky|wine|vodka|brandy|ouzo)\b",
    r"\boverdose\b", r"\bhurt myself\b", r"\bharm myself\b",
    r"\bdon'?t want to (wake up|live|be here|go on)\b",
    r"\bdo not want to (wake up|live|be here|go on)\b",
    r"\bnothing to live for\b", r"\bno reason to (live|go on)\b",
    r"\bbetter off (dead|without me)\b", r"\bwant to die\b",
    r"\bwish (i were|i was|it would) (dead|over|end)\b",
]

# Worth mentioning, not an emergency.
HEALTH_PATTERNS = [
    # Deliberately loose. A false positive costs one unnecessary email; a
    # miss costs a family not being told their father said his knee hurts.
    r"\bpain\b", r"\bhurts?\b", r"\bhurting\b", r"\bsore\b", r"\baching\b",
    r"\bdizzy\b", r"\bfaint\b", r"\bweak\b",
    r"\bunwell\b", r"\bnot feeling well\b", r"\bfeel(?:ing)? sick\b",
    r"\bill\b", r"\bfever\b", r"\btemperature\b",
    r"\bmedication\b", r"\bmedicine\b", r"\bpills\b", r"\btablets\b",
    r"\bcan(?:'t|not) sleep\b", r"\bdidn(?:'t| not) sleep\b",
    r"\bdoctor\b", r"\bhospital\b", r"\bappointment\b",
    r"\bhungry\b", r"\bnothing to eat\b", r"\bno food\b",
    r"\blonely\b", r"\ball alone\b", r"\bfrightened\b", r"\bscared\b",
    r"\bworried\b", r"\bafraid\b",
    # Not being able to bear weight. Health rather than urgent: they have
    # not fallen, and "I fell" is already caught above -- but this is the
    # state immediately before a fall, which makes it the most useful
    # thing on this list to catch early.
    #
    # Added after a live call where "my legs won't hold me up" matched
    # nothing at all. It contains no pain, no hurt, no fall: it is a
    # plain description of not being able to stand, in the words somebody
    # would actually use, and the whole list walked straight past it.
    r"\b(legs?|knees?|they)\b[^.?!]{0,15}\b(won'?t|will not|can'?t|cannot|"
    r"do(?:n'?t| not))\b[^.?!]{0,10}\b(hold|carry|support|take)\b",
    r"\bhold me up\b", r"\bhold my weight\b",
    # First person, and "stand" needs guarding: "she can't stand that
    # programme" is about the television, not about her legs.
    r"\bi can(?:'t|not) (?:walk|stay up(?:right)?|manage the stairs|"
    r"stand(?! (?:that|this|the|it|him|her|them|people|being|listening)))",
    r"\b(gave|give|giving|gives) way\b", r"\bgone from under me\b",
    r"\bunsteady\b", r"\bwobbly\b", r"\boff balance\b", r"\bstumbl\w*\b",
    r"\bshaky\b", r"\btrembl\w*\b", r"\blegs? are? (weak|going)\b",
    r"\bout of breath\b", r"\bbreathless\b", r"\bshort of breath\b",
]

# The person is asking for something to be passed on.
MESSAGE_PATTERNS = [
    r"\b(tell|ask|call|phone|text|message|let)\b[^.?!]{0,40}\b"
    r"(son|daughter|wife|husband|family|him|her|them|doctor|nurse)\b",
    r"\bbring (me )?some\b", r"\bcan you tell\b", r"\bcould you tell\b",
    r"\blet (him|her|them) know\b", r"\bpass on\b",
    # Asking for something they cannot get for themselves.
    #
    # The agent is instructed to answer these with "it is noted, and the
    # guardian will be notified by email". This list is the half of that
    # promise the program has to keep. A pattern missing here is a
    # promise made to someone who then waits for shopping that nobody
    # was ever told about -- worse than never having offered at all.
    #
    # Loose on purpose, and the cost is asymmetric in the same direction
    # as the health list: one extra line in an email, against a request
    # that nobody hears.
    r"\b(i|we)\b[^.?!]{0,12}\b(would like|want|need)\b",
    r"\bcan you (get|order|bring|buy|arrange)\b",
    r"\b(get|bring|order|buy) me\b",
    r"\bgroceries\b", r"\bshopping\b", r"\bpizza\b", r"\btakeaway\b",
    r"\bprescription\b", r"\brun out of\b",
    r"\bnothing in the (house|fridge|cupboard)\b",
    r"\barrange it\b",
]

_URGENT = [re.compile(p, re.I) for p in URGENT_PATTERNS]
_HEALTH = [re.compile(p, re.I) for p in HEALTH_PATTERNS]
_MESSAGE = [re.compile(p, re.I) for p in MESSAGE_PATTERNS]


@dataclass
class RuleHit:
    category: Literal["urgent", "health", "message"]
    quote: str
    pattern: str


@dataclass
class RuleResult:
    hits: list[RuleHit] = field(default_factory=list)

    @property
    def urgent(self) -> bool:
        return any(h.category == "urgent" for h in self.hits)

    @property
    def any_hit(self) -> bool:
        return bool(self.hits)

    def of(self, category: str) -> list[RuleHit]:
        return [h for h in self.hits if h.category == category]


def check_rules(text: str) -> RuleResult:
    """Phrase-match one utterance. Never raises."""
    result = RuleResult()
    if not text or not text.strip():
        return result
    quote = text.strip()
    for category, patterns in (("urgent", _URGENT), ("health", _HEALTH),
                               ("message", _MESSAGE)):
        for pattern in patterns:
            if pattern.search(quote):
                result.hits.append(RuleHit(category, quote, pattern.pattern))
                break        # one hit per category per utterance is enough
    return result


# ── Layer 2: structured LLM assessment ───────────────────────────────

class SafetyAssessment(BaseModel):
    """0 normal · 1 observe · 2 follow up · 3 notify · 4 urgent."""

    level: Literal[0, 1, 2, 3, 4] = 0
    concern_detected: bool = False
    reason: str | None = None
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    guardian_notification_recommended: bool = False
    immediate_attention_recommended: bool = False
    message_to_pass_on: str | None = Field(
        default=None,
        description="What the person explicitly asked to be relayed, in their own words.")


class GuardianReport(BaseModel):
    """The factual content of the email. No diagnosis, no invention."""

    subject: str
    summary: str = Field(description="Two or three plain sentences of what happened.")
    reported_items: list[str] = Field(
        default_factory=list,
        description="Each thing the guardian should know, one per line.")
    quotes: list[str] = Field(
        default_factory=list,
        description="Verbatim lines from the transcript supporting the above.")


NOTIFY_LEVEL = 3       # assessment at or above this notifies the guardian


def should_notify(rules: RuleResult, assessment: SafetyAssessment | None) -> bool:
    """Either layer can raise a concern; both must be quiet to stay silent."""
    if rules.urgent:
        return True
    if rules.of("message"):
        return True
    if assessment is None:
        return False
    if assessment.immediate_attention_recommended:
        return True
    if assessment.guardian_notification_recommended:
        return True
    return assessment.level >= NOTIFY_LEVEL


def severity_of(rules: RuleResult, assessment: SafetyAssessment | None) -> str:
    if rules.urgent or (assessment and assessment.immediate_attention_recommended):
        return "URGENT"
    if assessment and assessment.level >= NOTIFY_LEVEL:
        return "ATTENTION"
    if rules.of("health"):
        return "ATTENTION"
    return "INFO"
