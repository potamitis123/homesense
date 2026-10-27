"""CLASSIFY and INTERPRET: the only two places a model is used.

Between them sits engine.py, which the model never touches. The planner
chooses an operation from a closed list and never sees data; the narrator sees
the operation's output and never sees the graph. That separation is what keeps
a local model honest — it cannot quietly invent a number, because it is not
the thing doing arithmetic.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from .schema import render_operations, validate_plan

# Measured on this machine, narrating a real result: llama3.2 answered in
# 11.4s with the required structure intact; qwen3.5:4b returned nothing usable;
# gemma2:9b did not finish inside 180s. gemma2:9b is the better reader of Greek
# and is worth passing with --model when the GPU is not also holding the
# Sentinel models, so the timeout below leaves room for it.
DEFAULT_MODEL = "llama3.2:latest"
# Cloud models are reachable by name -- --model gpt-4o -- but never default:
# they send household speech off this machine, which the local path does not.
DEFAULT_HOST = "http://localhost:11434"


# Seconds to wait before trying again after a rate-limit refusal; None marks
# the last attempt, after which the refusal is reported like any other error.
RATE_LIMIT_PAUSES = (5, 15, 30, 60, None)


def _is_openai(model: str) -> bool:
    return model.lower().startswith(("gpt", "o1", "o3", "o4"))


def _call_openai(model: str, prompt: str, as_json: bool,
                 timeout: float = 120.0) -> str | None:
    """A cloud model, for comparison against the local default.

    Reuses the key helper the LLM report already uses, so there is no second
    place for a credential to live. Note what this means: the prompt carries
    household utterances, so choosing a gpt- model sends them off the machine.
    Everything else in this package runs locally.
    """
    try:
        from openai import OpenAI
        import sys as _sys
        _root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from misc.secrets import openai_api_key
    except Exception as exc:
        print(f"  [llm] OpenAI unavailable: {exc}")
        return None

    key = openai_api_key()
    if not key:
        print("  [llm] no OPENAI_API_KEY configured")
        return None
    client = OpenAI(api_key=key, timeout=timeout)
    kwargs = {"model": model,
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.0 if as_json else 0.3}
    if as_json:
        kwargs["response_format"] = {"type": "json_object"}
    else:
        kwargs["max_tokens"] = 700
    # A refusal for being over the rate limit is not an answer, and it is not
    # a failure either: waiting cures it. Reading a day for incidents sends
    # some two hundred thousand tokens in twenty seconds, which is the whole of
    # a per-minute allowance, and two days read back to back had 154 calls
    # refused -- each of which the layer would have recorded as five minutes
    # nobody read. So this one error is waited out; every other is reported.
    for pause in RATE_LIMIT_PAUSES:
        try:
            resp = client.chat.completions.create(**kwargs)
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            if type(exc).__name__ != "RateLimitError" or pause is None:
                print(f"  [llm] {type(exc).__name__}: {exc}")
                return None
            time.sleep(pause)
    return None


def _call(model: str, prompt: str, host: str, as_json: bool,
          timeout: float = 300.0) -> str | None:
    if _is_openai(model):
        return _call_openai(model, prompt, as_json)
    # num_predict and repeat_penalty are not tuning knobs here, they are
    # guards. A small model handed several similar quotes will copy one of
    # them until it runs out of context; capping the answer and penalising
    # repetition bounds that failure instead of printing it.
    options = {"temperature": 0.0 if as_json else 0.3}
    if not as_json:
        options.update({"num_predict": 700, "repeat_penalty": 1.25})
    body = {"model": model, "prompt": prompt, "stream": False,
            "options": options}
    if as_json:
        body["format"] = "json"
    req = urllib.request.Request(
        f"{host}/api/generate", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("response")
    except Exception as exc:
        print(f"  [llm] {type(exc).__name__}: {exc}")
        return None


# ── CLASSIFY ────────────────────────────────────────────────────────

PLANNER_PROMPT = """You turn a question about a household into ONE operation.

You have no data. You only choose which operation would answer the question,
and with what arguments.

PEOPLE IN THIS HOUSEHOLD: {people}
DAYS AVAILABLE (use one of these exactly, as the `day` argument): {daylist}
"today" means {today}. "yesterday" means {yesterday}.

