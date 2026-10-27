"""What the graph contains, and what may be asked of it.

Both vocabularies are deliberately closed. The planner LLM chooses from the
operations listed here and nothing else, so a malformed or imaginative plan is
rejected by validation rather than executed. That is the whole reason a local
model can drive this safely: it never writes a query, it picks one.
"""

from __future__ import annotations

import re

# ── node kinds ──────────────────────────────────────────────────────
PERSON = "person"
DAY = "day"
ROOM = "room"
TOPIC = "topic"
SEGMENT = "segment"      # one diarised utterance; the unit of evidence
EPISODE = "episode"      # a Level 3 social episode
EXCHANGE = "exchange"    # one Level 1.5 reading: what an exchange meant
INCIDENT = "incident"    # something that happened between people, with a start and an end

# What an incident may be. Closed for the same reason the operations are: the
# reader chooses from these and nothing else, so two days' incidents can be
# counted together. Called incidents because "event" already means a recorded
# clip everywhere in this codebase.
INCIDENT_KINDS = ("quarrel", "complaint", "worry", "reconciliation",
                  "warm_moment", "rebuff")

# ── edge kinds ──────────────────────────────────────────────────────
SPOKE = "spoke"                  # person   -> segment
MENTIONS = "mentions"            # segment  -> topic
ON_DAY = "on_day"                # segment/episode/exchange -> day
IN_ROOM = "in_room"              # segment/episode/exchange -> room
PARTICIPATED = "participated"    # person   -> episode/exchange
EVIDENCED_BY = "evidenced_by"    # exchange/incident -> segment

# Metrics carried per day. Dyad metrics come from Level 4 pair_dynamics,
# person metrics from individual_patterns.
DYAD_METRICS = [
    "conversation_episode_count",
    "interaction_event_count",
    "copresence_candidate_count",
    "shared_activity_candidate_count",
    "turn_balance",
]
PERSON_METRICS = [
    "speech_episode_count",
    "presence_evidence_count",
    "segment_count",
    "word_count",
]

# Carried for people and pairs alike, from the Level 1.5 reading. Listed so a
# trend or a comparison may be asked for them: "has tension risen?" is the
# question a long record is kept for, and it was being rejected as an unknown
# metric before these were named here.
SEMANTIC_METRICS = [
    "warmth", "joy", "withdrawal", "tension",
    "tension_peak", "tension_peak_count", "tension_repaired_count",
    "interpreted_exchanges", "complaint_count",
    "complaints_someone_present", "complaints_family_absent",
    "complaints_outsider", "complaints_circumstance",
]

# Emotion is available per day but is deliberately NOT in the metric lists
# above. Blind annotation of this household put the emotion model at 12.5%
# balanced accuracy, predicting "sad" almost regardless of input, so any
# conclusion resting on it would be confident and wrong. It is carried
# through as a separate, explicitly flagged channel instead.
EMOTION_IS_UNRELIABLE = (
    "The emotion model scored 12.5% balanced accuracy on blind annotation of "
    "this household and predicted 'sad' for nearly everything. Emotion figures "
    "must not be used to support a conclusion.")


# ── the operations a question may be turned into ────────────────────
#
# Each entry: what it answers, and the arguments it takes. The planner sees
# this table rendered into its prompt.

def resolve_person(token: str, roles: dict, people: list[str]) -> str | None:
    """Turn "the child", "his father", "Rafael" into a resident's name.

    Every question this system is asked is phrased in family terms rather than
    by name, so the role words are resolved here once and the operations only
    ever see a name.
    """
    if not token:
        return None
    t = str(token).strip().lower()
    for name in people:
        if t == name.lower():
            return name
    for name, spec in (roles or {}).items():
        if name not in people:
            continue
        if t in [w.lower() for w in spec.get("words", [])]:
            return name
    # "his son", "the mother", "my wife" -- match on the role word inside it
    for name, spec in (roles or {}).items():
        if name not in people:
            continue
        for word in spec.get("words", []):
            if re.search(rf"{re.escape(word.lower())}", t):
                return name
    return None


