"""Blind labelling: what is drawn for a day, what may be tagged, how it is scored.

A day is built in memory and the log is a temporary file. No model, no store.
"""

import os
import sys
import tempfile
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from graphrag import blind                                         # noqa: E402

DAY = "2026-10-12"


def seg(number, clock, speaker="Ilyas", text="some words"):
    return {"id": f"seg::{number}", "kind": "segment", "day": DAY,
            "timestamp": f"{DAY} {clock}", "speaker": speaker,
            "text": f"{text} {number}", "audio_file": f"{number}.wav"}


def incident(number, kind, start, end):
    return {"id": f"incident::{DAY}::{number:04d}", "kind": "incident", "type": kind,
            "start": f"{DAY}T{start}", "end": f"{DAY}T{end}",
            "participants": ["Ilyas", "Voula"]}


def a_day(read_at="2026-10-13T08:00:00"):
    """Twenty windows with speech, 08:00 to 09:40; the system listed in three."""
    segments = [seg(n, f"{8 + n // 12:02d}:{n % 12 * 5:02d}:30") for n in range(20)]
    return {"date": DAY, "incident_layer": {"read_at": read_at},
            "nodes": segments + [
                incident(1, "quarrel", "08:11:00", "08:18:00"),     # two windows
                incident(2, "worry", "09:01:00", "09:02:00")]}


LO = lambda clock: int(clock[:2]) * 3600 + int(clock[3:]) * 60     # noqa: E731


class Blind(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "labels.jsonl")
        self.day = a_day()

    def draw(self, day=None):
        return blind.draw(DAY, day or self.day, path=self.path)

    def answer_all(self, tags):
        for lo in self.draw()["windows"]:
            blind.label(DAY, lo, tags.get(lo, []), path=self.path)
        blind.seal(DAY, path=self.path)

    def rows(self):
        return blind.labelled_windows(self.path, days_dir_loader=lambda d: self.day)


class Drawing(Blind):
    def test_every_flagged_window_and_a_sample_of_the_quiet_ones(self):
        drawn = self.draw()
        self.assertEqual(sorted(drawn["flagged"]), [LO("08:10"), LO("08:15"), LO("09:00")])
        self.assertEqual(drawn["quiet_total"], 17)
        self.assertEqual(len(drawn["quiet_shown"]), blind.QUIET_SHOWN)
        self.assertFalse(set(drawn["quiet_shown"]) & set(drawn["flagged"]))
        self.assertEqual(sorted(drawn["windows"]),
                         sorted(drawn["flagged"] + drawn["quiet_shown"]))

    def test_it_is_made_once(self):
        self.assertEqual(self.draw()["windows"], self.draw()["windows"])
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 1)

    def test_a_day_read_again_is_drawn_again_unless_it_is_sealed(self):
        first = self.draw()
        blind.label(DAY, first["windows"][0], ["quarrel"], path=self.path)
        again = self.draw(a_day(read_at="2026-10-14T08:00:00"))
        self.assertEqual(again["read_at"], "2026-10-14T08:00:00")
        self.assertEqual(blind.state(DAY, self.path)["labels"], {})   # they were about the old list

    def test_a_day_with_no_list_is_not_drawn(self):
        self.assertIsNone(blind.draw(DAY, {"date": DAY, "nodes": []}, path=self.path))


class Tagging(Blind):
    def test_kinds_are_added_and_taken_off_and_nothing_is_an_answer(self):
        lo = self.draw()["windows"][0]
        blind.toggle(DAY, lo, "quarrel", path=self.path)
        blind.toggle(DAY, lo, "worry", path=self.path)
        self.assertEqual(blind.state(DAY, self.path)["labels"][lo], ["quarrel", "worry"])
        blind.toggle(DAY, lo, "quarrel", path=self.path)
        self.assertEqual(blind.state(DAY, self.path)["labels"][lo], ["worry"])
        blind.toggle(DAY, lo, "nothing", path=self.path)
        self.assertEqual(blind.state(DAY, self.path)["labels"][lo], [])
        self.assertNotIn(lo, blind.unlabelled(DAY, self.path))

    def test_only_what_was_drawn_and_only_real_kinds(self):
        self.draw()
        with self.assertRaises(ValueError):
            blind.label(DAY, 3 * 3600, ["quarrel"], path=self.path)
        with self.assertRaises(ValueError):
            blind.label(DAY, self.draw()["windows"][0], ["gossip"], path=self.path)

    def test_the_day_is_not_closed_with_a_stretch_unanswered_nor_changed_after(self):
        drawn = self.draw()
        with self.assertRaises(ValueError):
            blind.seal(DAY, path=self.path)
        self.answer_all({})
        with self.assertRaises(ValueError):
            blind.label(DAY, drawn["windows"][0], ["quarrel"], path=self.path)
        self.assertEqual(self.draw()["windows"], drawn["windows"])    # the draw stands


class Scoring(Blind):
    def test_a_quiet_window_stands_for_all_the_quiet_windows_of_its_day(self):
        quiet = self.draw()["quiet_shown"]
        # The resident agrees with the quarrel and the worry, and finds one
        # more quarrel in a stretch the system passed over.
        self.answer_all({LO("08:10"): ["quarrel"], LO("08:15"): ["quarrel"],
                         LO("09:00"): ["worry"], quiet[0]: ["quarrel"]})
        rows = self.rows()
        self.assertEqual(len(rows), 11)
        self.assertEqual({r["weight"] for r in rows if not r["flagged"]}, {17 / 8})
        got = blind.score(rows, resamples=0)["quarrel"]
        self.assertEqual((got["tp"], got["fn"]), (2.0, 17 / 8))
        self.assertAlmostEqual(got["recall"], 2 / (2 + 17 / 8))
        self.assertEqual(got["precision"], 1.0)
        self.assertEqual(blind.score(rows, resamples=0)["worry"]["recall"], 1.0)

    def test_a_listed_incident_the_resident_does_not_see_costs_precision(self):
        self.answer_all({LO("08:10"): ["quarrel"], LO("09:00"): ["complaint"]})
        scores = blind.score(self.rows(), resamples=0)
        self.assertEqual(scores["quarrel"]["precision"], 0.5)      # 08:15 was not one
        self.assertEqual(scores["worry"]["precision"], 0.0)        # tagged a complaint
        self.assertEqual(scores["complaint"]["recall"], 0.0)       # and that was missed
        self.assertEqual(scores["any"]["precision"], 2 / 3)

    def test_nothing_is_scored_until_the_day_is_closed(self):
        for lo in self.draw()["windows"]:
            blind.label(DAY, lo, [], path=self.path)
        self.assertEqual(self.rows(), [])

    def test_intervals_come_with_enough_resamples(self):
        quiet = self.draw()["quiet_shown"]
        self.answer_all({LO("08:10"): ["quarrel"], LO("08:15"): ["quarrel"],
                         quiet[0]: ["quarrel"]})
        got = blind.score(self.rows(), resamples=300)["quarrel"]
        low, high = got["recall_interval"]
        self.assertTrue(0 <= low <= got["recall"] <= high <= 1)


if __name__ == "__main__":
    unittest.main()
