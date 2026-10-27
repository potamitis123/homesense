"""The EXECUTE stage: deterministic answers over the graph.

No model runs in this file. Every operation is ordinary Python over the store,
so a number in an answer can be recomputed by hand and a bug here is a bug in
arithmetic rather than in a prompt. The planner decides *which* operation
runs; this decides what it returns.

Every result carries `evidence`: a list of utterances with speaker, day, room,
originating event id and audio file. That is what the narrator is required to
cite, and what makes a claim checkable.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

from .schema import DYAD_METRICS, EMOTION_IS_UNRELIABLE, PERSON_METRICS

STORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store")
DAYS_DIR = os.path.join(STORE_DIR, "days")
INDEX_PATH = os.path.join(STORE_DIR, "index.json")

MAX_EVIDENCE = 12          # what a narrator can actually read and cite
MAX_INCIDENTS = 40         # what a question may be asked to choose from


class Graph:
    def __init__(self, data: dict):
        self.data = data
        self.people = data["people"]
        self.days = data["days"]
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.segments = [n for n in data["nodes"] if n["kind"] == "segment"]
        self.episodes = [n for n in data["nodes"] if n["kind"] == "episode"]
        self.exchanges = [n for n in data["nodes"] if n["kind"] == "exchange"]
        self.incidents = [n for n in data["nodes"] if n["kind"] == "incident"]
        # day -> how that day was read for incidents. A day missing from here
        # was never read, which is a different thing from a day read and found
        # to hold none, and every answer has to be able to tell them apart.
        self.incident_layer = data.get("incident_layer") or {}
        self.dyad_series = data["dyad_series"]
        self.person_series = data["person_series"]

        # segment -> topics, and exchange -> the utterances it was read from,
        # both built once
        self._topics_of = defaultdict(list)
        self._said_in = defaultdict(list)
        for e in data["edges"]:
            if e["kind"] == "mentions":
                self._topics_of[e["src"]].append(e["dst"].split("::", 1)[1])
            elif e["kind"] == "evidenced_by":
                self._said_in[e["src"]].append(e["dst"])

    @classmethod
    def load(cls, since: str = None, until: str = None,
             days: list = None) -> "Graph":
        """Merge day files into one in-memory graph.

        Days are immutable and disjoint, so merging them is concatenation:
        nodes are unioned by id (a person, a room or a topic recurs across
        days), edges are appended, and the per-day series stay keyed by date.
        A question scoped to one day therefore opens one file, and a question
        about three years opens as many as it needs and no more.
        """
        if not os.path.exists(INDEX_PATH):
            raise FileNotFoundError(
                "No store yet. Build one first:\n"
                "    python -m graphrag.ingest --catch-up")
        with open(INDEX_PATH, encoding="utf-8") as fh:
            index = json.load(fh)

        wanted = days or index.get("days") or []
        if since:
            wanted = [d for d in wanted if d >= since]
        if until:
            wanted = [d for d in wanted if d <= until]
        if not wanted:
            raise FileNotFoundError(
                f"No stored day matches that range; the store holds "
                f"{index.get('first_day')} .. {index.get('last_day')}.")

        nodes, edges = {}, []
        dyad_series, person_series = defaultdict(dict), defaultdict(dict)
        incident_layer = {}
        events = 0
        for date in wanted:
            path = os.path.join(DAYS_DIR, f"{date}.json")
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            events += payload.get("event_count", 0)
            for n in payload["nodes"]:
                nodes.setdefault(n["id"], n)
            edges.extend(payload["edges"])
            for pair, row in (payload.get("dyad_series") or {}).items():
                dyad_series[pair][date] = row
            for who, row in (payload.get("person_series") or {}).items():
                person_series[who][date] = row
            if payload.get("incident_layer"):
                incident_layer[date] = payload["incident_layer"]

        people = sorted({n["name"] for n in nodes.values() if n["kind"] == "person"})
        return cls({
            "window": {"start": wanted[0], "end": wanted[-1],
                       "event_count": events},
            "people": people,
            "roles": {k: v for k, v in (index.get("roles") or {}).items()
                      if k in people},
            "days": wanted,
            "nodes": list(nodes.values()),
            "edges": edges,
            "dyad_series": dict(dyad_series),
            "person_series": dict(person_series),
            "incident_layer": incident_layer,
            "counts": {
                "events": events,
                "segments": sum(1 for n in nodes.values() if n["kind"] == "segment"),
                "topics": sum(1 for n in nodes.values() if n["kind"] == "topic"),
                "episodes": sum(1 for n in nodes.values() if n["kind"] == "episode"),
                "exchanges": sum(1 for n in nodes.values() if n["kind"] == "exchange"),
                "incidents": sum(1 for n in nodes.values() if n["kind"] == "incident"),
                "edges": len(edges),
                "days": len(wanted),
            },
        })

    def topics_of(self, segment_id: str) -> list[str]:
        return self._topics_of.get(segment_id, [])

    def segments_of(self, person: str | None = None,
                    topic: str | None = None,
                    day: str | None = None) -> list[dict]:
        out = []
        for s in self.segments:
            if person and s["speaker"] != person:
                continue
            if day and s["day"] != day:
                continue
            if topic and topic not in self.topics_of(s["id"]):
                continue
            out.append(s)
        return out

    def exchanges_of(self, person: str | None = None,
                     day: str | None = None) -> list[dict]:
        """Interpreted exchanges, optionally one person's or one day's."""
        out = []
        for x in self.exchanges:
            if person and person not in (x.get("participants") or []):
                continue
            if day and str(x.get("start") or "")[:10] != day:
                continue
            out.append(x)
        return out

    def said_in(self, exchange_id: str) -> list[dict]:
        """The utterances one exchange was read from, as segment nodes."""
        return [self.nodes[sid] for sid in self._said_in.get(exchange_id, [])
                if sid in self.nodes]

    def dyad_key(self, a: str, b: str) -> str | None:
        """Level 4 names a pair alphabetically, joined by a double underscore."""
        for key in (f"{a}__{b}", f"{b}__{a}"):
            if key in self.dyad_series:
                return key
        return None


# ── evidence ────────────────────────────────────────────────────────

def as_evidence(segments: list[dict], limit: int = MAX_EVIDENCE) -> list[dict]:
    """Utterances trimmed to what a narrator needs to quote one.

    Deduplicated by text first. The recogniser repeats itself across clips --
    the same sentence can appear a dozen times -- and handing a small model a
    list of near-identical quotes sends it into a repetition loop that fills
    the answer with one line copied a hundred times. Distinct evidence is also
    simply better evidence: twelve copies of one sentence support a claim no
    more than one copy does.
    """
    seen, unique = set(), []
    for s in sorted(segments, key=lambda s: -s.get("words", 0)):
        key = " ".join((s.get("text") or "").lower().split())
        if key and key not in seen:
            seen.add(key)
            unique.append(s)
    ranked = unique[:limit]
    return [{
        "quote": s["text"],
        "speaker": s["speaker"],
        "day": s["day"],
        "room": s["room"],
        "event_id": s["event_id"],
        "audio_file": s.get("audio_file"),
    } for s in sorted(ranked, key=lambda s: s["timestamp"])]


def _series_values(series: dict, metric: str, days: list[str]) -> list:
    return [(series.get(d) or {}).get(metric) for d in days]


def _describe_trend(days: list[str], values: list) -> dict:
    """Direction across the window, and the largest single day-to-day shift.

    Deliberately modest: with a week of data a change-point test would be
    theatre. It reports what moved and by how much, and says so.
    """
    pairs = [(d, v) for d, v in zip(days, values) if isinstance(v, (int, float))]
    if len(pairs) < 2:
        return {"direction": "not enough days to say", "points": pairs}
    first_half = [v for _, v in pairs[: max(1, len(pairs) // 2)]]
    second_half = [v for _, v in pairs[len(pairs) // 2:]]
    a = sum(first_half) / len(first_half)
    b = sum(second_half) / len(second_half)
    biggest = max(
        ((pairs[i + 1][0], pairs[i + 1][1] - pairs[i][1])
         for i in range(len(pairs) - 1)),
        key=lambda t: abs(t[1]), default=(None, 0))
    if a == 0 and b == 0:
        direction = "flat at zero"
    elif b > a * 1.15:
        direction = "higher in the second half of the window"
    elif b < a * 0.85:
        direction = "lower in the second half of the window"
    else:
        direction = "roughly level across the window"
    return {
        "direction": direction,
        "first_half_mean": round(a, 2),
        "second_half_mean": round(b, 2),
        "largest_day_change": {"day": biggest[0], "change": round(biggest[1], 2)},
        "points": [{"day": d, "value": v} for d, v in pairs],
        "caution": ("A window this short shows movement, not trend. Treat any "
                    "direction as provisional." if len(pairs) <= 10 else None),
    }


# ── operations ──────────────────────────────────────────────────────

def overview(g: Graph, **_) -> dict:
    per_person = {}
    for p in g.people:
        segs = g.segments_of(person=p)
        per_person[p] = {
            "utterances": len(segs),
            "words": sum(s.get("words", 0) for s in segs),
            "days_heard": len({s["day"] for s in segs}),
        }
    return {
        "window": g.data["window"],
        "days": g.days,
        "people": per_person,
        "totals": g.data["counts"],
        "emotion_overall": emotion_mix(g.segments),
        "note": ("Only utterances the system attributed to a named resident "
                 "are counted. Speech it could not attribute is excluded."),
    }


def person_topics(g: Graph, person: str = None, top_k: int = 10,
                  day: str = None, **_) -> dict:
    segs = g.segments_of(person=person, day=day)
    counts, days_for, sample = Counter(), defaultdict(set), defaultdict(list)
    for s in segs:
        for t in g.topics_of(s["id"]):
            counts[t] += 1
            days_for[t].add(s["day"])
            sample[t].append(s)
    ranked = counts.most_common(int(top_k or 10))
    return {
        "person": person,
        "day": day or f"all {len(g.days)} days in the window",
        "utterances_examined": len(segs),
        "topics": [{
            "topic": t,
            "mentions": n,
            "days": sorted(days_for[t]),
            "day_count": len(days_for[t]),
        } for t, n in ranked],
        "emotion": emotion_mix(segs),
        "evidence": as_evidence(
            [s for t, _ in ranked[:4] for s in sample[t]]),
        "note": ("'mentions' counts utterances, not conversations. A subject "
                 "raised across several days is better supported than one "
                 "raised many times on a single day."),
    }


def emotion_mix(segments: list[dict]) -> dict:
    """The distribution of emotion labels over a set of utterances.

    Reported as a distribution, never as a verdict on any one utterance: the
    model's aggregate proportions for this household are broadly plausible
    while its per-clip label is not, so the only defensible unit is the mix.
    """
    counts = Counter(s.get("emotion") for s in segments if s.get("emotion"))
    total = sum(counts.values())
    if not total:
        return {"labelled_utterances": 0,
                "note": "no emotion labels on these utterances"}
    return {
        "labelled_utterances": total,
        "distribution": {k: round(v / total, 3) for k, v in counts.most_common()},
        "counts": dict(counts),
        "note": ("Proportions over many utterances. A single utterance's label "
                 "is not reliable and must not carry a claim on its own."),
    }


def subject_summary(g: Graph, members=None, day: str = None, **_) -> dict:
    """Any combination of residents: one alone, a pair, or the whole family.

    One operation rather than three, because the questions are asked the same
    way at every size -- how is he, how are those two, how is the family --
    and splitting it would only make the planner choose between near-identical
    options.
    """
    members = [m for m in (members or g.people) if m in g.people]
    if not members:
        return {"members": [], "result": "no residents named"}
    members = sorted(members)

    segs = [s for s in g.segments_of(day=day) if s["speaker"] in members]
    topics = Counter()
    by_person = {}
    for who in members:
        mine = [s for s in segs if s["speaker"] == who]
        t = Counter()
        for s in mine:
            t.update(g.topics_of(s["id"]))
        by_person[who] = {
            "role": (g.data.get("roles", {}).get(who) or {}).get("role"),
            "utterances": len(mine),
            "words": sum(x.get("words", 0) for x in mine),
            "rooms": dict(Counter(x["room"] for x in mine).most_common(4)),
            "top_topics": [k for k, _ in t.most_common(6)],
            "emotion": emotion_mix(mine),
        }
        topics.update(t)

    # Episodes in which EVERY member of this combination took part. For one
    # person that is simply their episodes; for the whole family it is the
    # times all three were in something together.
    shared = [ep for ep in g.episodes
              if set(members).issubset(set(ep.get("participants") or []))
              and (not day or (ep.get("start") or "").startswith(day))]

    pairs = {}
    if len(members) > 1:
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                key = g.dyad_key(a, b)
                if not key:
                    continue
                series = g.dyad_series[key]
                days = [day] if day else g.days
                totals = Counter()
                balances = []
                for d in days:
                    row = series.get(d) or {}
                    for m in DYAD_METRICS:
                        if m != "turn_balance" and isinstance(row.get(m), (int, float)):
                            totals[m] += row[m]
                    if isinstance(row.get("turn_balance"), (int, float)):
                        balances.append(row["turn_balance"])
                pairs[f"{a} & {b}"] = {
                    **dict(totals),
                    "turn_balance_mean": (round(sum(balances) / len(balances), 3)
                                          if balances else None),
                }

    # A computed verdict on tone, for the same reason emotion_evidence has
    # one: asked to weigh four proportions itself, the narrator reports them
    # back as a list and calls it high confidence. The comparison that matters
    # is warmth against distress, so it is made here.
    mix = emotion_mix(segs)
    dist = mix.get("distribution") or {}
    happy = dist.get("happy", 0.0)
    distress = dist.get("sad", 0.0) + dist.get("angry", 0.0)
    labelled = mix.get("labelled_utterances", 0)
    who = "this household" if len(members) == len(g.people) else " and ".join(members)
    if not labelled:
        tone, suggested = f"No emotion labels for {who}.", "low"
    else:
        shape = (f"across {labelled} utterances, {happy:.0%} sounded happy "
                 f"against {distress:.0%} sad or angry")
        if labelled < 30:
            tone = (f"For {who}, {shape} -- too little to characterise a "
                    f"household on.")
            suggested = "low"
        elif happy >= 2 * distress:
            tone = (f"For {who}, {shape}: markedly more warmth than distress, "
                    f"so on this evidence yes.")
            suggested = "moderate" if labelled < 300 else "high"
        elif happy > distress:
            tone = (f"For {who}, {shape}: more warmth than distress, though "
                    f"not markedly.")
            suggested = "moderate"
        else:
            tone = (f"For {who}, {shape}: distress at least matches warmth, "
                    f"which is worth attention.")
            suggested = "moderate" if labelled >= 100 else "low"

    return {
        "members": members,
        "is_whole_household": len(members) == len(g.people),
        "day": day or f"all {len(g.days)} days in the window",
        "assessment": tone,
        "suggested_confidence": suggested,
        "assessment_caveat": ("This describes how speech sounded to an audio "
                              "model, not anyone's wellbeing."),
        "per_person": by_person,
        "pairs": pairs,
        "pair_note": ("turn_balance closer to 0 means one of the two did most "
                      "of the talking; it does not say which."),
        "episodes_involving_all_members": len(shared),
        "episode_types": dict(Counter(ep.get("type") for ep in shared)),
        "shared_topics": [{"topic": t, "mentions": n} for t, n in topics.most_common(12)],
        "emotion_overall": emotion_mix(segs),
        "evidence": as_evidence(segs),
    }


def emotion_evidence(g: Graph, person: str = None, emotion: str = None,
                     day: str = None, **_) -> dict:
    """The utterances carrying one emotion label, for a person and/or a day.

    The counterpart to the distributions: those say how much, this says which.
    A question like "was he angry today" needs both -- the rate, so the answer
    is not built on one clip, and the clips themselves, so it can be checked
    by ear.
    """
    emotion = (emotion or "").strip().lower() or None
    scope = [s for s in g.segments_of(person=person, day=day)]
    hits = [s for s in scope if s.get("emotion") == emotion] if emotion else scope

    # Ranked by the model's own confidence: if these are to be listened to,
    # the strongest candidates should be at the top of the list.
    hits = sorted(hits, key=lambda s: -(s.get("emotion_confidence") or 0))
    confs = [s.get("emotion_confidence") for s in hits
             if isinstance(s.get("emotion_confidence"), (int, float))]

    rate = (len(hits) / len(scope)) if scope else None
    baseline = None
    if emotion:
        whole = g.segments_of(person=person)
        baseline = (sum(1 for s in whole if s.get("emotion") == emotion)
                    / max(1, len(whole)))

    # The verdict is computed here rather than left to the narrator. Asked to
    # compare a rate against a baseline, a small model reliably works out the
    # ratio and then mis-bands it -- 1.17x came back as "somewhat more than
    # usual" when it plainly is not. Thresholds are arithmetic, so they belong
    # on this side of the boundary like every other number.
    ratio = (rate / baseline) if (rate and baseline) else None
    # A complete sentence, not a set of fields to be assembled. Given parts,
    # the narrator composed them wrongly in both directions -- calling 1.17x
    # "more than usual", then calling four utterances "none". Handed a finished
    # sentence it restates it correctly, which is all that is wanted here.
    who = person or "anyone"
    counts = (f"{len(hits)} of {who}'s {len(scope)} utterances "
              f"{'today' if day else 'in this window'} were labelled "
              f"{emotion} ({rate:.1%})" if scope else "there were no utterances")
    usual = f"against a usual rate of {baseline:.1%}" if baseline else ""

    if ratio is None:
        assessment = f"{counts}. There is no baseline to compare it against."
        suggested = "low"
    elif len(hits) < 5:
        assessment = (f"{counts}, {usual}. That is too few occurrences to show "
                      f"anything either way, so the answer is no.")
        suggested = "low"
    elif ratio < 1.5:
        assessment = (f"{counts}, {usual} -- {ratio:.2f} times normal, which is "
                      f"ordinary variation and NOT an episode. The answer is no.")
        suggested = "low"
    elif ratio < 3.0:
        assessment = (f"{counts}, {usual} -- {ratio:.2f} times normal, somewhat "
                      f"above usual but not markedly so.")
        suggested = "moderate"
    else:
        assessment = (f"{counts}, {usual} -- {ratio:.2f} times normal, clearly "
                      f"above usual. The answer is yes.")
        suggested = "high" if len(hits) >= 20 else "moderate"

    # Something real arrives in a burst; the background rate is scattered.
    hours = sorted({(s["timestamp"] or "")[11:13] for s in hits})
    clustered = bool(hours) and (len(hours) <= max(2, len(hits) // 3))

    return {
        "person": person or "anyone",
        "emotion": emotion or "any",
        "day": day or f"all {len(g.days)} days in the window",
        "matching_utterances": len(hits),
        "utterances_in_scope": len(scope),
        "rate": round(rate, 4) if rate is not None else None,
        "baseline_rate_whole_window": round(baseline, 4) if baseline else None,
        "ratio_to_baseline": round(ratio, 2) if ratio else None,
        "assessment": assessment,
        "clustered_in_time": clustered,
        "suggested_confidence": suggested,
        "confidence_of_matches": {
            "max": max(confs) if confs else None,
            "median": sorted(confs)[len(confs) // 2] if confs else None,
        },
        "by_room": dict(Counter(s["room"] for s in hits).most_common()),
        "by_hour": dict(Counter((s["timestamp"] or "")[11:13] for s in hits).most_common()),
        "evidence": as_evidence(hits),
        "note": ("Compare 'rate' against 'baseline_rate_whole_window'. A label "
                 "on one clip is not reliable; a rate well above baseline, "
                 "clustered in time, is the thing worth attending to."),
    }


def household_topics(g: Graph, day: str = None, top_k: int = 12, **_) -> dict:
    """What everyone talked about, and who raised it.

    The question "what were they talking about" is about the household, not
    about one member of it. Answering it with one person's topics -- which is
    what a planner does when the vocabulary offers nothing better -- quietly
    drops two thirds of the household.
    """
    segs = g.segments_of(day=day)
    counts, by_speaker, days_for, sample = Counter(), defaultdict(Counter), defaultdict(set), defaultdict(list)
    for s in segs:
        for t in g.topics_of(s["id"]):
            counts[t] += 1
            by_speaker[t][s["speaker"]] += 1
            days_for[t].add(s["day"])
            sample[t].append(s)
    ranked = counts.most_common(int(top_k or 12))
    return {
        "day": day or f"all {len(g.days)} days in the window",
        "people_heard": sorted({s["speaker"] for s in segs}),
        "utterances_examined": len(segs),
        "topics": [{
            "topic": t,
            "mentions": n,
            "raised_by": dict(by_speaker[t]),
            "days": sorted(days_for[t]),
        } for t, n in ranked],
        "emotion": emotion_mix(segs),
        "emotion_by_person": {who: emotion_mix([s for s in segs if s["speaker"] == who])
                              for who in sorted({s["speaker"] for s in segs})},
        "evidence": as_evidence([s for t, _ in ranked[:5] for s in sample[t]]),
        "note": ("'raised_by' counts utterances per speaker, so a subject one "
                 "person raised alone is distinguishable from one the "
                 "household discussed together."),
    }


def topic_evidence(g: Graph, topic: str = None, person: str = None, **_) -> dict:
    segs = g.segments_of(person=person, topic=(topic or "").lower())
    return {
        "topic": topic,
        "person": person,
        "utterance_count": len(segs),
        "speakers": dict(Counter(s["speaker"] for s in segs)),
        "days": sorted({s["day"] for s in segs}),
        "evidence": as_evidence(segs),
    }


def person_profile(g: Graph, person: str = None, **_) -> dict:
    segs = g.segments_of(person=person)
    series = g.person_series.get(person, {})
    topics = Counter()
    for s in segs:
        topics.update(g.topics_of(s["id"]))

    partners = Counter()
    for ep in g.episodes:
        parts = ep.get("participants") or []
        if person in parts:
            for other in parts:
                if other != person:
                    partners[other] += 1

    return {
        "person": person,
        "utterances": len(segs),
        "words": sum(s.get("words", 0) for s in segs),
        "days_heard": sorted({s["day"] for s in segs}),
        "rooms": dict(Counter(s["room"] for s in segs).most_common()),
        "top_topics": [{"topic": t, "mentions": n} for t, n in topics.most_common(12)],
        "shared_episodes_with": dict(partners),
        "daily": {d: {k: v for k, v in (series.get(d) or {}).items()
                      if k in PERSON_METRICS} for d in g.days},
        "evidence": as_evidence(segs),
        "emotion_note": EMOTION_IS_UNRELIABLE,
    }


def dyad_summary(g: Graph, person_a: str = None, person_b: str = None, **_) -> dict:
    key = g.dyad_key(person_a, person_b)
    if key is None:
        return {"pair": [person_a, person_b],
                "result": "no interaction recorded between these two in this window"}
    series = g.dyad_series[key]
    totals = {m: 0 for m in DYAD_METRICS if m != "turn_balance"}
    balances = []
    for day in g.days:
        row = series.get(day) or {}
        for m in totals:
            totals[m] += row.get(m) or 0
        if isinstance(row.get("turn_balance"), (int, float)):
            balances.append(row["turn_balance"])

    shared = [ep for ep in g.episodes
              if person_a in (ep.get("participants") or [])
              and person_b in (ep.get("participants") or [])]
    shared_days = sorted({(ep.get("start") or "")[:10] for ep in shared if ep.get("start")})

    # What they talked about while together: topics from utterances by either,
    # on the days they actually shared an episode.
    topics = Counter()
    for s in g.segments:
        if s["speaker"] in (person_a, person_b) and s["day"] in shared_days:
            topics.update(g.topics_of(s["id"]))

    return {
        "pair": [person_a, person_b],
        "totals": totals,
        "turn_balance_mean": (round(sum(balances) / len(balances), 3)
                              if balances else None),
        "turn_balance_note": ("Lower means one of the two did most of the "
                              "talking; it does not say which."),
        "days_with_shared_episodes": shared_days,
        "shared_episode_count": len(shared),
        "episode_types": dict(Counter(ep.get("type") for ep in shared)),
        "daily": {d: series.get(d) for d in g.days},
        "topics_while_together": [{"topic": t, "mentions": n}
                                  for t, n in topics.most_common(10)],
        "evidence": as_evidence(
            [s for s in g.segments
             if s["speaker"] in (person_a, person_b) and s["day"] in shared_days]),
        "emotion_note": EMOTION_IS_UNRELIABLE,
    }


def compare_people(g: Graph, person_a: str = None, person_b: str = None,
                   metric: str = "segment_count", **_) -> dict:
    out = {}
    for who in (person_a, person_b):
        series = g.person_series.get(who, {})
        values = _series_values(series, metric, g.days)
        nums = [v for v in values if isinstance(v, (int, float))]
        out[who] = {
            "total": sum(nums),
            "mean_per_day": round(sum(nums) / len(nums), 2) if nums else None,
            "days_present": len(nums),
            "by_day": {d: v for d, v in zip(g.days, values)},
        }
    return {"metric": metric, "comparison": out,
            "evidence": as_evidence(
                g.segments_of(person=person_a) + g.segments_of(person=person_b))}


def trend(g: Graph, subject: str = None, metric: str = None, **_) -> dict:
    if subject in g.person_series:
        series, kind = g.person_series[subject], "person"
    else:
        key = None
        if subject and "__" in subject:
            key = subject if subject in g.dyad_series else None
        if key is None:
            return {"subject": subject,
                    "result": "no series for this subject in this window"}
        series, kind = g.dyad_series[key], "pair"
    values = _series_values(series, metric, g.days)
    return {"subject": subject, "subject_kind": kind, "metric": metric,
            **_describe_trend(g.days, values)}


def room_profile(g: Graph, person: str = None, **_) -> dict:
    segs = g.segments_of(person=person)
    by_room = Counter(s["room"] for s in segs)
    words = defaultdict(int)
    for s in segs:
        words[s["room"]] += s.get("words", 0)
    return {
        "person": person,
        "rooms": [{"room": r, "utterances": n, "words": words[r]}
                  for r, n in by_room.most_common()],
        "evidence": as_evidence(segs),
        "note": ("Rooms reflect where a sentinel heard the speech, which "
                 "follows sensor placement as much as behaviour."),
    }


def timeline(g: Graph, person: str = None, **_) -> dict:
    rows = []
    for day in g.days:
        segs = [s for s in g.segments_of(person=person) if s["day"] == day]
        topics = Counter()
        for s in segs:
            topics.update(g.topics_of(s["id"]))
        rows.append({
            "day": day,
            "utterances": len(segs),
            "words": sum(s.get("words", 0) for s in segs),
            "topics": [t for t, _ in topics.most_common(5)],
            "rooms": sorted({s["room"] for s in segs}),
        })
    return {"person": person, "days": rows,
            "evidence": as_evidence(g.segments_of(person=person))}


# ── what an exchange meant ──────────────────────────────────────────
#
# Levels 0 to 4 count interactions; Level 1.5 read them. These three
# operations are the only ones that see that reading exchange by exchange, and
# each quotes the utterances it rests on, so any moment they return can be
# listened to. They say nothing about people, only about exchanges.

WARM_TYPES = {"affection", "praise", "humor", "shared_enjoyment", "support",
              "reassurance", "gratitude", "comfort", "inclusion", "invitation",
              "repair"}
SHARP_TYPES = {"conflict", "criticism", "insult", "hostility", "threat",
               "rejection", "complaint", "disagreement", "withdrawal"}
# Where a lamp lights on the dashboard, and the line this uses for "a tense
# exchange" or "a warm one" so the two agree.
# 0.40 for both. Measured against a listener on 90 exchanges (2026-09-22,
# tests/diagnostics/dynamics_pilot): the interpreter's warmth reaches 0.55 on
# 3 exchanges in five days, so warm_moments found almost nothing, while at
# 0.40 it calls an exchange warm with 71% sensitivity and 80% precision.
LIT_PROBLEM, LIT_GOOD = 0.40, 0.40


def _dim(exchange: dict, name: str) -> float:
    return (exchange.get("dimensions") or {}).get(name) or 0.0


def _moment(g: "Graph", exchange: dict, quotes: int = 4) -> dict:
    """One exchange as an answer shows it: the reading, then the words."""
    dims = exchange.get("dimensions") or {}
    return {
        "when": exchange.get("start"),
        "room": exchange.get("room"),
        "participants": exchange.get("participants") or [],
        "types": exchange.get("types") or [],
        "tension": dims.get("tension"),
        "warmth": dims.get("warmth"),
        "joy": dims.get("joy"),
        "confidence": exchange.get("confidence"),
        "complaints": exchange.get("complaints") or [],
        "what_the_model_read": exchange.get("evidence"),
        "said": as_evidence(g.said_in(exchange["id"]), quotes),
    }


def tension_moments(g: Graph, person: str = None, day: str = None,
                    top_k: int = None, **_) -> dict:
    """The sharpest exchanges, with what was said in them."""
    read = g.exchanges_of(person=person, day=day)
    lit = [x for x in read if _dim(x, "tension") >= LIT_PROBLEM]
    ranked = sorted(read, key=lambda x: -_dim(x, "tension"))
    top = [x for x in ranked[:int(top_k or 8)] if _dim(x, "tension") > 0]
    return {
        "person": person,
        "day": day,
        "exchanges_read": len(read),
        "tense_exchanges": len(lit),
        "share_tense": round(len(lit) / len(read), 3) if read else None,
        "days_involved": sorted({str(x.get("start") or "")[:10] for x in lit}),
        "people_involved": dict(Counter(p for x in lit
                                        for p in (x.get("participants") or []))),
        "kinds": dict(Counter(t for x in lit for t in (x.get("types") or [])
                              if t in SHARP_TYPES)),
        "lit_line": LIT_PROBLEM,
        "moments": [_moment(g, x) for x in top],
        "note": f"Tension is the reading of what was said, not a measurement "
                f"of anger in the voices. An exchange counts as tense at "
                f"{LIT_PROBLEM}; the moments below are the sharpest found, "
                f"which on a calm day may all sit under that line.",
    }


def warm_moments(g: Graph, person: str = None, day: str = None,
                 top_k: int = None, **_) -> dict:
    """The warmest exchanges: affection, humour, praise, support.

    Warmth and joy only. `connection` is not warmth -- it reads as high in a
    row as in an embrace, because two people arguing are thoroughly engaged
    with each other, and including it made the sharpest exchange of a day come
    back as one of its warmest. A reading with real tension in it is left out
    of the count for the same reason.
    """
    def glow(x):
        return max(_dim(x, "warmth"), _dim(x, "joy"))

    read = g.exchanges_of(person=person, day=day)
    lit = [x for x in read
           if glow(x) >= LIT_GOOD and _dim(x, "tension") < LIT_PROBLEM]
    top = [x for x in sorted(read, key=lambda x: -glow(x))[:int(top_k or 8)]
           if glow(x) > 0]
    return {
        "person": person,
        "day": day,
        "exchanges_read": len(read),
        "warm_exchanges": len(lit),
        "share_warm": round(len(lit) / len(read), 3) if read else None,
        "days_involved": sorted({str(x.get("start") or "")[:10] for x in lit}),
        "people_involved": dict(Counter(p for x in lit
                                        for p in (x.get("participants") or []))),
        "kinds": dict(Counter(t for x in lit for t in (x.get("types") or [])
                              if t in WARM_TYPES)),
        "lit_line": LIT_GOOD,
        "moments": [_moment(g, x) for x in top],
        "note": f"Warmth and joy are readings of what was said. An exchange "
                f"counts as warm when either reaches {LIT_GOOD} and the "
                f"tension in it stays under {LIT_PROBLEM}.",
    }


def complaints(g: Graph, person: str = None, day: str = None,
               top_k: int = None, **_) -> dict:
    """What is complained about, and whether it lands on somebody in the room.

    The distinction is the point: a household that grumbles about buses and a
    household that criticises each other produce the same tension average.
    """
    read = g.exchanges_of(person=person, day=day)
    lodged = []
    for x in read:
        for c in x.get("complaints") or []:
            if person and c.get("by") != person:
                continue
            lodged.append((c, x))
    carrying = sorted({id(x) for _c, x in lodged})
    by_target = Counter(c.get("about") for c, _x in lodged)
    ranked = sorted((x for _c, x in lodged), key=lambda x: -_dim(x, "tension"))
    seen, moments = set(), []
    for x in ranked:
        if x["id"] in seen:
            continue
        seen.add(x["id"])
        moments.append(_moment(g, x))
        if len(moments) >= int(top_k or 8):
            break
    return {
        "person": person,
        "day": day,
        "exchanges_read": len(read),
        "complaint_count": len(lodged),
        "exchanges_with_a_complaint": len(carrying),
        "by_target": dict(by_target),
        "by_person": dict(Counter(c.get("by") for c, _x in lodged)),
        "about_person": dict(Counter(c["who"] for c, _x in lodged if c.get("who"))),
        "days_involved": sorted({str(x.get("start") or "")[:10] for _c, x in lodged}),
        "moments": moments,
        "note": "Only complaints about someone present are friction between the "
                "people talking; the rest is irritation told to them.",
    }


# ── what happened, and when ─────────────────────────────────────────
#
# The three operations above rank EXCHANGES by a score. An exchange is whatever
# stretch of talk the pipeline grouped -- twenty minutes of it, sometimes -- and
# one line of reading has to stand for all of it. These are INCIDENTS instead:
# listed at ingest with no question in mind, each with its own start and end.
# incidents.py says why, and what was measured.

# Kinds in which people are at odds, where "which was the sharpest" is a fair
# question to ask of a list.
CONFLICT_KINDS = {"quarrel", "complaint", "rebuff"}


def _overlapping_tension(g: Graph, incident: dict) -> float | None:
    """The sharpest Level 1.5 reading that overlaps an incident.

    An incident says what happened and when, and carries no intensity. The
    exchanges do, so the two are joined: an overlapping span, and at least one
    person in common. None when no interpreted exchange overlaps it, which
    means unmeasured and not calm.
    """
    start, end = incident.get("start") or "", incident.get("end") or ""
    who = set(incident.get("participants") or [])
    best = None
    for x in g.exchanges:
        x_start = str(x.get("start") or "")
        x_end = str(x.get("end") or x_start)
        if not x_start or x_start > end or x_end < start:
            continue
        if who and not who & set(x.get("participants") or []):
            continue
        tension = (x.get("dimensions") or {}).get("tension")
        if isinstance(tension, (int, float)) and (best is None or tension > best):
            best = tension
    return best


def _incident_row(g: Graph, incident: dict) -> dict:
    start, end = incident.get("start") or "", incident.get("end") or ""
    return {
        "id": incident["id"],
        "kind": incident.get("type"),
        "day": start[:10],
        "start": start[11:19],
        "end": end[11:19],
        "participants": incident.get("participants") or [],
        "about": incident.get("about"),
        "what": incident.get("what"),
        "rooms": incident.get("rooms") or [],
        "tension": _overlapping_tension(g, incident),
        "span_method": incident.get("span_method"),
    }


def _who(row: dict) -> str:
    people = row.get("participants") or []
    return " and ".join(people) if people else "nobody the system could name"


def _incident_sentence(row: dict) -> str:
    return (f"{row['kind']} ({_who(row)}), {row['start'][:5]} to "
            f"{row['end'][:5]} on {row['day']}"
            + (f", about {row['about']}" if row.get("about") else ""))


def _incident_evidence(g: Graph, rows: list[dict]) -> list[dict]:
    """A few of the lines each incident rests on, the first incidents first."""
    out = []
    for row in rows:
        out.extend(as_evidence(g.said_in(row["id"]), 3))
        if len(out) >= MAX_EVIDENCE:
            break
    return out[:MAX_EVIDENCE]


def _never_read(unread: list[str]) -> str:
    if not unread:
        return ""
    return (f" {len(unread)} day(s) in the range were never read for "
            f"incidents ({unread[0]}"
            + (f" .. {unread[-1]}" if len(unread) > 1 else "")
            + "), so nothing is known about them either way.")


def incidents(g: Graph, kind: str = None, person: str = None, day: str = None,
              top_k: int = None, person_b: str = None, **_) -> dict:
    """What happened between people, each with a start and an end.

    With `person_b` as well as `person`, only what BOTH were in. Without it a
    question about a pair was served every incident of the first person, and
    the answer said yes to "did A and B quarrel" on 22 days of 22 on which
    they had not (tests/diagnostics/graphrag_retrieval/graphrag_incident_eval.py).
    """
    kind = (kind or "").strip().lower() or None
    scope = [day] if day else list(g.days)
    read = [d for d in scope if d in g.incident_layer]
    unread = [d for d in scope if d not in g.incident_layer]

    found = []
    for node in g.incidents:
        if (node.get("start") or "")[:10] not in scope:
            continue
        if kind and node.get("type") != kind:
            continue
        if person and person not in (node.get("participants") or []):
            continue
        if person_b and person_b not in (node.get("participants") or []):
            continue
        found.append(_incident_row(g, node))

    if kind in CONFLICT_KINDS:
        # Sharpest first. An unmeasured one goes last, not first: it is
        # unknown, and unknown must not outrank a real reading.
        found.sort(key=lambda r: (-(r["tension"] if r["tension"] is not None
                                    else -1.0), r["day"], r["start"]))
    else:
        found.sort(key=lambda r: (r["day"], r["start"]))
    shown = found[:int(top_k or MAX_INCIDENTS)]

    what = f"{kind}" if kind else "incident"
    whose = (f" between {person} and {person_b}" if person and person_b
             else f" involving {person or person_b}" if person or person_b else "")
    if not read:
        assessment = (
            f"Nothing can be said: none of the {len(scope)} day(s) asked about "
            f"has been read for incidents. That is not the same as nothing "
            f"having happened.")
    elif not found:
        assessment = (f"No. No {what}{whose} is recorded on the {len(read)} "
                      f"day(s) that were read." + _never_read(unread))
    else:
        listed = "; ".join(_incident_sentence(r) for r in shown[:3])
        order = ", sharpest first" if kind in CONFLICT_KINDS and len(found) > 1 else ""
        assessment = (f"Yes. {len(found)} {what}(s){whose} recorded{order}: "
                      f"{listed}" + ("; and others" if len(found) > 3 else "")
                      + "." + _never_read(unread))

    return {
        "kind": kind,
        "person": person,
        "person_b": person_b,
        "day": day or f"all {len(scope)} days in the window",
        "days_read_for_incidents": len(read),
        "days_never_read_for_incidents": unread,
        "incidents_found": len(found),
        "by_kind": dict(Counter(r["kind"] for r in found)),
        "assessment": assessment,
        "incidents": shown,
        "evidence": _incident_evidence(g, shown),
        "note": ("An incident's start and end are the times of the first and "
                 "last recorded line that show it. Recording is triggered by "
                 "sound, so it may have begun earlier and gone on longer. "
                 "'tension' is the sharpest Level 1.5 reading that overlaps "
                 "it, and null means none did."),
    }


def focus_incidents(g: Graph, result: dict, chosen: list[str]) -> dict:
    """Narrow an incidents result to the ones a question was judged to ask for.

    The choosing is done by a model, in llm.select_incidents; what is done
    with the choice is done here, so the sentence the narrator follows and the
    lines it may cite are computed and not composed.
    """
    rows = result.get("incidents") or []
    keep = [r for r in rows if r["id"] in set(chosen)]
    out = dict(result)
    out["incidents_considered"] = len(rows)
    out["incidents"] = keep
    out["evidence"] = _incident_evidence(g, keep)
    unread = result.get("days_never_read_for_incidents") or []
    if keep:
        listed = "; ".join(_incident_sentence(r) for r in keep[:3])
        out["assessment"] = (f"Yes. {listed}"
                             + ("; and others" if len(keep) > 3 else "") + "."
                             + _never_read(unread))
    else:
        out["assessment"] = (
            f"No. None of the {len(rows)} recorded incident(s) that were "
            f"looked at answers the question." + _never_read(unread))
    return out


OPS = {
    "overview": overview,
    "subject_summary": subject_summary,
    "family_summary": subject_summary,   # the whole household, by default
    "household_topics": household_topics,
    "emotion_evidence": emotion_evidence,
    "tension_moments": tension_moments,
    "warm_moments": warm_moments,
    "complaints": complaints,
    "incidents": incidents,
    "person_topics": person_topics,
    "topic_evidence": topic_evidence,
    "person_profile": person_profile,
    "dyad_summary": dyad_summary,
    "compare_people": compare_people,
    "trend": trend,
    "room_profile": room_profile,
    "timeline": timeline,
}


def execute(g: Graph, plan: dict) -> dict:
    from .schema import resolve_person

    op = OPS[plan["operation"]]
    args = dict(plan.get("arguments") or {})
    roles = g.data.get("roles", {})

    # "the child", "his father", "wife" -> a name, before any operation runs.
    for key in ("person", "person_a", "person_b", "subject"):
        if args.get(key):
            args[key] = resolve_person(args[key], roles, g.people) or args[key]
    if args.get("members"):
        args["members"] = [resolve_person(m, roles, g.people) or m
                           for m in args["members"]]
    # family_summary means everyone unless told otherwise
    if plan["operation"] == "family_summary":
        args.setdefault("members", list(g.people))

    result = op(g, **args)
    result["_operation"] = plan["operation"]
    result["_arguments"] = args
    return result