OPERATIONS:
{operations}

METRICS usable as the `metric` argument:
  per person: segment_count, word_count, speech_episode_count, presence_evidence_count
  per pair:   conversation_episode_count, interaction_event_count,
              copresence_candidate_count, turn_balance

Return JSON only:
{{"operation": "<name>", "arguments": {{...}}, "why": "<one short sentence>"}}

RULES

1. NEVER NARROW WHAT THE QUESTION DID NOT NARROW.
   - No person named in the question -> pass NO person argument.
   - No day named in the question -> pass NO day argument.
   Adding either invents a scope the asker did not ask for, and turns a
   question about the household into an answer about one person on one day.

2. Use only names from PEOPLE, or the family words that map to them
   (father, dad, husband / mother, mum, wife / son, child). Never invent one.

3. Choosing the operation:
   - the home, the family, "they", "everyone", how things are going, whether
     it is happy or tense  -> family_summary
   - what was discussed, subjects, topics                -> household_topics
     (or person_topics if, and only if, ONE person is named)
   - was someone angry / happy / sad at some point       -> emotion_evidence
     with emotion one of: angry, happy, sad, neutral
   - fights, rows, arguments, friction, tense moments    -> tension_moments
   - bonding, closeness, warmth, happy or funny moments  -> warm_moments
   - problems, grievances, what they complain about      -> complaints
   - WHETHER a particular thing happened, or WHEN it started and ended:
     a quarrel, a complaint about some subject, a worry, a making-up,
     a rebuff, or simply "what happened"                 -> incidents
     with kind one of: quarrel, complaint, worry, reconciliation,
     warm_moment, rebuff (omit kind for "what happened")
   - two named people and how they get on                -> dyad_summary
   - change over time, trend, "since when"               -> trend

   emotion_evidence is about how a voice SOUNDED and is unreliable on its own.
   For what actually happened between people, prefer tension_moments,
   warm_moments and complaints: those read the words.

   tension_moments, warm_moments and complaints RANK exchanges by a score and
   give no end time. When the question asks whether something happened, names
   a subject ("about work", "about the car"), or wants a start and an end,
   use incidents.

   An incident is something BETWEEN people. Whether a subject was merely
   mentioned ("did anyone talk about the dentist") is not one: use
   topic_evidence with that subject as the topic.

WORKED EXAMPLES

Q: Is this a happy home?
{{"operation": "family_summary", "arguments": {{}}, "why": "the household as a whole, over the whole window"}}

Q: What were they talking about today?
{{"operation": "household_topics", "arguments": {{"day": "{today}"}}, "why": "everyone, on the named day"}}

Q: Was Ilyas angry today?
{{"operation": "emotion_evidence", "arguments": {{"person": "Ilyas", "emotion": "angry", "day": "{today}"}}, "why": "one named person, one named emotion, one named day"}}

Q: How do the father and son get on?
{{"operation": "dyad_summary", "arguments": {{"person_a": "father", "person_b": "son"}}, "why": "two people by role, no day named"}}

Q: What does Rafael talk about?
{{"operation": "person_topics", "arguments": {{"person": "Rafael"}}, "why": "one named person, no day named"}}

Q: Find me the fights.
{{"operation": "tension_moments", "arguments": {{}}, "why": "the sharpest exchanges, nobody and no day named"}}

Q: Were there any happy moments between the mother and the boy?
{{"operation": "warm_moments", "arguments": {{}}, "why": "warm exchanges; two people are named but this operation takes one person at most, so neither is passed"}}

Q: What are the main problems in this family?
{{"operation": "complaints", "arguments": {{}}, "why": "what is complained about and where it points, over the whole window"}}

Q: Was there a quarrel today? When did it start and finish?
{{"operation": "incidents", "arguments": {{"kind": "quarrel", "day": "{today}"}}, "why": "whether a quarrel happened and its start and end, on the named day"}}

Q: Did Voula and Rafael quarrel yesterday?
{{"operation": "incidents", "arguments": {{"kind": "quarrel", "person": "Voula", "person_b": "Rafael", "day": "{yesterday}"}}, "why": "whether something happened between two named people: both are passed"}}

Q: Did anyone complain about their work?
{{"operation": "incidents", "arguments": {{"kind": "complaint"}}, "why": "whether a complaint on a named subject was made; nobody and no day named"}}