OPERATIONS = {
    "overview": {
        "answers": "What is in the data at all: people, days, volume of speech.",
        "args": [],
    },
    "family_summary": {
        "answers": "The household as a unit: every pair of residents side by "
                   "side, who talks with whom, what about, and how much each "
                   "person contributes. The default for any question about "
                   "'them', 'the family', or how the household is doing.",
        "args": ["day"],
    },
    "household_topics": {
        "answers": "What the household as a whole talked about, with who "
                   "raised each subject. Pass `day` for a single date.",
        "args": ["day", "top_k"],
    },
    "person_topics": {
        "answers": "What subjects one person returns to, ranked by how often, "
                   "with the days they occurred on. Pass `day` to restrict it "
                   "to a single date.",
        "args": ["person", "day", "top_k"],
    },
    "emotion_evidence": {
        "answers": "Whether someone sounded angry, happy or sad at some point: "
                   "the rate against their own baseline, and the actual "
                   "utterances, ranked by the model's confidence.",
        "args": ["person", "emotion", "day"],
    },
    "tension_moments": {
        "answers": "The sharpest exchanges in the window -- arguments, "
                   "criticism, complaints, raised voices -- ranked by how tense "
                   "the reading was, each with what was actually said. The "
                   "answer to 'find me the fights', 'when did they argue', "
                   "'moments of tension'.",
        "args": ["person", "day", "top_k"],
    },
    "warm_moments": {
        "answers": "The warmest exchanges in the window -- affection, praise, "
                   "humour, support, shared enjoyment -- each with what was "
                   "said. The answer to 'happy moments', 'bonding', 'when were "
                   "they close', 'good times'.",
        "args": ["person", "day", "top_k"],
    },
    "complaints": {
        "answers": "What the household complains about, and where the "
                   "complaints point: at someone in the room, at an absent "
                   "member of the family, at an outsider, or at a situation. "
                   "The answer to 'what are their problems', 'what do they "
                   "argue about', 'recurring grievances'.",
        "args": ["person", "day", "top_k"],
    },
    "incidents": {
        "answers": "Things that HAPPENED between people, each with who was in "
                   "it, what it was about, and when it STARTED and ENDED: a "
                   "quarrel, a complaint about something, a worry voiced, a "
                   "making-up, a warm moment, a rebuff. The answer to 'was "
                   "there a quarrel', 'did anyone complain about X', 'when "
                   "did it start and finish', 'what happened that day'. "
                   "`kind` is one of quarrel, complaint, worry, "
                   "reconciliation, warm_moment, rebuff; omit it for "
                   "everything that happened. For something between TWO "
                   "named people, pass both: `person` and `person_b`.",
        "args": ["kind", "person", "person_b", "day", "top_k"],
    },
    "topic_evidence": {
        "answers": "The actual utterances behind a subject, quoted, with "
                   "speaker, day and room.",
        "args": ["topic", "person"],
    },
    "person_profile": {
        "answers": "One person across the window: how much they spoke, where, "
                   "with whom, and what about.",
        "args": ["person"],
    },
    "dyad_summary": {
        "answers": "How two people interacted: conversations, co-presence, "
                   "turn balance, over the window.",
        "args": ["person_a", "person_b"],
    },
    "compare_people": {
        "answers": "Two people set side by side on one metric.",
        "args": ["person_a", "person_b", "metric"],
    },
    "trend": {
        "answers": "How one metric moved across the days of the window, with "
                   "the direction and the largest single shift.",
        "args": ["subject", "metric"],
    },
    "room_profile": {
        "answers": "Which rooms a person's speech happens in.",
        "args": ["person"],
    },
    "timeline": {
        "answers": "Day by day, what a person did and talked about.",
        "args": ["person"],
    },
}


def render_operations() -> str:
    """The operation table, as the planner prompt sees it."""
    lines = []
    for name, spec in OPERATIONS.items():
        args = ", ".join(spec["args"]) or "none"
        lines.append(f'- {name}(args: {args})\n    {spec["answers"]}')
    return "\n".join(lines)


def validate_plan(plan: dict, people: list[str],
                  days: list[str] | None = None) -> tuple[bool, str]:
    """Is this plan executable? Returns (ok, reason)."""
    if not isinstance(plan, dict):
        return False, "plan is not an object"
    op = plan.get("operation")
    if op not in OPERATIONS:
        return False, f"unknown operation {op!r}"
    args = plan.get("arguments") or {}
    if not isinstance(args, dict):
        return False, "arguments is not an object"

    # A person the model invented is the common failure, and it produces an
    # empty result that reads like a finding. Catch it here instead.
    for key in ("person", "person_a", "person_b"):
        who = args.get(key)
        if who and who not in people:
            return False, f"{who!r} is not one of {people}"
    metric = args.get("metric")
    if metric and metric not in DYAD_METRICS + PERSON_METRICS + SEMANTIC_METRICS:
        return False, f"unknown metric {metric!r}"
    # An invented kind matches nothing and comes back as "no such incident",
    # which reads like a finding about the household.
    kind = args.get("kind")
    if op == "incidents" and kind and str(kind).lower() not in INCIDENT_KINDS:
        return False, f"unknown kind of incident {kind!r}"
    # A day outside the window returns nothing, which reads like a finding
    # rather than a mistake. Reject it here so the planner is told instead.
    day = args.get("day")
    if day and days and day not in days:
        return False, f"{day!r} is not a day in this window ({days[0]}..{days[-1]})"
    return True, ""
