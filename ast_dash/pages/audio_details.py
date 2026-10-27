from itertools import groupby
import dash
from dash import  html,callback,get_app,Output,Input,State,dcc,callback_context,no_update, clientside_callback, ctx
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import os, sys, shutil
import sqlite3
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import librosa
import numpy as np
from flask import request
import json
import base64
from datetime import datetime, timedelta
from ast_dash.sql_to_pandas import SQL_to_Pandas
import pandas as pd
import sys
import traceback
#sys.path.append("../misc")
from misc import *
from ast_sql.ast_sql_lite import Ast_Sql_lite
from misc.load_parameters import parameters
import zipfile
from dash.exceptions import PreventUpdate
import uuid
import time
from PIL import Image
import torch

# Hold-to-talk: press → start_live_audio + stream; release → stop_live_audio.
# Legacy MQTT play remains available as automatic / configured fallback.
# UI → PushToTalkController → StreamingAudioTransport (or MQTTAudioTransport)
from ast_src.audio_transport import AudioSessionManager
from ast_src.audio_transport.ptt_controller import MODE_MQTT_PLAY, MODE_STREAMING

_AUDIO_SESSIONS = AudioSessionManager.get_instance()
_PTT = _AUDIO_SESSIONS.get_ptt_controller()


def get_ptt_controller(device_ids=None):
    """Return the shared PTT controller, optionally updating target Sentinels."""
    if device_ids is not None:
        _PTT.set_targets(device_ids)
    return _PTT


app=get_app()

dash.register_page(
    __name__,
    path='/',
    title='Audio Dashboard',
    name='Audio Dashboard'
)

# Initialize status for UI
try:
    raw_status = f"GPU: {torch.cuda.get_device_name(0)}" if torch.cuda.is_available() else "CPU"
except:
    raw_status = "CPU"

# Dark theme tokens, kept in sync with assets/sentinel_dark.css :root and
# the same constants used in statistics.py. Set directly in Python because
# CSS cannot reach inside an SVG's own filled elements or a Heatmap's
# colorbar.
SX_TEXT_DIM = "#c3cee1"
SX_MUTED = "#9aa8c0"
SX_GRID = "rgba(255,255,255,0.08)"
SX_BORDER = "rgba(255,255,255,0.16)"
SX_TRANSPARENT = "rgba(0,0,0,0)"

# How the Audio Dashboard's spectrogram panel is rendered.
#   'plotly' - computed on the fly from the audio file via librosa and styled
#              for this page's dark theme. Nothing is written to disk, so
#              colours can be whatever the theme needs and stay that way --
#              a saved PNG's pixels can't be recoloured after the fact,
#              which is why the old approach couldn't just be restyled.
#   'png'    - the original behaviour: display the pre-rendered image named
#              in row['spectrogram_image'].
# Flipping this one value and rebooting is the entire rollback: no git, no
# data change, nothing else in the file needs touching.
SPECTROGRAM_RENDER_MODE = 'plotly'


def _styled_message_fig(text, height=780):
    """A dark-themed placeholder for the missing-audio / error states."""
    fig = go.Figure()
    fig.add_annotation(text=text, showarrow=False,
                       font=dict(size=14, color=SX_MUTED))
    fig.update_layout(
        paper_bgcolor=SX_TRANSPARENT, plot_bgcolor=SX_TRANSPARENT,
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        margin=dict(l=0, r=0, t=0, b=0), height=height,
    )
    return fig


def _find_audio_file(audio_filename, base_assets):
    """Locate the recording on disk, trying the sibling extension too.

    Returns (audio_path, audio_src) -- the filesystem path for reading the
    samples, and the /assets/... URL the <audio> element's src uses. Neither
    depends on a spectrogram image existing.
    """
    audio_basename = os.path.basename(audio_filename)
    search_names = [audio_basename]
    if audio_basename.endswith('.mp3'):
        search_names.append(audio_basename.replace('.mp3', '.wav'))
    elif audio_basename.endswith('.wav'):
        search_names.append(audio_basename.replace('.wav', '.mp3'))

    for name in search_names:
        for folder in ['audio_mp3', 'audios', 'audiosuser']:
            path = os.path.join(base_assets, folder, name)
            if os.path.exists(path):
                return path, f"/assets/{folder}/{name}"
    return None, None


def build_spectrogram_plotly_fig(audio_path):
    """Waveform, frequency spectrum and spectrogram computed live from audio.

    Mirrors the numeric content of ast_src.create_spectrogram's
    create_freq_analyzer_spectrogram -- same STFT settings, same three
    panels, same 0-8 kHz view -- rendered as native Plotly traces instead of
    a matplotlib PNG, so it can be styled for the page's dark theme.
    """
    audio_data, audio_sr = librosa.load(audio_path, sr=None)

    n_fft = 2048
    D = librosa.stft(audio_data, n_fft=n_fft)
    D_db = librosa.amplitude_to_db(np.abs(D), ref=np.max)

    freq_spectrum = np.mean(np.abs(D), axis=1)
    freq_db = librosa.amplitude_to_db(freq_spectrum, ref=np.max)
    frequencies = librosa.fft_frequencies(sr=audio_sr, n_fft=n_fft)

    duration = len(audio_data) / audio_sr
    time_axis = np.linspace(0, duration, len(audio_data))

    frame_times = librosa.frames_to_time(np.arange(D_db.shape[1]), sr=audio_sr, n_fft=n_fft)
    # Same 0-8 kHz view as the original's ax1.set_ylim(0, 8000); a Heatmap
    # trace has no "view range without touching the data" option, so the
    # array itself is sliced rather than just the axis range.
    freq_mask = frequencies <= 8000
    spec_freqs = frequencies[freq_mask]
    spec_db = D_db[freq_mask, :]

    # Frequencies are converted to kHz for display: "8" reads better than
    # "8000" and the axis labels stop eating horizontal space. Presentation
    # only -- the STFT above is untouched, and hover now agrees with the
    # tick labels rather than reporting Hz against a kHz axis.
    frequencies_khz = frequencies / 1000.0
    spec_freqs_khz = spec_freqs / 1000.0

    # 0.13 rather than 0.09: each row carries an x-axis title below it and
    # the next row's subplot title above it, and at the old spacing those two
    # collided -- "Frequency Analyzer" landed on the waveform's "Time (s)".
    fig = make_subplots(
        rows=3, cols=1,
        row_heights=[1, 1, 3],
        vertical_spacing=0.13,
        subplot_titles=("Audio Waveform", "Frequency Analyzer", "Spectrogram"),
    )

    # WebGL trace: a several-second clip is tens of thousands of samples,
    # which a plain SVG Scatter renders noticeably slower.
    fig.add_trace(go.Scattergl(
        x=time_axis, y=audio_data, mode='lines',
        line=dict(color='#7c9cff', width=1), hoverinfo='skip', showlegend=False,
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=frequencies_khz, y=freq_db, mode='lines',
        line=dict(color='#22d3ee', width=1.5), showlegend=False,
    ), row=2, col=1)

    # Match the colourbar to the spectrogram's own panel instead of guessing
    # at it. make_subplots has already computed the row domains by this
    # point, so reading yaxis3 keeps the bar aligned automatically if the
    # row heights or spacing are ever changed again.
    spec_bottom, spec_top = fig.layout.yaxis3.domain

    fig.add_trace(go.Heatmap(
        x=frame_times, y=spec_freqs_khz, z=spec_db,
        colorscale='Magma', reversescale=True,   # visual match for cmap='magma_r'
        zmin=float(spec_db.min()), zmax=float(spec_db.max()),
        colorbar=dict(
            title=dict(text='dB', font=dict(color=SX_TEXT_DIM)),
            tickfont=dict(color=SX_TEXT_DIM),
            outlinewidth=0,
            len=spec_top - spec_bottom,
            y=(spec_top + spec_bottom) / 2,
            yanchor='middle',
        ),
    ), row=3, col=1)

    axis_style = dict(gridcolor=SX_GRID, zerolinecolor=SX_BORDER, color=SX_MUTED)
    fig.update_xaxes(title_text="Time (s)", row=1, col=1, **axis_style)
    fig.update_yaxes(title_text="Amplitude", row=1, col=1, **axis_style)

    fig.update_xaxes(title_text="Frequency (kHz)", row=2, col=1,
                     range=[0, 8], dtick=1, **axis_style)
    fig.update_yaxes(title_text="Magnitude (dB)", row=2, col=1, **axis_style)

    fig.update_xaxes(title_text="Time (s)", row=3, col=1, **axis_style)
    fig.update_yaxes(title_text="Frequency (kHz)", row=3, col=1,
                     range=[0, 8], dtick=1, **axis_style)

    fig.update_layout(
        paper_bgcolor=SX_TRANSPARENT, plot_bgcolor=SX_TRANSPARENT,
        font=dict(color=SX_TEXT_DIM),
        # Right margin leaves room for the colourbar and its tick labels,
        # which sit outside the plotting area.
        margin=dict(l=60, r=80, t=40, b=50),
        height=780,
        showlegend=False,
    )
    # Subplot titles are plain annotations in Plotly, not covered by the
    # figure-level font default above.
    for annotation in fig['layout']['annotations']:
        annotation['font'] = dict(color=SX_TEXT_DIM, size=14)

    return fig


def _build_spectrogram_png_fig(image_filename, base_assets):
    """Original behaviour: display the pre-rendered PNG, unstyled.

    Kept byte-for-byte as it was, as the instant-rollback target for
    SPECTROGRAM_RENDER_MODE = 'png'.
    """
    if not image_filename:
        fig = go.Figure()
        fig.add_annotation(text="No data", showarrow=False)
        return fig

    image_basename = os.path.basename(image_filename)
    image_path = None
    image_folder = None
    for folder in ['specrogram_images', 'spectrogram_images']:
        path = os.path.join(base_assets, folder, image_basename)
        if os.path.exists(path):
            image_path = path
            image_folder = folder
            break
    image_src = f"/assets/{image_folder}/{image_basename}" if image_folder else None

    try:
        if not image_path:
            fig = go.Figure()
            fig.add_annotation(text="Spectrogram image not found", showarrow=False)
            return fig

        img = Image.open(image_path)
        width, height = img.size

        fig = go.Figure()
        fig.add_layout_image(dict(
            source=image_src, xref="x", yref="y", x=0, y=height,
            sizex=100, sizey=height, sizing="stretch", opacity=1, layer="below"
        ))
        fig.update_layout(
            xaxis=dict(range=[0, 100], visible=False, fixedrange=True),
            yaxis=dict(range=[0, height], visible=False, fixedrange=True),
            margin=dict(l=0, r=0, t=0, b=0),
            height=350,
        )
        return fig

    except Exception as e:
        print(f"ERROR in _build_spectrogram_png_fig: {e}")
        fig = go.Figure()
        fig.add_annotation(text=f"Error: {str(e)}", showarrow=False)
        return fig


