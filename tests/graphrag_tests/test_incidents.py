"""The incident layer: listing, settling spans, storing, asking.

No model is contacted. The reader is a function, as it is in production, and
here it answers from a script -- so what is tested is everything the layer
does WITH a reading: which times and names it believes, how it settles a
span, what it stores, and what an answer is allowed to say when a day was
never read.
"""

import os
import sys
import tempfile
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from graphrag import incidents as inc                              # noqa: E402
from graphrag import llm as gllm                                   # noqa: E402
from graphrag.engine import Graph, focus_incidents                 # noqa: E402
from graphrag.engine import incidents as incidents_op             # noqa: E402
from graphrag.schema import INCIDENT_KINDS, validate_plan          # noqa: E402

DAY = "2026-10-02"


def seg(number, clock, speaker, text, room="Kitchen"):
    return {"id": f"seg::{number}", "kind": "segment", "day": DAY,
            "timestamp": f"{DAY} {clock}", "speaker": speaker, "text": text,
            "room": room, "event_id": 1000 + number, "words": len(text.split()),
            "audio_file": f"{number}.wav"}


def a_day(segments, extra_nodes=()):
    nodes = [{"id": "day::" + DAY, "kind": "day", "date": DAY}]
    nodes += [{"id": f"person::{p}", "kind": "person", "name": p}
              for p in ("Ilyas", "Voula", "Rafael")]
    nodes += list(segments) + list(extra_nodes)
    return {"date": DAY, "nodes": nodes, "edges": [
        {"src": s["id"], "dst": "day::" + DAY, "kind": "on_day"} for s in segments]}


def graph_of(day, read=True):
    return Graph({
        "people": ["Ilyas", "Rafael", "Voula"], "days": [DAY],
        "nodes": day["nodes"], "edges": day["edges"],
        "dyad_series": {}, "person_series": {}, "roles": {},
        "incident_layer": ({DAY: day["incident_layer"]}
                           if read and day.get("incident_layer") else {}),
    })


class Script:
    """A reader that answers by what the prompt is asking for."""

    def __init__(self, listing=None, delimit=None, follow=None):
        self.listing, self.delimit, self.follow = listing, delimit, follow
        self.asked = []

    def __call__(self, prompt):
        lines = prompt.split("LINES:\n", 1)[1]
        if "List every notable event" in prompt:
            self.asked.append("list")
            return self.listing(lines) if self.listing else {"events": []}
        if "Find where it begins" in prompt:
            self.asked.append("delimit")
            return self.delimit(lines) if self.delimit else None
        if "Do these lines belong" in prompt:
            self.asked.append("follow")
            return self.follow(prompt, lines) if self.follow else {"continues": "no"}
        raise AssertionError("an unexpected prompt")


QUARREL = [
    seg(1, "20:11:24", "Ilyas", "you are rude"),
    seg(2, "20:12:24", "Rafael", "and you are fat"),
    seg(3, "20:16:05", "Ilyas", "that is no way to speak to a father"),
    seg(4, "20:18:13", "Rafael", "leave me alone", room="Entrance"),
    seg(5, "20:22:40", "Voula", "who wants tea"),
]


class Windows(unittest.TestCase):
    def test_five_minute_bins_in_time_order(self):
        windows = inc.windows_for(QUARREL)
        self.assertEqual([w["lo"] for w in windows],
                         [20 * 3600 + 10 * 60, 20 * 3600 + 15 * 60, 20 * 3600 + 20 * 60])
        self.assertEqual(windows[0]["times"], ["20:11:24", "20:12:24"])

    def test_the_copy_a_second_microphone_makes_is_shown_once(self):
        twice = [seg(1, "20:11:24", "Ilyas", "you are rude"),
                 seg(2, "20:11:24", "Ilyas", "you are rude", room="Entrance"),
                 seg(3, "20:11:30", "Ilyas", "you are very rude")]
        self.assertEqual(len(inc.windows_for(twice)[0]["lines"]), 2)


