"""Visuals tab: a live intercom to an ESP32-P4 sentinel.

The peer connection is not built here. Everything this page does is lay out the
media elements and controls that ``assets/webrtc_intercom_v2.js`` drives; the
signaling it needs is served by ``ast_src/webrtc/routes.py``.

Media is deliberately asymmetric, because the sentinel is:

    operator  --  G.711 A-law 8 kHz, duplex  --  sentinel      (RTP)
    operator  --  JPEG frames, one way       ->  sentinel      (data channel)
    operator  <-  JPEG frames, one way       --  sentinel      (data channel,
                                                  not enabled in firmware yet)
    operator  <-  JPEG frames, continuous    --  sentinel      (HTTP uplink)

"What the sentinel sees" is fed by the last of those, not by the call. The
sentinel posts its OV5647 frames to /video/api/ingest whenever it is powered
on (firmware: ov5647_video_enabled=1), and this page renders them through
/video/api/stream as MJPEG -- so the panel is live before anyone calls, and
stays live during and after a call.
"""

import json
import os
import sys
from datetime import datetime, timedelta

import dash
import dash_bootstrap_components as dbc
from dash import ALL, ClientsideFunction, Input, Output, State, callback
from dash import clientside_callback, ctx, dcc, html, no_update

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# The uplink store lives in this same Flask process, so the panel's liveness
# check is a dict lookup rather than an HTTP call back into ourselves.
from ast_src.video_stream import get_store
from ast_src.video_stream.clip_receiver import get_receiver

dash.register_page(
    __name__,
    path='/visuals',
    title='Visuals (ESP32-P4)',
    name='Visuals'
)

STATUS_FILE = os.path.join(project_root, 'sentinel_status.json')

# Matches the staleness rule the Sentinels panel uses on the audio page: a
# device that claims to be online but has said nothing for this long is
# reported as unconfirmed rather than green.
STALE_AFTER = timedelta(minutes=5)


def _is_stale(last_seen):
    if not last_seen:
        return True
    try:
        seen = datetime.fromisoformat(str(last_seen))
    except (TypeError, ValueError):
        return True
    return datetime.now() - seen > STALE_AFTER


