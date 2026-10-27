"""The incidents of a window, listed under the lamps for the residents to judge.

The lamps say how a week was: warm, tense, withdrawn. They are averages, and an
average cannot say that on Friday two people quarrelled from 20:11 to 20:18.
This panel says that. It shows the incidents GraphRag listed for the days in
the window -- what happened, between whom, from when to when -- and puts two
buttons on each one: it happened, or it did not.

The list is READ FROM THE STORE and never made by opening the page. Making it
means sending the day's transcript to a model, which is somebody's decision and
not a side effect; and two readings of one day do not list the same incidents,
so a list made on the spot could change under the person judging it.

A day with no list says so. It is never drawn as a day on which nothing
happened. For the latest such day that is over, the panel ASKS: "Do you want me
to prepare the list for Saturday 3 October?" Yes starts the reading, and the
question says what a Yes sends out of the house. No is remembered, so the day
is not asked about again on every visit.

This module only draws. The page owns its window and its callback,
graphrag/judgements.py owns what Confirm and Reject record, and
graphrag/prepare.py owns what a Yes starts.
"""

from datetime import datetime

from dash import ALL, dcc, html

from graphrag import blind, judgements, prepare
from graphrag.incidents import KIND_DEFINITIONS

PEOPLE = ("Ilyas", "Voula", "Rafael")

# The tags of the blind-labelling step, and the button that closes it.
BLIND_BUTTONS = {"type": "blind-act", "day": ALL, "window": ALL, "kind": ALL}

# Every confirm and reject button on the page, for the page's one callback.
VERDICT_BUTTONS = {"type": "incident-verdict", "key": ALL, "verdict": ALL,
                   "act": ALL}

# The answers to "do you want me to prepare the list": yes, no, and the request
# to be asked again about days that were declined.
PREPARE_BUTTONS = {"type": "incident-prepare", "day": ALL, "answer": ALL}

# How a kind is written for a resident, and which lamp it belongs beside.
# "strain" is drawn in the Tension lamp's amber and "warm" in the Warmth lamp's
# green, so the list and the lamps above it read as one account.
_KINDS = {
    "quarrel": ("Quarrel", "strain"),
    "complaint": ("Complaint", "strain"),
    "rebuff": ("Rebuff", "strain"),
    "worry": ("Worry", "worry"),
    "reconciliation": ("Making up", "warm"),
    "warm_moment": ("Warm moment", "warm"),
}

# Lines shown under one incident. A long quarrel rests on a hundred, and the
# first of them are what a resident recognises it by.
MAX_LINES = 120


def _day_name(day):
    when = datetime.strptime(day, "%Y-%m-%d")
    return f"{when:%A} {when.day} {when:%B %Y}"


def _short(day):
    when = datetime.strptime(day, "%Y-%m-%d")
    return f"{when.day} {when:%b}"


def _runs(days):
    """Dates as a resident would say them: '27 Sep to 1 Oct, 3 Oct'."""
    out, run = [], []
    for day in sorted(days):
        when = datetime.strptime(day, "%Y-%m-%d").date()
        if run and (when - run[-1]).days == 1:
            run.append(when)
            continue
        if run:
            out.append(run)
        run = [when]
    if run:
        out.append(run)
    return ", ".join(
        f"{r[0].day} {r[0]:%b}" if len(r) == 1
        else f"{r[0].day} {r[0]:%b} to {r[-1].day} {r[-1]:%b}" for r in out)


def _span(row):
    start, end = row["start"][:5], row["end"][:5]
    return start if start == end else f"{start} – {end}"


def _length(row):
    try:
        a = datetime.strptime(row["start"], "%H:%M:%S")
        b = datetime.strptime(row["end"], "%H:%M:%S")
    except (TypeError, ValueError):
        return ""
    seconds = (b - a).total_seconds()
    if seconds < 60:
        return "under a minute"
    return f"{round(seconds / 60)} min"


def _who(row):
    people = row.get("participants") or []
    if not people:
        return "nobody the system could name"
    if len(people) == 1:
        return people[0]
    return ", ".join(people[:-1]) + " and " + people[-1]


def _button(row, verdict, label):
    """One of the two buttons.

    Its id says what a click is to DO, decided when the row was drawn: set
    this verdict, or -- on the button that is already lit -- clear it. A page
    left open elsewhere may be showing an older state, and its buttons must
    still do what they say.
    """
    lit = row.get("verdict") == verdict
    return html.Button(
        label,
        id={"type": "incident-verdict", "key": row["key"], "verdict": verdict,
            "act": "clear" if lit else "set"},
        n_clicks=0,
        className=f"inc-btn inc-{verdict}" + (" on" if lit else ""),
        title=("Click again to take this answer back" if lit else None),
        **{"aria-pressed": "true" if lit else "false"})