class Listing(unittest.TestCase):
    def test_it_keeps_only_times_and_names_that_are_on_the_lines(self):
        def listing(lines):
            if "you are rude" not in lines:
                return {"events": []}
            return {"events": [{"kind": "quarrel", "who": ["Ilyas", "Rafael", "Anna"],
                                "about": "manners", "what": "they argue",
                                "first_time": "20:11:24", "last_time": "20:14:00"}]}
        found, stats = inc.list_incidents(QUARREL, Script(listing))
        self.assertEqual(len(found), 1)
        one = found[0]
        self.assertEqual(one["who"], ["Ilyas", "Rafael"])      # Anna is on no line
        self.assertEqual(one["first"], "20:11:24")
        # 20:14:00 is not the time of any line, so the window's own last line
        # stands in for it: an incident is never placed where nothing was said.
        self.assertEqual(one["last"], "20:12:24")
        self.assertEqual(stats, {"windows": 3, "windows_failed": 0})

    def test_a_kind_outside_the_vocabulary_is_not_an_incident(self):
        found, _ = inc.list_incidents(QUARREL, Script(lambda lines: {"events": [
            {"kind": "gossip", "who": ["Ilyas"], "first_time": "20:11:24",
             "last_time": "20:11:24"}]}))
        self.assertEqual(found, [])
        self.assertNotIn("gossip", INCIDENT_KINDS)

    def test_a_window_nobody_read_is_counted_not_called_quiet(self):
        found, stats = inc.list_incidents(QUARREL, lambda prompt: None)
        self.assertEqual(found, [])
        self.assertEqual(stats["windows_failed"], 3)


class Spans(unittest.TestCase):
    def listed(self, kind, first, last, who):
        return {"kind": kind, "who": who, "about": "x", "what": "something",
                "first": first, "last": last, "seen_first": first,
                "seen_last": last, "span_method": "listed"}

    def test_a_quarrel_is_followed_until_the_conflict_stops(self):
        def follow(prompt, lines):
            if "just before" in prompt and "no way to speak" in lines:
                return {"continues": "yes", "last_time": "20:18:13"}
            return {"continues": "no"}
        one = self.listed("quarrel", "20:11:24", "20:12:24", ["Ilyas", "Rafael"])
        reader = Script(follow=follow)
        inc.settle_spans([one], QUARREL, reader)
        self.assertEqual((one["first"], one["last"]), ("20:11:24", "20:18:13"))
        self.assertEqual(one["span_method"], "followed")
        # forward into 20:15 (yes), forward into 20:20 (no); nothing lies before
        self.assertEqual(reader.asked, ["follow", "follow"])

    def test_a_quarrel_is_followed_a_bounded_distance(self):
        long = [seg(n, f"20:{m:02d}:00", "Ilyas", f"line {n}")
                for n, m in enumerate(range(0, 59, 5), 1)]
        one = self.listed("quarrel", "20:00:00", "20:00:00", ["Ilyas"])
        inc.settle_spans([one], long, Script(
            follow=lambda prompt, lines: {"continues": "yes"}))
        self.assertEqual(one["last"], f"20:{5 * inc.FOLLOW_LIMIT:02d}:00")

    def test_a_reader_that_fails_half_way_leaves_the_span_as_listed(self):
        """Followed must mean the pass finished, not that it was begun."""
        calls = {"n": 0}

        def follow(prompt, lines):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"continues": "yes", "last_time": "20:18:13"}
            return None                          # the connection dropped
        one = self.listed("quarrel", "20:11:24", "20:12:24", ["Ilyas", "Rafael"])
        unsettled = inc.settle_spans([one], QUARREL, Script(follow=follow))
        self.assertEqual(unsettled, 1)
        self.assertEqual((one["first"], one["last"]), ("20:11:24", "20:12:24"))
        self.assertEqual(one["span_method"], "listed")

    def test_a_complaint_is_delimited_not_followed(self):
        talk = [seg(1, "15:14:01", "Voula", "the road is closed"),
                seg(2, "15:22:09", "Voula", "the morning shift wanted to leave"),
                seg(3, "15:26:52", "Voula", "you know what complaints I have"),
                seg(4, "15:27:24", "Voula", "nobody listens there"),
                seg(5, "15:33:10", "Ilyas", "which payroll is this")]
        one = self.listed("complaint", "15:26:52", "15:26:52", ["Voula"])
        reader = Script(delimit=lambda lines: {"first_time": "15:22:09",
                                               "last_time": "15:27:24"})
        inc.settle_spans([one], talk, reader)
        self.assertEqual((one["first"], one["last"]), ("15:22:09", "15:27:24"))
        self.assertEqual(one["span_method"], "delimited")
        self.assertEqual(reader.asked, ["delimit"])

    def test_a_span_the_reader_cannot_place_stays_as_listed(self):
        talk = [seg(1, "15:26:52", "Voula", "you know what complaints I have")]
        one = self.listed("complaint", "15:26:52", "15:26:52", ["Voula"])
        inc.settle_spans([one], talk, Script(
            delimit=lambda lines: {"first_time": "15:00:00", "last_time": "16:00:00"}))
        self.assertEqual((one["first"], one["last"]), ("15:26:52", "15:26:52"))
        self.assertEqual(one["span_method"], "listed")

    def test_other_kinds_keep_the_span_they_were_listed_with(self):
        one = self.listed("warm_moment", "20:22:40", "20:22:40", ["Voula"])
        reader = Script()
        inc.settle_spans([one], QUARREL, reader)
        self.assertEqual(reader.asked, [])
        self.assertEqual(one["span_method"], "listed")


