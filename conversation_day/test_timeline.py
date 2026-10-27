#!/usr/bin/env python3
"""Discussions per room, and the picture drawn from them.

    python -m pytest conversation_day/test_timeline.py -q

The interpreter is stubbed: these check what the viewer does with speech and
with a reading, not what any model reads.
"""
import json
import os
import sys

import plotly

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (HERE, ROOT, os.path.join(ROOT, "ast_dash", "llm_report")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import derive_home_dynamics as H                                   # noqa: E402
import timeline as T                                               # noqa: E402

CFG = H.load_config(None)
DAY = "2026-09-01"
NOON = 12 * 12               # the 12:00-12:05 slot at five minutes a slot
RESIDENTS = ["Ilyas", "Rafael", "Voula"]
IR, IV, RV = "Ilyas – Rafael", "Ilyas – Voula", "Rafael – Voula"
TRIGGER = "Έλα να μιλήσουμε λίγο για τους λογαριασμούς του σπιτιού."
REPLY = "Πάλι τα ίδια; Σου είπα ότι θα τα κανονίσω όλα αύριο το πρωί."
WHEN = "Και πότε ακριβώς θα γίνει αυτό, αφού πάντα κάτι σου τυχαίνει;"
LATER = "Δεν μπορώ να τα κάνω όλα μόνος μου, το καταλαβαίνεις ή όχι;"
JACKET = "Μαμά, πού είναι το μπουφάν μου;"
ANSWER = "Στην ντουλάπα του διαδρόμου, εκεί που το άφησες χθες."


def row(event_id, when, speaker, text, device="box3-1", room="Kitchen"):
    return {"ID": event_id, "Timestamp": when, "Device": device, "room": room,
            "Class 1": "Speech", "Prob 1": 0.8,
            "transcription": f"[ 00: 00. 00 - 00: 04. 00 ] {speaker}: {text}",
            "emotion": "NEU (0.5)", "rms": 0.2}


def reading(types, dims, complaints=None):
    return {"semantic_status": "interpretable", "semantic_confidence": 0.9,
            "interaction_types": types, "relational_dimensions": dims,
            "complaints": complaints or []}


def criticism(_payload):
    return reading(["criticism"], {"tension": 0.8, "warmth": 0.7,
                                   "connection": 0.7})   # stale key, ignored


def quarrel():
    """Voula raises the bills, Ilyas answers twice; later Rafael, alone, in the Lab."""
    return [row(1, f"{DAY} 12:00:00", "Voula", TRIGGER),
            row(2, f"{DAY} 12:00:40", "Ilyas", REPLY),
            row(3, f"{DAY} 12:03:10", "Ilyas", LATER),
            row(4, f"{DAY} 12:30:00", "Rafael", JACKET, device="esp32s3-2", room="Lab")]


def dialogue():
    """Voula and Ilyas answer each other over the bills: three replies by 12:04."""
    return [row(1, f"{DAY} 12:00:00", "Voula", TRIGGER),
            row(2, f"{DAY} 12:00:40", "Ilyas", REPLY),
            row(3, f"{DAY} 12:02:00", "Voula", WHEN),
            row(4, f"{DAY} 12:03:10", "Ilyas", LATER)]


def rafael_alone():
    return quarrel()[3:]


def anchored(events):
    level0 = H.derive_level0(events, CFG)
    discussions, left_out = T.anchor(level0, CFG, 5)
    return level0, discussions, left_out


def alone(left_out):
    return [next(iter(s["residents"]), None) for s in left_out]


def picture(events, interpreter=criticism):
    level0, discussions, left_out = anchored(events)
    read = T.read_discussions(level0, CFG, discussions, interpreter)
    return {"day": DAY, "residents": RESIDENTS, "discussions": read,
            "left_out": T.left_out_dicts(left_out)}


# ── discussions ────────────────────────────────────────────────────────
def test_a_reply_belongs_to_the_discussion_it_answers():
    _l0, discussions, left_out = anchored(quarrel())
    assert len(discussions) == 1
    assert sorted(discussions[0]["residents"]) == ["Ilyas", "Voula"]
    assert len(discussions[0]["turns"]) == 3
    # Rafael, alone in the Lab half an hour later, is in no discussion.
    assert alone(left_out) == ["Rafael"]


def test_five_minutes_of_silence_ends_a_discussion():
    events = quarrel()[:3] + [row(5, f"{DAY} 12:20:00", "Voula", TRIGGER)]
    _l0, discussions, left_out = anchored(events)
    assert len(discussions) == 1
    assert alone(left_out) == ["Voula"]


def test_moving_to_another_room_starts_a_new_discussion_there():
    # Voula leaves the kitchen quarrel and talks with Rafael in the Lab.
    lab = [row(5, f"{DAY} 12:04:00", "Voula", "Ραφαήλ, έλα λίγο να δούμε τα μαθήματα.",
               device="esp32s3-2", room="Lab"),
           row(6, f"{DAY} 12:04:30", "Rafael", ANSWER, device="esp32s3-2", room="Lab")]
    _l0, discussions, left_out = anchored(quarrel()[:3] + lab)
    assert [(d["room"], sorted(d["residents"])) for d in discussions] == [
        ("Kitchen", ["Ilyas", "Voula"]), ("Lab", ["Rafael", "Voula"])]
    assert all({t.room for t in d["turns"]} == {d["room"]} for d in discussions)
    assert not left_out


def test_no_line_from_another_room_is_added_to_a_discussion():
    overheard = row(9, f"{DAY} 12:00:41", "Ilyas", REPLY, device="p4-door-1", room="Entrance")
    later = row(10, f"{DAY} 12:07:00", "Voula", "Καλά, θα τα πούμε το βράδυ.",
                device="p4-door-1", room="Entrance")
    _l0, discussions, left_out = anchored(quarrel()[:3] + [overheard, later])
    assert len(discussions) == 1 and len(discussions[0]["turns"]) == 3
    assert {t.room for t in discussions[0]["turns"]} == {"Kitchen"}
    # The Entrance sentinel heard Ilyas's reply through the wall, and some
    # minutes later Voula alone: the first is overheard, the second is not.
    assert [(alone([s])[0], s["overheard"]) for s in left_out] == [("Ilyas", 1), ("Voula", 0)]


def test_a_conversation_two_rooms_heard_is_read_by_both_and_counted_once():
    copies = [dict(e, ID=100 + e["ID"], Device="box3-2", room="Bedroom_2") for e in dialogue()]
    _l0, discussions, _left = anchored(dialogue() + copies)
    assert sorted(d["room"] for d in discussions) == ["Bedroom_2", "Kitchen"]
    assert all(len(d["turns"]) == 4 for d in discussions), "each sentinel reads what it heard"
    g = T.grid(picture(dialogue() + copies), DAY, 5)
    assert g["who"][IV][NOON] == 3, "three replies, not six"
    assert g["drawn"][IV][NOON]


# ── reading ────────────────────────────────────────────────────────────
def sparse():
    """One line every four minutes or so, noon to 12:17: one discussion, few replies."""
    return [row(1, f"{DAY} 12:00:00", "Voula", TRIGGER),
            row(2, f"{DAY} 12:04:00", "Ilyas", REPLY),
            row(3, f"{DAY} 12:08:00", "Ilyas", LATER),
            row(4, f"{DAY} 12:12:00", "Voula", TRIGGER),
            row(5, f"{DAY} 12:16:30", "Ilyas", REPLY)]


def test_the_model_reads_each_discussion_once_and_whole():
    level0, discussions, _left = anchored(sparse())
    seen = []

    def interpreter(payload):
        seen.append(payload)
        return criticism(payload)

    read = T.read_discussions(level0, CFG, discussions, interpreter)
    assert len(seen) == 1, "one reading per discussion, not one per sentence"
    assert [t.get("speaker") for t in seen[0]["exchange"]] == [
        "Voula", "Ilyas", "Ilyas", "Voula", "Ilyas"]
    assert seen[0]["known_speakers_present"] == ["Ilyas", "Voula"]
    assert seen[0]["context_before"] == [] and seen[0]["context_after"] == []
    assert read[0]["status"] == "interpretable" and read[0]["dimensions"]["tension"] == 0.8


def test_the_lamps_carry_one_reading_across_the_whole_discussion():
    g = T.grid(picture(sparse()), DAY, 5)
    span = range(NOON, NOON + 4)                   # 12:00 to 12:20
    assert [g["lamps"]["tension"][i] for i in span] == [0.8] * 4
    assert [g["lamps"]["warmth"][i] for i in span] == [0.7] * 4
    # connection was withdrawn as a dimension; a reading that still
    # carries it must not put it back on the panel.
    assert "connection" not in g["lamps"]
    assert g["lamps"]["tension"][NOON + 4] is None
    assert not any(g["unscored"]["tension"][i] for i in span)
    # A reply every few minutes is a discussion, and is read as one, but no
    # slot of it holds enough back-and-forth to draw the two as talking.
    assert not any(g["drawn"][IV])


def test_a_failed_call_is_shown_as_a_failure():
    def refuses(payload):
        raise RuntimeError("Error code: 429 - rate limit")

    discussion = picture(quarrel()[:3], refuses)["discussions"][0]
    assert discussion["status"] == "failed"
    assert "429" in discussion["reason"]
    assert all(value is None for value in discussion["dimensions"].values())


# ── the picture ────────────────────────────────────────────────────────
def test_a_pair_is_drawn_only_where_the_two_answer_each_other():
    # Ilyas and Rafael talk; Voula puts in a single line a few minutes later.
    events = [row(1, f"{DAY} 12:00:00", "Ilyas", TRIGGER),
              row(2, f"{DAY} 12:00:30", "Rafael", REPLY),
              row(3, f"{DAY} 12:01:00", "Ilyas", LATER),
              row(4, f"{DAY} 12:01:30", "Rafael", ANSWER),
              row(5, f"{DAY} 12:05:00", "Voula", WHEN),
              row(6, f"{DAY} 12:05:30", "Ilyas", LATER)]
    data = picture(events)
    assert data["discussions"][0]["participants"] == RESIDENTS
    g = T.grid(data, DAY, 5)
    assert g["drawn"][IR][NOON] and g["who"][IR][NOON] == 3
    for label in (IV, RV):
        assert not any(g["drawn"][label]) and not any(g["ongoing"][label]), label
    # The lamps still hold the discussion's one reading across both slots.
    assert g["lamps"]["tension"][NOON] == g["lamps"]["tension"][NOON + 1] == 0.8


def test_a_quiet_slot_inside_a_conversation_shows_it_goes_on():
    events = [row(1, f"{DAY} 12:00:00", "Voula", TRIGGER),
              row(2, f"{DAY} 12:00:40", "Ilyas", REPLY),
              row(3, f"{DAY} 12:01:20", "Voula", WHEN),
              row(4, f"{DAY} 12:05:10", "Ilyas", LATER),
              row(5, f"{DAY} 12:10:00", "Voula", TRIGGER),
              row(6, f"{DAY} 12:10:30", "Ilyas", REPLY),
              row(7, f"{DAY} 12:11:00", "Voula", WHEN)]
    g = T.grid(picture(events), DAY, 5)
    assert g["drawn"][IV][NOON] and g["drawn"][IV][NOON + 2]
    assert not g["drawn"][IV][NOON + 1] and g["ongoing"][IV][NOON + 1]
    assert g["who"][IV][NOON + 1] == 1
    assert not g["ongoing"][IV][NOON + 3]


def test_lone_speech_is_never_read_and_never_drawn():
    seen = []

    def interpreter(payload):
        seen.append(payload)
        return criticism(payload)

    data = picture(dialogue() + rafael_alone(), interpreter)
    assert len(seen) == 1
    assert "Rafael" not in {t.get("speaker") for t in seen[0]["exchange"]}
    g = T.grid(data, DAY, 5)
    assert [label for label, _p in g["rows"]] == [IR, IV, RV]
    half_past = NOON + 6                            # Rafael's lone line
    assert not any(g["drawn"][label][half_past] or g["ongoing"][label][half_past]
                   for label in g["who"])
    assert all(g["lamps"][key][half_past] is None and not g["unscored"][key][half_past]
               for key, _nm, _p in T.LAMPS)
    assert g["who"][IV][NOON] == 3 and g["drawn"][IV][NOON]
    assert g["lamps"]["tension"][NOON] == 0.8
    assert T.build_figure(data, DAY, 5).data


def test_clicking_a_pair_or_a_lamp_finds_its_discussions():
    data = picture(dialogue() + rafael_alone())
    pair = {"points": [{"y": IV, "x": f"{DAY} 12:02:30"}]}
    title, found = T.cell_discussions(data, DAY, 5, pair)
    assert title.startswith(f"{IV} · 12:00–12:05") and [d["id"] for d in found] == ["d0"]
    nobody = {"points": [{"y": IR, "x": f"{DAY} 12:02:30"}]}
    assert T.cell_discussions(data, DAY, 5, nobody) == (None, [])
    lamp = {"points": [{"y": "Tension", "x": f"{DAY} 12:02:30"}]}
    assert T.cell_discussions(data, DAY, 5, lamp)[1]
    empty = {"points": [{"y": "Tension", "x": f"{DAY} 03:02:30"}]}
    assert T.cell_discussions(data, DAY, 5, empty) == (None, [])
    assert [d["id"] for d, _lit in T.moments(data, DAY, 0.55, 0.40)] == ["d0"]


def test_a_discussion_too_short_to_read_is_left_out_of_the_picture():
    # One word that a clip split between two speaker names: too short to send.
    fragment = [row(8, f"{DAY} 16:10:45", "Rafael", "Συγγνώμη.", device="box3-2", room="Bedroom_2"),
                row(9, f"{DAY} 16:10:47", "Voula", "Συγγνώμη.", device="box3-2", room="Bedroom_2")]
    data = picture(dialogue() + fragment)
    assert [d["status"] for d in data["discussions"]] == ["interpretable", "not_read"]
    g = T.grid(data, DAY, 5)
    ten_past_four = 16 * 12 + 2
    assert all(g["lamps"][key][ten_past_four] is None and not g["unscored"][key][ten_past_four]
               for key, _nm, _p in T.LAMPS)
    assert not any(g["who"][label][ten_past_four] or g["ongoing"][label][ten_past_four]
                   for label in g["who"])
    lamp = {"points": [{"y": "Tension", "x": f"{DAY} 16:12:30"}]}
    assert T.cell_discussions(data, DAY, 5, lamp) == (None, [])
    assert g["lamps"]["tension"][NOON] == 0.8, "the discussion that was read is untouched"


def test_the_figure_carries_a_scale_for_each_colour():
    fig = T.build_figure(picture(dialogue()), DAY, 5, 0.55, 0.40)
    blue, green, amber = [trace for trace in fig.data if trace.showscale]
    assert blue.zmin == T.MIN_EXCHANGES and blue.zmax == 3
    assert list(blue.colorbar.ticktext) == ["2", "3 replies"]
    assert list(green.colorbar.tickvals) == [0, 0.55, 1]
    assert list(amber.colorbar.tickvals) == [0, 0.40, 1]
    # Green beside the four hopes on top, amber beside the two worries below.
    assert amber.colorbar.y - amber.colorbar.len < green.colorbar.y - green.colorbar.len


def test_every_heatmap_layer_covers_every_row_of_its_band():
    # plotly.js 3.4 throws on a heatmap with fewer rows than its category axis,
    # which left the viewer with empty axes: every layer lists all its band's rows.
    fig = T.build_figure(picture(dialogue()), DAY, 5, 0.55, 0.40)
    bands = {}
    for trace in fig.data:
        assert len(trace.z) == len(trace.y), trace.name
        bands.setdefault(trace.yaxis, set()).add(tuple(trace.y))
    assert all(len(rows) == 1 for rows in bands.values()), bands


# ── facts ──────────────────────────────────────────────────────────────
def lecture():
    """Rafael says two things, then Ilyas goes on for four lines with no reply."""
    return [row(1, f"{DAY} 12:00:00", "Rafael", JACKET),
            row(2, f"{DAY} 12:00:30", "Rafael", ANSWER),
            row(3, f"{DAY} 12:01:00", "Ilyas", REPLY),
            row(4, f"{DAY} 12:02:00", "Ilyas", LATER),
            row(5, f"{DAY} 12:03:00", "Ilyas", TRIGGER),
            row(6, f"{DAY} 12:04:00", "Ilyas", WHEN)]


def test_the_facts_are_counted_not_inferred():
    discussion = picture(lecture())["discussions"][0]
    measured = T.facts(discussion)
    assert measured["lines"] == {"Ilyas": 4, "Rafael": 2}
    assert measured["replies"] == 1 and measured["answered"] == [(("Ilyas", "Rafael"), 1)]
    assert measured["quiet"]["who"] == "Rafael" and measured["quiet"]["others_lines"] == 4
    # Each person's longest line, in time order: Rafael's at 12:00:30, Ilyas's at 12:01.
    assert [(t["speaker"], t["time"][11:19]) for t in measured["quotes"]] == [
        ("Rafael", "12:00:30"), ("Ilyas", "12:01:00")]
    assert T.fact_line(discussion) == "Ilyas 4 · Rafael 2 lines · 1 reply · no line from Rafael after 12:00"


def test_the_detail_window_opens_with_what_was_measured():
    discussion = picture(lecture())["discussions"][0]
    loudness = {"median": 3.2, "loudest": 5.4, "usual": 70.1, "clips": 6}
    detail = json.dumps(T.discussion_detail(discussion, 0.55, 0.40, lambda _d: loudness),
                        cls=plotly.utils.PlotlyJSONEncoder, ensure_ascii=False)
    for fact in ("What was measured", "Ilyas answered Rafael 1", "+3 dB", "loudest clip +5 dB",
                 "After Rafael's last line at 12:00:30, Ilyas spoke 4 more lines over 4 min."):
        assert fact in detail, fact
    assert detail.index("What was measured") < detail.index("What the model concluded")


def test_hover_and_key_moments_carry_the_facts():
    data = picture(dialogue())
    fig = T.build_figure(data, DAY, 5)
    blue = next(trace for trace in fig.data if trace.showscale and trace.zmin == T.MIN_EXCHANGES)
    assert "Ilyas 2 · Voula 2 lines · 3 replies" in blue.text[1][NOON]
    cards = json.dumps(T.render_cards(data, DAY, 0.55, 0.40), cls=plotly.utils.PlotlyJSONEncoder,
                       ensure_ascii=False)
    assert "Ilyas 2 · Voula 2 lines" in cards and "3 replies" in cards


def test_a_chosen_discussion_opens_on_top_and_leaves_the_key_moments():
    later = [dict(e, ID=50 + e["ID"], Timestamp=e["Timestamp"].replace(" 12:", " 14:"))
             for e in lecture()]
    data = picture(dialogue() + later)
    first, second = [d["id"] for d in data["discussions"]]
    chosen, key_moments = T.render_cards(data, DAY, 0.55, 0.40, selected=[second],
                                         expanded={second})

    def text(cards):
        return json.dumps(cards, cls=plotly.utils.PlotlyJSONEncoder, ensure_ascii=False)

    assert f'"index": "{second}"' in text(chosen) and "What was measured" in text(chosen)
    # Shown once: the chosen one is not repeated below, and the rest stay closed.
    assert f'"index": "{second}"' not in text(key_moments)
    assert f'"index": "{first}"' in text(key_moments) and "What was measured" not in text(key_moments)
    closed = text(T.card(data["discussions"][0], 0.55, 0.40))
    assert '"open"' in closed and "What was measured" not in closed


def test_a_transcription_loop_is_counted_once_and_given_once():
    loop = [row(20 + k, f"{DAY} 12:00:{k * 2:02d}", "Rafael", "Ευχαριστούμε!") for k in range(10)]
    talk = [row(40, f"{DAY} 12:00:30", "Ilyas", REPLY), row(41, f"{DAY} 12:01:00", "Rafael", ANSWER)]
    seen = []

    def interpreter(payload):
        seen.append(payload)
        return criticism(payload)

    discussion = picture(loop + talk, interpreter)["discussions"][0]
    measured = T.facts(discussion)
    assert measured["lines"] == {"Rafael": 2, "Ilyas": 1}
    assert [(item["speaker"], item["count"]) for item in measured["loops"]] == [("Rafael", 10)]
    given = [(t["speaker"], t.get("repeated")) for t in seen[0]["exchange"] if "speaker" in t]
    assert given == [("Rafael", 10), ("Ilyas", None), ("Rafael", None)]


def test_a_complaint_and_its_reader_are_shown():
    def vents(_payload):
        return reading(["complaint", "support"], {"tension": 0.5, "warmth": 0.7},
                       complaints=[{"by": "Ilyas", "about": "outsider", "who": None}])

    data = picture(dialogue(), vents)
    data.update(model="chatgpt", model_label="ChatGPT (gpt-4o-mini)", prompt_sha256="abcdef123456",
                prompt_file="LLM_prompt_L1_5.txt", read_at="2026-09-15T14:52:35")
    discussion = data["discussions"][0]
    assert discussion["complaints"] == [{"by": "Ilyas", "about": "outsider", "who": None}]
    shown = json.dumps(T.card(discussion, 0.55, 0.40, expanded=True, provenance=T.provenance(data)),
                       cls=plotly.utils.PlotlyJSONEncoder, ensure_ascii=False)
    assert "Complaint: Ilyas about someone outside the family" in shown
    assert "Level 1.5 reading by ChatGPT (gpt-4o-mini) · prompt LLM_prompt_L1_5.txt, version abcdef12" in shown