def prepare_spectrogram_fig(audio_filename, image_filename):
    """Returns (audio_src, fig) for the Audio Analysis panel.

    audio_src feeds the <audio> element and is computed the same way
    regardless of render mode or of whether a spectrogram image exists --
    the audio player must keep working even for an event that has no image
    (already true for anything recorded after PNG generation is turned off,
    and now also true if one was ever simply missing).
    """
    if not audio_filename:
        return None, _styled_message_fig("No data")

    base_assets = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'assets'))
    audio_path, audio_src = _find_audio_file(audio_filename, base_assets)

    if SPECTROGRAM_RENDER_MODE == 'png':
        return audio_src, _build_spectrogram_png_fig(image_filename, base_assets)

    if not audio_path:
        return audio_src, _styled_message_fig("Audio file not found")
    try:
        return audio_src, build_spectrogram_plotly_fig(audio_path)
    except Exception as e:
        print(f"ERROR in build_spectrogram_plotly_fig: {e}")
        return audio_src, _styled_message_fig(f"Could not render spectrogram: {e}")

# Secondary/tertiary detections below this confidence are hidden entirely
# rather than shown as noise. Consumed by the SentinelDetections renderer.
SECONDARY_CLASS_THRESHOLD = 0.10

# Fields the grid needs in rowData but never shows. 'ID' in particular backs
# dashGridOptions.getRowId and the cellClicked -> play_row_audio lookup, so it
# must stay in the data even though the operator never sees the column.
_HIDDEN_FIELDS = [
    'ID', 'rms', 'file_name', 'spectrogram_image', 'clap_embedding',
    'Class 2', 'Prob 2', 'Class 3', 'Prob 3', 'emotion', 'Emotion',
    'llm_verdict',
]


def build_grid_columns(df):
    """
    Column definitions for the Audio Events grid.

    Replaces the generic one-column-per-DB-field layout with five readable
    columns, so the operator sees what happened, where, what was said and how
    confident the model is without scanning sideways. The underlying rowData
    is untouched -- secondary classes and probabilities are still present, they
    are simply rendered inside the detections badge cell instead of occupying
    six columns of their own.

    sql_to_pandas.get_columns() is deliberately left alone: the settings page
    still uses it.
    """
    if df is None:
        return []

    present = list(df.columns)

    columns = [{
        "headerName": "",
        "checkboxSelection": True,
        "headerCheckboxSelection": True,
        "width": 44,
        "minWidth": 44,
        "maxWidth": 44,
        "pinned": "left",
        "lockPosition": True,
        "suppressMovable": True,
        "resizable": False,
        "sortable": False,
        "filter": False,
        "colId": "checkbox_col",
    }]

    spec = [
        ('Timestamp', {
            "headerName": "Time",
            "cellRenderer": "SentinelTime",
            "width": 150, "minWidth": 130,
        }),
        ('Device', {
            "headerName": "Device",
            "cellRenderer": "SentinelDevice",
            "width": 170, "minWidth": 140,
        }),
        ('transcription', {
            "headerName": "Transcription (speaker & emotion)",
            "cellRenderer": "SentinelTranscript",
            "flex": 1.4, "minWidth": 300,
            "autoHeight": False,
            "sortable": True,
        }),
        ('Class 1', {
            "headerName": "Top detections",
            "cellRenderer": "SentinelDetections",
            "cellRendererParams": {"threshold": SECONDARY_CLASS_THRESHOLD},
            "flex": 1.3, "minWidth": 290,
        }),
        # No Confidence column: "Top detections" already prints the score
        # beside each label ("Speech 0.89"), so a column of its own repeated
        # the first one and spent 140px doing it. The field stays in the row
        # data -- SentinelDetections reads props.data['Prob 1'] from there,
        # not from a column definition -- so nothing downstream notices.
    ]

    for field, extra in spec:
        if field not in present:
            continue
        col = {
            "field": field,
            "hide": False,
            "sortable": True,
            "filter": True,
            "resizable": True,
            "suppressMovable": True,
        }
        col.update(extra)
        columns.append(col)

    # CLAP search adds this; keep it visible and pinned as before.
    if 'similarity' in present:
        columns.append({
            "field": "similarity",
            "headerName": "Similarity",
            "hide": False,
            "width": 110,
            "suppressMovable": True,
            "resizable": True,
            "pinned": "right",
            "cellStyle": {"textAlign": "left", "fontWeight": "bold", "color": "#4ade80"},
            "valueFormatter": {"function": "params.value ? Number(params.value).toFixed(3) : ''"},
        })

    # Everything else travels in rowData but stays out of sight.
    shown = {c.get("field") for c in columns}
    for field in present:
        if field in shown:
            continue
        columns.append({
            "field": field,
            "hide": True,
            "suppressMovable": True,
            "resizable": True,
        })

    return columns


# ── Dashboard view filter ───────────────────────────────────────────
#
# Presentation only. Nothing below touches the query, the database or the
# retention of events: it narrows the already-fetched row list on its way
# into the grid, and switching back to "All" brings every row straight back.

VIEW_SPEECH, VIEW_OTHER, VIEW_ALL = 'speech', 'other', 'all'
DEFAULT_EVENT_VIEW = VIEW_SPEECH

# Substring-matched against the top-3 AST class names, so "Male speech, man
# speaking" and "Narration, monologue" are covered without enumerating every
# AudioSet variant.
_SPEECH_LABEL_HINTS = ('speech', 'monologue', 'narration', 'conversation',
                       'whisper', 'babbling')

# The pipeline writes one of these when there is nothing to read. The suffix
# records which stage gave up -- (AST) never reached ASR, (VAD) found no
# voiced frames, (ASR) decoded nothing -- and all three mean the same thing
# to an operator scanning the Transcription column.
_NO_SPEECH_MARKER = 'no speech detected'


def _row_has_speech(row):
    """True when the event carries words a person could actually read.

    Both halves are required, which makes this the exact complement of the
    "everything but speech" view as specified: no speech label in the first
    three ranks OR the ASR reported no speech. Because they are complements,
    every event lands in exactly one of the two filtered views -- nothing can
    fall out of both.

    On current data the label test never changes the outcome: ASR only runs
    once AST has flagged speech, so a transcript implies a speech label (0
    counter-examples in 6,358 rows of 27 Aug). It is kept because it is part
    of the stated definition and costs nothing if that ordering ever changes.
    """
    labelled = any(
        any(hint in str(row.get(field) or '').lower()
            for hint in _SPEECH_LABEL_HINTS)
        for field in ('Class 1', 'Class 2', 'Class 3')
    )
    if not labelled:
        return False
    text = str(row.get('transcription') or '').strip()
    if not text or text.lower() in ('none', 'nan'):
        return False
    return _NO_SPEECH_MARKER not in text.lower()


def filter_rows_for_view(data, view):
    """The subset of `data` the chosen view shows. `data` itself is unchanged.

    Only the two filtering views narrow anything; VIEW_ALL, None (component
    not yet rendered) and any unrecognised value all show everything. Hiding
    rows is the surprising outcome, so it happens only on an explicit request.
    """
    if not data:
        return data or []
    if view == VIEW_SPEECH:
        return [row for row in data if _row_has_speech(row)]
    if view == VIEW_OTHER:
        return [row for row in data if not _row_has_speech(row)]
    return data


# Room lives in rowData already. It is stamped at write time from
# common_files/rooms.json, so an event records where the device was *then* --
# which is why the options below come from the fetched rows as well as the
# current mapping. A room renamed in rooms.json must not orphan its history.
ROOM_UNASSIGNED = '__unassigned__'


def _row_room(row):
    """This event's room, or None for the pre-rooms events (15k of them)."""
    value = row.get('room')
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ('none', 'nan'):
        return None
    return text


def room_filter_options(data):
    """Room chips to offer: everything configured, everything seen, in order.

    The union matters in both directions -- a configured but quiet room should
    still be selectable (so "nothing in the Bathroom today" is an answer you
    can get), and a room only present in old rows must stay reachable after it
    is renamed or removed from rooms.json.
    """
    names = set()
    try:
        from misc.rooms import all_rooms
        names.update(r for r in all_rooms().values() if r)
    except Exception as exc:                                  # pragma: no cover
        print(f"[audio_details] cannot read room mapping: {exc}")

    has_unassigned = False
    for row in data or []:
        room = _row_room(row)
        if room:
            names.add(room)
        else:
            has_unassigned = True

    options = [{"label": _room_chip(name), "value": name} for name in sorted(names)]
    if has_unassigned:
        # Only offered when such rows are actually in range, so the chip row
        # stays clean for recent windows where every event has a room.
        options.append({"label": _room_chip("Unassigned"), "value": ROOM_UNASSIGNED})
    return options


# A glyph per room, so a chip is recognisable before the word is read. Matched
# on the lower-cased name rather than an exact key, because rooms.json is
# edited by hand and "Bedroom_couple" and "bedroom" should both find the bed.
ROOM_ICONS = [
    ("bath", "fa-bath"),
    ("shower", "fa-shower"),
    ("wc", "fa-toilet"),
    ("toilet", "fa-toilet"),
    ("bed", "fa-bed"),
    ("kitchen", "fa-utensils"),
    ("living", "fa-couch"),
    ("lounge", "fa-couch"),
    ("entrance", "fa-door-open"),
    ("door", "fa-door-open"),
    ("hall", "fa-door-open"),
    ("garden", "fa-tree"),
    ("garage", "fa-car"),
    ("office", "fa-desktop"),
    ("stair", "fa-stairs"),
]


