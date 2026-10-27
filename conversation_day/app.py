#!/usr/bin/env python3
"""Discussions through the day -- a standalone viewer, outside the dashboard.

    python conversation_day/app.py          # then open http://127.0.0.1:8071

Choose a day, and optionally a time range to keep the model's work small. The
viewer anchors every voice to the discussion its sentinel heard it in (see
timeline.py), has the model read each discussion once, whole -- with the same
L1.5 prompt, payload and model call as the LLM tab -- and draws when each pair
talked and which lamps each discussion lit, across its whole span. Under the
figure each discussion worth a look is a card that opens in place -- what was
measured, what the model concluded, and what it was given -- and clicking a
cell puts that cell's discussions on top of the cards, already open.

Readings are saved in conversation_day/cache, one file per window, model,
prompt version and silence rule, so reopening a window costs nothing. Nothing
in the dashboard imports this folder, and nothing here changes the pipeline.
"""
import functools
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (HERE, ROOT, os.path.join(ROOT, "ast_dash", "llm_report")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import dash                                                        # noqa: E402
import dash_bootstrap_components as dbc                            # noqa: E402
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update  # noqa: E402

import derive_home_dynamics as H                                   # noqa: E402
import timeline as T                                               # noqa: E402
from misc.load_parameters import parameters                        # noqa: E402

PORT = 8071
CACHE = os.path.join(HERE, "cache")
PROMPT = os.path.join(ROOT, "assets", "LLM_prompt_L1_5.txt")
CONFIG = os.path.join(ROOT, "ast_dash", "llm_report", "home_dynamics_config.json")

# The LLM tab's model settings and lamp thresholds (pages/llm_report.py),
# repeated rather than imported: that module registers a dashboard page on
# import, which is exactly the coupling this viewer exists to avoid.
OPENAI_MODEL = "gpt-4o-mini"
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_NUM_CTX = 65536
OLLAMA_TIMEOUT_S = 1800
# 0.40 for both: measured against a listener on 90 exchanges (2026-09-22,
# tests/diagnostics/dynamics_pilot), the interpreter reaches 0.55 on 3
# exchanges in five days, while at 0.40 it calls an exchange warm with 71%
# sensitivity and 80% precision. See SEMANTIC_PRESENT_FLOOR.
GREEN, AMBER = 0.40, 0.40
NOTHING_OPEN = {"selected": [], "title": "", "expanded": [], "jump": 0}

COLUMNS = {"id": "ID", "date": "Timestamp", "device_name": "Device",
           "sound1": "Class 1", "conf_sound1": "Prob 1", "sound2": "Class 2",
           "conf_sound2": "Prob 2", "sound3": "Class 3", "conf_sound3": "Prob 3",
           "rms": "rms", "transcription": "transcription", "emotion": "emotion",
           "room": "room", "file_name": "file_name"}


# ── reading a window ───────────────────────────────────────────────────
def _db_path():
    return os.path.join(ROOT, parameters.get_parameters()["sqlite"]["db_name"])


def load_events(start, end):
    """The window's events, in the dashboard column format the pipeline reads."""
    db = _db_path()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    available = {row[1] for row in conn.execute("PRAGMA table_info(ast_table)")}
    columns = [c for c in COLUMNS if c in available]
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM ast_table WHERE date >= ? AND date <= ?"
        " ORDER BY date", (f"{start:%Y-%m-%d %H:%M:%S}", f"{end:%Y-%m-%d %H:%M:%S}")).fetchall()
    conn.close()
    return [{COLUMNS[c]: row[c] for c in columns} for row in rows]


SPEECH_CLIPS = ("SELECT rms FROM ast_table WHERE device_name = ? AND date >= ? AND date <= ? "
                "AND rms IS NOT NULL AND transcription IS NOT NULL "
                "AND transcription NOT LIKE 'No Speech%'")


def _clip_loudness(sentinel, first, last):
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    try:
        return sorted(row[0] for row in conn.execute(SPEECH_CLIPS, (sentinel, first, last)))
    finally:
        conn.close()