Q: Did they make up afterwards?
{{"operation": "incidents", "arguments": {{"kind": "reconciliation"}}, "why": "whether a making-up happened"}}

QUESTION: {question}

JSON:"""


DAY_WORDS = ("today", "tonight", "yesterday", "this morning", "this evening",
             "this afternoon", "last night")

# Words in a question that name a kind of incident, for the fallback planner.
# Making-up comes first: "did they make up after the argument" names two kinds
# and is asking about the first.
INCIDENT_WORDS = (
    ("reconciliation", ("make up", "made up", "making up", "reconcil", "apolog")),
    ("quarrel", ("quarrel", "fight", "fought", "argument", "argue", "argued",
                 "dispute", "conflict")),
    ("complaint", ("complain", "complaint", "grievance")),
    ("worry", ("worry", "worried", "worries", "anxious", "afraid", "distress")),
    ("rebuff", ("rebuff", "brush off", "brushed off", "turned away")),
)

# A question that asks WHETHER something happened or WHEN, as opposed to one
# asking for a ranking or a description.
WHETHER_OR_WHEN = re.compile(
    r"\b(was there|were there|did|when did|start|started|finish|finished|end|"
    r"ended|how long|what happened)\b")
# A question that names what the thing was ABOUT. Only such a question needs
# the incidents in scope narrowed by a model; see ask.py.
NAMES_A_SUBJECT = re.compile(r"\b(about|regarding|concerning|because of|due to)\b")
# The operations that rank exchanges by a score. A whether-or-when question
# sent to one of these is redirected to the incident list in tighten().
RANKING_OPERATIONS = ("tension_moments", "warm_moments", "complaints")


def incident_kind_asked(question: str) -> str | None:
    """The kind of incident a whether-or-when question is about, if any."""
    q = question.lower()
    if not WHETHER_OR_WHEN.search(q):
        return None
    for kind, words in INCIDENT_WORDS:
        if any(w in q for w in words):
            return kind
    return None


def names_a_subject(question: str) -> bool:
    return bool(NAMES_A_SUBJECT.search(question.lower()))


# Operations that need a person, and what to use instead when the question
# named nobody.
WITHOUT_PERSON = {
    "person_topics": "household_topics",
    "person_profile": "family_summary",
    "room_profile": "family_summary",
    "timeline": "family_summary",
}


def _names_a_person(question: str, people: list[str], roles: dict) -> bool:
    q = question.lower()
    if any(p.lower() in q for p in people):
        return True
    for spec in (roles or {}).values():
        for word in spec.get("words", []):
            if re.search(rf"\b{re.escape(word.lower())}\b", q):
                return True
    return False


def _names_a_day(question: str, days: list[str]) -> bool:
    q = question.lower()
    return (any(w in q for w in DAY_WORDS)
            or any(d in question for d in (days or []))
            or bool(re.search(r"\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|"
                              r"sep|oct|nov|dec)", q)))


def tighten(plan: dict, question: str, people: list[str], roles: dict,
            days: list[str]) -> dict:
    """Strip any narrowing the question did not ask for.

    The planner's recurring failure is not choosing a wrong operation, it is
    quietly adding scope: answering "is this a happy home?" about one person
    on one day. A prompt rule helps; this enforces it, because the question
    itself is the ground truth about what was asked.
    """
    args = dict(plan.get("arguments") or {})
    op = plan["operation"]
    notes = []

    if not _names_a_day(question, days) and args.pop("day", None) is not None:
        notes.append("dropped a day the question did not name")

    if not _names_a_person(question, people, roles):
        removed = [k for k in ("person", "person_a", "person_b", "subject")
                   if args.pop(k, None) is not None]
        if args.pop("members", None) is not None:
            removed.append("members")
        if removed:
            notes.append("dropped a person the question did not name")
        if op in WITHOUT_PERSON:
            notes.append(f"{op} needs a person, so used {WITHOUT_PERSON[op]}")
            op = WITHOUT_PERSON[op]

    # A question that names two residents is about what was between THEM. The
    # planner passed only the first, and every incident of that one person
    # then answered for the pair. Set here from the question itself.
    if op == "incidents":
        named = [p for p in people if re.search(rf"\b{re.escape(p)}\b", question, re.I)]
        if len(named) == 2 and {args.get("person"), args.get("person_b")} != set(named):
            args["person"], args["person_b"] = named
            notes.append("the question names two people, so both are passed")

    # "Did anyone complain about X", "was there a quarrel and when did it end"
    # are questions about incidents, and a small planner keeps answering them
    # with a ranking operation, which has no subject and no end time. Measured:
    # the local model sent the work-complaint question to `complaints`, where
    # the complaint is not, on the same day GPT-4o-mini sent it to `incidents`.
    kind = incident_kind_asked(question)
    if kind and op in RANKING_OPERATIONS:
        notes.append(f"asks whether or when a {kind} happened, so used "
                     f"incidents and not {op}")
        op = "incidents"
        args = {k: v for k, v in args.items() if k in ("person", "day")}
        args["kind"] = kind

    if notes:
        plan = dict(plan)
        plan["operation"] = op
        plan["arguments"] = args
        plan["why"] = (plan.get("why") or "") + "  [" + "; ".join(notes) + "]"
    return plan


def plan(question: str, people: list[str], days: list[str],
         model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST,
         roles: dict = None) -> dict:
    """Choose an operation. Falls back to a keyword rule if the model fails."""
    prompt = PLANNER_PROMPT.format(
        people=", ".join(people) or "(none)",
        daylist=", ".join(days) or "(none)",
        today=days[-1] if days else "(unknown)",
        yesterday=days[-2] if len(days) > 1 else "(not in the window)",
        operations=render_operations(), question=question)
    raw = _call(model, prompt, host, as_json=True)
    if raw:
        try:
            candidate = json.loads(raw)
            ok, why = validate_plan(candidate, people, days)
            if ok:
                return tighten(candidate, question, people, roles or {}, days)
            print(f"  [planner] rejected: {why}")
        except json.JSONDecodeError:
            print("  [planner] model did not return JSON")
    return tighten(fallback_plan(question, people, days),
                   question, people, roles or {}, days)


def fallback_plan(question: str, people: list[str],
                  days: list[str] | None = None) -> dict:
    """A deterministic plan, so a model failure degrades instead of stopping.

    Crude on purpose: it exists to keep the tool usable when Ollama is down,
    not to be a second planner.
    """
    q = question.lower()
    named = [p for p in people if p.lower() in q]
    # Resolved first, because several branches below pass it on.
    args = {}
    if days and any(w in q for w in ("today", "tonight")):
        args["day"] = days[-1]
    elif days and len(days) > 1 and "yesterday" in q:
        args["day"] = days[-2]

    # What happened between people, read from the words. These come before the
    # rules below on purpose: "were there happy moments" answered from the
    # emotion classifier, or "find me the fights" answered with a week's
    # averages, are exactly the answers these operations exist to replace.
    # One person is passed only when one is named; "the fights between A and B"
    # is a question about exchanges, not about a pair's counts.
    one = {"person": named[0]} if len(named) == 1 else {}
    # Whether a particular thing happened, or when: the incident list. Before
    # the ranking rules, because "was there a quarrel, and when did it end"
    # is not answered by the eight sharpest exchanges of the week.
    kind = incident_kind_asked(question)
    if kind:
        return {"operation": "incidents",
                "arguments": {"kind": kind, **one, **args},
                "why": f"whether a {kind} happened, or when (fallback rule)"}
    if "what happened" in q:
        return {"operation": "incidents", "arguments": {**one, **args},
                "why": "what happened between people (fallback rule)"}
    if any(w in q for w in ("fight", "fought", "argument", "argue", "argued",
                            "row", "rows", "quarrel", "clash", "conflict",
                            "tension", "tense", "friction", "shout")):
        return {"operation": "tension_moments", "arguments": {**one, **args},
                "why": "asking for arguments or tense moments (fallback rule)"}
    if any(w in q for w in ("bond", "warm", "affection", "close", "closeness",
                            "happy moment", "good moment", "laugh", "laughter",
                            "fun", "joke", "joking", "tender", "playful")):
        return {"operation": "warm_moments", "arguments": {**one, **args},
                "why": "asking for warmth or bonding (fallback rule)"}
    if any(w in q for w in ("problem", "complain", "complaint", "grievance",
                            "issue", "wrong with")):
        return {"operation": "complaints", "arguments": {**one, **args},
                "why": "asking what the household complains about (fallback rule)"}

    if len(named) >= 2:
        return {"operation": "dyad_summary",
                "arguments": {"person_a": named[0], "person_b": named[1]},
                "why": "two residents named in the question (fallback rule)"}
    if named and any(w in q for w in ("angry", "anger", "cross", "upset",
                                      "happy", "sad", "mood", "shout")):
        emo = ("angry" if any(w in q for w in ("angry", "anger", "cross", "shout"))
               else "sad" if "sad" in q or "upset" in q else "happy")
        return {"operation": "emotion_evidence",
                "arguments": {"person": named[0], "emotion": emo, **args},
                "why": "asking whether one person sounded a certain way (fallback rule)"}
    if named:
        interest = any(w in q for w in (
            "interest", "like", "likes", "talk", "talks", "about", "subject",
            "profession", "career", "job", "suited", "suit", "hobby", "enjoy"))
        op = "person_topics" if interest else "person_profile"
        return {"operation": op,
                "arguments": {"person": named[0], "top_k": 12, **args},
                "why": "one resident named in the question (fallback rule)"}
    if any(w in q for w in ("talk", "talking", "about", "subject", "topic",
                            "discuss", "said", "saying")):
        return {"operation": "household_topics", "arguments": args,
                "why": "what was being discussed (fallback rule)"}
    # Anything about the home, the family or how it is doing is a question
    # about the household as a unit, which is what nearly every question here
    # turns out to be. overview describes the dataset, not the family, and is
    # the wrong answer to "is this a happy home".
    if any(w in q for w in ("happy", "happiness", "home", "house", "family",
                            "they", "everyone", "together", "mood", "wellbeing",
                            "well-being", "atmosphere", "tense", "tension",
                            "doing", "getting on", "relationship")):
        return {"operation": "family_summary", "arguments": args,
                "why": "a question about the household as a unit (fallback rule)"}
    return {"operation": "overview", "arguments": {},
            "why": "no resident named; describing the window (fallback rule)"}


# ── SELECT ──────────────────────────────────────────────────────────
#
# Only the incidents operation has this stage. Its result is a list of things
# that happened, and which of them a question is asking about is a judgement
# about meaning -- "work" against "her grievances at the school" -- that no
# filter in engine.py can make. The model is shown the list and returns
# numbers; it sees no utterances and writes no prose, and the numbers are
# checked against the list before anything is done with them.

SELECT_PROMPT = """Below is a list of events that were recorded in a home {span}, one per line: id, time span, who, kind, what it concerns, and a sentence.

