"""The residents' diary: closed before the list is shown, and how it is scored.

Everything is written to a temporary file; no store is opened.
"""

import os
import sys
import tempfile
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from graphrag import diary                                         # noqa: E402

DAY = "2026-10-09"


def listed(number, kind, start, end):
    return {"id": f"incident::{DAY}::{number:04d}", "kind": kind, "day": DAY,
            "start": start, "end": end, "participants": ["Ilyas", "Voula"]}


class Diary(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "diary.jsonl")
        self.judged = os.path.join(folder.name, "judgements.jsonl")

    def add(self, kind, start, end=None, **more):
        return diary.add(DAY, kind, start, end, path=self.path, **more)

    def seal(self):
        return diary.seal(DAY, path=self.path, judged_path=self.judged)

    def recall(self, rows, margin=5):
        return diary.recall({DAY: rows}, path=self.path, margin=margin)


class Writing(Diary):
    def test_an_entry_is_kept_with_its_kind_span_and_people(self):
        self.add("quarrel", "18:20", "18:40", who=["Voula", "Ilyas"], note=" the car ")
        (entry,) = diary.day_state(DAY, self.path)["entries"]
        self.assertEqual((entry["kind"], entry["start"], entry["end"]),
                         ("quarrel", "18:20", "18:40"))
        self.assertEqual((entry["who"], entry["note"]), (["Ilyas", "Voula"], "the car"))

    def test_a_single_time_is_a_moment(self):
        self.add("worry", "09:05")
        self.assertEqual(diary.day_state(DAY, self.path)["entries"][0]["end"], "09:05")

    def test_what_is_not_an_incident_or_not_a_time_is_refused(self):
        for bad in (("gossip", "18:20", None), ("quarrel", "25:00", None),
                    ("quarrel", "18:40", "18:20")):
            with self.assertRaises(ValueError):
                self.add(*bad)
        self.assertFalse(os.path.exists(self.path))

    def test_an_entry_can_be_taken_out_before_the_day_is_closed(self):
        first = self.add("quarrel", "18:20")
        self.add("worry", "21:00")
        diary.remove(DAY, first["id"], path=self.path)
        self.assertEqual([e["kind"] for e in diary.day_state(DAY, self.path)["entries"]],
                         ["worry"])

    def test_once_the_list_is_shown_the_diary_is_closed(self):
        entry = self.add("quarrel", "18:20")
        self.assertIsNotNone(self.seal()["sealed_at"])
        with self.assertRaises(ValueError):
            self.add("worry", "21:00")
        with self.assertRaises(ValueError):
            diary.remove(DAY, entry["id"], path=self.path)
        self.assertEqual(len(diary.day_state(DAY, self.path)["entries"]), 1)

    def test_nothing_happened_is_an_answer_and_not_a_missing_day(self):
        self.seal()
        self.assertEqual(diary.blind_days(self.path), [DAY])
        result = self.recall([listed(1, "quarrel", "20:11:24", "20:18:13")])
        self.assertEqual((result["quiet_days"], result["written"]), (1, 0))


class Scoring(Diary):
    def test_five_minutes_either_way_still_counts(self):
        self.add("quarrel", "18:20", "18:40")     # the list says 18:44 to 18:50
        self.add("quarrel", "12:00", "12:10")     # the list says 12:16: too far
        self.add("worry", "09:00")                # the list says 08:56 to 08:58
        self.seal()
        result = self.recall([listed(1, "quarrel", "18:44:10", "18:50:00"),
                              listed(2, "quarrel", "12:16:00", "12:20:00"),
                              listed(3, "worry", "08:56:00", "08:58:00")])
        self.assertEqual((result["written"], result["found"]), (3, 2))
        found = {e["start"]: e["found"] for e in result["entries"]}
        self.assertEqual(found, {"18:20": True, "12:00": False, "09:00": True})
        self.assertEqual(self.recall([listed(2, "quarrel", "12:16:00", "12:20:00")],
                                     margin=10)["found"], 1)

    def test_the_right_time_under_another_name_is_counted_apart(self):
        self.add("worry", "15:22", "15:27")
        self.seal()
        result = self.recall([listed(1, "complaint", "15:22:09", "15:27:24")])
        self.assertEqual((result["found"], result["found_any_kind"]), (0, 1))
        self.assertEqual(result["by_kind"]["worry"],
                         {"written": 1, "found": 0, "found_any_kind": 1})

    def test_a_diary_closed_after_the_list_was_judged_is_not_counted(self):
        with open(self.judged, "w", encoding="utf-8") as fh:
            fh.write('{"key": "k", "verdict": "confirmed", "incident": {"day": "%s"}}\n' % DAY)
        self.add("quarrel", "18:20")
        self.seal()
        self.assertEqual(diary.blind_days(self.path), [])
        self.assertEqual(
            self.recall([listed(1, "quarrel", "18:20:00", "18:21:00")])["written"], 0)

    def test_a_day_never_read_is_left_out_of_recall(self):
        self.add("quarrel", "18:20")
        self.seal()
        self.assertEqual(diary.recall({}, path=self.path)["written"], 0)


if __name__ == "__main__":
    unittest.main()
