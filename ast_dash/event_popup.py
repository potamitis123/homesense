"""The clips behind a number, listed with players so they can be checked by ear.

A chart says "61" and there is no way to ask which 61. These helpers answer
that, and they live here rather than in one page because Daily Events and
Statistics ask the same question of different selections -- a heatmap cell, a
bar, an hour of a polar plot, a point on a timeline. Sharing the rendering is
what keeps the answer identical whichever chart was clicked.

Each page builds its own WHERE clause, because only the page knows what its
chart was counting. Everything after that -- the cap, the row, the player, the
reason a player is missing -- is here.
"""

import os
import sqlite3
from pathlib import Path

from dash import html
import dash_bootstrap_components as dbc

from misc.load_parameters import parameters

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# A busy selection can hold far more clips than anyone wants to scroll, and
# every row carries an audio element. Capped, with the caller told the real
# total so the modal can say what was left out rather than imply the list is
# complete.
EVENT_LIMIT = 25

# Every column a row needs. Named once so the two pages cannot drift into
# fetching different things and rendering them the same way.
EVENT_COLUMNS = (
    "id, date, device_name, room, file_name, transcription, emotion,"
    " sound1, conf_sound1, sound2, conf_sound2, sound3, conf_sound3"
)


def db_path():
    return os.path.join(PROJECT_ROOT,
                        parameters.get_parameters()['sqlite']['db_name'])


def fetch(where, args, limit=EVENT_LIMIT):
    """(rows, total) for a WHERE clause over ast_table, oldest first.

    `total` is the count before the cap, so the caller can say how many were
    not shown instead of quietly truncating.
    """
    try:
        conn = sqlite3.connect(db_path())
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            f"SELECT COUNT(*) FROM ast_table WHERE {where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM ast_table WHERE {where}"
            f" ORDER BY date LIMIT ?", list(args) + [int(limit)]).fetchall()
        conn.close()
        return rows, total
    except Exception as e:
        print(f"DB error listing events for a chart selection: {e}")
        return [], 0


def count_detections(where, args, ranks=2, sound=None):
    """How many DETECTIONS a selection holds, the way the charts count them.

    The charts expand sound1..soundN into separate rows, so one clip can be
    two detections. The popup lists clips, because a clip is what can be
    played. For a selection about one class those numbers agree -- a clip
    rarely carries the same label at two ranks -- but for an hour or a
    timeline bucket they do not: 1199 detections in hour 14 came from 841
    clips.

    Returned so the header can say both rather than show a list whose length
    silently contradicts the number that was clicked.

    `sound` must be given when the selection is about one class, and counts
    only the ranks holding that label. Without it the count would include the
    clip's OTHER labels too: the 1481 Speech detections a bar shows came from
    1481 clips whose remaining labels bring the total to 2424, which is not
    what the bar is measuring.
    """
    total = 0
    try:
        conn = sqlite3.connect(db_path())
        for n in range(1, ranks + 1):
            if sound is not None:
                total += conn.execute(
                    f"SELECT COUNT(*) FROM ast_table WHERE {where}"
                    f" AND sound{n} = ?", list(args) + [sound]).fetchone()[0]
            else:
                total += conn.execute(
                    f"SELECT COUNT(*) FROM ast_table WHERE {where}"
                    f" AND sound{n} IS NOT NULL"
                    f" AND LOWER(TRIM(sound{n})) NOT IN ('none', 'silence')",
                    args).fetchone()[0]
        conn.close()
    except Exception as e:
        print(f"DB error counting detections: {e}")
        return None
    return total


def rank_clause(sound_placeholder_count):
    """`(sound1 = ? OR sound2 = ? ...)` for however deep the page counts."""
    return "(" + " OR ".join(f"sound{n} = ?" for n in
                             range(1, sound_placeholder_count + 1)) + ")"