def _room_icon(name):
    lowered = (name or "").lower()
    for token, icon in ROOM_ICONS:
        if token in lowered:
            return icon
    return "fa-location-dot"          # a room we have no glyph for


def _room_chip(name):
    """The chip's label: icon then name, as one inline component."""
    return html.Span([
        html.I(className=f"fas {_room_icon(name)} sx-room-icon"),
        name,
    ], className="d-inline-flex align-items-center")


def filter_rows_for_rooms(data, rooms):
    """Rows in the selected rooms. No selection means no room filtering."""
    if not data or not rooms:
        return data or []
    wanted = set(rooms)
    want_unassigned = ROOM_UNASSIGNED in wanted
    return [row for row in data
            if (_row_room(row) in wanted
                or (want_unassigned and _row_room(row) is None))]


def view_count_label(shown, total, view=None, rooms=None):
    """Caption under the controls, so a hidden event never looks like a lost one."""
    if total == 0:
        return "no events in range"

    active = []
    if view == VIEW_SPEECH:
        active.append("Speech only")
    elif view == VIEW_OTHER:
        active.append("Non-speech only")
    if rooms:
        shown_rooms = ["Unassigned" if r == ROOM_UNASSIGNED else r for r in rooms]
        active.append(", ".join(sorted(shown_rooms)))

    if not active:
        return f"showing all {total:,} events"
    return (f"showing {shown:,} of {total:,} events "
            f"({' · '.join(active)}; {total - shown:,} hidden)")


def apply_event_view(data, view, rooms=None):
    """Filter for display and describe the result. Returns (rows, caption).

    The two filters combine with AND -- selecting Kitchen and Non-speech
    gives the non-speech events in the Kitchen, not the union of the two.
    """
    rows = filter_rows_for_rooms(filter_rows_for_view(data, view), rooms)
    return rows, view_count_label(len(rows), len(data or []), view, rooms)


# ── Go to time ──────────────────────────────────────────────────────
#
# The grid is either live -- the last week, reloaded every 10 s -- or parked
# on a fixed range. It parks when you go to a moment, and when Refresh is used
# with dates picked in the calendar; parked, the reload stops, so the page you
# landed on keeps its rows until "Back to latest". Before this the reload
# always fetched the live week, so a range picked with Refresh was replaced
# within ten seconds.
#
# The landing itself happens in the browser (assets/grid_time_jump.js): only
# the grid knows its current sort, column filters and page size, which
# follows the grid's height. The server's part is having the rows around the
# moment loaded.

LIVE_DAYS = 7
JUMP_DAYS_EACH_SIDE = 3


def live_range(now=None):
    """(from, to) of the live week: from midnight seven days back to the end of today."""
    now = now or datetime.now()
    return (date_to_str(get_date_before(now, LIVE_DAYS)),
            date_to_str(get_date_99(date_to_str(now))))


def parse_jump_time(value):
    """The moment typed into Go to time, or None. The browser's field gives "2026-09-16T14:05"."""
    text = str(value or '').strip().replace('T', ' ')[:19]
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _range_end(value, end):
    """One end of a range as a datetime. A bare day, as the date picker gives it, is the whole day."""
    text = str(value or '').strip()
    if ':' in text:
        return parse_jump_time(text)
    try:
        day = datetime.strptime(text[:10], '%Y-%m-%d')
    except ValueError:
        return None
    return day.replace(hour=23, minute=59, second=59) if end else day


def query_range(date_from, date_to, now=None):
    """The (from, to) strings get_db_rec queries.

    A day picked in the calendar arrives as "2026-09-16". Compared as text with
    "2026-09-16 14:05:36" it sorts first, so the query used to leave the picked
    end day out entirely, and picking a single day fell back to the live week.
    Both ends are now whole days. A missing, unreadable or reversed range is
    the live week.
    """
    start, end = _range_end(date_from, False), _range_end(date_to, True)
    if start is None or end is None or start >= end:
        return live_range(now)
    return date_to_str(start), date_to_str(end)


def range_holds(rng, moment):
    """True when a (from, to) range covers the moment."""
    start, end = _range_end(rng[0], False), _range_end(rng[1], True)
    return start is not None and end is not None and start <= moment <= end


def range_around(moment, now=None):
    """The rows to load for a moment outside what is loaded: three days either side, never past today."""
    now = now or datetime.now()
    moment = min(moment, now)
    start = moment - timedelta(days=JUMP_DAYS_EACH_SIDE)
    end = min(moment + timedelta(days=JUMP_DAYS_EACH_SIDE), now)
    return (date_to_str(start.replace(hour=0, minute=0, second=0, microsecond=0)),
            date_to_str(end.replace(hour=23, minute=59, second=59, microsecond=0)))


def picked_in_calendar(date_from, date_to):
    """True when either picker date was chosen by hand.

    The page sets the picker to full timestamps ("2026-09-10 00:00:00"); a day
    chosen in the calendar comes back bare ("2026-09-10"). That is what tells
    Refresh "show me these dates" apart from "show me the latest" when the
    picker still holds a week set days ago.
    """
    return any(value and ':' not in str(value) for value in (date_from, date_to))


def rows_signature(rows):
    """Enough for the browser to tell that the grid now holds these rows, not the ones before."""
    rows = rows or []
    return {"count": len(rows),
            "first": rows[0].get('ID') if rows else None,
            "last": rows[-1].get('ID') if rows else None}


def jump_request(moment=None, view=None, rows=None, first_page=False):
    """What assets/grid_time_jump.js acts on.

    `rows` is given only when the same response reloads the grid, so the
    browser waits for them before landing. The token makes a second jump to the
    same moment run again.
    """
    return {"at": date_to_str(moment) if moment else None, "view": view,
            "first_page": first_page,
            "rows": rows_signature(rows) if rows is not None else None,
            "token": uuid.uuid4().hex}


def plan_jump(value, parked, columns, view, rooms, query_text, threshold, now=None):
    """What Go to time changes, as a dict keyed by what it sets.

    The rows are reloaded only when the moment lies outside what is loaded --
    the parked range, or else the live week -- and then as the week around it.
    A grid showing CLAP results is reloaded as CLAP results.
    """
    moment = parse_jump_time(value)
    if moment is None:
        return {"note": "Pick a date and a time first."}
    now = now or datetime.now()
    loaded = tuple((parked or {}).get('range') or live_range(now))
    if range_holds(loaded, min(moment, now)):
        return {"parked": {"range": list(loaded), "at": date_to_str(moment)},
                "jump": jump_request(moment, view), "note": "Finding…"}

    rng = range_around(moment, now)
    searching = any((c or {}).get('field') == 'similarity' for c in columns or [])
    data, new_columns, _from, _to = get_db_rec(rng[0], rng[1], query_text if searching else None, threshold)
    data, caption = apply_event_view(data, view, rooms)
    return {"rows": data, "columns": new_columns, "dates": rng, "caption": caption,
            "parked": {"range": list(rng), "at": date_to_str(moment)},
            "jump": jump_request(moment, view, data), "note": "Finding…"}


def plan_back_to_latest(view, rooms, threshold, now=None):
    """Back to latest: the live week, page one, the CLAP search cleared, reloading again."""
    rng = live_range(now)
    data, columns, _from, _to = get_db_rec(rng[0], rng[1], None, threshold)
    data, caption = apply_event_view(data, view, rooms)
    return {"rows": data, "columns": columns, "dates": rng, "caption": caption,
            "parked": None, "jump": jump_request(view=view, rows=data, first_page=True),
            "note": "", "query": ""}


def get_db_rec(str_date_from=None, str_date_to=None, query_text=None, threshold=0.25):
    try:
        sql_to_pandas = SQL_to_Pandas()
        str_date_from, str_date_to = query_range(str_date_from, str_date_to)

        if query_text and len(query_text.strip()) > 0:
            df = sql_to_pandas.get_clap_search_results(query_text, threshold, str_date_from, str_date_to)
        else:
            df = sql_to_pandas.get_date_from_to(str_date_from, str_date_to)

        df = sql_to_pandas.rename_columns(df)
        
        # Drop non-serializable columns before sending to Dash frontend
        if 'clap_embedding' in df.columns:
            df = df.drop(columns=['clap_embedding'])

        # A NULL snr comes back from pandas as NaN, which is not None and is
        # not valid JSON -- it reached the cell renderer and drew "SNR nan dB".
        # Events predating the column, and those whose audio has already been
        # purged, legitimately have no value; they must render as nothing.
        if 'snr' in df.columns:
            df['snr'] = df['snr'].astype(object).where(df['snr'].notna(), None)
            
        data = df.to_dict('records')
        columns = build_grid_columns(df)
        del sql_to_pandas
        return data, columns, str_date_from, str_date_to
    except Exception as e:
        print(f"ERROR in get_db_rec: {e}")
        traceback.print_exc()
        return [], [], str_date_from, str_date_to

def get_active_group_name():
    """How many audio classes the sentinels are currently allowed to report.

    This used to name the active group preset. Presets were removed from the
    Audio Classes tab -- a home has one permissible set, not six -- so the
    label now reports that set directly from ontology.used, which is what
    ast_src/main.py has always actually filtered on. The group tables still
    exist but nothing maintains them, so reading them here would have shown
    a preset name that no longer described anything.
    """
    try:
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', parameters.get_parameters()['sqlite']['db_name']))
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT SUM(used), COUNT(*) FROM ontology").fetchone()
        conn.close()
        enabled, total = int(row[0] or 0), int(row[1] or 0)
        return f"Audio classes enabled: {enabled} of {total}"
    except Exception as e:
        print(f"Error counting enabled audio classes: {e}")
        return "Audio classes enabled: unknown"

