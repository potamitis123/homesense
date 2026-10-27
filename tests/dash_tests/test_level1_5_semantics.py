#!/usr/bin/env python3
"""Level 1.5 — semantic interaction.

The interpreter is stubbed throughout: these tests are about the pipeline's
handling of an interpretation, not about any model's judgement. A test that
called a real LLM would be slow, costly and non-deterministic, and would stop
testing the thing it is named after.

The load-bearing assertions are the three the layer exists for:

  * a hostile exchange must not read as warm just because people interacted
  * a hostile exchange MAY be high connection and high tension at once
  * uninterpretable speech must not produce confident dimensions -- and must
    produce null, never zero
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ast_dash", "llm_report"))

import derive_home_dynamics as H                                   # noqa: E402


def make_events(turns, room="Kitchen", hour=19, first_id=1):
    """One dashboard row per turn, nine seconds apart so they cluster."""
    rows = []
    for index, (speaker, text) in enumerate(turns):
        second = index * 9
        rows.append({
            "ID": first_id + index,
            "Timestamp": f"2026-09-01 {hour:02d}:{second // 60:02d}:{second % 60:02d}",
            "Device": "dev-1", "room": room,
            "Class 1": "Speech", "Prob 1": 0.8,
            "transcription": f"[ 00: 00. 00 - 00: 04. 00 ] {speaker}: {text}",
            "emotion": "NEU (0.5)", "rms": 0.2,
        })
    return rows


def run(turns, reply, **kw):
    """Push turns through L0 -> L1.5 -> L3 -> L4 with a fixed interpreter reply."""
    cfg = H.load_config(None)
    level0 = H.derive_level0(make_events(turns, **kw), cfg)
    level1 = H.derive_level1(level0, cfg)
    calls = []

    def interpreter(payload):
        calls.append(payload)
        return reply

    level1_5 = H.derive_level1_5(level0, cfg, interpreter=interpreter, cache={})
    level2 = H.derive_level2(level1, cfg)
    level3 = H.derive_level3(level0, level2, cfg, level1_5=level1_5)
    gaps = H.derive_observation_gaps(level0, cfg)
    level4 = H.derive_level4(level0, level2, level3, gaps, cfg)
    return level1_5, level3, level4, calls


def reply(types, dims, status="interpretable", confidence=0.9, **extra):
    body = {"semantic_status": status, "semantic_confidence": confidence,
            "interaction_types": types, "relational_dimensions": dims}
    body.update(extra)
    return body


AFFECTION = [("Ilyas", "Σ' αγαπάω πολύ."), ("Voula", "Κι εγώ, είσαι πάντα εδώ.")]
HOSTILE = [("Ilyas", "Είσαι ηλίθια."), ("Voula", "Σκάσε, δε σε αντέχω.")]


# --- the three load-bearing invariants ------------------------------------

def test_hostile_exchange_is_not_warm():
    """A reciprocal conversation is not evidence of warmth."""
    _l15, l3, _l4, _ = run(HOSTILE, reply(
        ["insult", "hostility"],
        {"connection": 0.85, "warmth": 0.02, "joy": 0.0,
         "inclusion": 0.1, "withdrawal": 0.2, "tension": 0.95}))
    conv = [e for e in l3 if e.type == "reciprocal_conversation_candidate"]
    assert conv, "two known speakers should still make a conversation candidate"
    dims = conv[0].details["semantic_interaction"]["dimensions"]
    assert dims["warmth"]["mean"] < 0.2
    assert dims["tension"]["mean"] > 0.8


def test_warmth_and_tension_are_separate_axes():
    """A hostile exchange is cold AND tense; neither answer implies the other.

    This replaced a test of `connection`, which was dropped as a dimension on
    2026-09-25 -- it could not vary, because an exchange reaches this level
    only when two residents spoke in it. The stale keys are left in the reply
    on purpose: cached readings and stored days still carry them, and the
    normaliser has to ignore them rather than choke.
    """
    l15, _l3, _l4, _ = run(HOSTILE, reply(
        ["insult"], {"connection": 0.9, "warmth": 0.02, "joy": 0.0,
                     "inclusion": 0.1, "withdrawal": 0.15, "tension": 0.97}))
    dims = l15[0].relational_dimensions
    assert dims["tension"] > 0.8 and dims["warmth"] < 0.2
    assert "connection" not in dims and "inclusion" not in dims


def test_uninterpretable_yields_null_not_zero():
    """None means no evidence; 0.0 would mean evidence of absence."""
    l15, _l3, _l4, _ = run(
        [("Ilyas", "Με καθίστηκα ο Βουράλης έβυλο"),
         ("Voula", "και έτσι ο Βιωφυλάκης φρέγγα")],
        reply(["uninterpretable"],
              {"connection": 0.9, "warmth": 0.8, "joy": 0.7,
               "inclusion": 0.6, "withdrawal": 0.1, "tension": 0.1},
              status="uninterpretable", confidence=0.05))
    record = l15[0]
    assert record.semantic_status == "uninterpretable"
    # Scores offered alongside an abstention are discarded, not kept.
    assert all(v is None for v in record.relational_dimensions.values())


# --- what the interpreter is given, and where complaints point ------------

def test_the_payload_carries_no_emotion_label():
    """The classifier's label pulled readings toward its own guess; words only."""
    _l15, _l3, _l4, calls = run(AFFECTION, reply(["affection"], {"warmth": 0.9}))
    turns = [t for t in calls[0]["exchange"] if "speaker" in t]
    assert turns and all("emotion_model_said" not in t for t in turns)


