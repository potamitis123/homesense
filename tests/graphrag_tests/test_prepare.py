"""Preparing a day's list from the report page: what a Yes starts, and when not.

Nothing is read and no model is contacted. The command is never run -- a
stand-in process is handed back in its place and told when to end -- and the
store and the state of each asking live in a temporary directory.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from graphrag import prepare as prep                               # noqa: E402

TODAY = "2026-10-04"
NOW = datetime(2026, 10, 4, 11, 30, 0)
DAY, EARLIER = "2026-10-03", "2026-10-01"


class Process:
    """A command that ends when the test says so."""
    pid = 4242

    def __init__(self):
        self._ended, self.code = threading.Event(), 0

    def wait(self):
        self._ended.wait(5)
        return self.code

    def end(self, code=0):
        self.code = code
        self._ended.set()


class Preparing(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.days = os.path.join(folder.name, "days")
        os.makedirs(self.days)
        for name, value in (("DAYS_DIR", self.days),
                            ("STATE_DIR", os.path.join(folder.name, "preparing"))):
            patcher = patch.object(prep, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.started = []          # (argv, process), one per command started

    def spawn(self, argv, log_path):
        with open(log_path, "w", encoding="utf-8") as log:
            log.write("L1.5 relational coding via gpt-4o-mini\n")
        process = Process()
        self.started.append((argv, process))
        return process

    def store(self, day, listed):
        with open(os.path.join(self.days, f"{day}.json"), "w", encoding="utf-8") as fh:
            json.dump({"date": day, **({"incident_layer": {"incidents": 3}}
                                       if listed else {})}, fh)

    def start(self, day=DAY):
        return prep.start(day, today=TODAY, now=NOW, spawn=self.spawn)

    def end(self, day=DAY, code=0, said=None):
        if said:
            with open(prep._log_path(day), "a", encoding="utf-8") as log:
                log.write(said + "\n")
        self.started[-1][1].end(code)
        for thread in threading.enumerate():
            if thread.name == f"prepare-{day}":
                thread.join(5)


class Yes(Preparing):
    def test_it_starts_the_command_a_person_would_type(self):
        state = self.start()
        (argv, _process), = self.started
        self.assertEqual(argv[1:], ["-m", "graphrag.ingest", "--day", DAY,
                                    "--semantic-model", "gpt-4o-mini"])
        self.assertEqual(state["state"], "running")
        self.assertEqual(state["started_at"], "2026-10-04T11:30:00")
        self.assertEqual(prep.running(NOW), DAY)
        self.end()

    def test_a_day_already_in_the_store_only_gets_its_list_added(self):
        self.store(DAY, listed=False)
        self.start()
        self.assertEqual(self.started[0][0][3:],
                         ["--day", DAY, "--add-incidents", "--incident-model",
                          "gpt-4o-mini"])
        self.end()

    def test_when_the_list_is_in_the_store_nothing_more_is_kept(self):
        self.start()
        self.store(DAY, listed=True)             # what the command does, last
        self.end(code=0)
        self.assertIsNone(prep.state_of(DAY, NOW)["state"])
        self.assertIsNone(prep.running(NOW))
        self.assertFalse(os.path.exists(prep._state_path(DAY)))

    def test_when_no_list_comes_of_it_the_reason_is_kept(self):
        self.start()
        self.end(code=1, said="openai.APIConnectionError: Connection error.")
        state = prep.state_of(DAY, NOW)
        self.assertEqual((state["state"], state["exit_code"]), ("failed", 1))
        self.assertIn("Connection error.", state["error"])
        self.assertIsNone(prep.running(NOW))     # and the day can be asked again
        self.start()
        self.assertEqual(len(self.started), 2)
        self.end()

    def test_ending_well_without_a_list_is_still_no_list(self):
        self.start()
        self.end(code=0, said=f"  {DAY}: no events, skipped")
        state = prep.state_of(DAY, NOW)
        self.assertEqual(state["state"], "failed")
        self.assertIn("no events, skipped", state["error"])


class Refused(Preparing):
    def refused(self, day, because):
        with self.assertRaises(ValueError) as raised:
            self.start(day)
        self.assertIn(because, str(raised.exception))
        self.assertEqual(self.started, [])

    def test_today_is_not_over(self):
        self.refused(TODAY, "once it is over")
        self.refused("2026-10-09", "once it is over")

    def test_what_is_not_a_date(self):
        # The day arrives from a browser and ends up on a command line.
        for day in ("2026-13-40", "yesterday", "2026-10-03; del x", "", None):
            self.refused(day, "not a date")

    def test_a_day_that_has_its_list(self):
        self.store(DAY, listed=True)
        self.refused(DAY, "already has its list")

    def test_a_second_day_while_one_is_being_prepared(self):
        self.start(DAY)
        with self.assertRaises(ValueError) as raised:
            self.start(EARLIER)
        self.assertIn(f"{DAY} is being prepared", str(raised.exception))
        # The same day asked for twice is one command, not two.
        self.assertEqual(self.start(DAY)["state"], "running")
        self.assertEqual(len(self.started), 1)
        self.end(DAY)


class NobodyWaiting(Preparing):
    """The application was restarted while the command ran."""

    def left_running(self, started_at):
        os.makedirs(prep.STATE_DIR, exist_ok=True)
        prep._write(DAY, {"state": "running", "started_at": started_at})

    def test_recently_started_it_is_still_taken_to_be_going(self):
        self.left_running("2026-10-04T11:20:00")
        self.assertEqual(prep.running(NOW), DAY)

    def test_long_ago_it_is_taken_to_have_died_and_can_be_asked_again(self):
        self.left_running("2026-10-04T09:00:00")
        self.assertEqual(prep.state_of(DAY, NOW)["state"], "failed")
        self.assertIsNone(prep.running(NOW))
        self.start()
        self.assertEqual(len(self.started), 1)
        self.end()

    def test_if_the_list_is_there_it_succeeded_whatever_the_file_says(self):
        self.left_running("2026-10-04T11:20:00")
        self.store(DAY, listed=True)
        self.assertIsNone(prep.state_of(DAY, NOW)["state"])
        self.assertIsNone(prep.running(NOW))


class No(Preparing):
    def test_it_is_remembered_until_somebody_asks_again(self):
        prep.decline(DAY)
        self.assertEqual(prep.state_of(DAY, NOW)["state"], "declined")
        self.assertIsNone(prep.running(NOW))
        prep.ask_again([DAY, "not a date", EARLIER])
        self.assertIsNone(prep.state_of(DAY, NOW)["state"])

    def test_it_does_not_stop_a_day_that_is_being_prepared(self):
        self.start()
        prep.decline(DAY, now=NOW)       # judged at the test's own moment
        self.assertEqual(prep.state_of(DAY, NOW)["state"], "running")
        self.end()

    def test_a_yes_after_a_no_starts_it(self):
        prep.decline(DAY)
        self.assertEqual(self.start()["state"], "running")
        self.end()


if __name__ == "__main__":
    unittest.main()