audio_component=html.Div(
    [
        dcc.Graph(
            id='spectrogram-graph',
            config={'displayModeBar': False, 'staticPlot': False},
            # 780px to match build_spectrogram_plotly_fig's own layout.height:
            # three stacked panels need more room than the old single PNG
            # view did. Both numbers must move together.
            style={'height': '780px', 'width': '100%'}
        ),
        html.Div(
            id='audio-player-container',
            children=html.Audio(
                id='audio-player-details',
                controls=True,
                style={'width': '100%', 'marginTop': '10px'}
            )
        ),
        html.Div(id='speech-analysis-output', className="mt-3 p-3 border rounded bg-white shadow-sm", style={'display': 'none'}),
        html.Div(id='dummy-sync-output', style={'display': 'none'}),
        html.Div(id='dummy-play-output', style={'display': 'none'}),
        html.Div(id='dummy-context-output', style={'display': 'none'}),
        html.Div(id='dummy-autosize-output', style={'display': 'none'})
    ],
    style={'border': '1px solid #ccc', 'padding': '10px', 'borderRadius': '5px'}
)

date_picker=html.Div([
    dcc.DatePickerRange(
        id='date_picker_details',
        month_format='M-D-Y',
        display_format='YYYY/MM/DD',
    )
])

auto_refresh = dcc.Interval(
    id='interval-component',
    interval=10*1000, 
    n_intervals=0
)

legend = html.Div([
    html.Div([
        html.Span(className='sx-legend-dot sx-band-high'),
        html.Span('High confidence (> 0.70)'),
    ], className='sx-legend-item'),

    html.Div([
        html.Span(className='sx-legend-dot sx-band-med'),
        html.Span('Medium (0.30 - 0.70)'),
    ], className='sx-legend-item'),

    html.Div([
        html.Span(className='sx-legend-dot sx-band-low'),
        html.Span('Low (< 0.30)'),
    ], className='sx-legend-item'),
], className='sx-legend', style={'marginLeft': 'auto'})

modal=dbc.Modal([
    dbc.ModalHeader(dbc.ModalTitle(id='modal_title_id')),
    dbc.ModalBody(id='modal_body'),
    dbc.ModalFooter(),
], id="modal_image", is_open=False, size='xl', centered=True)

SENTINEL_STALE_AFTER_MIN = 5


def _is_last_seen_stale(last_seen, threshold_min=SENTINEL_STALE_AFTER_MIN):
    """True when last_seen is older than the threshold.

    Unparseable or missing values are not treated as stale - the entry simply
    predates last_seen tracking, and flagging it would be a guess.
    """
    if not last_seen or last_seen in ("Never", "Unknown"):
        return False
    try:
        seen = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    return (datetime.now() - seen) > timedelta(minutes=threshold_min)


def get_sentinel_status_elements():
    import os
    import json
    import sqlite3
    import re
    
    proj_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    status_file = os.path.join(proj_root, 'sentinel_status.json')
    db_name = parameters.get_parameters()['sqlite']['db_name']
    db_path = os.path.join(proj_root, db_name)
    
    def is_valid_device_name(name):
        if not name:
            return False
        name_lower = name.lower()
        if name_lower in ['manual_upload', 'unknown', 'test', 'ex', 'all', 'none', 'user', 'users', 'group', 'device_name']:
            return False
        # Sentinels that no longer exist. They stay out of the list without
        # being deleted anywhere: each still owns rows in ast_table, and
        # removing the device would orphan its recordings and the reports
        # built from them. Matched whole, so "box3" hides only the malformed
        # name and leaves box3-1 and box3-2 alone -- likewise "esp32s3-",
        # which is a truncated registration, not the three real units.
        if name_lower in ['box3', 'esp32s3-', 'p4-intercom-01',
                          'p4-sentinel-1', 'user2']:
            return False
        if re.match(r'^\d+$', name_lower):
            return False
        return True

    # 1. Fetch registered devices from remote_devices
    registered_devices = []
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT device_name, description FROM remote_devices")
        registered_devices = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"Error fetching registered devices: {e}")
        
    # 2. Fetch distinct device names from historical audio events
    historical_devices = []
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT device_name FROM ast_table")
        historical_devices = [row[0] for row in cursor.fetchall() if row[0] and is_valid_device_name(row[0])]
        conn.close()
    except Exception as e:
        print(f"Error fetching historical devices: {e}")
        
    status_data = {}
    if os.path.exists(status_file):
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                status_data = json.load(f)
        except Exception:
            pass
            
    # Combine registered, historical, and live statuses
    devices_status = {}
    
    # Add registered remote devices
    for name, desc in registered_devices:
        if is_valid_device_name(name):
            devices_status[name] = {
                "status": "offline",
                "last_seen": "Never",
                "ip_address": "N/A",
                "description": desc or "Registered Sentinel"
            }
        
    # Add historical devices
    for name in historical_devices:
        if name not in devices_status:
            devices_status[name] = {
                "status": "offline",
                "last_seen": "Never",
                "ip_address": "N/A",
                # No label. "Historical Sentinel" said nothing the red dot and
                # "Never" had not already said, on every row that had no other
                # description to show.
                "description": ""
            }
        
    # Overlay live status info
    for name, info in status_data.items():
        if is_valid_device_name(name):
            if name not in devices_status:
                devices_status[name] = {
                    "status": "offline",
                    "last_seen": "Never",
                    "ip_address": "N/A",
                    "description": "Auto-registered sentinel"
                }
            devices_status[name]["status"] = info.get("status", "offline")
            devices_status[name]["last_seen"] = info.get("last_seen", "Unknown")
            devices_status[name]["ip_address"] = info.get("ip_address", "N/A")

    if not devices_status:
        return [html.Div("No devices registered or detected.", className="text-muted small text-center py-3")]

    elements = []
    # Sort devices by status (online first) and then by name
    sorted_devices = sorted(devices_status.items(), key=lambda x: (0 if x[1]["status"] == "online" else 1, x[0]))
    
    for dev_name, info in sorted_devices:
        is_online = info["status"] == "online"
        # A device claiming to be online that the broker has not heard from in
        # a long time is reported amber rather than green: its Last Will may
        # have been missed while the broker or the pipeline was down.
        is_stale = is_online and _is_last_seen_stale(info.get("last_seen"))
        if is_online and not is_stale:
            color = "#2ecc71"   # green - confirmed by the broker
        elif is_stale:
            color = "#f39c12"   # amber - claims online, but silent
        else:
            color = "#e74c3c"   # red - offline
        
        # IP address badge/tag
        ip_info = f"IP: {info['ip_address']}" if info['ip_address'] != "N/A" else ""
        last_seen_info = f"Last Seen: {info['last_seen']}"
        if is_stale:
            last_seen_info += f"\nNo traffic for over {SENTINEL_STALE_AFTER_MIN} min - status unconfirmed"
        
        tooltip_text = f"{info['description']}\n{ip_info}\n{last_seen_info}".strip()
        
        item_id = f"sentinel-{dev_name}"
        
        elements.append(
            html.Div([
                # Bullet indicator
                html.Span(className="sx-sentinel-dot", style={
                    "backgroundColor": color,
                    "boxShadow": f"0 0 0 3px {color}33",
                }),
                html.Div([
                    # Name
                    html.Span(dev_name, className="sx-sentinel-name"),
                    # Description or IP. Joined from whichever parts actually
                    # say something, so a sentinel with no description does not
                    # render a dangling separator -- and "None", which arrives
                    # as a string from the live status file, is treated as the
                    # absence it means rather than printed.
                    html.Div(
                        " | ".join(
                            part for part in (info['ip_address'], info['description'])
                            if part and part not in ('N/A', 'None')),
                        className="sx-sentinel-meta"
                    )
                ], style={"flex": "1", "minWidth": "0"}),
                
                # Tooltip for diagnostics
                dbc.Tooltip(
                    tooltip_text,
                    target=item_id,
                    placement="left",
                ) if tooltip_text else None
            ], 
            id=item_id,
            className="d-flex align-items-center p-2 mb-2 rounded border-start border-3 sx-sentinel",
            style={"borderStartColor": color})
        )
        
    return elements