def test_a_transcription_loop_is_given_once_but_a_repeat_is_not():
    loop = [("Rafael", "Ευχαριστούμε!")] * 5 + [("Ilyas", "Ωραία, πάμε σπίτι τώρα.")]
    _l15, _l3, _l4, calls = run(loop, reply(["neutral_conversation"], {"connection": 0.5}))
    given = [(t["speaker"], t.get("repeated")) for t in calls[0]["exchange"] if "speaker" in t]
    assert given == [("Rafael", 5), ("Ilyas", None)]

    twice = [("Voula", "Έλα εδώ."), ("Voula", "Έλα εδώ."), ("Ilyas", "Έρχομαι αμέσως, μισό λεπτό.")]
    _l15, _l3, _l4, calls = run(twice, reply(["request"], {"connection": 0.5}))
    given = [(t["speaker"], t.get("repeated")) for t in calls[0]["exchange"] if "speaker" in t]
    assert given == [("Voula", None), ("Voula", None), ("Ilyas", None)]


def test_complaints_say_where_they_point():
    """Kept with their target; an unheard speaker or an unknown target is dropped."""
    l15, *_ = run(HOSTILE, reply(
        ["criticism"], {"connection": 0.8, "tension": 0.6},
        complaints=[{"by": "Ilyas", "about": "someone_present", "who": "Voula"},
                    {"by": "Voula", "about": "outsider", "who": "Rafael"},
                    {"by": "Rafael", "about": "circumstance"},
                    {"by": "Ilyas", "about": "the weather"}]))
    record = l15[0]
    assert record.complaints == [{"by": "Ilyas", "about": "someone_present", "who": "Voula"},
                                 {"by": "Voula", "about": "outsider", "who": None}]
    assert "complaint" in record.interaction_types

    l15, *_ = run(HOSTILE, reply(["uninterpretable"], {}, status="uninterpretable",
                                 complaints=[{"by": "Ilyas", "about": "outsider"}]))
    assert l15[0].complaints == []


# --- the interaction catalogue --------------------------------------------