def load_p4_devices():
    """P4-class sentinels from sentinel_status.json.

    A device qualifies on its name or on an advertised camera/WebRTC
    capability. Audio-only ESP32-S3-BOX units are filtered out: they have no
    signaling room and calling one would just time out.
    """
    devices = []
    if not os.path.exists(STATUS_FILE):
        return devices

    try:
        with open(STATUS_FILE, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[visuals] cannot read {STATUS_FILE}: {exc}")
        return devices

    for dev_id, info in (data or {}).items():
        capabilities = info.get("capabilities", {}) or {}
        is_p4 = "p4" in dev_id.lower()
        has_media = capabilities.get("WebRTC", False) or capabilities.get("Camera", False)
        # A camera board that advertises nothing still belongs here once it
        # has shown a picture: named as a camera, or having sent a clip.
        has_media = (has_media or "cam" in dev_id.lower()
                     or dev_id in get_receiver().devices())
        if not (is_p4 or has_media):
            continue

        last_seen = info.get("last_seen")
        status = info.get("status", "offline")
        if status == "online" and _is_stale(last_seen):
            status = "unconfirmed"

        devices.append({
            "id": dev_id,
            "ip": info.get("ip_address", "unknown"),
            "status": status,
            "last_seen": last_seen or "never",
            "capabilities": capabilities,
        })
    return devices


def _status_colour(status):
    return {"online": "success", "unconfirmed": "warning"}.get(status, "secondary")


def _video_profile():
    """The uplink profile, from the same place the browser is handed one.

    Read through the signaling bridge rather than from parameters.json here,
    so the sliders below and the profile served at /webrtc/api/session cannot
    drift apart. The bridge's constructor only reads configuration -- it does
    not touch the broker until a call opens a session.
    """
    fallback = {"width": 720, "height": 1280, "fps": 8, "quality": 0.6}
    try:
        from ast_src.webrtc.signaling import get_bridge
        profile = dict(fallback)
        profile.update(get_bridge().video_profile() or {})
        return profile
    except Exception as exc:  # a broken config must not blank the page
        print(f"[visuals] falling back to default video profile: {exc}")
        return fallback


def _clamp(value, low, high):
    return max(low, min(high, value))


# ── video library ───────────────────────────────────────────────────────
#
# The operator's uplink to the sentinel is a JPEG frame pump that draws
# whatever #p4-local-video is showing (see assets/webrtc_intercom_v2.js). So
# "send a clip to the sentinel" needs nothing more than pointing that element
# at a file instead of the camera -- the pump, the data channel and the
# firmware side all stay exactly as they are.
#
# Browsing happens server-side because this is a desktop app: the Flask
# process and the operator share a filesystem. Everything is confined to
# VIDEO_ROOT so a stray "../.." cannot walk out of the project.

VIDEO_ROOT = project_root
VIDEO_HOME = os.path.join(project_root, 'video')
VIDEO_EXTS = {'.mp4', '.webm', '.ogg', '.ogv', '.mov', '.m4v', '.mkv', '.avi'}

# Chrome will not decode these in a <video>, so they are listed but not
# offered for playback rather than failing silently at play() time.
VIDEO_EXTS_UNPLAYABLE = {'.mkv', '.avi', '.mov'}


def _safe_dir(rel):
    """Resolve a browser-supplied relative path to a directory inside VIDEO_ROOT."""
    root = os.path.realpath(VIDEO_ROOT)
    candidate = os.path.realpath(os.path.join(root, rel or ''))
    if candidate != root and not candidate.startswith(root + os.sep):
        return root
    return candidate if os.path.isdir(candidate) else root


def _rel_to_root(path):
    rel = os.path.relpath(path, os.path.realpath(VIDEO_ROOT))
    return '' if rel == '.' else rel.replace('\\', '/')


def _list_dir(rel):
    """Sub-directories and video files of `rel`, as (dirs, files)."""
    here = _safe_dir(rel)
    dirs, files = [], []
    try:
        for name in sorted(os.listdir(here), key=str.lower):
            full = os.path.join(here, name)
            if os.path.isdir(full):
                if not name.startswith('.') and name not in {'__pycache__', 'node_modules'}:
                    dirs.append({'name': name, 'rel': _rel_to_root(full)})
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext in VIDEO_EXTS:
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                    files.append({
                        'name': name,
                        'rel': _rel_to_root(full),
                        'size': size,
                        'playable': ext not in VIDEO_EXTS_UNPLAYABLE,
                    })
    except OSError as exc:
        print(f"[visuals] cannot list {here}: {exc}")
    return dirs, files


def _human_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _default_cwd():
    """Start in ast_home/video, falling back to the project root if absent."""
    return _rel_to_root(VIDEO_HOME) if os.path.isdir(VIDEO_HOME) else ''


def _register_video_route():
    """Serve files under VIDEO_ROOT to the <video> element.

    The clips live outside assets/, so Dash's static handler will not serve
    them. Registered here rather than in ast_app.py to keep this feature to a
    single file; guarded by a flag because Dash imports pages once but Flask
    rejects a duplicate endpoint name.

    send_file(conditional=True) gives Range replies, which browsers need in
    order to decode and seek a video rather than buffering the whole file.
    """
    try:
        from dash import get_app
        from flask import abort, send_file

        server = get_app().server
        if getattr(server, '_sentinel_video_route', False):
            return
        server._sentinel_video_route = True

        @server.route('/media/video/<path:relpath>')
        def sentinel_video_file(relpath):
            root = os.path.realpath(VIDEO_ROOT)
            full = os.path.realpath(os.path.join(root, relpath))
            if full != root and not full.startswith(root + os.sep):
                abort(403)
            if not os.path.isfile(full):
                abort(404)
            if os.path.splitext(full)[1].lower() not in VIDEO_EXTS:
                abort(415)
            return send_file(full, conditional=True)
    except Exception as exc:
        print(f"[visuals] could not register video route: {exc}")


_register_video_route()


# ── layout fragments ────────────────────────────────────────────────────

def _device_panel(devices):
    options = [
        {"label": f"{d['id']}  ({d['status']})", "value": d['id']}
        for d in devices
    ]
    return dbc.Card([
        dbc.CardHeader(html.H5("Sentinel", className="mb-0")),
        dbc.CardBody([
            dbc.Label("Discovered P4 units"),
            dcc.Dropdown(
                id="p4_device_select",
                options=options,
                value=devices[0]['id'] if devices else None,
                placeholder="No P4 sentinel has reported in yet",
                className="mb-2",
            ),
            dbc.Input(
                id="p4_device_manual",
                placeholder="or type a signaling room, e.g. p4-door-1",
                debounce=True,
                className="mb-2",
            ),
            dbc.FormText(
                "The room is the sentinel's DEVICE_ID from its settings.txt. "
                "Typing one here overrides the dropdown.",
                className="d-block mb-3",
            ),
            html.Div(id="p4_device_metadata"),
        ]),
    ], className="mb-3 shadow-sm")


def _hardware_panel():
    return dbc.Card([
        dbc.CardHeader(html.H5("Operator hardware", className="mb-0")),
        dbc.CardBody([
            dbc.Label("Microphone", html_for="p4-mic-select"),
            html.Select(id="p4-mic-select", className="form-select mb-3"),
            dbc.Label("Camera", html_for="p4-cam-select"),
            html.Select(id="p4-cam-select", className="form-select mb-3"),
            dbc.Label("Speaker", html_for="p4-spk-select"),
            html.Select(id="p4-spk-select", className="form-select mb-1"),
            dbc.FormText(
                "Device names stay blank until the browser has been granted "
                "microphone and camera access once.",
            ),
        ]),
    ], className="mb-3 shadow-sm")


def _call_panel(profile):
    """Call controls, with the sliders starting where parameters.json says.

    The sliders are what the browser actually uses: control() passes their
    values to the peer, and they overwrite whatever the session profile said
    (webrtc_intercom_v2.js, call()). Starting them anywhere else -- as they used
    to, at a hardcoded 8 and 0.6 -- made webrtc.video_fps and
    webrtc.jpeg_quality dead config that looked live.

    The fps slider's top end follows the configured value, so a setting above
    the usual ceiling is still reachable on the slider rather than silently
    unrepresentable.
    """
    fps = int(profile["fps"])
    quality = _clamp(float(profile["quality"]), 0.3, 0.9)
    fps_max = max(15, fps)
    fps_marks = {1: "1", 5: "5", 10: "10", fps_max: str(fps_max)}

    return dbc.Card([
        dbc.CardHeader(html.H5("Call", className="mb-0")),
        dbc.CardBody([
            dbc.Row([
                dbc.Col(dbc.Button("Call", id="p4_call_btn", color="success",
                                   className="w-100", n_clicks=0), width=6),
                dbc.Col(dbc.Button("Hang up", id="p4_hangup_btn", color="danger",
                                   className="w-100", n_clicks=0), width=6),
            ], className="mb-3"),

            html.Div("Idle", id="p4-status", className="fw-bold text-secondary"),
            html.Small("", id="p4-stats", className="text-muted d-block mb-3"),

            dbc.Row([
                dbc.Col(dbc.Switch(id="p4_mic_on", label="Microphone",
                                   value=True), width=6),
                dbc.Col(dbc.Switch(id="p4_cam_on", label="Camera",
                                   value=True), width=6),
            ]),

            html.Hr(),
            dbc.Label("To sentinel: frame rate"),
            dcc.Slider(
                id="p4_fps", min=1, max=fps_max, step=1, value=fps,
                marks=fps_marks,
                tooltip={"placement": "bottom", "always_visible": False},
            ),
            dbc.Label("To sentinel: JPEG quality", className="mt-2"),
            dcc.Slider(
                # Floor lowered from 0.3: frame geometry is fixed at the
                # panel's 720x1280 (settings.h -- the renderer decodes into
                # the framebuffer, so the stride cannot change), which makes
                # compression the only way to trade sharpness for frame rate.
                id="p4_quality", min=0.15, max=0.9, step=0.05, value=quality,
                marks={0.3: "small", 0.6: "balanced", 0.9: "sharp"},
                tooltip={"placement": "bottom", "always_visible": False},
            ),
            dbc.FormText(
                f"Frames are {profile['width']}x{profile['height']} JPEGs, "
                "fixed by VIDEO_WIDTH and VIDEO_HEIGHT in the sentinel "
                "firmware and webrtc.video_width/video_height in "
                "parameters.json, which must be changed together. Rate and "
                "quality start from webrtc.video_fps and webrtc.jpeg_quality "
                "and are the only safe things to trade against the link.",
                className="d-block mt-2",
            ),
            dbc.FormText(
                "The link to the sentinel carries on the order of 40 kB/s: "
                "every frame crosses SDIO to the ESP32-C6 and back through "
                "DTLS. Asking for more than that does not go faster, it just "
                "drops frames once the channel backs up, so lower the quality "
                "before raising the rate.",
                className="d-block mt-2",
            ),
        ]),
    ], className="mb-3 shadow-sm")


_SCREEN_STYLE = {
    "background": "#101014",
    "borderRadius": "8px",
    "border": "1px solid #2a2a35",
    "overflow": "hidden",
    "display": "flex",
    "alignItems": "center",
    "justifyContent": "center",
    "minHeight": "260px",
}


def _viewer_panel():
    return html.Div([
        dbc.Card([
            dbc.CardHeader(html.H5("What you are sending", className="mb-0")),
            dbc.CardBody([
                html.Div(
                    html.Video(
                        id="p4-local-video",
                        autoPlay=True,
                        muted=True,
                        style={"width": "100%", "maxHeight": "360px",
                               "objectFit": "contain", "background": "#000"},
                    ),
                    style=_SCREEN_STYLE,
                ),
                html.Small(
                    "Your camera, as the sentinel's panel receives it.",
                    className="text-muted d-block mt-2",
                ),
            ]),
        ], className="mb-3 shadow-sm"),

        dbc.Card([
            dbc.CardHeader(html.H5("What the sentinel sees", className="mb-0")),
            dbc.CardBody([
                html.Div([
                    # The continuous OV5647 uplink. src is only set while the
                    # device is actually streaming: pointing an <img> at the
                    # MJPEG endpoint of a silent device would just hang the
                    # request open behind a blank panel.
                    html.Img(
                        id="p4-device-mjpeg",
                        src="",
                        style={"width": "100%", "maxHeight": "360px",
                               "objectFit": "contain", "display": "none"},
                    ),
                    # Frames arriving over the call's data channel, for the
                    # day the firmware's VIDEO_SEND_ENABLED is turned on.
                    # Driven by webrtc_intercom_v2.js, not by this page.
                    html.Canvas(
                        id="p4-device-canvas",
                        style={"width": "100%", "maxHeight": "360px",
                               "objectFit": "contain", "display": "none"},
                    ),
                    html.Div([
                        html.I(className="fas fa-eye-slash fa-2x text-secondary"),
                        html.P(
                            "This sentinel is not streaming. Set "
                            "ov5647_video_enabled=1 in its settings.txt and "
                            "its camera appears here, call or no call.",
                            className="text-secondary text-center px-4 mt-3 mb-0",
                            style={"fontSize": "0.9rem", "maxWidth": "34rem"},
                        ),
                    ], id="p4-device-empty",
                        className="d-flex flex-column align-items-center"),
                ], style=_SCREEN_STYLE),
                html.Small("", id="p4-video-status",
                           className="text-muted d-block mt-2"),
            ]),
        ], className="shadow-sm"),

        _library_panel(),

        # Playback sink for the sentinel's microphone. Kept out of sight
        # because the operator controls level from the speaker picker.
        html.Audio(id="p4-remote-audio", autoPlay=True,
                   style={"display": "none"}),
    ])


def _library_panel():
    """Pick a clip from disk and send it up the call in place of the camera."""
    return dbc.Card([
        dbc.CardHeader([
            html.H5([
                html.I(className="fas fa-film me-2"),
                "Load video",
            ], className="mb-0"),
        ]),
        dbc.CardBody([
            # Breadcrumb + up-one-level, so the operator can walk the tree
            # instead of being pinned to a single folder.
            html.Div([
                dbc.Button(html.I(className="fas fa-level-up-alt"),
                           id="p4_lib_up", size="sm", color="secondary",
                           outline=True, className="me-2",
                           title="Up one folder"),
                dbc.Button(html.I(className="fas fa-home"),
                           id="p4_lib_home", size="sm", color="secondary",
                           outline=True, className="me-2",
                           title="Back to the video folder"),
                html.Span(id="p4_lib_path", className="small text-muted"),
                dbc.Button(html.I(className="fas fa-sync-alt"),
                           id="p4_lib_refresh", size="sm", color="secondary",
                           outline=True, className="ms-auto",
                           title="Rescan this folder"),
            ], className="d-flex align-items-center mb-2"),

            html.Div(id="p4_lib_listing", className="sx-filelist"),

            html.Hr(className="my-3"),

            html.Div([
                dbc.Button([html.I(className="fas fa-play me-2"), "Send to sentinel"],
                           id="p4_lib_send", color="success", disabled=True,
                           className="me-2"),
                dbc.Button([html.I(className="fas fa-video me-2"), "Back to camera"],
                           id="p4_lib_camera", color="secondary", outline=True),
            ], className="d-flex align-items-center"),

            html.Small(
                "Calls the selected sentinel if no call is up, then streams "
                "the clip in place of your camera.",
                className="text-muted d-block mt-2",
            ),
            html.Small(id="p4_lib_status", className="d-block mt-1 sx-lib-status"),

            dcc.Store(id="p4_lib_cwd", data=_default_cwd()),
            dcc.Store(id="p4_lib_selected", data=None),
        ]),
    ], className="mt-3 shadow-sm")


def layout(**kwargs):
    devices = load_p4_devices()
    profile = _video_profile()
    return html.Div([
        dcc.Store(id="p4_effective_device"),
        dcc.Store(id="p4_call_state"),
        dcc.Store(id="p4_toggle_sink"),
        dcc.Interval(id="p4_discovery_poll", interval=10000, n_intervals=0),
        dcc.Interval(id="p4_video_poll", interval=3000, n_intervals=0),
        dcc.Interval(id="p4_boot", interval=700, n_intervals=0, max_intervals=1),

        dbc.Row(html.H3("Intercom", className="mb-1 text-primary")),
        dbc.Row(html.P(
            "Duplex audio with an ESP32-P4 sentinel, with your camera sent to "
            "its panel.",
            className="text-muted mb-4",
        )),

        dbc.Row([
            dbc.Col([_device_panel(devices), _hardware_panel(),
                     _call_panel(profile)], md=5),
            dbc.Col(_viewer_panel(), md=7),
        ]),
    ], style={"padding": "20px"})


# ── server-side callbacks ───────────────────────────────────────────────

@callback(
    Output("p4_device_select", "options"),
    Input("p4_discovery_poll", "n_intervals"),
)
def refresh_device_options(_n):
    return [
        {"label": f"{d['id']}  ({d['status']})", "value": d['id']}
        for d in load_p4_devices()
    ]


@callback(
    Output("p4_effective_device", "data"),
    Input("p4_device_select", "value"),
    Input("p4_device_manual", "value"),
)
def resolve_device(selected, typed):
    return (typed or "").strip() or selected


@callback(
    Output("p4_device_metadata", "children"),
    Input("p4_effective_device", "data"),
)
def show_device_metadata(device_id):
    if not device_id:
        return dbc.Alert(
            "Pick a sentinel, or type its signaling room to call one that has "
            "not reported in yet.",
            color="secondary", className="mb-0 py-2",
        )

    device = next((d for d in load_p4_devices() if d['id'] == device_id), None)
    if device is None:
        return dbc.Alert(
            f"'{device_id}' has never reported in. Calling it still works if "
            f"its firmware is listening on that room.",
            color="warning", className="mb-0 py-2",
        )

    badges = [
        dbc.Badge(name, color="info", className="me-1 mb-1")
        for name, supported in device['capabilities'].items() if supported
    ]
    return html.Div([
        html.Hr(className="mt-0"),
        dbc.Row([
            dbc.Col([
                html.Small("Address", className="text-secondary d-block"),
                html.Strong(device['ip']),
                html.Small("Last seen", className="text-secondary d-block mt-2"),
                html.Span(device['last_seen']),
            ], width=6),
            dbc.Col([
                html.Small("Status", className="text-secondary d-block"),
                dbc.Badge(device['status'].upper(),
                          color=_status_colour(device['status']),
                          className="mb-2"),
                html.Small("Capabilities", className="text-secondary d-block"),
                html.Div(badges or html.Span("none advertised",
                                             className="text-muted")),
            ], width=6),
        ]),
    ])


_MJPEG_VISIBLE = {"width": "100%", "maxHeight": "360px",
                  "objectFit": "contain", "display": "block"}
_MJPEG_HIDDEN = {"width": "100%", "maxHeight": "360px",
                 "objectFit": "contain", "display": "none"}

# The placeholder is hidden by *replacing* its class list, not by an inline
# display:none. Bootstrap's .d-flex is `display:flex !important`, which beats
# an inline style, so setting style={"display":"none"} on that div leaves it
# on screen -- and since the screen div is itself a flex row, the panel ends
# up split between the image and the placeholder text. Swapping the class for
# .d-none is the only thing that actually hides it.
_EMPTY_VISIBLE_CLASS = "d-flex flex-column align-items-center"
_EMPTY_HIDDEN_CLASS = "d-none"


@callback(
    Output("p4-device-mjpeg", "src"),
    Output("p4-device-mjpeg", "style"),
    Output("p4-device-empty", "className"),
    Output("p4-video-status", "children"),
    Input("p4_effective_device", "data"),
    Input("p4_video_poll", "n_intervals"),
)
def show_uplink(device_id, _n):
    """Point the panel at the sentinel's MJPEG stream while it is live.

    The src is left byte-identical between polls on purpose. Dash only patches
    attributes it sees change, so an unchanged src leaves the <img> connected;
    appending a cache-buster would tear the stream down and reopen it every
    three seconds.
    """
    if not device_id:
        return "", _MJPEG_HIDDEN, _EMPTY_VISIBLE_CLASS, ""

    status = get_store().status(device_id)
    if not status or not status.get("live"):
        # A camera board that sends clips is silent between them. Its last
        # clip is replayed on a loop; the src carries the clip's start time,
        # so it changes -- and the <img> reconnects -- only for a new clip.
        last = get_receiver().last_clip(device_id)
        if last is not None:
            rel = os.path.relpath(last.path, project_root) if last.path else "not saved"
            note = (f"Clip from {device_id} received at "
                    f"{datetime.fromtimestamp(last.started):%H:%M:%S} on "
                    f"{datetime.fromtimestamp(last.started):%d-%m-%Y}: "
                    f"{len(last.frames)} frames at {last.fps:.1f} fps, replaying. "
                    f"Saved as {rel}.")
            return (f"/video/api/clip/{device_id}?t={int(last.started)}",
                    _MJPEG_VISIBLE, _EMPTY_HIDDEN_CLASS, note)
        if status:
            note = (f"Last frame from {device_id} was "
                    f"{status['age_seconds']:.0f}s ago.")
        else:
            note = f"{device_id} has never posted a frame."
        return "", _MJPEG_HIDDEN, _EMPTY_VISIBLE_CLASS, note

    note = (f"Live from {device_id}: {status['fps']:.1f} fps, "
            f"{status['frame_bytes'] // 1024} kB per frame, "
            f"{status['frames']} frames received.")
    return (f"/video/api/stream/{device_id}", _MJPEG_VISIBLE,
            _EMPTY_HIDDEN_CLASS, note)


# ── clientside bindings ─────────────────────────────────────────────────

clientside_callback(
    ClientsideFunction(namespace="p4intercom", function_name="control"),
    Output("p4_call_state", "data"),
    Input("p4_call_btn", "n_clicks"),
    Input("p4_hangup_btn", "n_clicks"),
    State("p4_effective_device", "data"),
    State("p4_fps", "value"),
    State("p4_quality", "value"),
    prevent_initial_call=True,
)

clientside_callback(
    ClientsideFunction(namespace="p4intercom", function_name="toggles"),
    Output("p4_toggle_sink", "data"),
    Input("p4_mic_on", "value"),
    Input("p4_cam_on", "value"),
    prevent_initial_call=True,
)

clientside_callback(
    ClientsideFunction(namespace="p4intercom", function_name="listDevices"),
    Output("p4_boot", "disabled"),
    Input("p4_boot", "n_intervals"),
    prevent_initial_call=True,
)


# ── video library callbacks ─────────────────────────────────────────────

@callback(
    Output("p4_lib_cwd", "data"),
    Input({"type": "p4-lib-dir", "rel": ALL}, "n_clicks"),
    Input("p4_lib_up", "n_clicks"),
    Input("p4_lib_home", "n_clicks"),
    State("p4_lib_cwd", "data"),
    prevent_initial_call=True,
)
def browse_video_folder(dir_clicks, _up, _home, cwd):
    trigger = ctx.triggered_id
    if trigger == "p4_lib_home":
        return _default_cwd()
    if trigger == "p4_lib_up":
        here = _safe_dir(cwd)
        if os.path.realpath(here) == os.path.realpath(VIDEO_ROOT):
            return no_update
        return _rel_to_root(os.path.dirname(here))
    if isinstance(trigger, dict) and trigger.get("type") == "p4-lib-dir":
        # Re-rendering the list resets n_clicks to None, which fires this
        # callback again; ignore those phantom triggers.
        if not any(n for n in (dir_clicks or []) if n):
            return no_update
        return trigger["rel"]
    return no_update


@callback(
    Output("p4_lib_listing", "children"),
    Output("p4_lib_path", "children"),
    Input("p4_lib_cwd", "data"),
    Input("p4_lib_selected", "data"),
    Input("p4_lib_refresh", "n_clicks"),
)
def render_video_listing(cwd, selected, _refresh):
    here = _safe_dir(cwd)
    dirs, files = _list_dir(cwd)

    crumb = _rel_to_root(here) or os.path.basename(VIDEO_ROOT)
    rows = []

    for d in dirs:
        rows.append(dbc.Button([
            html.I(className="fas fa-folder me-2 sx-file-folder"),
            html.Span(d["name"]),
        ], id={"type": "p4-lib-dir", "rel": d["rel"]},
            color="link", className="sx-file-row text-start"))

    for f in files:
        is_sel = (selected or {}).get("rel") == f["rel"]
        rows.append(dbc.Button([
            html.I(className="fas fa-file-video me-2 sx-file-video"),
            html.Span(f["name"], className="sx-file-name"),
            html.Span(_human_size(f["size"]), className="sx-file-size ms-auto"),
            html.Span("" if f["playable"] else " not playable in browser",
                      className="sx-file-warn"),
        ], id={"type": "p4-lib-file", "rel": f["rel"]},
            color="link",
            disabled=not f["playable"],
            className="sx-file-row text-start" + (" sx-file-selected" if is_sel else "")))

    if not rows:
        rows = [html.Div("No sub-folders or video files here.",
                         className="text-muted small py-2")]

    return rows, crumb


@callback(
    Output("p4_lib_selected", "data"),
    Input({"type": "p4-lib-file", "rel": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def select_video_file(file_clicks):
    trigger = ctx.triggered_id
    if not isinstance(trigger, dict):
        return no_update
    if not any(n for n in (file_clicks or []) if n):
        return no_update
    rel = trigger["rel"]
    return {"rel": rel, "url": "/media/video/" + rel, "name": os.path.basename(rel)}


@callback(
    Output("p4_lib_send", "disabled"),
    Input("p4_lib_selected", "data"),
)
def enable_send(selected):
    return not bool(selected)


# Swapping the uplink source is a browser-side job: the JPEG pump reads
# whatever #p4-local-video is showing, so these hand it a file URL or put the
# camera back. Nothing about the call, the data channel or the firmware side
# changes.
clientside_callback(
    ClientsideFunction(namespace="p4intercom", function_name="playFile"),
    Output("p4_lib_status", "children"),
    Input("p4_lib_send", "n_clicks"),
    State("p4_lib_selected", "data"),
    # Enough to place the call itself when none is up, so the operator does
    # not have to remember to dial before picking a clip.
    State("p4_effective_device", "data"),
    State("p4_fps", "value"),
    State("p4_quality", "value"),
    prevent_initial_call=True,
)

clientside_callback(
    ClientsideFunction(namespace="p4intercom", function_name="playCamera"),
    Output("p4_lib_status", "children", allow_duplicate=True),
    Input("p4_lib_camera", "n_clicks"),
    prevent_initial_call=True,
)