def layout(**kwargs):
    return html.Div([
        dcc.Location(id='audio_details_url', refresh=False),
        dcc.Store(id='context-menu-action', data=None),
        
        dcc.Store(id='ptt-active-store', data=False),
        dcc.Store(id='ptt-channel-store', data=False),
        # None while live; {"range": [from, to], "at": moment or None} while
        # parked. See "Go to time" above get_db_rec.
        dcc.Store(id='grid-parked', data=None),
        dcc.Store(id='grid-jump', data=None),
        dcc.Interval(id='ptt-ui-interval', interval=500, disabled=True),
        # Two hidden buttons — one for press, one for release.
        # JavaScript clicks these directly; Dash callbacks react to n_clicks.
        html.Button(id="ptt-press",   style={"display": "none"}),
        html.Button(id="ptt-release", style={"display": "none"}),
        html.Div(id='dummy-ptt-setup'),
        dbc.Container([
            dbc.Row([
                dbc.Col(html.H2("Audio Dashboard", className="text-primary"), width="auto"),
                dbc.Col(html.Div([
                    dbc.Badge([html.I(className="fas fa-microchip me-2"), f"AI ENGINE: {raw_status}"], 
                              color="success" if "GPU" in raw_status else "warning", 
                              className="p-2 fs-6 shadow-sm")
                ], className="d-flex align-items-center h-100 ms-3"), width="auto"),
            ], align="center", className="mb-4"),
            
            dbc.Row([
                dbc.Col([
                    dbc.Row([
                        dbc.Col([
                            dbc.Card([
                                # The date range belongs with the panel's title
                                # rather than in the row of actions: it scopes
                                # everything below it, and sitting in the header
                                # says so without a label.
                                dbc.CardHeader([
                                    html.Span([
                                        html.I(className="fas fa-filter me-2"),
                                        "Filter & Search",
                                    ], className="font-weight-bold"),
                                    html.Div(date_picker, className="ms-auto"),
                                ], className="bg-light font-weight-bold d-flex align-items-center"),
                                dbc.CardBody([
                                    # Actions in the order they are reached for:
                                    # talk to a room, refresh what is shown, then
                                    # narrow what is shown.
                                    dbc.Row([
                                        dbc.Col(dbc.Button([html.I(className="fas fa-microphone me-2"), "Push to Talk"], id='btn-ptt', color="danger"), width="auto"),
                                        dbc.Col(dbc.Button([html.I(className="fas fa-sync-alt me-2"), "Refresh"], id='rec_database_refresh_button', color="primary"), width="auto"),

                                        # View filter. Segmented buttons rather than a
                                        # dropdown: three mutually exclusive views, and
                                        # which one is active should be readable at a
                                        # glance from across the room. The caption below
                                        # states what is hidden, so a filtered grid is
                                        # never mistaken for missing data.
                                        dbc.Col(
                                            dbc.RadioItems(
                                                id='event-view-filter',
                                                className="btn-group",
                                                inputClassName="btn-check",
                                                labelClassName="btn btn-outline-primary",
                                                labelCheckedClassName="active",
                                                options=[
                                                    {"label": "Speech", "value": VIEW_SPEECH},
                                                    {"label": "Non-speech", "value": VIEW_OTHER},
                                                    {"label": "All", "value": VIEW_ALL},
                                                ],
                                                value=DEFAULT_EVENT_VIEW,
                                            ), width="auto"),

                                        dbc.Col(html.Div(id='ptt-status', className="small text-muted"), width="auto"),
                                    ], align="center", className="g-3 sx-filter-group mb-3"),

                                    # Rooms are multi-select and combine with the
                                    # view above using AND: Kitchen + Non-speech
                                    # means the non-speech events in the Kitchen.
                                    # Selecting none means every room, so the
                                    # unfiltered state needs no special chip.
                                    dbc.Row([
                                        dbc.Col(html.Span([
                                                    html.I(className="fas fa-house me-2 sx-count-icon"),
                                                    "Rooms",
                                                ], className="sx-group-label d-inline-flex align-items-center"),
                                                width="auto"),
                                        dbc.Col(
                                            dbc.Checklist(
                                                id='room-filter',
                                                className="btn-group flex-wrap",
                                                inputClassName="btn-check",
                                                labelClassName="btn btn-outline-secondary btn-sm",
                                                labelCheckedClassName="active",
                                                options=[],
                                                value=[],
                                            ), width="auto"),
                                        dbc.Col(
                                            dbc.Button("Clear", id='room-filter-clear',
                                                       color="link", size="sm",
                                                       className="text-muted px-2"),
                                            width="auto"),
                                    ], align="center", className="g-2 sx-filter-group mb-3"),

                                    # The count is the panel's answer to "what am
                                    # I looking at", so it reads as a statement
                                    # rather than a footnote. The text itself is
                                    # still written by the callback; only the
                                    # surround and the glyph are added here.
                                    html.Div([
                                        html.I(className="fas fa-users me-3 sx-count-icon"),
                                        html.Div(id='event-view-count'),
                                    ], className="sx-filter-group sx-count-banner mb-3"),

                                    # CLAP side by side: the dial on the left, the
                                    # action and its query on the right. The two
                                    # are set once and used together, so putting
                                    # them level keeps both in view while tuning.
                                    dbc.Row([
                                        dbc.Col([
                                            html.Label("CLAP Similarity Threshold",
                                                       className="small text-muted mb-0"),
                                            dcc.Slider(
                                                id='clap-threshold-slider',
                                                min=0.1, max=0.6, step=0.05, value=0.25,
                                                marks={0.1: '0.1', 0.25: '0.25', 0.4: '0.4', 0.6: '0.6'},
                                                tooltip={"placement": "bottom", "always_visible": False},
                                                className="mt-1"
                                            ),
                                        ], width=12, lg=6, className="sx-clap-stack"),
                                        dbc.Col([
                                            dbc.Button([html.I(className="fas fa-search me-2"), "CLAP Search"],
                                                       id='clap-search-button', color="success"),
                                            dbc.Input(
                                                id='clap-search-input',
                                                placeholder='Describe what you are looking for...',
                                                type='text',
                                                className="mt-2"
                                            ),
                                        ], width=12, lg=6, className="sx-clap-stack"),
                                    ], className="g-3 sx-filter-group sx-clap-row mb-3"),

                                    dbc.Row([
                                        dbc.Col([
                                            html.Label("Latest Hazard Assessment", className="fw-bold mb-1 small text-muted"),
                                            dbc.Textarea(
                                                id='llm-verdict-output',
                                                readOnly=True,
                                                rows=2,
                                                style={'resize': 'none', 'backgroundColor': '#fff0f0', 'fontSize': '14px'},
                                                placeholder="Waiting for analysis..."
                                            )
                                        ], width=12)
                                    ], className="mt-3")
                                ])
                            ], className="shadow-sm"),
                        ], width=9),

                        # The Sentinels card is sized by the Filter & Search card
                        # beside it, and scrolls inside that height rather than
                        # growing past it. Three things are needed for that, and
                        # leaving any one out breaks it:
                        #
                        #   d-flex flex-column on the column, so the card is a flex
                        #     item of a box the row has already stretched
                        #   flex-grow-1 + minHeight 0 + overflow hidden on the card,
                        #     so it fills that box and is not pushed taller by its
                        #     own contents
                        #   flex 1 1 0 on the list, so its basis is zero: with
                        #     "auto" the list's content height becomes the card's
                        #     natural height and the whole panel overshoots, which
                        #     is exactly what happened with a long device list
                        dbc.Col([
                            dbc.Card([
                                dbc.CardHeader([
                                    html.I(className="fas fa-microchip me-2 text-primary"),
                                    "Sentinels"
                                ], className="bg-light font-weight-bold d-flex align-items-center"),
                                dbc.CardBody([
                                    html.Div(
                                        id="sentinel-status-container",
                                        style={
                                            "flex": "1 1 0",
                                            "minHeight": 0,
                                            "overflowY": "auto",
                                            "paddingRight": "5px"
                                        },
                                        children=get_sentinel_status_elements()
                                    )
                                ], style={"padding": "10px", "display": "flex",
                                          "flexDirection": "column", "minHeight": 0}),
                            ], className="shadow-sm flex-grow-1",
                               style={"minHeight": 0, "overflow": "hidden"})
                        ], width=3, className="d-flex flex-column")
                    ], className="g-3 mb-4"),
                    
                    dbc.Card([
                        dbc.CardHeader([
                            html.Div([
                                html.Span("Audio Events", className="font-weight-bold"),
                                html.Small(" (Click any cell to play | Right-click for options)", className="text-muted ms-2"),
                            ], className="d-flex align-items-center"),
                            html.Span(id='active-group-display', className="mx-auto fw-bold text-primary"),
                            legend
                        ], className="bg-light d-flex align-items-center justify-content-between"),
                        dbc.CardBody([
                            # Go to time lands on the page holding a moment
                            # rather than filtering to it, so what came just
                            # before and after is still a page away. The span
                            # at the right says what time the page on screen
                            # covers, so plain paging tells you too.
                            html.Div([
                                html.I(className="fas fa-clock sx-count-icon"),
                                dbc.Input(id='grid-jump-time', type='datetime-local',
                                          className="sx-jump-input"),
                                dbc.Button([html.I(className="fas fa-location-arrow me-2"), "Go"],
                                           id='grid-jump-go', color="primary", size="sm"),
                                dbc.Button([html.I(className="fas fa-forward me-2"), "Back to latest"],
                                           id='grid-back-live', color="success", outline=True,
                                           size="sm", style={'display': 'none'}),
                                html.Span(id='grid-jump-note', className="sx-jump-note"),
                                html.Span(id='grid-page-span', className="sx-page-span ms-auto"),
                            ], className="sx-jump-bar d-flex align-items-center mb-2"),
                            dag.AgGrid(
                                id="audio_grid_table",
                                persistence=False, # Temporarily disabled to fix callback errors
                                # The row Go to time landed on stays marked
                                # until the next jump or Back to latest.
                                rowClassRules={"sx-row-jumped": {"function": "sxIsJumpedRow(params)"}},
                                defaultColDef={
                                    "filter": True,
                                    "sortable": True,
                                    "resizable": True,
                                    "suppressMovable": True,
                                    "headerClass": "ag-left-aligned-header"
                                },
                                dashGridOptions={
                                    "rowSelection": "multiple",
                                    "suppressRowClickSelection": False,
                                    "animateRows": True,
                                    "rowHeight": 84,
                                    "headerHeight": 40,
                                    "pagination": True,
                                    "paginationAutoPageSize": True,
                                    "suppressColumnVirtualisation": True,
                                    "domLayout": "normal",
                                    "getRowId": {"function": "params.data.ID"}
                                },
                                style={"height": 720, "width": "100%"},
                                className="ag-theme-alpine-dark",
                             ),
                            html.Div(id='grid-jump-sink', style={'display': 'none'}),
                            html.Div(id='grid-page-sink', style={'display': 'none'}),
                        ])
                    ], className="shadow-sm"),
                    
                    dbc.Row([
                        dbc.Col([
                            dbc.ButtonGroup([
                                dbc.Button([html.I(className="fas fa-download me-2"), "Download Selected"], id='btn_download_selected', color="success"),
                                dbc.Button([html.I(className="fas fa-file-code me-2"), "Download JSON"], id='btn_download_json', color="info"),
                                dbc.Button([html.I(className="fas fa-file-csv me-2"), "Download CSV"], id='btn_download_csv', color="primary"),
                                dbc.Button([html.I(className="fas fa-trash-alt me-2"), "Delete Selected"], id='btn_delete_selected', color="danger"),
                            ], className="mt-3")
                        ], width="auto")
                    ])
                ], width=8),
                
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(id='spectrogram-header', children="Spectrogram Analysis", className="bg-dark text-white font-weight-bold"),
                        dbc.CardBody([
                            audio_component
                        ])
                    # Deliberately not sticky. With `sticky-top` this panel
                    # detached at 120px and rode the viewport down while every
                    # other card scrolled with the page, so it was the one
                    # thing on the dashboard that moved when nothing else did.
                    # It now sits at the top of its column, level with the
                    # events grid beside it.
                    ], className="shadow-sm")
                ], width=4),
            ], className="g-4")
        ], fluid=True, className="p-4"),
        
        dcc.Download(id="download_json"),
        dcc.Download(id="download_csv"),
        dcc.Download(id="download_export"),
        dcc.Download(id="download_selected"),
        auto_refresh,
        html.Div([
            dbc.ListGroup([
                dbc.ListGroupItem([html.I(className="fas fa-download me-2"),
                                   "Download selected audio"],
                                  id="ctx-download", action=True,
                                  color="success", n_clicks=0),
                dbc.ListGroupItem([html.I(className="fas fa-trash-alt me-2"),
                                   "Delete selected rows"],
                                  id="ctx-delete", action=True,
                                  color="danger", n_clicks=0),
            ], id="context-menu", style={
                "display": "none", "position": "fixed", "zIndex": "1000",
                "width": "230px", "boxShadow": "0 4px 8px rgba(0,0,0,0.2)", "cursor": "pointer"
            })
        ]),

        # Deleting removes the database rows AND the audio and spectrogram
        # files from disk, with nothing to undo it. That was reachable only
        # from a labelled button before; now a stray right-click can land on
        # it, so both routes ask first.
        dcc.ConfirmDialog(id="confirm-delete"),
        modal,
    ])