@pytest.mark.parametrize("name,turns,types,dims,expect", [
    ("affection", AFFECTION, ["affection"],
     {"connection": 0.86, "warmth": 0.94, "joy": 0.61,
      "inclusion": 0.74, "withdrawal": 0.03, "tension": 0.02},
     lambda d: d["warmth"] > 0.8 and d["tension"] < 0.2),
    ("neutral", [("Ilyas", "Το τρένο φεύγει στις έξι."), ("Voula", "Εντάξει.")],
     ["information_sharing", "neutral_conversation"],
     {"connection": 0.6, "warmth": 0.3, "joy": 0.05,
      "inclusion": 0.4, "withdrawal": 0.05, "tension": 0.05},
     lambda d: d["tension"] < 0.2 and d["warmth"] < 0.5),
    ("argument", HOSTILE, ["disagreement", "hostility"],
     {"connection": 0.88, "warmth": 0.05, "joy": 0.0,
      "inclusion": 0.2, "withdrawal": 0.3, "tension": 0.9},
     lambda d: d["tension"] > 0.8 and d["warmth"] < 0.2),
    ("disagreement_without_hostility",
     [("Ilyas", "Διαφωνώ, νομίζω είναι νωρίς."), ("Voula", "Καταλαβαίνω, αλλά ας δοκιμάσουμε.")],
     ["disagreement"],
     {"connection": 0.75, "warmth": 0.45, "joy": 0.1,
      "inclusion": 0.6, "withdrawal": 0.05, "tension": 0.35},
     lambda d: d["tension"] < 0.5 and d["warmth"] > 0.3),
    # `inclusion` was dropped as a dimension on 2026-09-22 but survives as an
    # interaction type, which is what this case now checks. The stale
    # `inclusion` key is left in every reply below on purpose: cached readings
    # and stored days still carry it, and the normaliser has to keep ignoring
    # it rather than choking on it.
    ("invitation", [("Voula", "Έλα κάτσε μαζί μας."), ("Rafael", "Έρχομαι!")],
     ["invitation", "inclusion"],
     {"connection": 0.8, "warmth": 0.6, "joy": 0.4,
      "inclusion": 0.9, "withdrawal": 0.02, "tension": 0.02},
     lambda d: d["warmth"] > 0.5 and d["joy"] > 0.3
     and "inclusion" not in d and "connection" not in d),
    ("withdrawal", [("Voula", "Έρχεσαι να φάμε;"), ("Ilyas", "Άσε με ήσυχο.")],
     ["rejection", "withdrawal"],
     {"connection": 0.4, "warmth": 0.1, "joy": 0.0,
      "inclusion": 0.15, "withdrawal": 0.85, "tension": 0.55},
     lambda d: d["withdrawal"] > 0.7),
    ("humor", [("Rafael", "Μπαμπά είσαι σαν τον παππού!"), ("Ilyas", "Χαχα, ευχαριστώ πολύ!")],
     ["humor", "shared_enjoyment"],
     {"connection": 0.8, "warmth": 0.7, "joy": 0.9,
      "inclusion": 0.7, "withdrawal": 0.02, "tension": 0.05},
     lambda d: d["joy"] > 0.8),
    ("apology", [("Ilyas", "Είχες δίκιο. Συγγνώμη."), ("Voula", "Εντάξει, το ξεχνάμε.")],
     ["repair", "apology"],
     {"connection": 0.88, "warmth": 0.73, "joy": 0.15,
      "inclusion": 0.81, "withdrawal": 0.08, "tension": 0.32},
     lambda d: d["warmth"] > 0.6),
])
def test_interaction_catalogue(name, turns, types, dims, expect):
    l15, _l3, _l4, _ = run(turns, reply(types, dims))
    assert l15, f"{name}: no interaction produced"
    assert expect(l15[0].relational_dimensions), f"{name}: dimensions wrong"


# --- speakers and attribution ---------------------------------------------

def test_unknown_speaker_is_not_pair_evidence():
    turns = [("Unknown", "κάτι λέει εδώ πέρα τώρα"),
             ("Unknown", "και κάτι άλλο ακόμα λέει")]
    l15, _l3, _l4, _ = run(turns, reply(
        ["neutral_conversation"],
        {"connection": 0.5, "warmth": 0.3, "joy": 0.1,
         "inclusion": 0.3, "withdrawal": 0.1, "tension": 0.1}))
    assert l15[0].participants == []
    assert any("two identified speakers" in u for u in l15[0].uncertainty)


def test_interpreter_cannot_invent_a_speaker():
    """Identity comes from diarisation only, never from the interpretation."""
    l15, _l3, _l4, _ = run(AFFECTION, reply(
        ["affection"],
        {"connection": 0.8, "warmth": 0.9, "joy": 0.5,
         "inclusion": 0.7, "withdrawal": 0.0, "tension": 0.0},
        participants=["Ilyas", "Voula", "Giannis"], target="Giannis"))
    assert "Giannis" not in l15[0].participants
    assert l15[0].target != "Giannis"


def test_single_person_speech_is_interpreted_but_not_paired():
    turns = [("Ilyas", "Πρέπει να θυμηθώ να πάρω ψωμί αύριο το πρωί.")]
    l15, l3, _l4, _ = run(turns, reply(
        ["information_sharing"],
        {"connection": 0.2, "warmth": 0.1, "joy": 0.0,
         "inclusion": 0.1, "withdrawal": 0.1, "tension": 0.0}))
    assert l15 and l15[0].participants == ["Ilyas"]
    assert not [e for e in l3 if e.type == "reciprocal_conversation_candidate"]


# --- mixed episodes, repair, and aggregation ------------------------------