QUESTION: {question}

Return the ids of the events that answer the question. Return an empty list if none does. Do not include an event merely because it shares a word with the question.

Return JSON only: {{"ids": [ids]}}

EVENTS:
{events}"""


def select_incidents(question: str, rows: list[dict],
                     model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST,
                     memory: str = "") -> list[str] | None:
    """Which incidents answer the question. None if the model did not say.

    The model is given small numbers, not the store's ids: an id like
    incident::2026-10-02::0007 is long enough for a small model to mistype,
    and a mistyped id would silently drop the answer.
    """
    if not rows:
        return []
    days = {r.get("day") for r in rows}
    lines = "\n".join(
        f"{n} | {r.get('day')} {r.get('start')}-{r.get('end')} | "
        f"{'+'.join(r.get('participants') or [])} | {r.get('kind')} | "
        f"{r.get('about')} | {r.get('what')}" for n, r in enumerate(rows, 1))
    head = ""
    if memory.strip():
        head = ("WHAT IS KNOWN ABOUT THIS HOUSEHOLD (written by the residents):\n"
                f"{memory.strip()}\n\n")
    prompt = head + SELECT_PROMPT.format(
        span="on one day" if len(days) == 1 else "over several days",
        question=question, events=lines)
    raw = _call(model, prompt, host, as_json=True)
    if not raw:
        return None
    try:
        picked = json.loads(raw).get("ids")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(picked, list):
        return None
    chosen = []
    for value in picked:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= index <= len(rows) and rows[index - 1]["id"] not in chosen:
            chosen.append(rows[index - 1]["id"])
    return chosen


# ── INTERPRET ───────────────────────────────────────────────────────

NARRATOR_PROMPT = """You answer questions about a household from sensor-derived
evidence. You are given the QUESTION and the RESULT of one database operation.
The result is all you know. You did not gather it and you cannot extend it.