def build_speech_ui(transcription, emotion):
    if not transcription and not emotion:
        return None, {'display': 'none'}
    
    # Mapping for raw codes if they appear
    emotion_map = {
        'neu': 'NEUTRAL',
        'hap': 'HAPPY',
        'ang': 'ANGRY',
        'sad': 'SAD'
    }
    
    display_emotion = emotion
    if emotion:
        # Check if it's a raw code (like 'sad') or a formatted string (like 'SAD (0.85)')
        raw_part = emotion.split('(')[0].strip().lower()
        if raw_part in emotion_map:
            mapped_label = emotion_map[raw_part]
            if '(' in emotion:
                # Reconstruct with the confidence part if it exists
                conf_part = emotion.split('(')[1]
                display_emotion = f"{mapped_label} ({conf_part}"
            else:
                display_emotion = mapped_label
        else:
            display_emotion = emotion.upper()

    content = []
    if emotion:
        content.append(html.Div([
            html.Span("Emotion: ", className="fw-bold me-2"),
            dbc.Badge(display_emotion, color="info", className="p-2")
        ], className="mb-3"))
    if transcription:
        content.append(html.Div([
            html.Label("Recognized Speech:", className="fw-bold mb-1 small text-muted"),
            dbc.Textarea(
                value=transcription, readOnly=True, rows=4,
                style={'resize': 'none', 'backgroundColor': '#fcfcfc', 'fontSize': '14px'}
            )
        ]))
    return content, {'display': 'block'}

# --- PTT channel toggle ---------------------------------------------
# Red  = closed. Click to open: the channel stays up and you hear the room.
# Green= open.   Hold SPACE to talk (inbound is muted while you do).
# Click again to close, which also clears the device tick.
@callback(
    Output('btn-ptt', 'children'),
    Output('btn-ptt', 'color'),
    Output('ptt-status', 'children', allow_duplicate=True),
    Output('ptt-channel-store', 'data'),
    Output('audio_grid_table', 'selectedRows', allow_duplicate=True),
    Input('btn-ptt', 'n_clicks'),
    State('ptt-channel-store', 'data'),
    State('audio_grid_table', 'selectedRows'),
    prevent_initial_call=True,
)
def toggle_ptt_channel(n_clicks, channel_open, selected_rows):
    closed = ([html.I(className="fas fa-microphone me-2"), "Push to Talk"], "danger")
    listening = ([html.I(className="fas fa-headphones me-2"), "Listening"], "success")

    if channel_open:
        try:
            get_ptt_controller().close_channel()
        except Exception as e:
            traceback.print_exc()
            return (*closed, f"Error closing: {e}", False, no_update)
        # Untick the device so the next call starts from a clean selection.
        return (*closed, "Channel closed", False, [])

    device_ids = []
    for row in (selected_rows or []):
        dev = row.get('Device', row.get('device_name'))
        if dev and dev not in device_ids:
            device_ids.append(dev)
    if not device_ids:
        return (*closed, "Tick a device first", False, no_update)

    try:
        ptt = get_ptt_controller(device_ids)
        ptt.open_channel(device_ids)
    except Exception as e:
        traceback.print_exc()
        return (*closed, f"Could not open channel: {e}", False, no_update)

    return (*listening, f"Listening to {', '.join(device_ids)} — hold SPACE to talk",
            True, no_update)


# --- PTT Callbacks ---
clientside_callback(
    """
    function(id) {
        if (window._pttInit) return window.dash_clientside.no_update;
        let held = false;
        
        const handleKeyDown = function(e) {
            if (e.code === 'Space' && e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
                e.preventDefault();
                e.stopImmediatePropagation(); // Stop Ag-Grid from seeing the Space key
                if (!held) {
                    held = true;
                    document.getElementById('ptt-press').click();
                }
            }
        };
        
        const handleKeyUp = function(e) {
            if (e.code === 'Space') {
                e.preventDefault();
                e.stopImmediatePropagation();
                held = false;
                document.getElementById('ptt-release').click();
            }
        };

        // Use capturing phase (true) to intercept the event before Ag-Grid
        window.addEventListener('keydown', handleKeyDown, true);
        window.addEventListener('keyup', handleKeyUp, true);
        
        window._pttInit = true;
        return window.dash_clientside.no_update;
    }
    """,
    Output("ptt-press", "title"),
    Input("ptt-press", "id"),
)

@callback(
    Output('btn-ptt', 'children', allow_duplicate=True),
    Output('btn-ptt', 'color', allow_duplicate=True),
    Output('ptt-status', 'children'),
    Output('ptt-active-store', 'data', allow_duplicate=True),
    Input('ptt-press', 'n_clicks'),
    Input('ptt-release', 'n_clicks'),
    State('audio_grid_table', 'selectedRows'),
    State('ptt-channel-store', 'data'),
    prevent_initial_call=True
)
def handle_ptt_logic(press, release, selected_rows, channel_open):
    try:
        triggered = ctx.triggered_id

        # With the channel already open, SPACE only gates the microphone:
        # the session, and therefore listening, persists across presses.
        if channel_open:
            ptt = get_ptt_controller()
            targets = ", ".join(ptt.device_ids) or "device"
            if triggered == "ptt-press":
                ptt.press()
                return ([html.I(className="fas fa-broadcast-tower me-2"), "Talking"],
                        "warning", f"Talking to {targets}", True)
            ptt.release()
            return ([html.I(className="fas fa-headphones me-2"), "Listening"],
                    "success", f"Listening to {targets} — hold SPACE to talk", False)
        
        # Extract unique device_ids from selected rows
        device_ids = []
        if selected_rows:
            for row in selected_rows:
                dev = row.get('Device', row.get('device_name'))
                if dev and dev not in device_ids:
                    device_ids.append(dev)
        
        if not device_ids:
            # If no row is selected, warn the user and do not activate streaming
            if triggered == "ptt-press":
                return [html.I(className="fas fa-exclamation-triangle me-2"), "Select Device!"], "warning", "Please select device(s) first", False
            return [html.I(className="fas fa-microphone me-2"), "Push to Talk"], "danger", "Please select device(s) first", False

        # Hold-to-talk via transport abstraction (UI never touches MQTT)
        ptt = get_ptt_controller(device_ids)
        target_desc = ", ".join(device_ids)

        if triggered == "ptt-press":
            mode = ptt.press(device_ids)
            if mode == MODE_STREAMING:
                return (
                    [html.I(className="fas fa-broadcast-tower me-2"), "Live stream.."],
                    "success",
                    f"start_live_audio → {target_desc}",
                    True,
                )
            return (
                [html.I(className="fas fa-volume-up me-2"), "Recording.."],
                "warning",
                f"MQTT play fallback → {target_desc}",
                True,
            )

        # triggered == "ptt-release"
        mode = ptt.active_mode  # capture before release() clears it
        ptt.release()
        if mode == MODE_MQTT_PLAY:
            return (
                [html.I(className="fas fa-microphone me-2"), "Push to Talk"],
                "danger",
                f"Sent (MQTT play) to: {target_desc}",
                False,
            )
        if mode == MODE_STREAMING:
            return (
                [html.I(className="fas fa-microphone me-2"), "Push to Talk"],
                "danger",
                f"stop_live_audio → {target_desc}",
                False,
            )
        return (
            [html.I(className="fas fa-microphone me-2"), "Push to Talk"],
            "danger",
            f"Released → {target_desc}",
            False,
        )
        
    except Exception as e:
        print(f"PTT CALLBACK ERROR: {e}")
        return [html.I(className="fas fa-exclamation-triangle me-2"), "Mic Error"], "warning", f"Error: {str(e)}", False

# Existing callbacks...
clientside_callback(
    "dash_clientside.clientside.force_play",
    Output('dummy-play-output', 'children'),
    Input('audio-player-details', 'src'),
    State('audio-player-details', 'id'),
)

clientside_callback(
    "dash_clientside.clientside.sync_audio_spectrogram",
    Output('dummy-sync-output', 'children'),
    Input('audio-player-details', 'src'),
    State('spectrogram-graph', 'id'),
    State('audio-player-details', 'id'),
)