def test_tension_then_repair_keeps_both():
    """Repair is recorded beside the tension it followed, never cancelling it."""
    hostile = H.SemanticInteraction(
        interaction_id="sem_1", start="2026-09-01T19:00:00",
        end="2026-09-01T19:00:20", room="Kitchen",
        semantic_status="interpretable", semantic_confidence=0.9,
        interaction_types=["insult"],
        relational_dimensions={"connection": 0.9, "warmth": 0.05, "joy": 0.0,
                               "inclusion": 0.2, "withdrawal": 0.2, "tension": 0.95})
    repair = H.SemanticInteraction(
        interaction_id="sem_2", start="2026-09-01T19:01:00",
        end="2026-09-01T19:01:20", room="Kitchen",
        semantic_status="interpretable", semantic_confidence=0.9,
        interaction_types=["repair", "apology"],
        relational_dimensions={"connection": 0.88, "warmth": 0.73, "joy": 0.15,
                               "inclusion": 0.81, "withdrawal": 0.08, "tension": 0.32})
    summary = H.summarise_semantics([hostile, repair])
    assert summary["tension_peak"]["followed_by_repair"] is True
    # The peak survives the average: a repaired argument still happened.
    assert summary["dimensions"]["tension"]["max"] == 0.95
    assert summary["dimensions"]["tension"]["mean"] < 0.95


def test_aggregate_keeps_temporal_order():
    items = H.summarise_semantics([
        H.SemanticInteraction(
            interaction_id=f"sem_{n}", start=f"2026-09-01T19:0{n}:00",
            end=None, room="Kitchen", semantic_status="interpretable",
            semantic_confidence=0.8, interaction_types=[t],
            relational_dimensions={"connection": 0.8, "warmth": w, "joy": 0.1,
                                   "inclusion": 0.5, "withdrawal": 0.1,
                                   "tension": 0.2})
        for n, (t, w) in enumerate([("complaint", 0.1), ("repair", 0.7)], start=1)])
    assert [s["types"][0] for s in items["sequence"]] == ["complaint", "repair"]


# --- graceful degradation --------------------------------------------------

def test_no_interpreter_marks_not_attempted():
    """Missing interpretation is missing evidence, never zero relational quality."""
    cfg = H.load_config(None)
    level0 = H.derive_level0(make_events(AFFECTION), cfg)
    l15 = H.derive_level1_5(level0, cfg, interpreter=None)
    assert l15 and l15[0].semantic_status == "not_attempted"
    assert all(v is None for v in l15[0].relational_dimensions.values())
    # Speakers are still known: diarisation ran even though nothing read it.
    assert l15[0].participants == ["Ilyas", "Voula"]


def test_interpreter_failure_does_not_break_the_pipeline():
    cfg = H.load_config(None)
    level0 = H.derive_level0(make_events(AFFECTION), cfg)

    def broken(payload):
        raise RuntimeError("model unreachable")

    l15 = H.derive_level1_5(level0, cfg, interpreter=broken)
    assert l15[0].semantic_status == "uninterpretable"
    assert all(v is None for v in l15[0].relational_dimensions.values())
    # And the levels above still build.
    level1 = H.derive_level1(level0, cfg)
    level2 = H.derive_level2(level1, cfg)
    level3 = H.derive_level3(level0, level2, cfg, level1_5=l15)
    assert level3


def test_existing_levels_unchanged_without_l1_5():
    """L3 must produce exactly what it did before when no semantics are passed."""
    cfg = H.load_config(None)
    level0 = H.derive_level0(make_events(AFFECTION), cfg)
    level1 = H.derive_level1(level0, cfg)
    level2 = H.derive_level2(level1, cfg)
    before = H.derive_level3(level0, level2, cfg)
    assert before
    assert all("semantic_interaction" not in (e.details or {}) for e in before)


# --- efficiency ------------------------------------------------------------

def test_one_call_per_exchange_not_per_sentence():
    turns = AFFECTION + [("Ilyas", "Να πάμε αύριο βόλτα μαζί το πρωί;")]
    _l15, _l3, _l4, calls = run(turns, reply(
        ["affection"], {"connection": 0.8, "warmth": 0.9, "joy": 0.4,
                        "inclusion": 0.7, "withdrawal": 0.0, "tension": 0.0}))
    assert len(calls) == 1, "three turns in one cluster is one exchange"
    assert len(calls[0]["exchange"]) == 3