def quarrel_reader():
    def listing(lines):
        if "you are rude" in lines:
            return {"events": [{"kind": "quarrel", "who": ["Ilyas", "Rafael"],
                                "about": "manners", "what": "Ilyas and Rafael argue",
                                "first_time": "20:11:24", "last_time": "20:12:24"}]}
        return {"events": []}

    def follow(prompt, lines):
        if "just before" in prompt and "no way to speak" in lines:
            return {"continues": "yes", "last_time": "20:18:13"}
        return {"continues": "no"}
    return Script(listing, follow=follow)


class Layer(unittest.TestCase):
    def test_it_is_written_into_the_day_with_its_words(self):
        day = a_day(QUARREL)
        meta = inc.add_incident_layer(day, quarrel_reader(), model="test")
        self.assertEqual(meta["incidents"], 1)
        self.assertEqual(meta["windows_failed"], 0)
        node = next(n for n in day["nodes"] if n["kind"] == "incident")
        self.assertEqual(node["type"], "quarrel")
        self.assertEqual((node["start"], node["end"]),
                         (f"{DAY}T20:11:24", f"{DAY}T20:18:13"))
        self.assertEqual(node["listed_end"], f"{DAY}T20:12:24")
        self.assertEqual(node["rooms"], ["Entrance", "Kitchen"])
        words = [e["dst"] for e in day["edges"]
                 if e["src"] == node["id"] and e["kind"] == "evidenced_by"]
        self.assertEqual(words, ["seg::1", "seg::2", "seg::3", "seg::4"])
        people = [e["src"] for e in day["edges"]
                  if e["dst"] == node["id"] and e["kind"] == "participated"]
        self.assertEqual(sorted(people), ["person::Ilyas", "person::Rafael"])

    def test_what_the_reader_did_not_answer_is_counted(self):
        day = a_day(QUARREL)
        reader = quarrel_reader()
        reader.follow = lambda prompt, lines: None
        meta = inc.add_incident_layer(day, reader)
        self.assertEqual(meta["spans_unsettled"], 1)
        node = next(n for n in day["nodes"] if n["kind"] == "incident")
        self.assertEqual(node["span_method"], "listed")
        self.assertEqual(node["end"], f"{DAY}T20:12:24")

    def test_adding_it_twice_replaces_it_and_touches_nothing_else(self):
        day = a_day(QUARREL)
        before = ([n for n in day["nodes"]], [e for e in day["edges"]])
        inc.add_incident_layer(day, quarrel_reader())
        inc.add_incident_layer(day, quarrel_reader())
        self.assertEqual(sum(1 for n in day["nodes"] if n["kind"] == "incident"), 1)
        inc.strip_layer(day)
        self.assertEqual((day["nodes"], day["edges"]), before)
        self.assertNotIn("incident_layer", day)