REASON FROM THE NUMBERS, NOT FROM THE QUOTES

The figures in RESULT are computed and exact. The quotes are in Greek and you
cannot read them reliably, so:

  * Base every claim on the counts, rates and distributions.
  * Cite quotes as support, by tag -- [E1], [E3] -- and nothing else.
  * NEVER retype, translate, or say what a quote means. You will get it wrong.
    "as in [E2]" is correct. "in [E2] she says something negative" is not.
  * If RESULT has no evidence at all, cite no tags and say which figures you
    are reasoning from.

WHEN RESULT CONTAINS "assessment"

That field is the computed verdict. FOLLOW IT. It has already compared the
rate against the person's own baseline, and it decides the answer:

  * if it says NOT above normal, your first sentence must say no
  * if it says somewhat above, say so, and no more than that
  * if it says clearly above, say yes

Do not recompute the comparison, soften it, or reach a different conclusion
from it. "clustered_in_time" being false is further reason to say no:
something real arrives in a burst, the background rate is scattered.

WHEN RESULT CONTAINS "incidents"

Each one is something that happened between people, listed from the record
before the question was asked: its kind, who was in it, what it was about, and
the times of the first and last recorded line that show it.

  * Say whether it happened and between whom, and give the start and the end
    exactly as written. Copy the times; do not round or recompute them.
  * If "incidents" is empty, say that nothing of the kind is recorded. If
    "days_never_read_for_incidents" is not empty, say that those days were
    never read for incidents, which is not the same as nothing happening.
  * "about" and "what" are in English and may be put in your own words.