def _lines(row):
    said = row.get("lines") or []
    if not said:
        return None
    shown = said[:MAX_LINES]
    body = [html.Div([
        html.Span(line["time"], className="inc-said-time"),
        html.Span(line.get("speaker") or "?", className="inc-said-who"),
        html.Span(line.get("text") or "", className="inc-said-text"),
    ], className="inc-said") for line in shown]
    if len(said) > len(shown):
        body.append(html.Div(f"and {len(said) - len(shown)} more lines",
                             className="inc-said-more"))
    count = "the 1 line" if len(said) == 1 else f"the {len(said)} lines"
    return html.Details([
        html.Summary(f"Show {count} it rests on"),
        html.Div(body, className="inc-lines-box"),
    ], className="inc-lines")


def _row(row):
    name, family = _KINDS.get(row["kind"], (str(row["kind"]).title(), "worry"))
    head = [html.Span(name, className="inc-kind"),
            html.Span(_who(row), className="inc-who")]
    if row.get("tension") is not None and family == "strain":
        head.append(html.Span(
            f"tension {row['tension']:.2f}", className="inc-tension",
            title="The sharpest reading, from 0 to 1, of the conversation "
                  "these minutes belong to. It is what the Tension lamp counts."))
    return html.Div([
        html.Div([html.Div(_span(row), className="inc-clock"),
                  html.Div(_length(row), className="inc-length")],
                 className="inc-time"),
        html.Div([
            html.Div(head, className="inc-headline"),
            html.Div(row.get("what") or row.get("about") or "", className="inc-what"),
        ], className="inc-body"),
        html.Div([_button(row, "confirmed", "Confirm"),
                  _button(row, "rejected", "Reject")], className="inc-verdict"),
        # Last, and a child of the row rather than of the text beside the
        # buttons: opened, the lines run under the buttons as well, which is
        # the width a spoken sentence needs.
        _lines(row),
    ], className="inc-row", **{"data-verdict": row.get("verdict") or "none",
                               "data-family": family})


def _tally(tally):
    cells = [("listed", tally["listed"]), ("confirmed", tally["confirmed"]),
             ("rejected", tally["rejected"]), ("not judged", tally["unjudged"])]
    return html.Div([html.Span([html.B(str(n)), f" {label}"]) for label, n in cells],
                    className="inc-tally")


def _read_note(reading):
    """When a day's list was made and by what, for the day's heading."""
    try:
        when = datetime.fromisoformat(str(reading.get("read_at")))
        stamp = f"list read {when.day} {when:%b %H:%M}"
    except (TypeError, ValueError):
        stamp = "list read"
    model = reading.get("model")
    note = stamp + (f" by {model}" if model else "")
    unread = reading.get("windows_failed") or 0
    if unread:
        # Minutes nobody read are not quiet minutes, and the day says so.
        note += f" · {unread} five-minute window(s) could not be read"
    return note


def _answer(day, answer, label, lit=False):
    return html.Button(
        label, id={"type": "incident-prepare", "day": day, "answer": answer},
        n_clicks=0, className="inc-btn inc-answer" + (" on" if lit else ""))