def test_short_exchanges_are_not_sent_at_all():
    """Below the word floor there is nothing to interpret, so nothing is spent."""
    _l15, _l3, _l4, calls = run([("Ilyas", "Ναι.")], reply(
        ["other"], {k: 0.0 for k in H.RELATIONAL_DIMENSIONS}))
    assert calls == []


def test_cache_prevents_repeat_calls():
    cfg = H.load_config(None)
    level0 = H.derive_level0(make_events(AFFECTION), cfg)
    calls, cache = [], {}

    def interpreter(payload):
        calls.append(payload)
        return reply(["affection"],
                     {"connection": 0.8, "warmth": 0.9, "joy": 0.4,
                      "inclusion": 0.7, "withdrawal": 0.0, "tension": 0.0})

    H.derive_level1_5(level0, cfg, interpreter=interpreter, cache=cache)
    H.derive_level1_5(level0, cfg, interpreter=interpreter, cache=cache)
    assert len(calls) == 1, "identical exchange must not be re-interpreted"


# --- the dashboard end of the chain ---------------------------------------

def test_bulbs_follow_the_semantics():
    """The lamps must move with meaning, not merely with conversation."""
    import dash
    dash.Dash(__name__, use_pages=True, pages_folder="")
    from ast_dash.pages import llm_report as LR

    def payload_for(dims, counted=True):
        # `present` is how many of the six exchanges showed the dimension. The
        # positive lamps count exchanges rather than averaging them -- see
        # SEMANTIC_GREEN_SHARE -- so a fixture built without it exercises the
        # compatibility path for reports stored before that change.
        def cell(v):
            out = {"mean": v, "max": v, "n": 6}
            if counted:
                out["present"] = 6 if v >= 0.40 else 0
            return out

        return {"level_4_daily_dynamics": [{
            "date": "2026-09-01",
            "household_semantics": {"dimensions": {
                k: cell(v) for k, v in dims.items()}}}]}

    warm = LR.bulbs_from_semantics(payload_for(
        {"connection": 0.8, "warmth": 0.9, "joy": 0.7,
         "inclusion": 0.8, "withdrawal": 0.05, "tension": 0.05}))
    hostile = LR.bulbs_from_semantics(payload_for(
        {"connection": 0.85, "warmth": 0.05, "joy": 0.0,
         "inclusion": 0.15, "withdrawal": 0.3, "tension": 0.9}))

    assert warm["warmth"]["state"] == "green"
    assert hostile["warmth"]["state"] == "amber"
    assert "connection" not in warm and "connection" not in hostile
    assert warm["tension"]["state"] == "green"      # inverted: no problem found
    assert hostile["tension"]["state"] == "amber"

    # A week where warmth was PRESENT in most exchanges but never strong is
    # exactly the shape the old mean-against-0.55 rule could not light: the
    # interpreter's warmth ran to a median of 0.50 and a mean of 0.39.
    lukewarm = LR.bulbs_from_semantics({"level_4_daily_dynamics": [{
        "date": "2026-09-01",
        "household_semantics": {"dimensions": {
            "warmth": {"mean": 0.39, "max": 1.0, "n": 18, "present": 12}}}}]})
    assert lukewarm["warmth"]["state"] == "green"

    # Reports stored before exchanges were counted fall back to the mean, so
    # reopening one does not turn its positive lamps amber all at once.
    old = LR.bulbs_from_semantics(payload_for(
        {"warmth": 0.9, "joy": 0.2}, counted=False))
    assert old["warmth"]["state"] == "green"
    assert old["joy"]["state"] == "amber"
    assert "not counted" in old["warmth"]["evidence"]


def test_the_panel_says_how_many_lamps_it_has():
    """The panel went on saying six after two of the six were removed."""
    import dash
    dash.Dash(__name__, use_pages=True, pages_folder="")
    from ast_dash.pages import llm_report as LR

    def said(node):
        if node is None:
            return ""
        if isinstance(node, (list, tuple)):
            return " ".join(said(item) for item in node)
        if isinstance(node, str):
            return node
        return said(getattr(node, "children", None))

    def lamps(node):
        if isinstance(node, (list, tuple)):
            return sum(lamps(item) for item in node)
        own = 1 if getattr(node, "className", None) == "fb-bulb" else 0
        return own + lamps(getattr(node, "children", None) or [])

    # Nothing observed, so every lamp is unlit and the note counts them all.
    unseen = LR.render_part_b(LR.normalise_part_b({}, {}))
    assert lamps(unseen) == len(LR.PART_B_BULBS) == 4
    text = said(unseen)
    assert "Four lamps for the week" in text
    assert "4 of 4 indicators are unlit" in text
    assert "Six" not in text and "of 6" not in text

    # One unlit among lit ones is singular.
    evidence = {key: 99 for key, *_ in LR.PART_B_BULBS}
    evidence["joy"] = 0
    replies = {"bulbs": {key: {"state": "green"} for key, *_ in LR.PART_B_BULBS}}
    one = said(LR.render_part_b(LR.normalise_part_b(replies, evidence)))
    assert "1 of 4 indicators is unlit" in one
    assert "closely enough to judge it." in one


