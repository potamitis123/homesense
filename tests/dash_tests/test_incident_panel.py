"""The incident list as it is drawn on the report page.

No store is opened, no browser is started and nothing is prepared: the listing
and the state of every asking are supplied, and what is checked is the
component tree -- what each button will do when it is clicked, what a window
with no list is made to say, and which day the panel asks about.
"""

import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from ast_dash import incident_panel as panel                       # noqa: E402

START, END = datetime(2026, 10, 1, 0, 0), datetime(2026, 10, 2, 23, 59)
READING = {"read_at": "2026-10-03T16:10:14", "model": "gpt-4o-mini"}
NOW = datetime(2026, 10, 10, 9, 0, 0)


def row(number, day, start, end, kind="quarrel", verdict=None, lines=2):
    return {"id": f"incident::{day}::{number:04d}", "key": f"{day}|r|{number}",
            "kind": kind, "day": day, "start": start, "end": end,
            "participants": ["Ilyas", "Rafael"], "about": "manners",
            "what": "They argue about manners.", "rooms": ["Kitchen"],
            "tension": 0.75, "verdict": verdict, "judged_at": None,
            "lines": [{"time": start, "speaker": "Ilyas", "text": f"line {n}",
                       "room": "Kitchen"} for n in range(lines)]}


def listing(rows, read=("2026-10-02",), not_listed=(), not_stored=()):
    verdicts = [r["verdict"] for r in rows]
    return {"days": [], "rows": rows, "read": {d: READING for d in read},
            "not_listed": list(not_listed), "not_stored": list(not_stored),
            "tally": {"listed": len(rows), "confirmed": verdicts.count("confirmed"),
                      "rejected": verdicts.count("rejected"),
                      "unjudged": verdicts.count(None)}}


def walk(node):
    """Every component in a tree, and every piece of text."""
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from walk(item)
        return
    yield node
    yield from walk(getattr(node, "children", None))


def drawn(the_listing, notice=None, now=NOW, busy=None, states=None, held=None):
    """The panel for a listing, with every asking in the state given.

    A fixed moment, after every day in these listings unless a test says
    otherwise: what the panel says must not depend on when the test is run,
    nor on what the real store happens to be preparing.
    """
    states = states or {}

    def state_of(day, _now=None):
        return {"day": day, **states.get(day, {"state": None})}

    # No day is held back for blind labelling unless a test says so: `held`
    # is day -> (windows, the draw, the labels given so far).
    held = held or {}
    with patch.object(panel.judgements, "listed_between", return_value=the_listing), \
            patch.object(panel.blind, "load_day",
                         side_effect=lambda day: {"date": day} if day in held else None), \
            patch.object(panel.blind, "draw",
                         side_effect=lambda day, stored=None: held[day][1]), \
            patch.object(panel.blind, "state",
                         side_effect=lambda day: {"draw": held[day][1],
                                                  "labels": held[day][2],
                                                  "sealed_at": None}), \
            patch.object(panel.blind, "day_windows",
                         side_effect=lambda stored: held[stored["date"]][0]), \
            patch.object(panel.prepare, "running", return_value=busy), \
            patch.object(panel.prepare, "state_of", side_effect=state_of):
        return panel.render(START, END, notice=notice, now=now)


def text_of(tree):
    return " ".join(n for n in walk(tree) if isinstance(n, str))


def buttons(tree, kind="incident-verdict"):
    return [n for n in walk(tree)
            if isinstance(getattr(n, "id", None), dict) and n.id["type"] == kind]