clientside_callback(
    """
    function(n_clicks) {
        const menu = document.getElementById('context-menu');
        const grid = document.getElementById('audio_grid_table');
        if (grid) {
            if (!grid._ctxListenerAttached) {
                grid.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    menu.style.display = 'block';
                    menu.style.left = e.clientX + 'px';
                    menu.style.top = e.clientY + 'px';
                });
                document.addEventListener('click', () => { if(menu) menu.style.display = 'none'; });
                grid._ctxListenerAttached = true;
            }
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output('dummy-context-output', 'children'),
    Input('audio_grid_table', 'id'),
)

clientside_callback(
    """
    function(rowData) {
        if (rowData) {
            const currentCount = rowData.length;
            if (currentCount !== window._lastRowCount) {
                setTimeout(() => {
                    const grid = dash_ag_grid.getApi("audio_grid_table");
                    if (grid) { grid.autoSizeAllColumns(); }
                }, 100);
                window._lastRowCount = currentCount;
            }
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output('dummy-autosize-output', 'children'),
    Input('audio_grid_table', 'rowData'),
)

@callback( 
    Output('audio_grid_table','rowData'),
    Output('audio_grid_table','columnDefs'),
    Output('date_picker_details','start_date'), 
    Output('date_picker_details','end_date'),
    Output('audio-player-container', 'children'),
    Output('spectrogram-graph', 'figure'),
    Output('speech-analysis-output', 'children'),
    Output('speech-analysis-output', 'style'),
    Output('active-group-display', 'children'),
    Output('llm-verdict-output', 'value'),
    Output('sentinel-status-container', 'children'),
    Output('event-view-count', 'children'),
    Output('room-filter', 'options'),
    Output('grid-jump', 'data'),
    Input('audio_details_url', 'pathname'),
    Input('interval-component', 'n_intervals'),
    Input('event-view-filter', 'value'),
    Input('room-filter', 'value'),
    State('clap-search-input', 'value'),
    State('clap-threshold-slider', 'value'),
    State('ptt-active-store', 'data'),
    State('grid-parked', 'data'),
)
def refresh_page(p, n, event_view, rooms, query_text, threshold, ptt_active, parked):
    if p=='/' or p=="" or p is None:
        try:
            trigger = ctx.triggered_id

            # If auto-refreshing via interval AND we have a search query OR PTT is active, DO NOT update.
            # This prevents the grid from 'blinking' or resetting while the user is searching or speaking.
            # Nor while parked: the rows would move under the page you went to.
            if trigger == 'interval-component' and (ptt_active or parked or (query_text and len(query_text.strip()) > 0)):
                return no_update, no_update, no_update, no_update, no_update, no_update, no_update, no_update, no_update, no_update, get_sentinel_status_elements(), no_update, no_update, no_update

            # Parked, a change of view or rooms reloads the parked range, not
            # the live week, and lands again on the moment you went to.
            rng = (parked or {}).get('range') or (None, None)
            data, columns, str_date_from, str_date_to = get_db_rec(rng[0], rng[1], query_text=None, threshold=threshold)
            # Options come from the unfiltered rows, so selecting a room never
            # removes the chips for the rooms you did not pick.
            room_options = room_filter_options(data)
            data, view_caption = apply_event_view(data, event_view, rooms)
            active_group = get_active_group_name()
            
            verdict_text = ""
            if data and len(data) > 0:
                row = data[0]
                raw_verdict = row.get('llm_verdict', "")
                if raw_verdict is None: raw_verdict = ""
                
                # Check if verdict already has the [ID: ...] prefix
                if raw_verdict.strip().startswith("[ID:"):
                    verdict_text = raw_verdict
                else:
                    # Prepend ID and Timestamp if missing
                    evt_id = row.get('ID', '?')
                    evt_time = row.get('Timestamp', '?')
                    verdict_text = f"[ID: {evt_id} | {evt_time}] {raw_verdict}"

            # Changing the view must not disturb what is loaded in the player
            # or the spectrogram -- it re-filters the grid and nothing else.
            if trigger in ('interval-component', 'event-view-filter', 'room-filter'):
                 moment = parse_jump_time((parked or {}).get('at'))
                 jump = jump_request(moment, event_view, data) if moment else no_update
                 return data, columns, no_update, no_update, no_update, no_update, no_update, no_update, active_group, verdict_text, get_sentinel_status_elements(), view_caption, room_options, jump

            audio_component_ui = no_update
            fig = no_update
            speech_ui = None
            speech_style = {'display': 'none'}
            verdict_text = ""

            if data:
                # Find the first entry that actually has a valid audio file
                selected_row = data[0]
                audio_src = None
                for row in data:
                    src, tmp_fig = prepare_spectrogram_fig(row.get('file_name'), row.get('spectrogram_image'))
                    if src:
                        audio_src = src
                        fig = tmp_fig
                        selected_row = row
                        break
                
                if not audio_src:
                    # Fallback to first row if none found (will show 'not found' in UI)
                    audio_src, fig = prepare_spectrogram_fig(data[0].get('file_name'), data[0].get('spectrogram_image'))
                    selected_row = data[0]

                speech_ui, speech_style = build_speech_ui(selected_row.get('transcription'), selected_row.get('emotion'))
                
                # Format verdict for the selected row
                raw_verdict = selected_row.get('llm_verdict', "") or ""
                evt_id = selected_row.get('ID', '?')
                evt_time = selected_row.get('Timestamp', '?')
                verdict_text = raw_verdict if raw_verdict.strip().startswith("[ID:") else f"[ID: {evt_id} | {evt_time}] {raw_verdict}"

                # Create a fresh Audio component to ensure reactivity
                audio_component_ui = html.Audio(
                    id='audio-player-details',
                    src=audio_src,
                    controls=True,
                    autoPlay=False,
                    style={'width': '100%', 'marginTop': '10px'}
                )

            return data, columns, str_date_from, str_date_to, audio_component_ui, fig, speech_ui, speech_style, active_group, verdict_text, get_sentinel_status_elements(), view_caption, room_options, no_update
        except Exception as e:
            print(f"ERROR in refresh_page: {e}")
            traceback.print_exc()
            return [], [], no_update, no_update, no_update, no_update, no_update, no_update, no_update, no_update, get_sentinel_status_elements(), no_update, no_update, no_update
    return no_update, no_update, no_update, no_update, no_update, no_update, no_update, no_update, no_update, no_update, get_sentinel_status_elements(), no_update, no_update, no_update

@callback(
    Output('audio_grid_table','rowData', allow_duplicate=True), 
    Output('audio_grid_table','columnDefs', allow_duplicate=True), 
    Output('clap-search-input', 'value'),
    Output('llm-verdict-output', 'value', allow_duplicate=True),
    Output('event-view-count', 'children', allow_duplicate=True),
    Output('date_picker_details', 'start_date', allow_duplicate=True),
    Output('date_picker_details', 'end_date', allow_duplicate=True),
    Output('grid-parked', 'data', allow_duplicate=True),
    Output('grid-jump', 'data', allow_duplicate=True),
    Output('grid-jump-note', 'children', allow_duplicate=True),
    Input('rec_database_refresh_button','n_clicks'),
    Input('clap-search-button', 'n_clicks'),
    State('date_picker_details','start_date'),
    State('date_picker_details','end_date'),
    State('clap-search-input', 'value'),
    State('clap-threshold-slider', 'value'),
    State('event-view-filter', 'value'),
    State('room-filter', 'value'),
    prevent_initial_call=True,
)
def refresh_button_db(n_refresh, n_search, start_date, end_date, query_text, threshold, event_view, rooms):
    try:
        trigger = ctx.triggered_id

        # If manual Refresh button is clicked, clear the search and show latest records
        if trigger == 'rec_database_refresh_button':
            # Dates picked in the calendar are shown and held (parked). Without
            # them Refresh means the latest, and the picker is set to the live
            # week -- it may still hold a week set days ago.
            now = datetime.now()
            live = live_range(now)
            picked = picked_in_calendar(start_date, end_date)
            rng = query_range(start_date, end_date, now) if picked else live
            parked = None if rng == live else {"range": list(rng), "at": None}
            dates = (no_update, no_update) if picked else live
            data, columns, _, _ = get_db_rec(rng[0], rng[1], query_text=None)
            data, view_caption = apply_event_view(data, event_view, rooms)
            verdict_text = ""
            if data and len(data) > 0:
                row = data[0]
                raw_verdict = row.get('llm_verdict', "") or ""
                if raw_verdict.strip().startswith("[ID:"):
                    verdict_text = raw_verdict
                else:
                    verdict_text = f"[ID: {row.get('ID','?')} | {row.get('Timestamp','?')}] {raw_verdict}"
            return (data, columns, "", verdict_text, view_caption, dates[0], dates[1], parked,
                    jump_request(view=event_view, rows=data, first_page=True), "")

        # If CLAP Search button is clicked, perform the search and keep the query text
        data, columns, _, _ = get_db_rec(start_date, end_date, query_text, threshold)
        data, view_caption = apply_event_view(data, event_view, rooms)
        # For search, we might want to show verdict of the top match, or no update?
        # Let's show top match if available
        verdict_text = no_update
        if data and len(data) > 0:
            row = data[0]
            raw_verdict = row.get('llm_verdict', "") or ""
            if raw_verdict.strip().startswith("[ID:"):
                verdict_text = raw_verdict
            else:
                verdict_text = f"[ID: {row.get('ID','?')} | {row.get('Timestamp','?')}] {raw_verdict}"
        return data, columns, no_update, verdict_text, view_caption, no_update, no_update, no_update, no_update, no_update
    except Exception as e:
        print(f"ERROR in refresh_button_db: {e}")
        traceback.print_exc()
        return [], [], no_update, no_update, no_update, no_update, no_update, no_update, no_update, no_update


@callback(
    Output('room-filter', 'value'),
    Input('room-filter-clear', 'n_clicks'),
    prevent_initial_call=True,
)
def clear_room_filter(_n_clicks):
    """Deselect every room. An empty selection *is* the 'all rooms' state, so
    this is the quick way back once several chips are on."""
    return []


@callback(
    Output('audio_grid_table', 'rowData', allow_duplicate=True),
    Output('audio_grid_table', 'columnDefs', allow_duplicate=True),
    Output('date_picker_details', 'start_date', allow_duplicate=True),
    Output('date_picker_details', 'end_date', allow_duplicate=True),
    Output('event-view-count', 'children', allow_duplicate=True),
    Output('grid-parked', 'data', allow_duplicate=True),
    Output('grid-jump', 'data', allow_duplicate=True),
    Output('grid-jump-note', 'children', allow_duplicate=True),
    Input('grid-jump-go', 'n_clicks'),
    Input('grid-jump-time', 'n_submit'),
    State('grid-jump-time', 'value'),
    State('grid-parked', 'data'),
    State('audio_grid_table', 'columnDefs'),
    State('event-view-filter', 'value'),
    State('room-filter', 'value'),
    State('clap-search-input', 'value'),
    State('clap-threshold-slider', 'value'),
    prevent_initial_call=True,
)
def go_to_time(_clicks, _submits, value, parked, columns, event_view, rooms, query_text, threshold):
    """Go (or Enter in the field): load the rows around the moment if needed, then land on it."""
    plan = plan_jump(value, parked, columns, event_view, rooms, query_text, threshold)
    dates = plan.get("dates") or (no_update, no_update)
    return (plan.get("rows", no_update), plan.get("columns", no_update), dates[0], dates[1],
            plan.get("caption", no_update), plan.get("parked", no_update),
            plan.get("jump", no_update), plan["note"])


@callback(
    Output('audio_grid_table', 'rowData', allow_duplicate=True),
    Output('audio_grid_table', 'columnDefs', allow_duplicate=True),
    Output('date_picker_details', 'start_date', allow_duplicate=True),
    Output('date_picker_details', 'end_date', allow_duplicate=True),
    Output('event-view-count', 'children', allow_duplicate=True),
    Output('grid-parked', 'data', allow_duplicate=True),
    Output('grid-jump', 'data', allow_duplicate=True),
    Output('grid-jump-note', 'children', allow_duplicate=True),
    Output('clap-search-input', 'value', allow_duplicate=True),
    Input('grid-back-live', 'n_clicks'),
    State('event-view-filter', 'value'),
    State('room-filter', 'value'),
    State('clap-threshold-slider', 'value'),
    prevent_initial_call=True,
)
def back_to_latest(_clicks, event_view, rooms, threshold):
    plan = plan_back_to_latest(event_view, rooms, threshold)
    return (plan["rows"], plan["columns"], plan["dates"][0], plan["dates"][1], plan["caption"],
            plan["parked"], plan["jump"], plan["note"], plan["query"])


@callback(
    Output('grid-back-live', 'style'),
    Input('grid-parked', 'data'),
)
def show_back_to_latest(parked):
    """The way back is offered only while there is somewhere to come back from."""
    return {} if parked else {'display': 'none'}


# The landing and the page's time span happen in the browser; see
# assets/grid_time_jump.js. Both write their text with set_props, so the
# outputs here are placeholders.
clientside_callback(
    "dash_clientside.sx_grid.jump",
    Output('grid-jump-sink', 'children'),
    Input('grid-jump', 'data'),
)

clientside_callback(
    "dash_clientside.sx_grid.watch_pages",
    Output('grid-page-sink', 'children'),
    Input('audio_grid_table', 'id'),
)

@callback(
    Output('audio-player-container', 'children', allow_duplicate=True),
    Output('spectrogram-graph', 'figure', allow_duplicate=True),
    Output('speech-analysis-output', 'children', allow_duplicate=True),
    Output('speech-analysis-output', 'style', allow_duplicate=True),
    Output('spectrogram-header', 'children'),
    Input('audio_grid_table', 'cellClicked'),
    State('audio_grid_table', 'rowData'),
    prevent_initial_call=True
)
def play_row_audio(cell_event, all_rows):
    if not cell_event or not all_rows: return no_update, no_update, no_update, no_update, no_update
    row_id = cell_event.get('rowId')
    if not row_id: return no_update, no_update, no_update, no_update, no_update
    
    row_data = next((row for row in all_rows if str(row.get('ID')) == str(row_id)), None)
    if not row_data: return no_update, no_update, no_update, no_update, no_update
    
    event_id = row_data.get('ID')
    try:
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', parameters.get_parameters()['sqlite']['db_name']))
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT file_name, spectrogram_image, transcription, emotion FROM ast_table WHERE id = ?", (event_id,))
        result = cursor.fetchone()
        conn.close()
        if not result: return no_update, no_update, no_update, no_update, no_update

        audio_filename, image_filename, transcription, emotion = result
        audio_src, fig = prepare_spectrogram_fig(audio_filename, image_filename)
        
        # Create a fresh Audio component to ensure reactivity and immediate playback
        new_audio_player = html.Audio(
            id='audio-player-details',
            src=f"{audio_src}?t={int(time.time() * 1000)}" if audio_src else None,
            controls=True,
            autoPlay=True, # Start playing immediately when clicked
            style={'width': '100%', 'marginTop': '10px'}
        )
            
        speech_ui, speech_style = build_speech_ui(transcription, emotion)
        return new_audio_player, fig, speech_ui, speech_style, f"Spectrogram Analysis: Event #{event_id}"
    except Exception as e:
        print(f"ERROR in play_row_audio: {e}")
        return no_update, no_update, no_update, no_update, no_update

@callback(
    Output('confirm-delete', 'displayed'),
    Output('confirm-delete', 'message'),
    Input('btn_delete_selected', 'n_clicks'),
    Input('ctx-delete', 'n_clicks'),
    State('audio_grid_table', 'selectedRows'),
    prevent_initial_call=True
)
def ask_before_delete(btn_clicks, ctx_clicks, selected_rows):
    """Name what is about to be destroyed, then let the user decide.

    The count matters: a right-click inside a multi-row selection deletes the
    whole selection, not the row under the cursor, and that is not obvious
    from the menu.
    """
    if not selected_rows:
        raise PreventUpdate
    n = len(selected_rows)
    return True, (f"Delete {n} event{'s' if n != 1 else ''}?\n\n"
                  f"This removes the row{'s' if n != 1 else ''} from the "
                  f"database and deletes the audio and spectrogram files from "
                  f"disk. It cannot be undone.")


@callback(
    Output('audio_grid_table', 'rowData', allow_duplicate=True),
    Input('confirm-delete', 'submit_n_clicks'),
    State('audio_grid_table', 'selectedRows'),
    State('grid-parked', 'data'),
    State('event-view-filter', 'value'),
    State('room-filter', 'value'),
    prevent_initial_call=True
)
def delete_selected_rows(confirmed, selected_rows, parked, event_view, rooms):
    if not confirmed or not selected_rows: raise PreventUpdate
    try:
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', parameters.get_parameters()['sqlite']['db_name']))
        ast_db = Ast_Sql_lite(db_path)
        ids_to_delete = []
        files_to_delete = []
        base_assets = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'assets'))
        audio_folders = ['audio_mp3', 'audios', 'audiosuser']
        image_folders = ['specrogram_images', 'spectrogram_images']
        
        for row in selected_rows:
            event_id = row.get('ID') or row.get('event_id')
            if event_id:
                ids_to_delete.append(event_id)
                def add_if_exists(filename, folders):
                    if not filename: return
                    basename = os.path.basename(filename)
                    for folder in folders:
                        path = os.path.join(base_assets, folder, basename)
                        if os.path.exists(path): files_to_delete.append(path)
                add_if_exists(row.get('file_name'), audio_folders)
                add_if_exists(row.get('spectrogram_image'), image_folders)

        if ids_to_delete: ast_db.ast_table.delete_ast_table_batch(ids_to_delete)
        for fpath in files_to_delete:
            try:
                if os.path.exists(fpath): os.remove(fpath)
            except: pass
        ast_db.close()
        # Reload what was on screen: the parked range or the live week, in the
        # current view. Unfiltered rows used to come back until the next
        # automatic reload, which no longer happens while parked.
        rng = (parked or {}).get('range') or live_range()
        data, columns, _, _ = get_db_rec(rng[0], rng[1])
        data, _caption = apply_event_view(data, event_view, rooms)
        return data
    except Exception as e:
        print(f"ERROR in delete_selected_rows: {e}")
        return no_update