def _minutes_since(stamp, now):
    try:
        began = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    minutes = int((now - began).total_seconds() // 60)
    ago = "just now" if minutes < 1 else f"{minutes} min ago"
    return f"Started at {began:%H:%M}, {ago}. "


def _asking(listing, today, now):
    """The question about one day, or how the answer to it is going.

    (what to draw above the list, what to draw below it). One day is asked
    about at a time -- the latest that is over and has no list -- because one
    day is prepared at a time; the days before it are asked about in turn.
    Today is never asked about. A day is written to the store once: read at
    eight in the evening it would hold the day up to eight, and what was said
    after that would never be listed.
    """
    above, below = [], []
    missing = sorted(d for d in listing["not_stored"] + listing["not_listed"]
                     if d < today)

    busy = prepare.running(now)
    if busy:
        began = prepare.state_of(busy, now).get("started_at")
        above.append(html.Div([
            html.Div(f"Preparing the list for {_day_name(busy)}.",
                     className="inc-ask-q"),
            html.Div(_minutes_since(began, now)
                     + "A day takes a few minutes. Its list will appear here "
                       "by itself when it is ready.", className="inc-ask-why"),
        ], className="inc-ask inc-working"))

    states = {d: prepare.state_of(d, now) for d in missing}
    declined = [d for d in missing if states[d].get("state") == "declined"]
    waiting = [d for d in missing if d not in declined and d != busy]

    if waiting and not busy:
        day = waiting[-1]
        where = ("It is in the store, but was never read for incidents."
                 if day in listing["not_listed"] else
                 "It is not in the store yet.")
        asked = []
        if states[day].get("state") == "failed":
            asked.append(html.Div(
                "It was tried and no list came of it: "
                f"{states[day].get('error') or 'no reason was recorded'}",
                className="inc-ask-failed"))
        asked += [
            html.Div(f"Do you want me to prepare the list for {_day_name(day)}?",
                     className="inc-ask-q"),
            html.Div(f"{where} Preparing it sends that day's transcript to "
                     f"{prepare.MODEL} and takes a few minutes.",
                     className="inc-ask-why"),
            html.Div([_answer(day, "yes", "Yes", lit=True),
                      _answer(day, "no", "No")], className="inc-ask-buttons"),
        ]
        if len(waiting) > 1:
            asked.append(html.Div(
                f"{_runs(waiting[:-1])} in this window "
                f"{'has' if len(waiting) == 2 else 'have'} no list either. "
                "I will ask about the day before once this one is answered.",
                className="inc-ask-more"))
        above.append(html.Div(asked, className="inc-ask"))
    elif waiting:
        below.append(html.Div(
            f"No list for {_runs(waiting)} either. I will ask about them when "
            "the day being prepared is done.", className="fb-note"))

    if declined:
        below.append(html.Div([
            f"No list for {_runs(declined)}, as you chose. That is not the "
            "same as nothing having happened. ",
            _answer(",".join(declined), "again", "Ask me again"),
        ], className="fb-note inc-declined"))
    if today in listing["not_stored"]:
        below.append(html.Div(
            "Today has no list yet. A day is read once, when it is over, so "
            "that nothing said late in the evening is left out of it: I will "
            "ask about today after midnight.", className="fb-note"))
    return above, below


def _blind_window(day, window, tagged, answered):
    """One five-minute stretch: its lines, its recordings, and the tags."""
    lo = window["lo"]
    span = f"{blind.clock_of(lo)} \u2013 {blind.clock_of(lo + blind.WINDOW_SECONDS)}"

    def tag(kind, name, lit):
        return html.Button(
            name, n_clicks=0,
            className="inc-btn inc-tag" + (" on" if lit else ""),
            id={"type": "blind-act", "day": day, "window": str(lo), "kind": kind})

    tags = [tag(kind, name, kind in tagged) for kind, (name, _f) in _KINDS.items()]
    tags.append(tag("nothing", "Nothing happened here", answered and not tagged))
    players = [html.Audio(src=f"/assets/audio_mp3/{name}", controls=True,
                          preload="none", className="inc-blind-audio")
               for name in window["audio"][:40]]
    return html.Div([
        html.Div([html.Span(span, className="inc-clock"),
                  html.Span("answered" if answered else "not answered yet",
                            className="inc-blind-done" if answered else "inc-blind-open")],
                 className="inc-blind-head"),
        html.Div([html.Div(line, className="inc-blind-line") for line in window["lines"]],
                 className="inc-lines-box"),
        html.Details([html.Summary(f"Listen: {len(window['audio'])} recording(s)"),
                      html.Div(players, className="inc-blind-players")],
                     className="inc-lines") if players else None,
        # Every kind, not the main one: a long stretch often holds a
        # complaint and a warm exchange both, and a single tag there counted
        # the system's second kind as an error (Section 3.4.4).
        html.Div("Tick every kind that happened in this stretch, not only the "
                 "main one." + (f" Ticked: {len(tagged)}." if tagged else ""),
                 className="inc-blind-ask"),
        html.Div(tags, className="inc-blind-tags"),
    ], className="inc-blind-window", **{"data-answered": "yes" if answered else "no"})


def _blind_step(day, drawn, have, windows):
    """'What happened in each of these?', asked before that day's list is shown."""
    by_lo = {w["lo"]: w for w in windows}
    shown = [by_lo[lo] for lo in drawn["windows"] if lo in by_lo]
    left = sum(1 for w in shown if w["lo"] not in have["labels"])
    return html.Div([
        html.Div(f"Before I show the list: what happened in these stretches of "
                 f"{_day_name(day)}?", className="inc-ask-q"),
        html.Div(f"{len(shown)} five-minute stretches, in no particular order. "
                 "Some are ones in which I listed something and some are not; "
                 "which is which is not shown. For each stretch, tick EVERY kind "
                 "of incident in it -- a stretch can hold several, for example a "
                 "complaint and then a warm moment -- or choose “Nothing "
                 "happened here”.", className="inc-ask-why"),
        html.Div([html.Div([html.Span(name, className="inc-kind"),
                            html.Span(KIND_DEFINITIONS[kind], className="inc-def-text")],
                           className="inc-def")
                  for kind, (name, _family) in _KINDS.items()]
                 + [html.Div("Ordinary conversation is not an incident, however "
                             "pleasant or serious.", className="inc-def-rule")],
                 className="inc-defs"),
        *[_blind_window(day, w, have["labels"].get(w["lo"], []),
                        w["lo"] in have["labels"]) for w in shown],
        html.Div([
            html.Div(f"{left} stretch(es) still have no answer." if left else
                     "Every stretch has an answer.", className="inc-ask-why"),
            html.Button("That is all \u2014 show the list", n_clicks=0,
                        disabled=bool(left), className="inc-btn inc-answer on",
                        id={"type": "blind-act", "day": day, "window": "",
                            "kind": "seal"}),
        ], className="inc-ask-buttons inc-blind-close"),
    ], className="inc-ask inc-blind")


def render(start, end, notice=None, today=None, now=None):
    """The panel for a window. `notice` is one line to show above the list.

    `today` is the date a day counts as still going on, YYYY-MM-DD, and `now`
    the moment; the clock supplies both unless a test does.
    """
    now = now or datetime.now()
    today = today or now.strftime("%Y-%m-%d")
    listing = judgements.listed_between(start, end)
    rows = listing["rows"]
    ask_above, ask_below = _asking(listing, today, now)

    # Blind labelling comes before the list. The latest day that is over, has
    # a list, and has had none of it judged is held back until the resident
    # has tagged the stretches drawn for it: tags given after reading the list
    # would be the list again.
    held, blind_block = None, []
    for day in sorted(listing["read"], reverse=True):
        if day >= today:
            continue
        untouched = not any(r["verdict"] for r in rows if r["day"] == day)
        stored = blind.load_day(day) if untouched else None
        drawn = blind.draw(day, stored) if stored else None
        have = blind.state(day) if drawn else None
        if drawn and not have["sealed_at"]:
            held = day
            blind_block = [_blind_step(day, drawn, have, blind.day_windows(stored))]
        break
    if held:
        rows = [r for r in rows if r["day"] != held]
        listing = {**listing, "tally": judgements.tally(rows)}

    window = (f"{start.day} {start:%b %Y %H:%M} to {end.day} {end:%b %Y %H:%M}")
    children = [html.Div([
        html.H3("What happened, and when"),
        html.P(f"Incidents listed for {window}."
               + (" Confirm the ones that happened, reject the ones that "
                  "did not." if rows else "")),
    ], className="fb-head")]
    if notice:
        children.append(html.Div(notice, className="inc-notice"))
    # The question first: the day it asks about is the newest one, and the
    # one a resident came to judge.
    children.extend(ask_above)
    children.extend(blind_block)

    if rows:
        children.append(_tally(listing["tally"]))
        by_day = {}
        for row in rows:
            by_day.setdefault(row["day"], []).append(row)
        # The newest day first, because that is the one being judged tonight;
        # within a day, the order things happened in.
        for day in sorted(by_day, reverse=True):
            children.append(html.Div([
                html.Span(_day_name(day), className="inc-day-name"),
                html.Span(_read_note(listing["read"].get(day) or {}),
                          className="inc-day-read"),
            ], className="inc-day"))
            children.extend(_row(row) for row in by_day[day])
    elif listing["read"]:
        quiet = _runs(listing["read"])
        children.append(html.Div(
            f"Nothing was listed in this window on {quiet}.", className="fb-empty"))
    else:
        children.append(html.Div(
            "No day in this window has an incident list.", className="fb-empty"))

    # A day that was read and holds nothing in the window still has to be told
    # apart from a day that was never read.
    empty_read = [d for d in listing["read"] if rows and d not in {r["day"] for r in rows}]
    if empty_read:
        children.append(html.Div(
            f"Read, with nothing listed in this window: {_runs(empty_read)}.",
            className="fb-note"))
    children.extend(ask_below)
    if rows:
        children.append(html.Div(
            "Times are those of the first and last recorded line that show an "
            "incident. Recording is triggered by sound, so it may have begun "
            "earlier and gone on longer. A rejected incident stays on the list: "
            "the answers are the record of how often the list is right.",
            className="fb-note"))
    return html.Div(children, className="fb-panel inc-panel")