class Buttons(unittest.TestCase):
    def test_their_ids_are_the_ones_the_page_listens_for(self):
        # A key missing from an id and the page's callback never fires: the
        # buttons would look right and do nothing.
        tree = drawn(listing([row(1, "2026-10-02", "20:11:24", "20:18:13")],
                             not_stored=["2026-10-01"]))
        verdicts, answers = buttons(tree), buttons(tree, "incident-prepare")
        self.assertTrue(verdicts and answers)
        for button in verdicts:
            self.assertEqual(set(button.id), set(panel.VERDICT_BUTTONS))
        for button in answers:
            self.assertEqual(set(button.id), set(panel.PREPARE_BUTTONS))

    def test_each_says_what_a_click_will_do(self):
        tree = drawn(listing([
            row(1, "2026-10-02", "20:11:24", "20:18:13"),
            row(2, "2026-10-02", "21:00:00", "21:00:30", verdict="confirmed"),
            row(3, "2026-10-02", "22:00:00", "22:05:00", verdict="rejected"),
        ]))
        will = {(b.id["key"], b.id["verdict"]): (b.id["act"], "on" in b.className.split())
                for b in buttons(tree)}
        self.assertEqual(will[("2026-10-02|r|1", "confirmed")], ("set", False))
        self.assertEqual(will[("2026-10-02|r|1", "rejected")], ("set", False))
        # The lit button takes its answer back; the other one changes it.
        self.assertEqual(will[("2026-10-02|r|2", "confirmed")], ("clear", True))
        self.assertEqual(will[("2026-10-02|r|2", "rejected")], ("set", False))
        self.assertEqual(will[("2026-10-02|r|3", "rejected")], ("clear", True))
        self.assertEqual(will[("2026-10-02|r|3", "confirmed")], ("set", False))


class Drawing(unittest.TestCase):
    def test_the_newest_day_comes_first_and_a_day_runs_in_order(self):
        tree = drawn(listing([
            row(1, "2026-10-01", "09:00:00", "09:05:00"),
            row(1, "2026-10-02", "08:00:00", "08:01:00"),
            row(2, "2026-10-02", "20:11:24", "20:18:13"),
        ], read=("2026-10-01", "2026-10-02")))
        text = text_of(tree)
        self.assertLess(text.index("Friday 2 October 2026"),
                        text.index("Thursday 1 October 2026"))
        self.assertLess(text.index("08:00"), text.index("20:11 – 20:18"))
        self.assertIn("7 min", text)
        self.assertIn("Ilyas and Rafael", text)
        self.assertIn("list read 3 Oct 16:10 by gpt-4o-mini", text)

    def test_the_words_it_rests_on_are_under_each_incident(self):
        text = text_of(drawn(listing([row(1, "2026-10-02", "20:11:24", "20:18:13",
                                          lines=3)])))
        self.assertIn("Show the 3 lines it rests on", text)
        self.assertIn("line 2", text)

    def test_a_day_read_and_found_empty_is_not_called_unread(self):
        tree = drawn(listing([], read=("2026-10-02",)))
        text = text_of(tree)
        self.assertIn("Nothing was listed in this window on 2 Oct.", text)
        self.assertNotIn("Do you want me to prepare", text)
        self.assertEqual(buttons(tree, "incident-prepare"), [])

    def test_a_notice_is_shown_above_the_list(self):
        text = text_of(drawn(listing([row(1, "2026-10-02", "20:11:24", "20:18:13")]),
                             notice="That day has been read again."))
        self.assertLess(text.index("That day has been read again."),
                        text.index("Friday 2 October 2026"))