@callback(
    Output('download_selected', 'data'),
    Input('btn_download_selected', 'n_clicks'),
    Input('ctx-download', 'n_clicks'),
    State('audio_grid_table', 'selectedRows'),
    prevent_initial_call=True
)
def download_selected_rows(btn_clicks, ctx_clicks, selected_rows):
    if not selected_rows: raise PreventUpdate
    try:
        zip_filename = f"selected_events_{uuid.uuid4()}.zip"
        zip_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'assets', zip_filename))
        base_assets = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'assets'))
        
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for row in selected_rows:
                audio_file = row.get('file_name')
                if audio_file:
                    audio_basename = os.path.basename(audio_file)
                    search_names = [audio_basename]
                    if audio_basename.endswith('.mp3'):
                        search_names.append(audio_basename.replace('.mp3', '.wav'))
                    elif audio_basename.endswith('.wav'):
                        search_names.append(audio_basename.replace('.wav', '.mp3'))
                    
                    found = False
                    for name in search_names:
                        for folder in ['audio_mp3', 'audios', 'audiosuser']:
                            abs_path = os.path.join(base_assets, folder, name)
                            if os.path.exists(abs_path):
                                zf.write(abs_path, arcname=name)
                                found = True
                                break
                        if found: break
        return dcc.send_file(zip_path)
    except Exception as e:
        print(f"ERROR in download_selected_rows: {e}")
        raise PreventUpdate

@callback(
    Output('download_json', 'data'),
    Input('btn_download_json', 'n_clicks'),
    State('date_picker_details','start_date'),
    State('date_picker_details', 'end_date'),
    prevent_initial_call=True
)
def download_json_data(n_clicks, start_date, end_date):
    if not n_clicks: raise PreventUpdate
    try:
        data, _, _, _ = get_db_rec(start_date, end_date)
        filename = f"audio_events_{start_date}_to_{end_date}.json" if start_date and end_date else "audio_events_all.json"
        # Prepend BOM for Excel compatibility on Windows
        json_str = json.dumps(data, indent=4, default=str, ensure_ascii=False)
        return dcc.send_string("\ufeff" + json_str, filename=filename)
    except Exception as e:
        print(f"ERROR in download_json_data: {e}")
        raise PreventUpdate

@callback(
    Output('download_csv', 'data'),
    Input('btn_download_csv', 'n_clicks'),
    State('date_picker_details','start_date'),
    State('date_picker_details', 'end_date'),
    prevent_initial_call=True
)
def download_csv_data(n_clicks, start_date, end_date):
    if not n_clicks: raise PreventUpdate
    try:
        data, _, _, _ = get_db_rec(start_date, end_date)
        filename = f"audio_events_{start_date}_to_{end_date}.csv" if start_date and end_date else "audio_events_all.csv"
        df = pd.DataFrame(data)
        # Prepend BOM manually to the string output for Excel compatibility
        return dcc.send_string("\ufeff" + df.to_csv(index=False), filename=filename)
    except Exception as e:
        print(f"ERROR in download_csv_data: {e}")
        raise PreventUpdate