class Asking(unittest.TestCase):
    def setUp(self):
        exchange = {"id": "exchange::1", "kind": "exchange",
                    "start": f"{DAY}T20:10:11", "end": f"{DAY}T20:15:44",
                    "participants": ["Ilyas", "Rafael"],
                    "dimensions": {"tension": 1.0}}
        self.day = a_day(QUARREL, [exchange])
        inc.add_incident_layer(self.day, quarrel_reader())

    def test_a_quarrel_comes_back_with_its_start_its_end_and_its_sharpness(self):
        result = incidents_op(graph_of(self.day), kind="quarrel", day=DAY)
        self.assertEqual(result["incidents_found"], 1)
        row = result["incidents"][0]
        self.assertEqual((row["start"], row["end"]), ("20:11:24", "20:18:13"))
        self.assertEqual(row["tension"], 1.0)
        self.assertTrue(result["assessment"].startswith("Yes."))
        self.assertIn("20:11 to 20:18", result["assessment"])
        self.assertTrue(result["evidence"])

    def test_nothing_found_on_a_day_that_was_read_is_a_no(self):
        result = incidents_op(graph_of(self.day), kind="reconciliation", day=DAY)
        self.assertEqual(result["incidents_found"], 0)
        self.assertTrue(result["assessment"].startswith("No."))

    def test_a_day_never_read_is_not_a_day_on_which_nothing_happened(self):
        result = incidents_op(graph_of(a_day(QUARREL)), kind="quarrel", day=DAY)
        self.assertEqual(result["days_never_read_for_incidents"], [DAY])
        self.assertTrue(result["assessment"].startswith("Nothing can be said"))

    def test_it_can_be_narrowed_to_a_person(self):
        g = graph_of(self.day)
        self.assertEqual(incidents_op(g, person="Rafael")["incidents_found"], 1)
        self.assertEqual(incidents_op(g, person="Voula")["incidents_found"], 0)

    def test_an_incident_no_exchange_overlaps_is_unmeasured_not_calm(self):
        day = a_day(QUARREL)
        inc.add_incident_layer(day, quarrel_reader())
        row = incidents_op(graph_of(day), kind="quarrel")["incidents"][0]
        self.assertIsNone(row["tension"])

    def test_the_choice_decides_the_verdict(self):
        g = graph_of(self.day)
        result = incidents_op(g, day=DAY)
        chosen = focus_incidents(g, result, [result["incidents"][0]["id"]])
        self.assertTrue(chosen["assessment"].startswith("Yes."))
        self.assertEqual(chosen["incidents_considered"], 1)
        nothing = focus_incidents(g, result, [])
        self.assertEqual(nothing["incidents"], [])
        self.assertTrue(nothing["assessment"].startswith("No."))
        self.assertEqual(nothing["evidence"], [])