class Asking(unittest.TestCase):
    """'Do you want me to prepare the list for <date>?'"""

    def answers(self, tree):
        return {(b.id["day"], b.id["answer"]) for b in buttons(tree, "incident-prepare")}

    def test_it_asks_about_the_latest_day_that_is_over_and_says_what_a_yes_sends(self):
        tree = drawn(listing([row(1, "2026-10-02", "20:11:24", "20:18:13")],
                             not_listed=["2026-09-17", "2026-09-18"],
                             not_stored=["2026-09-27", "2026-09-28", "2026-10-03"]))
        text = text_of(tree)
        self.assertIn("Do you want me to prepare the list for Saturday 3 October 2026?",
                      text)
        self.assertIn("It is not in the store yet.", text)
        self.assertIn("sends that day's transcript to gpt-4o-mini", text)
        self.assertEqual(self.answers(tree), {("2026-10-03", "yes"), ("2026-10-03", "no")})
        # One question at a time; the others are named, not asked about.
        self.assertEqual(text.count("Do you want me to prepare"), 1)
        self.assertIn("17 Sep to 18 Sep, 27 Sep to 28 Sep in this window have no "
                      "list either.", text)
        # And it comes before the list it will add to.
        self.assertLess(text.index("Do you want me to prepare"),
                        text.index("Friday 2 October 2026"))

    def test_a_day_in_the_store_without_a_list_is_asked_about_as_such(self):
        text = text_of(drawn(listing([], read=(), not_listed=["2026-09-18"])))
        self.assertIn("Do you want me to prepare the list for Friday 18 September 2026?",
                      text)
        self.assertIn("It is in the store, but was never read for incidents.", text)

    def test_today_is_never_asked_about(self):
        # A day is written once. Read before it is over, it would be stored
        # without its evening, so the question is about the last day that ended.
        today = datetime(2026, 10, 3, 21, 30)
        tree = drawn(listing([], read=(),
                             not_stored=["2026-10-01", "2026-10-02", "2026-10-03"]),
                     now=today)
        text = text_of(tree)
        self.assertIn("prepare the list for Friday 2 October 2026?", text)
        self.assertEqual(self.answers(tree), {("2026-10-02", "yes"), ("2026-10-02", "no")})
        self.assertIn("Today has no list yet.", text)
        self.assertIn("ask about today after midnight", text)

        only_today = drawn(listing([], read=(), not_stored=["2026-10-03"]), now=today)
        self.assertEqual(self.answers(only_today), set())
        self.assertNotIn("Do you want me to prepare", text_of(only_today))

    def test_while_a_day_is_prepared_nothing_else_is_asked(self):
        tree = drawn(listing([], read=(), not_stored=["2026-10-01", "2026-10-02"]),
                     busy="2026-10-02",
                     states={"2026-10-02": {"state": "running",
                                            "started_at": "2026-10-10T08:57:00"}})
        text = text_of(tree)
        self.assertIn("Preparing the list for Friday 2 October 2026.", text)
        self.assertIn("Started at 08:57, 3 min ago.", text)
        self.assertNotIn("Do you want me to prepare", text)
        self.assertEqual(self.answers(tree), set())
        self.assertIn("No list for 1 Oct either.", text)

    def test_a_reading_that_failed_says_why_and_asks_again(self):
        tree = drawn(listing([], read=(), not_stored=["2026-10-02"]),
                     states={"2026-10-02": {"state": "failed",
                                            "error": "the model could not be reached"}})
        text = text_of(tree)
        self.assertIn("It was tried and no list came of it: the model could not "
                      "be reached", text)
        self.assertEqual(self.answers(tree), {("2026-10-02", "yes"), ("2026-10-02", "no")})

    def test_a_no_is_not_asked_again_until_somebody_asks_for_it(self):
        declined = {"state": "declined"}
        tree = drawn(listing([], read=(), not_stored=["2026-10-01", "2026-10-02"]),
                     states={"2026-10-01": declined, "2026-10-02": declined})
        text = text_of(tree)
        self.assertNotIn("Do you want me to prepare", text)
        self.assertIn("No list for 1 Oct to 2 Oct, as you chose.", text)
        self.assertIn("That is not the same as nothing having happened.", text)
        self.assertEqual(self.answers(tree), {("2026-10-01,2026-10-02", "again")})

        # With the newest declined, the question moves to the day before it.
        tree = drawn(listing([], read=(), not_stored=["2026-10-01", "2026-10-02"]),
                     states={"2026-10-02": declined})
        self.assertIn("prepare the list for Thursday 1 October 2026?", text_of(tree))