def audio_element(file_name):
    """A player when the recording is still there, and a reason when not.

    Audio is deleted on a retention schedule and, depending on the storage
    setting, may never have been written at all -- so a missing recording is
    the normal case for older days, not a fault. Saying which is better than
    a control that silently does nothing.
    """
    if not file_name:
        return html.Span("no recording for this event",
                         className="text-muted small fst-italic")
    if not os.path.exists(PROJECT_ROOT / "assets" / "audio_mp3" / file_name):
        return html.Span("audio no longer on disk",
                         className="text-muted small fst-italic")
    return html.Audio(src=f"/assets/audio_mp3/{file_name}", controls=True,
                      preload="none", style={"height": "32px", "width": "260px"})


def event_rows(rows, matched_sound=None):
    """One block per clip: when, where, why it is here, what was said.

    `matched_sound` is the class the selection was about, when there is one.
    It earns a badge saying which of AST's ranks matched and how confident it
    was, because a clip can be in a selection on its third guess at 0.01 --
    and without saying so the row looks like a mistake rather than the filter
    working as asked. Selections that are not about one class (an hour, a
    timeline bucket) pass None and get no badge.
    """
    out = []
    for r in rows:
        labels = [(r["sound1"], r["conf_sound1"]), (r["sound2"], r["conf_sound2"]),
                  (r["sound3"], r["conf_sound3"])]
        room = (r["room"] or "").replace("_", " ").strip()
        where = f"{room} {r['device_name']}".strip() if room else (r["device_name"] or "")

        head = [html.Span(str(r["date"])[11:19], className="fw-bold me-3"),
                html.Span(where, className="text-muted me-3")]
        if matched_sound:
            rank = next((i + 1 for i, (lab, _c) in enumerate(labels)
                         if lab == matched_sound), None)
            conf = next((c for lab, c in labels if lab == matched_sound), None)
            head.append(dbc.Badge(
                f"rank {rank} · {conf:.2f}" if rank and conf is not None else "matched",
                color="secondary", className="me-2"))
        head.append(html.Span(
            " · ".join(f"{lab} {c:.2f}" for lab, c in labels
                       if lab and str(lab).lower() != 'none'),
            className="text-muted small"))

        text = (r["transcription"] or "").strip()
        spoke = text and not text.startswith("No Speech Detected")
        out.append(dbc.ListGroupItem([
            html.Div(head, className="d-flex align-items-center flex-wrap mb-2"),
            html.Div(text if spoke else (text or "—"),
                     className=("mb-2" if spoke
                                else "mb-2 text-muted small fst-italic")),
            html.Div([
                audio_element(r["file_name"]),
                html.Span(f"  #{r['id']}", className="text-muted small ms-3"),
                html.Span(f"  {r['emotion']}" if r["emotion"] else "",
                          className="text-muted small ms-3"),
            ], className="d-flex align-items-center"),
        ]))
    return out


def modal_body(rows, total, matched_sound=None, empty_note=None,
               detections=None):
    """The count line plus the rows, or an explanation when there are none.

    `detections` is the chart's own number when it differs from the number of
    clips -- see count_detections. Saying both is the point: a reader who
    clicked "1199" and is handed 841 rows should be told why, not left to
    conclude the list is broken.
    """
    if not rows:
        return html.Div(
            empty_note or "No events found for this selection.",
            className="text-muted")
    header = f"{total} event{'s' if total != 1 else ''}"
    if detections and detections != total:
        header += (f" · the chart counts {detections} detections here, because "
                   f"a clip's 1st and 2nd labels are counted separately")
    if total > len(rows):
        header += f", showing the first {len(rows)}"
    return [html.Div(header, className="text-muted small mb-2"),
            dbc.ListGroup(event_rows(rows, matched_sound), flush=True)]


def modal(prefix):
    """The dialog itself. `prefix` namespaces the ids so pages do not collide."""
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle(id=f"{prefix}-title")),
        dbc.ModalBody(id=f"{prefix}-body"),
        dbc.ModalFooter(dbc.Button("Close", id=f"{prefix}-close",
                                   color="secondary", n_clicks=0)),
    ], id=f"{prefix}-modal", is_open=False, size="xl", scrollable=True)