class Selecting(unittest.TestCase):
    ROWS = [{"id": "incident::a", "day": DAY, "start": "15:22:09", "end": "15:27:24",
             "participants": ["Voula"], "kind": "complaint", "about": "her work",
             "what": "Voula complains about her work"},
            {"id": "incident::b", "day": DAY, "start": "15:14:01", "end": "15:14:01",
             "participants": ["Voula"], "kind": "complaint", "about": "the road",
             "what": "Voula complains about the road"}]

    def answer(self, reply):
        original = gllm._call
        gllm._call = lambda *args, **kwargs: reply
        self.addCleanup(setattr, gllm, "_call", original)
        return gllm.select_incidents("did anyone complain about work", self.ROWS)

    def test_numbers_become_the_ids_they_stand_for(self):
        self.assertEqual(self.answer('{"ids": [1]}'), ["incident::a"])

    def test_a_number_that_is_not_on_the_list_is_ignored(self):
        self.assertEqual(self.answer('{"ids": [7, "x", 2, 2]}'), ["incident::b"])

    def test_no_answer_is_not_the_same_as_none_chosen(self):
        self.assertEqual(self.answer('{"ids": []}'), [])
        self.assertIsNone(self.answer("not json"))
        self.assertIsNone(self.answer(None))


class Definitions(unittest.TestCase):
    def test_the_diary_is_shown_the_definitions_the_reader_was_given(self):
        self.assertEqual(set(inc.KIND_DEFINITIONS), set(INCIDENT_KINDS))
        for kind, meaning in inc.KIND_DEFINITIONS.items():
            line = next(l for l in inc.EXTRACT.splitlines() if l.strip().startswith(kind))
            self.assertIn(meaning, line)
        self.assertIn("Ordinary conversation is not an event", inc.EXTRACT)


class Memory(unittest.TestCase):
    def test_only_what_somebody_filled_in_is_kept(self):
        text = ("# a note for the person filling this in\n"
                "HOUSEHOLD\n"
                "  Who lives here:            a couple and their son\n"
                "  Town or area:              ____\n"
                "\n"
                "VOULA  (female, adult)\n"
                "  Work, and where:           works at a school\n"
                "  Her role there:            ____            (teacher of what)\n"
                "  Usual working hours:       mornings            (when she is out)\n"
                "\n"
                "ROOMS\n"
                "  Kitchen:                   ____\n")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "memory.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            memory = inc.load_household_memory(path)
        self.assertIn("Who lives here: a couple and their son", memory)
        self.assertIn("Work, and where: works at a school", memory)
        self.assertIn("Usual working hours: mornings", memory)
        self.assertNotIn("when she is out", memory)        # the hint, not the answer
        self.assertNotIn("____", memory)
        self.assertNotIn("ROOMS", memory)                  # nothing was filled in
        self.assertNotIn("note for the person", memory)

    def test_the_reader_is_told_who_people_are_and_not_what_to_expect(self):
        text = ("HOUSEHOLD\n"
                "  Who lives here:            a couple and their son\n"
                "  Town or area:              a town by the sea\n"
                "\n"
                "VOULA  (female, adult)\n"
                "  Work, and where:           works at a school\n"
                "  Her role there:            teacher\n"
                '  What "work" means for her: the school, her colleagues\n'
                "  Usual working hours:       mornings\n"
                "  Recurring worries or pressures:     the school has no money\n"
                "\n"
                "RAFAEL  (male, child)\n"
                "  Age and school year:       15\n"
                "  School:                    the school in town\n"
                "\n"
                "HOW THIS FAMILY TALKS\n"
                "  What a real quarrel sounds like here:      loud\n")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "memory.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            told = inc.reader_memory(path)
            everything = inc.load_household_memory(path)
        self.assertEqual(told.splitlines(), [
            "HOUSEHOLD",
            "  Who lives here: a couple and their son",
            "VOULA  (female, adult)",
            "  Work, and where: works at a school",
            "  Her role there: teacher",
            '  What "work" means for her: the school, her colleagues',
            "RAFAEL  (male, child)",
            "  School: the school in town",
        ])
        # Nothing is lost from the file; it is only not given to the reader.
        self.assertIn("Recurring worries or pressures: the school has no money",
                      everything)
        self.assertIn("HOW THIS FAMILY TALKS", everything)

    def test_how_much_the_reader_was_told_is_kept_with_the_list(self):
        day = a_day(QUARREL)
        meta = inc.add_incident_layer(day, quarrel_reader(),
                                      memory="HOUSEHOLD\n  Who lives here: three")
        self.assertTrue(meta["household_memory"])
        self.assertEqual(meta["household_memory_lines"], 2)
        meta = inc.add_incident_layer(day, quarrel_reader())
        self.assertFalse(meta["household_memory"])
        self.assertEqual(meta["household_memory_lines"], 0)

    def test_no_file_is_no_memory(self):
        self.assertEqual(inc.load_household_memory("/no/such/file.txt"), "")
        self.assertEqual(inc.reader_memory("/no/such/file.txt"), "")
        self.assertEqual(inc.memory_block(""), "")