class BlindFirst(unittest.TestCase):
    """Stretches of the day are tagged before its list is shown."""

    WINDOWS = [
        {"lo": 20 * 3600 + 600, "lines": ["20:11:24 Ilyas: you are rude"],
         "system": ["quarrel"], "audio": ["a.wav"]},
        {"lo": 9 * 3600, "lines": ["09:01:00 Voula: who wants tea"],
         "system": [], "audio": []},
    ]
    DRAW = {"windows": [9 * 3600, 20 * 3600 + 600], "flagged": [20 * 3600 + 600],
            "quiet_shown": [9 * 3600], "quiet_total": 30}

    def held(self, labels):
        return {"2026-10-02": (self.WINDOWS, self.DRAW, labels)}

    def tags(self, tree):
        return {(b.id["window"], b.id["kind"]): b for b in buttons(tree, "blind-act")}

    def test_an_unjudged_day_is_held_back_and_nothing_gives_the_system_away(self):
        rows = [row(1, "2026-10-02", "20:11:24", "20:18:13")]
        tree = drawn(listing(rows), held=self.held({}))
        text = text_of(tree)
        self.assertIn("Before I show the list: what happened in these stretches of "
                      "Friday 2 October 2026?", text)
        self.assertNotIn("They argue about manners.", text)        # the list is not shown
        self.assertEqual(buttons(tree), [])
        self.assertIn("09:00 – 09:05", text)
        self.assertIn("20:10 – 20:15", text)
        self.assertIn("20:11:24 Ilyas: you are rude", text)
        # The flagged stretch and the quiet one are drawn alike.
        shown = [n for n in walk(tree)
                 if getattr(n, "className", None) == "inc-blind-window"]
        self.assertEqual(len(shown), 2)
        self.assertLess(text.index("09:00"), text.index("20:10"))    # the drawn order
        self.assertIn("affection, praise, support, shared laughter", text)
        for b in buttons(tree, "blind-act"):
            self.assertEqual(set(b.id), set(panel.BLIND_BUTTONS))

    def test_every_stretch_offers_the_six_kinds_and_nothing(self):
        tags = self.tags(drawn(listing([row(1, "2026-10-02", "20:11:24", "20:18:13")]),
                               held=self.held({})))
        for window in ("32400", "72600"):
            for kind in ("quarrel", "complaint", "rebuff", "worry", "reconciliation",
                         "warm_moment", "nothing"):
                self.assertIn((window, kind), tags)

    def test_the_list_cannot_be_asked_for_until_every_stretch_is_answered(self):
        rows = [row(1, "2026-10-02", "20:11:24", "20:18:13")]
        half = self.tags(drawn(listing(rows), held=self.held({72600: ["quarrel"]})))
        self.assertTrue(half[("", "seal")].disabled)
        self.assertIn("on", half[("72600", "quarrel")].className.split())
        text = text_of(drawn(listing(rows), held=self.held({72600: ["complaint", "warm_moment"]})))
        self.assertIn("tick EVERY kind", text)
        self.assertIn("Tick every kind that happened in this stretch", text)
        self.assertIn("Ticked: 2.", text)
        self.assertNotIn("on", half[("32400", "nothing")].className.split())

        done = self.tags(drawn(listing(rows),
                               held=self.held({72600: ["quarrel"], 32400: []})))
        self.assertFalse(done[("", "seal")].disabled)
        self.assertIn("on", done[("32400", "nothing")].className.split())

    def test_a_day_already_judged_is_not_held_back(self):
        # Its list has been seen; tags given now would not be blind.
        rows = [row(1, "2026-10-02", "20:11:24", "20:18:13", verdict="confirmed")]
        tree = drawn(listing(rows), held=self.held({}))
        self.assertNotIn("Before I show the list", text_of(tree))
        self.assertTrue(buttons(tree))


if __name__ == "__main__":
    unittest.main()