@functools.lru_cache(maxsize=128)
def _usual_loudness(sentinel, day):
    """The median loudness of one sentinel's clips with speech in them, over one day."""
    values = _clip_loudness(sentinel, f"{day} 00:00:00", f"{day} 23:59:59")
    return values[len(values) // 2] if values else None


def voice(discussion):
    """How loud a discussion's clips were, in dB against its sentinel's usual speech that day.

    A measurement, not a judgement: the rms level the pipeline stored for each
    clip with speech in it. A clip can start up to half a minute before the
    first line it holds, so the search starts that much earlier. None when
    there is nothing to compare.
    """
    sentinel, start = discussion.get("sentinel"), T._parse(discussion.get("start"))
    if not sentinel or start is None:
        return None
    end = T._parse(discussion.get("end")) or start
    usual = _usual_loudness(sentinel, f"{start:%Y-%m-%d}")
    clips = _clip_loudness(sentinel, f"{start - timedelta(seconds=30):%Y-%m-%d %H:%M:%S}",
                           f"{end:%Y-%m-%d %H:%M:%S}")
    if usual is None or not clips:
        return None
    return {"median": clips[len(clips) // 2] - usual, "loudest": clips[-1] - usual,
            "usual": usual, "clips": len(clips)}


def local_models():
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=2) as fh:
            return [m["name"] for m in json.load(fh).get("models") or [] if m.get("name")]
    except Exception:
        return []


def call_model(model, prompt):
    """One call, as the LLM tab's call_llm makes it. Raises instead of returning an error text."""
    if model == "chatgpt":
        from openai import OpenAI
        from misc.secrets import openai_api_key
        client = OpenAI(api_key=openai_api_key())
        response = client.chat.completions.create(
            model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}], stream=False)
        return response.choices[0].message.content
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "stream": False, "think": False,
                       "options": {"num_ctx": OLLAMA_NUM_CTX}}).encode("utf-8")
    request = urllib.request.Request(f"{OLLAMA_HOST}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_S) as fh:
        text = ((json.load(fh).get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError(f"empty response from {model}")
    return text


def parse_reply(text):
    """The JSON object in a model reply, located rather than assumed (as parse_part_b)."""
    if not text:
        return None
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    first, last = raw.find("{"), raw.rfind("}")
    if first == -1 or last <= first:
        return None
    try:
        data = json.loads(raw[first:last + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def make_interpreter(model, failures):
    """The L1.5 interpreter, patient with rate limits, recording calls that fail.

    A refused call is not a reading; `failures` collects them by context hash so
    the view says "the call failed" instead of "uninterpretable".
    """
    with open(PROMPT, encoding="utf-8") as fh:
        prompt = fh.read()

    def interpret(payload):
        exchange = json.dumps(payload, ensure_ascii=False, indent=2)
        delay = 5.0
        for attempt in range(6):
            try:
                return parse_reply(call_model(model, f"{prompt}\n\n{exchange}"))
            except Exception as exc:
                limited = "429" in str(exc) or "rate limit" in str(exc).lower()
                if limited and attempt < 5:
                    time.sleep(delay + random.uniform(0.0, 2.0))
                    delay = min(delay * 2, 60.0)
                    continue
                failures[H.window_hash(payload)] = str(exc)[:200]
                raise
    return interpret, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def clock(value, fallback):
    text = (value or "").strip()
    if not text:
        return fallback
    match = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", text)
    if not match or int(match.group(1)) > 23 or int(match.group(2) or 0) > 59:
        raise ValueError(f'"{value}" is not a time of day; write it as HH:MM.')
    return int(match.group(1)), int(match.group(2) or 0)


def summary(data):
    discussions = data.get("discussions") or []
    counts = Counter(d["status"] for d in discussions)
    bits = [f"{len(discussions) - counts['not_read']} discussions",
            f"{counts['interpretable'] + counts['partially_interpretable']} read by the model"]
    for status, words in (("uninterpretable", "judged uninterpretable"),
                          ("not_attempted", "not read (no model chosen)"),
                          ("failed", "failed at the model")):
        if counts[status]:
            bits.append(f"{counts[status]} {words}")
    left_out = data.get("left_out") or []
    if left_out or counts["not_read"]:
        overheard = sum(1 for s in left_out if s.get("overheard"))
        bits.append(f"left out: {len(left_out)} lone stretches ({overheard} overheard from "
                    f"another room), {counts['not_read']} too short for the model to read")
    return " · ".join(bits)


def read_day(day, start_clock, end_clock, model, fresh=False, silence=T.SILENCE_MINUTES):
    """Read one window, or load its saved reading: (store data, status line).

    Apart from the callback so a window can also be read from a script -- to
    prepare a long day before opening the viewer, for instance.
    """
    if not day:
        return no_update, dbc.Alert("Choose a day first.", color="warning")
    try:
        start_h, start_m = clock(start_clock, (0, 0))
        end_h, end_m = clock(end_clock, (23, 59))
        silence = int(silence or T.SILENCE_MINUTES)
        if not 1 <= silence <= 60:
            raise ValueError("The silence that ends a discussion must be 1 to 60 minutes.")
    except (TypeError, ValueError) as exc:
        return no_update, dbc.Alert(str(exc), color="danger")
    base = datetime.strptime(day[:10], "%Y-%m-%d")
    start = base.replace(hour=start_h, minute=start_m)
    end = base.replace(hour=end_h, minute=end_m, second=59)
    if end <= start:
        return no_update, dbc.Alert("“To” must come after “From”.", color="danger")

    cfg = H.load_config(CONFIG)
    prompt_sha = ""
    if model != "none":
        with open(PROMPT, encoding="utf-8") as fh:
            prompt_sha = hashlib.sha256(fh.read().encode("utf-8")).hexdigest()
    safe_model = re.sub(r"[^\w.-]+", "_", model)
    suffix = f"_{prompt_sha[:8]}" if prompt_sha else ""
    name = (f"{base:%Y-%m-%d}_{start:%H%M}-{end:%H%M}_{safe_model}{suffix}"
            f"_rooms-s{silence}.json")
    path = os.path.join(CACHE, name)
    window_text = f"{start:%d-%m-%Y %H:%M}–{end:%H:%M}"

    if os.path.exists(path) and not fresh:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data, html.Div([
            html.Span(summary(data), className="cd-status-main"),
            html.Span(f"{window_text} · saved reading from "
                      f"{data['read_at'][:16].replace('T', ' ')} · {data['model_label']}",
                      className="cd-status-note")])

    events = load_events(start, end)
    if not events:
        return None, dbc.Alert(f"No recordings between {window_text}.", color="warning")
    failures = {}
    interpreter = None
    if model != "none":
        interpreter, prompt_sha = make_interpreter(model, failures)
    began = time.monotonic()
    level0 = H.derive_level0(events, cfg)
    discussions, left_out = T.anchor(level0, cfg, silence)
    read = T.read_discussions(level0, cfg, discussions, interpreter, failures)
    elapsed = time.monotonic() - began

    model_label = ("no model" if model == "none"
                   else f"ChatGPT ({OPENAI_MODEL})" if model == "chatgpt" else model)
    data = {"day": f"{base:%Y-%m-%d}", "window": [start.isoformat(), end.isoformat()],
            "model": model, "model_label": model_label, "prompt_sha256": prompt_sha,
            "prompt_file": os.path.basename(PROMPT),
            "read_at": datetime.now().isoformat(timespec="seconds"),
            "residents": sorted(cfg.get("household_members") or {}),
            "silence_minutes": silence, "events": len(events), "discussions": read,
            "left_out": T.left_out_dicts(left_out)}
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    return data, html.Div([
        html.Span(summary(data), className="cd-status-main"),
        html.Span(f"{window_text} · {len(events)} recordings read with {model_label} in "
                  f"{elapsed:.0f} s · saved", className="cd-status-note")])


# ── the page ───────────────────────────────────────────────────────────
app = dash.Dash(__name__, title="Discussions through the day",
                external_stylesheets=[dbc.themes.DARKLY],
                assets_folder=os.path.join(HERE, "assets"))


def serve_layout():
    models = ([{"label": f"ChatGPT ({OPENAI_MODEL})", "value": "chatgpt"}]
              + [{"label": f"{name} (local)", "value": name} for name in local_models()]
              + [{"label": "No model — speech only", "value": "none"}])
    return html.Div([
        html.Div([
            html.Div([
                html.H1("Discussions through the day"),
                html.P("When each pair talked, and what the model read in each of their "
                       "discussions."),
            ]),
            html.Div(id="cd-day-label", className="cd-day"),
        ], className="cd-header"),

        html.Div([
            html.Div([html.Label("Day"),
                      dcc.DatePickerSingle(id="cd-date", date=datetime.now().date(),
                                           display_format="DD-MM-YYYY")], className="cd-field"),
            html.Div([html.Label("From"),
                      dbc.Input(id="cd-from", placeholder="00:00", value="")],
                     className="cd-field cd-narrow"),
            html.Div([html.Label("To"),
                      dbc.Input(id="cd-to", placeholder="23:59", value="")],
                     className="cd-field cd-narrow"),
            html.Div([html.Label("Silence ends a discussion (min)"),
                      dbc.Input(id="cd-silence", type="number", min=1, max=60, step=1,
                                value=T.SILENCE_MINUTES)], className="cd-field cd-silence"),
            html.Div([html.Label("Model"),
                      dcc.Dropdown(id="cd-model", options=models, value="chatgpt",
                                   clearable=False)], className="cd-field cd-wide"),
            html.Div([dbc.Checklist(id="cd-fresh", switch=True, value=[],
                                    options=[{"label": "Read again", "value": "fresh"}])],
                     className="cd-field cd-switch"),
            dbc.Button("Show the day", id="cd-show", color="success", n_clicks=0),
        ], className="cd-controls"),
        html.Div("Leave From and To empty for the whole day. A saved reading of the "
                 "same window is reused unless “Read again” is on.", className="cd-hint"),

        dcc.Loading(html.Div(id="cd-status", className="cd-status"), type="dot",
                    color="#57C98A"),

        html.Div([
            html.Div([html.Label("Slot"),
                      dcc.Dropdown(id="cd-slot", clearable=False, value=T.DEFAULT_MINUTES,
                                   options=[{"label": "1 min", "value": 1},
                                            {"label": "5 min", "value": 5},
                                            {"label": "15 min", "value": 15}])],
                     className="cd-slot"),
            dcc.Graph(id="cd-graph", config={"displayModeBar": False, "doubleClick": "reset"}),
            T.legend(),
            html.Div("A discussion is what one room's sentinel heard with two or more "
                     "residents in it, ending after the chosen silence; people who move to "
                     "another room start a new discussion there. Speech from one resident "
                     "alone is left out, and so is a discussion too short for the model to "
                     "read. A pair is drawn where its two people answer each other — at least "
                     "two replies between them in the slot — so a single line in someone "
                     "else's conversation does not light a pair. The model reads each "
                     "discussion once, whole, and that one reading lights the lamps across its "
                     "entire span; where discussions in different rooms overlap, a lamp shows "
                     "the stronger reading. Drag to zoom; double-click for the whole day; click "
                     "a cell to put its discussions on top of the cards below, already open.",
                     className="ct-note"),
            html.Div([
                html.Div([html.Span("Chosen from the figure", className="ct-moments-head"),
                          html.Span(id="cd-chosen-title", className="ct-chosen-title"),
                          dbc.Button("Clear", id="cd-clear", color="secondary", size="sm",
                                     outline=True, n_clicks=0, className="ms-auto")],
                         className="ct-chosen-head"),
                html.Div(id="cd-chosen", className="ct-moments"),
            ], id="cd-chosen-box", hidden=True),
            html.Div("Key moments", className="ct-moments-head"),
            html.Div(id="cd-moments", className="ct-moments"),
        ], id="cd-panel", hidden=True, className="ct-panel"),

        dcc.Store(id="cd-store"),
        dcc.Store(id="cd-open", data=dict(NOTHING_OPEN)),
        dcc.Store(id="cd-scrolled"),
    ], className="cd-page")


app.layout = serve_layout


@app.callback(
    Output("cd-store", "data"),
    Output("cd-status", "children"),
    Input("cd-show", "n_clicks"),
    State("cd-date", "date"),
    State("cd-from", "value"),
    State("cd-to", "value"),
    State("cd-model", "value"),
    State("cd-fresh", "value"),
    State("cd-silence", "value"),
    prevent_initial_call=True,
)
def read_window(_clicks, day, start_clock, end_clock, model, fresh, silence):
    return read_day(day, start_clock, end_clock, model, "fresh" in (fresh or []), silence)


@app.callback(
    Output("cd-panel", "hidden"),
    Output("cd-graph", "figure"),
    Output("cd-day-label", "children"),
    Input("cd-store", "data"),
    Input("cd-slot", "value"),
)
def render_day(data, minutes):
    if not data or not data.get("discussions"):
        return True, no_update, ""
    day = data["day"]
    return (False, T.build_figure(data, day, int(minutes or T.DEFAULT_MINUTES), GREEN, AMBER),
            f"{datetime.strptime(day, '%Y-%m-%d'):%a, %d-%m-%Y}")


def choose_cell(data, minutes, click, state):
    """The cards after a click on the figure: that cell's discussions on top, open.

    `jump` changes only here, so the page scrolls to the cards after a click on
    the figure and never when a card is merely opened or closed.
    """
    title, found = T.cell_discussions(data, data["day"], int(minutes or T.DEFAULT_MINUTES), click)
    if not found:
        return state
    ids = [d["id"] for d in found]
    return {"selected": ids, "title": title, "expanded": ids, "jump": time.time()}


def toggle_card(state, discussion_id):
    """The cards after a click on one card's summary: that card opens, or closes."""
    expanded = [d_id for d_id in state.get("expanded") or [] if d_id != discussion_id]
    if len(expanded) == len(state.get("expanded") or []):
        expanded.append(discussion_id)
    return dict(state, expanded=expanded)


@app.callback(
    Output("cd-open", "data"),
    Output("cd-graph", "clickData"),
    Input("cd-graph", "clickData"),
    Input({"type": "conv-card", "index": ALL}, "n_clicks"),
    Input("cd-clear", "n_clicks"),
    Input("cd-store", "data"),
    State("cd-slot", "value"),
    State("cd-open", "data"),
    prevent_initial_call=True,
)
def choose(click, _card_clicks, _clear, data, minutes, state):
    """Which discussions were chosen from the figure, and which cards are open."""
    state = state or dict(NOTHING_OPEN)
    trigger = ctx.triggered_id
    if trigger in ("cd-clear", "cd-store"):
        # A new reading starts with nothing chosen; Clear empties the choice.
        return dict(NOTHING_OPEN), no_update
    if trigger == "cd-graph":
        if not click or not data:
            return no_update, no_update
        # Cleared either way, so clicking the same cell again chooses it again.
        return choose_cell(data, minutes, click, state), None
    if isinstance(trigger, dict):
        # Every redraw recreates the cards, which fires this with no clicks;
        # only a real click opens or closes one.
        if not ctx.triggered[0].get("value"):
            return no_update, no_update
        return toggle_card(state, trigger.get("index")), no_update
    return no_update, no_update


@app.callback(
    Output("cd-chosen-box", "hidden"),
    Output("cd-chosen-title", "children"),
    Output("cd-chosen", "children"),
    Output("cd-moments", "children"),
    Input("cd-open", "data"),
    Input("cd-store", "data"),
)
def show_cards(state, data):
    if not data or not data.get("discussions"):
        return True, "", [], []
    state = state or NOTHING_OPEN
    chosen, key_moments = T.render_cards(data, data["day"], GREEN, AMBER,
                                         state.get("selected") or [],
                                         set(state.get("expanded") or []), voice,
                                         provenance=T.provenance(data))
    return not chosen, state.get("title") or "", chosen, key_moments


app.clientside_callback(
    """
    function (_chosen, state) {
        if (state && state.jump && state.jump !== window.cdLastJump) {
            window.cdLastJump = state.jump;
            setTimeout(function () {
                var box = document.getElementById("cd-chosen-box");
                if (box) { box.scrollIntoView({behavior: "smooth", block: "start"}); }
            }, 60);
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("cd-scrolled", "data"),
    Input("cd-chosen", "children"),
    State("cd-open", "data"),
)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False)