CONFIDENCE

If RESULT contains "suggested_confidence", use exactly that value and give the
reason in a few words. Otherwise: high for hundreds of utterances with a large
difference, moderate for tens or a modest difference, low for under about
twenty utterances or a difference near baseline. A large sample does NOT make
a weak effect high confidence.

WHAT THIS DATA IS

Short recordings triggered by sound in a home, transcribed automatically and
attributed to a speaker automatically. Both steps make mistakes. Silence in
the record means nothing was recorded, never that nothing happened.

Emotion labels come from an audio model. Its aggregate proportions over many
clips are broadly plausible for this household, but a single clip's label is
not reliable. Reason from a DISTRIBUTION over many utterances; never rest a
claim on one clip's emotion.

FORMAT

Answer the question directly in the first sentence -- commit to a view rather
than refusing. Then 2-5 sentences giving the figures that support it, with
tags inline. Do not include an evidence list; it is printed for you. End with
one line beginning "Confidence:" giving high, moderate or low and why in a few
words.

QUESTION: {question}

RESULT:
{result}

ANSWER:"""


def narrate(question: str, result: dict, model: str = DEFAULT_MODEL,
            host: str = DEFAULT_HOST) -> str:
    # Evidence is tagged and the model is told to cite the tag, never the
    # words. It was retyping Greek quotes and mangling them -- "μονοσκάρι"
    # came back as "μονοσκάri" -- which quietly falsified the one thing an
    # evidence-backed answer must get exactly right. ask.py prints the real
    # utterances from the record afterwards.
    result = dict(result)
    for i, item in enumerate(result.get("evidence") or [], 1):
        item = dict(item)
        item["tag"] = f"E{i}"
        (result.setdefault("_tagged", [])).append(item)
    if "_tagged" in result:
        result["evidence"] = result.pop("_tagged")
    payload = json.dumps(result, ensure_ascii=False, indent=1, default=str)
    # A result is small by construction, but a pathological one must not be
    # allowed to blow the context window and silently truncate the evidence.
    if len(payload) > 24000:
        trimmed = dict(result)
        trimmed["evidence"] = (result.get("evidence") or [])[:6]
        payload = json.dumps(trimmed, ensure_ascii=False, indent=1, default=str)
    prompt = NARRATOR_PROMPT.format(question=question, result=payload)
    answer = _call(model, prompt, host, as_json=False)
    return answer or "(the model did not respond; the raw result is above)"
