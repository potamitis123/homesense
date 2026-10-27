"""Preparing a day's incident list when the report page is told to.

The report page can show a day's incidents only once the day has been read,
and reading a day is a command typed at a terminal. This lets the page ask
instead -- "Do you want me to prepare the list for Saturday 3 October?" -- and
start that same command on a Yes.

IT IS THE SAME COMMAND, STARTED AS ITS OWN PROCESS

    python -m graphrag.ingest --day D --semantic-model gpt-4o-mini

and not the ingest called from inside the application, for three reasons:

  * reading a day takes minutes, calls a hosted and a local model, and can
    fail in ways that an application also relaying intercom calls must not
    share;
  * a process started here outlives a restart of the application, so a
    restart does not leave a day half read;
  * what the page runs and what the README tells a person to type stay one
    thing, with one behaviour.

WHAT A YES SENDS OUT OF THE HOUSE

The day's transcript goes to the hosted model that reads it. That is why the
page asks and never does this by itself, and why the question says so.

ONE DAY AT A TIME, AND NEVER TODAY

Two days read together would share one GPU for the topics and one per-minute
allowance at the hosted model. And a day is written to the store once, so it
is read only when it is over: today is refused here whatever the page sent.

WHAT IS KEPT

One small file per day under store/preparing/, holding where the asking
stands: being prepared, failed (with the last lines the command printed), or
declined. A No is kept so that a day somebody chose not to prepare is not
asked about again on every visit; the page offers to ask again. Nothing is
kept for a day whose list exists -- the store is the record of that.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime

from .engine import DAYS_DIR, STORE_DIR

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATE_DIR = os.path.join(STORE_DIR, "preparing")

# The model that reads the day. Not the page's choice of model: a local one did
# not finish listing a day in 34 minutes (see incidents.py).
MODEL = "gpt-4o-mini"

# A preparation that has not reported back in this long is taken to have died
# with the application that started it. A day reads in a few minutes.
STALE_AFTER_SECONDS = 45 * 60

_starting = threading.Lock()


# ── the day ─────────────────────────────────────────────────────────

def _valid(day) -> str:
    """The day as YYYY-MM-DD, or ValueError. It arrives from a browser."""
    try:
        return datetime.strptime(str(day), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"not a date: {day!r}") from None


def _today(today=None) -> str:
    return today or datetime.now().strftime("%Y-%m-%d")


def in_store(day: str) -> tuple[bool, bool]:
    """(the day is in the store, it has an incident list)."""
    try:
        with open(os.path.join(DAYS_DIR, f"{day}.json"), encoding="utf-8") as fh:
            return True, bool(json.load(fh).get("incident_layer"))
    except (OSError, ValueError):
        return False, False


def command_for(day: str, stored: bool) -> list[str]:
    """What a person would type for this day, as an argument list."""
    base = [sys.executable, "-m", "graphrag.ingest", "--day", day]
    if stored:      # in the store without a list: add the list, touch nothing else
        return base + ["--add-incidents", "--incident-model", MODEL]
    return base + ["--semantic-model", MODEL]


# ── where an asking stands ──────────────────────────────────────────

def _state_path(day: str) -> str:
    return os.path.join(STATE_DIR, f"{day}.json")


def _log_path(day: str) -> str:
    return os.path.join(STATE_DIR, f"{day}.log")


def _write(day: str, state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _state_path(day) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"day": day, **state}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _state_path(day))


def _clear(day: str) -> None:
    try:
        os.remove(_state_path(day))
    except OSError:
        pass


def _tail(path: str, lines: int = 3) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            said = [text.strip() for text in fh if text.strip()]
    except OSError:
        return ""
    return " / ".join(said[-lines:])


def state_of(day: str, now: datetime | None = None) -> dict:
    """{"state": "running" | "failed" | "declined" | None, ...} for one day."""
    try:
        with open(_state_path(day), encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {"day": day, "state": None}
    if state.get("state") == "running":
        # The application may have been restarted while the command ran, and
        # then nothing was left waiting to write down how it ended. The store
        # says whether it succeeded; the clock says whether it is still likely
        # to be going.
        if in_store(day)[1]:
            _clear(day)
            return {"day": day, "state": None}
        try:
            began = datetime.fromisoformat(state["started_at"])
        except (KeyError, ValueError, TypeError):
            began = None
        age = ((now or datetime.now()) - began).total_seconds() if began else None
        if age is None or age > STALE_AFTER_SECONDS:
            return {**state, "state": "failed",
                    "error": "it was started and never reported finishing"}
    return state


def running(now: datetime | None = None) -> str | None:
    """The day being prepared at this moment, if one is."""
    try:
        names = sorted(os.listdir(STATE_DIR))
    except OSError:
        return None
    for name in names:
        if name.endswith(".json") and state_of(name[:-5], now).get("state") == "running":
            return name[:-5]
    return None


# ── yes ─────────────────────────────────────────────────────────────

def _spawn(argv: list[str], log_path: str):
    """Start the command with what it prints going to a file. No window."""
    with open(log_path, "w", encoding="utf-8") as log:
        return subprocess.Popen(
            argv, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _watch(day: str, process) -> None:
    """Wait for the command and write down how it ended."""
    code = process.wait()
    if code == 0 and in_store(day)[1]:
        _clear(day)                      # the list is in the store; nothing to keep
        return
    said = _tail(_log_path(day))
    _write(day, {
        "state": "failed", "exit_code": code,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "error": said or ("it ended without printing anything" if code else
                          "it finished without making a list"),
    })


def start(day, today: str | None = None, now: datetime | None = None,
          spawn=None) -> dict:
    """Begin preparing a day's list. Returns the state it is now in.

    Raises ValueError, with the reason as its message, when it may not be
    started: not a date, a day that is not over, a day that has its list, or
    another day already being prepared.
    """
    day = _valid(day)
    if day >= _today(today):
        raise ValueError("a day is prepared once it is over, after midnight")
    stored, has_list = in_store(day)
    if has_list:
        raise ValueError(f"{day} already has its list")

    with _starting:
        busy = running(now)
        if busy == day:
            return state_of(day, now)
        if busy:
            raise ValueError(f"{busy} is being prepared; one day at a time")
        argv = command_for(day, stored)
        os.makedirs(STATE_DIR, exist_ok=True)
        process = (spawn or _spawn)(argv, _log_path(day))
        _write(day, {
            "state": "running",
            "started_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "pid": getattr(process, "pid", None),
            "command": " ".join(argv[1:]),
        })
    threading.Thread(target=_watch, args=(day, process),
                     name=f"prepare-{day}", daemon=True).start()
    return state_of(day, now)


# ── no ──────────────────────────────────────────────────────────────

def decline(day, now: datetime | None = None) -> None:
    """Remember that this day is not to be prepared, so it is not asked again."""
    day = _valid(day)
    if state_of(day, now).get("state") != "running":
        _write(day, {"state": "declined",
                     "declined_at": datetime.now().isoformat(timespec="seconds")})


def ask_again(days) -> None:
    """Forget the No given for these days."""
    for day in days:
        try:
            day = _valid(day)
        except ValueError:
            continue
        if state_of(day).get("state") == "declined":
            _clear(day)