# --- one utterance heard by several sentinels ------------------------------

def heard_by(copies, when="2026-09-01 19:00:00"):
    """Dashboard rows for ONE moment heard by several sentinels.

    copies: (device, room, speaker, text), one per sentinel. They share a
    timestamp -- that is what makes them one utterance heard twice rather
    than two utterances.
    """
    return [{
        "ID": 100 + index, "Timestamp": when,
        "Device": device, "room": room,
        "Class 1": "Speech", "Prob 1": 0.8,
        "transcription": f"[ 00: 00. 00 - 00: 04. 00 ] {speaker}: {text}",
        "emotion": "NEU (0.5)", "rms": 0.2,
    } for index, (device, room, speaker, text) in enumerate(copies)]


BANK_NEAR = "Πάλι δεν πήγες στην τράπεζα, όλη μέρα χαζεύεις με τα αθλητικά στην τηλεόραση."
BANK_FAR = "Πάλι δεν πήγες στην τράπεζα, όλη μέρα χαζεύεις με τα αθλητικά."


def test_one_utterance_heard_by_two_sentinels_is_one_turn():
    cfg = H.load_config(None)
    level0 = H.derive_level0(heard_by([
        ("p4-door-1", "Entrance", "Voula", BANK_NEAR),
        ("box3-2", "Bedroom_2", "Voula", BANK_FAR),
    ]), cfg)
    assert len(H._collect_turns(level0, cfg, merge=False)) == 2
    turns = H._collect_turns(level0, cfg)
    assert len(turns) == 1
    assert turns[0].device == "p4-door-1", "the copy that decoded more words is kept"


def test_the_interpreter_reads_a_shared_line_once():
    cfg = H.load_config(None)
    level0 = H.derive_level0(heard_by([
        ("p4-door-1", "Entrance", "Voula", BANK_NEAR),
        ("box3-2", "Bedroom_2", "Voula", BANK_FAR),
    ]), cfg)
    calls = []
    H.derive_level1_5(level0, cfg, interpreter=lambda p: calls.append(p), cache={})
    assert len(calls) == 1, "one sentence, read once -- not once per room that heard it"


def test_a_short_reply_by_someone_else_is_not_swallowed():
    # From the 2026-09-10 takes: Rafael's reply arrived moments after Ilyas's
    # line on another sentinel and is mostly "contained" in it letter for
    # letter. Only the speakers tell them apart, which is the point.
    near, far = "Σου είπα να κλείσεις το κινητό.", "Ένα λεπτό ακόμα."
    assert H._contained(H._flat_text(far), H._flat_text(near)) >= 0.7
    cfg = H.load_config(None)
    level0 = H.derive_level0(heard_by([
        ("box3-2", "Bedroom_2", "Ilyas", near),
        ("box3-1", "Bedroom_1", "Rafael", far),
    ]), cfg)
    assert len(H._collect_turns(level0, cfg)) == 2


def test_one_sentinel_hearing_it_twice_is_not_merged():
    # Two microphones on one sentence is a copy; one microphone hearing the
    # same words twice is a person repeating themselves.
    cfg = H.load_config(None)
    level0 = H.derive_level0(heard_by([
        ("p4-door-1", "Entrance", "Voula", BANK_NEAR),
        ("p4-door-1", "Entrance", "Voula", BANK_NEAR),
    ]), cfg)
    assert len(H._collect_turns(level0, cfg)) == 2


def test_the_merge_can_be_switched_off():
    cfg = H.load_config(None)
    cfg["thresholds"]["cross_sentinel_merge"] = False
    level0 = H.derive_level0(heard_by([
        ("p4-door-1", "Entrance", "Voula", BANK_NEAR),
        ("box3-2", "Bedroom_2", "Voula", BANK_FAR),
    ]), cfg)
    assert len(H._collect_turns(level0, cfg)) == 2