class Planning(unittest.TestCase):
    PEOPLE, DAYS = ["Ilyas", "Rafael", "Voula"], [DAY]

    def test_an_invented_kind_is_refused(self):
        ok, why = validate_plan({"operation": "incidents",
                                 "arguments": {"kind": "gossip"}}, self.PEOPLE, self.DAYS)
        self.assertFalse(ok)
        self.assertIn("gossip", why)
        ok, _ = validate_plan({"operation": "incidents",
                               "arguments": {"kind": "quarrel", "day": DAY}},
                              self.PEOPLE, self.DAYS)
        self.assertTrue(ok)

    def plan(self, question):
        return gllm.fallback_plan(question, self.PEOPLE, self.DAYS)

    def test_whether_and_when_go_to_the_incident_list(self):
        plan = self.plan("Was there a quarrel today? When did it start and finish?")
        self.assertEqual(plan["operation"], "incidents")
        self.assertEqual(plan["arguments"], {"kind": "quarrel", "day": DAY})
        plan = self.plan("Did they make up after the argument?")
        self.assertEqual(plan["arguments"]["kind"], "reconciliation")
        plan = self.plan("Did Voula complain about her work?")
        self.assertEqual(plan["arguments"], {"kind": "complaint", "person": "Voula"})

    def test_ranking_questions_stay_where_they_were(self):
        self.assertEqual(self.plan("Find me the fights.")["operation"], "tension_moments")
        self.assertEqual(self.plan("What do they complain about?")["operation"], "complaints")
        self.assertEqual(self.plan("Is this a happy home?")["operation"], "family_summary")

    def tightened(self, question, operation, arguments):
        return gllm.tighten({"operation": operation, "arguments": arguments, "why": ""},
                            question, self.PEOPLE, {}, self.DAYS)

    def test_a_planner_that_reaches_for_a_ranking_is_put_right(self):
        """What the local model did with the work-complaint question."""
        plan = self.tightened(
            f"Did a member of the household complain on {DAY} about the rough "
            f"conditions at his or her work?", "complaints", {"day": DAY})
        self.assertEqual(plan["operation"], "incidents")
        self.assertEqual(plan["arguments"], {"day": DAY, "kind": "complaint"})
        self.assertIn("incidents and not complaints", plan["why"])

    def test_a_planner_that_was_right_is_left_alone(self):
        plan = self.tightened("What do they complain about?", "complaints", {})
        self.assertEqual(plan["operation"], "complaints")
        plan = self.tightened("Find me the fights.", "tension_moments", {})
        self.assertEqual(plan["operation"], "tension_moments")
        plan = self.tightened("Was there a quarrel?", "incidents", {"kind": "quarrel"})
        self.assertEqual(plan["arguments"], {"kind": "quarrel"})

    def test_only_a_question_that_names_a_subject_is_narrowed_by_a_model(self):
        self.assertTrue(gllm.names_a_subject("Did anyone complain about their work?"))
        self.assertFalse(gllm.names_a_subject(
            "Was there a quarrel? Between whom, and when did it start and finish?"))


if __name__ == "__main__":
    unittest.main()
