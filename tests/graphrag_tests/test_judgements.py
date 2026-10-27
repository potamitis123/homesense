"""What the residents said about the listed incidents: the log and the count.

No model is contacted and no store is opened. The day is built in memory, as
in test_incidents, and every answer is written to a temporary file -- never to
the household's own record.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from graphrag import incidents as inc                              # noqa: E402
from graphrag import judgements as jud                             # noqa: E402
from tests.graphrag_tests.test_incidents import (                  # noqa: E402
    DAY, QUARREL, a_day, graph_of, quarrel_reader)

MORNING = datetime(2026, 10, 2, 0, 0, 0)
NIGHT = datetime(2026, 10, 2, 23, 59, 59)


class Record(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = os.path.join(self.folder.name, "judgements.jsonl")
        self.day = a_day(QUARREL)
        inc.add_incident_layer(self.day, quarrel_reader(), model="test")
        self.graph = graph_of(self.day)

    def listed(self, start=MORNING, end=NIGHT, graph=None):
        return jud.listed_between(start, end, graph=graph or self.graph,
                                  path=self.path)

    def the_quarrel(self):
        return self.listed()["rows"][0]

    def lines_on_file(self):
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(text) for text in fh]


class Listing(Record):
    def test_an_incident_comes_with_its_span_its_people_and_its_words(self):
        out = self.listed()
        self.assertEqual(out["tally"], {"listed": 1, "confirmed": 0,
                                        "rejected": 0, "unjudged": 1})
        row = out["rows"][0]
        self.assertEqual((row["kind"], row["start"], row["end"]),
                         ("quarrel", "20:11:24", "20:18:13"))
        self.assertEqual(row["participants"], ["Ilyas", "Rafael"])
        self.assertIsNone(row["verdict"])
        self.assertEqual([line["time"] for line in row["lines"]],
                         ["20:11:24", "20:12:24", "20:16:05", "20:18:13"])
        self.assertEqual(row["lines"][0]["speaker"], "Ilyas")

    def test_only_what_overlaps_the_window_is_listed(self):
        before = self.listed(MORNING, datetime(2026, 10, 2, 20, 0, 0))
        self.assertEqual(before["rows"], [])
        self.assertIn(DAY, before["read"])              # read, and nothing in it
        # It began before the window opened and was still going on inside it.
        across = self.listed(datetime(2026, 10, 2, 20, 15, 0), NIGHT)
        self.assertEqual(len(across["rows"]), 1)

    def test_a_day_never_read_is_not_a_day_with_nothing_in_it(self):
        unread = graph_of(a_day(QUARREL), read=False)
        out = self.listed(graph=unread)
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["read"], {})
        self.assertEqual(out["not_listed"], [DAY])      # stored, never read
        self.assertEqual(out["not_stored"], [])

        week = self.listed(datetime(2026, 10, 1, 0, 0), datetime(2026, 10, 3, 23, 59))
        self.assertEqual(week["not_stored"], ["2026-10-01", "2026-10-03"])
        self.assertEqual(list(week["read"]), [DAY])
        self.assertEqual(len(week["rows"]), 1)


class Judging(Record):
    def test_a_verdict_is_appended_with_what_it_was_about(self):
        key = self.the_quarrel()["key"]
        self.assertTrue(jud.judge(key, "confirmed", path=self.path,
                                  graph=self.graph,
                                  now=datetime(2026, 10, 3, 21, 0, 0)))
        (line,) = self.lines_on_file()
        self.assertEqual(line["verdict"], "confirmed")
        self.assertEqual(line["judged_at"], "2026-10-03T21:00:00")
        self.assertEqual(line["incident"]["kind"], "quarrel")
        self.assertEqual((line["incident"]["start"], line["incident"]["end"]),
                         ("20:11:24", "20:18:13"))
        self.assertEqual(line["incident"]["participants"], ["Ilyas", "Rafael"])
        self.assertEqual(line["reading"]["model"], "test")
        self.assertEqual(self.the_quarrel()["verdict"], "confirmed")
        self.assertEqual(self.listed()["tally"]["confirmed"], 1)

    def test_a_changed_mind_is_kept_as_a_changed_mind(self):
        key = self.the_quarrel()["key"]
        jud.judge(key, "confirmed", path=self.path, graph=self.graph)
        jud.judge(key, "rejected", path=self.path, graph=self.graph)
        self.assertEqual(self.the_quarrel()["verdict"], "rejected")
        self.assertEqual([l["verdict"] for l in self.lines_on_file()],
                         ["confirmed", "rejected"])       # nothing was rewritten

    def test_a_verdict_can_be_taken_back(self):
        key = self.the_quarrel()["key"]
        jud.judge(key, "rejected", path=self.path, graph=self.graph)
        jud.judge(key, None, path=self.path, graph=self.graph)
        self.assertIsNone(self.the_quarrel()["verdict"])
        self.assertEqual(self.listed()["tally"]["unjudged"], 1)
        self.assertEqual(len(self.lines_on_file()), 2)

    def test_the_same_answer_twice_is_written_once(self):
        # A second window still showing the old state: its button says
        # "confirm", and confirming a confirmed incident changes nothing.
        key = self.the_quarrel()["key"]
        self.assertTrue(jud.judge(key, "confirmed", path=self.path, graph=self.graph))
        self.assertFalse(jud.judge(key, "confirmed", path=self.path, graph=self.graph))
        self.assertEqual(len(self.lines_on_file()), 1)

    def test_taking_back_what_was_never_given_writes_nothing(self):
        key = self.the_quarrel()["key"]
        self.assertFalse(jud.judge(key, None, path=self.path, graph=self.graph))
        self.assertFalse(os.path.exists(self.path))

    def test_only_the_two_verdicts_exist(self):
        with self.assertRaises(ValueError):
            jud.judge(self.the_quarrel()["key"], "maybe", path=self.path,
                      graph=self.graph)
        self.assertFalse(os.path.exists(self.path))

    def test_a_damaged_line_does_not_hide_the_others(self):
        key = self.the_quarrel()["key"]
        jud.judge(key, "confirmed", path=self.path, graph=self.graph)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{ this is not json\n\n")
        self.assertEqual(self.the_quarrel()["verdict"], "confirmed")


class ReadAgain(Record):
    """Reading a day again gives the same ids to a new list."""

    def read_again(self):
        again = a_day(QUARREL)
        inc.add_incident_layer(again, quarrel_reader(), model="test")
        again["incident_layer"]["read_at"] = "2030-01-01T00:00:00"
        return graph_of(again)

    def test_the_new_list_starts_unjudged_and_the_old_answer_stays(self):
        old_key = self.the_quarrel()["key"]
        jud.judge(old_key, "confirmed", path=self.path, graph=self.graph)

        new = self.read_again()
        row = self.listed(graph=new)["rows"][0]
        self.assertEqual(row["id"], self.the_quarrel()["id"])      # the same id
        self.assertNotEqual(row["key"], old_key)                   # another reading
        self.assertIsNone(row["verdict"])
        self.assertEqual(jud.standing(self.path)[old_key]["verdict"], "confirmed")

    def test_an_answer_about_a_replaced_list_is_refused(self):
        old_key = self.the_quarrel()["key"]
        with self.assertRaises(LookupError):
            jud.judge(old_key, "confirmed", path=self.path, graph=self.read_again())
        self.assertFalse(os.path.exists(self.path))

    def test_a_key_that_names_nothing_is_refused(self):
        for key in ("", "nonsense", f"{DAY}|x|incident::{DAY}::9999"):
            with self.assertRaises(LookupError):
                jud.judge(key, "confirmed", path=self.path, graph=self.graph)


class Counting(Record):
    def test_precision_is_counted_by_kind_over_what_stands(self):
        key = self.the_quarrel()["key"]
        jud.judge(key, "rejected", path=self.path, graph=self.graph)
        jud.judge(key, "confirmed", path=self.path, graph=self.graph)
        summary = jud.summarise(self.path)
        self.assertEqual(summary["by_kind"], {"quarrel": {"confirmed": 1}})
        self.assertEqual((summary["confirmed"], summary["rejected"]), (1, 0))
        self.assertEqual(summary["days"], [DAY])
        self.assertEqual(jud.summarise(self.path, since="2026-10-03")["confirmed"], 0)

    def test_the_interval_is_wide_when_little_was_judged(self):
        low, high = jud.wilson(1, 1)
        self.assertLess(low, 0.25)                 # one confirmation proves little
        self.assertEqual(high, 1.0)
        low, high = jud.wilson(18, 20)
        self.assertTrue(0.68 < low < 0.72 and 0.96 < high < 0.99)
        self.assertIsNone(jud.wilson(0, 0))


if __name__ == "__main__":
    unittest.main()
