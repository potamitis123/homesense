import dash
from dash import html, callback, Output, Input, State, dcc
import dash_bootstrap_components as dbc
from datetime import datetime
from misc.load_parameters import parameters
from ast_src.housekeeper import get_last_backup_info, get_live_status




dash.register_page(
    __name__,
    path='/settings',
    title='Settings',
    name='Settings'
)



def home_server_communication_settings():
    """Built per page visit, not once at import.

    Every control on this page takes its value from parameters.json while the
    layout is being constructed. As module-level objects these blocks were
    constructed exactly once, when the app started, so they kept showing
    whatever was in the file at boot: you could save a setting, watch it take
    effect, leave the page, come back, and be shown the old value again -- and
    nothing on the page told you which of the two was actually in force.

    layout() calls these instead of embedding them, so Dash rebuilds them on
    every visit and the controls show what is really in the file. The bodies
    keep their original indentation because they sit inside brackets, where
    Python does not care.
    """
    return html.Div([
    dbc.Row([
        html.H4("Home Server & Communication Settings", className="mb-3")
    ]),
    dbc.Row([
        dbc.Col([
            dbc.Label("Communication Mode"),
            dbc.RadioItems(
                id="comm_mode",
                options=[
                    {"label": "Home Server Mode", "value": "Home Server"},
                    {"label": "Remote Server Mode (Reserved)", "value": "Remote Server", "disabled": True}
                ],
                value=parameters.get_parameters().get('communication', {}).get('mode', 'Home Server'),
                inline=True,
                className="mb-3"
            )
        ], width=12)
    ]),
    dbc.Row([
        dbc.Col([
            dbc.Label("Local MQTT Broker Address"),
            dbc.Input(id="local_mqtt_host", type="text", value=parameters.get_parameters().get('mqtt', {}).get('broker_host', 'localhost'))
        ], width=4),
        dbc.Col([
            dbc.Label("MQTT Port"),
            dbc.Input(id="local_mqtt_port", type="number", value=parameters.get_parameters().get('mqtt', {}).get('broker_port', 1883))
        ], width=2),
        dbc.Col([
            dbc.Label("API Listening Port"),
            dbc.Input(id="local_api_port", type="number", value=parameters.get_parameters().get('communication', {}).get('api_port', 8000))
        ], width=2),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Label("Broker Username"),
            dbc.Input(id="local_mqtt_user", type="text", value=parameters.get_parameters().get('mqtt', {}).get('username', ''))
        ], width=3),
        dbc.Col([
            dbc.Label("Broker Password"),
            dbc.Input(id="local_mqtt_pass", type="password", value=parameters.get_parameters().get('mqtt', {}).get('password', ''))
        ], width=3),
        dbc.Col([
            dbc.Label("Local Storage Path"),
            dbc.Input(id="local_storage_path", type="text", value=parameters.get_parameters().get('communication', {}).get('local_storage_path', 'assets/audios'))
        ], width=4),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Button("Save Communication Settings", id="save_comm_button", color="primary", n_clicks=0),
            html.Div(id="comm_save_status", className="mt-2 text-success")
        ])
    ]),
    dbc.Row([html.Hr()])
], className="mt-4")

def alert_communication_settings():
    """Rebuilt per visit -- see home_server_communication_settings."""
    return html.Div([
    dbc.Row([
        html.H4("Alert Communication (Brevo SMTP)", className="mb-3")
    ]),
    dbc.Row([
        dbc.Col([
            dbc.Label("SMTP Host"),
            dbc.Input(id="brevo_host", type="text", value=parameters.get_parameters().get('brevo', {}).get('BREVO_SMTP_HOST', ''))
        ], width=4),
        dbc.Col([
            dbc.Label("SMTP Port"),
            dbc.Input(id="brevo_port", type="number", value=parameters.get_parameters().get('brevo', {}).get('BREVO_SMTP_PORT', 587))
        ], width=2),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Label("SMTP User"),
            dbc.Input(id="brevo_user", type="text", value=parameters.get_parameters().get('brevo', {}).get('BREVO_SMTP_USER', ''))
        ], width=6),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Label("SMTP Key"),
            dbc.Input(id="brevo_key", type="password", value=parameters.get_parameters().get('brevo', {}).get('BREVO_SMTP_KEY', ''))
        ], width=6),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Label("From Email"),
            dbc.Input(id="brevo_from", type="email", value=parameters.get_parameters().get('brevo', {}).get('FROM_EMAIL', ''))
        ], width=6),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Label("Alert Recipient"),
            dbc.Input(id="brevo_to", type="email", value=parameters.get_parameters().get('brevo', {}).get('ALERT_TO', ''))
        ], width=6),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Button("Save Alert Settings", id="save_brevo_button", color="success", n_clicks=0),
            html.Div(id="brevo_save_status", className="mt-2 text-success")
        ])
    ]),
    dbc.Row([html.Hr()])
], className="mt-4")


# Speech recognition. Language is the one that matters day to day: forcing
# the wrong one does not fail loudly, it produces fluent nonsense in the
# forced language and stores it as if it were real.
def speech_settings():
    """Rebuilt per visit -- see home_server_communication_settings."""
    return html.Div([
    dbc.Row([
        html.H4("Speech Recognition", className="mb-3")
    ]),
    dbc.Row([
        dbc.Col([
            dbc.Label("Transcription language"),
            dbc.Select(
                id="asr_language",
                options=[
                    {"label": "Auto-detect", "value": "auto"},
                    {"label": "Greek (el)", "value": "el"},
                    {"label": "English (en)", "value": "en"},
                ],
                value=parameters.get_parameters().get('asr', {}).get('language', 'auto'),
            ),
            html.Small("One decoding pass either way. Auto-detect handles a "
                       "bilingual household; pick a language when the room is "
                       "reliably one or the other.",
                       className="text-muted"),
        ], width=6),
        dbc.Col([
            dbc.Label("Ignore speech shorter than (ms)"),
            dbc.Input(id="vad_min_speech", type="number", step=100,
                      value=parameters.get_parameters().get('vad', {}).get('min_speech_duration_ms', 1000)),
            html.Small("Short replies like \"yes\" or \"end discussion\" are "
                       "discarded above this. 1000 ms is a reasonable floor.",
                       className="text-muted"),
        ], width=6),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Button("Save Speech Settings", id="save_speech_button", color="success", n_clicks=0),
            html.Div(id="speech_save_status", className="mt-2 text-success")
        ])
    ]),
    dbc.Row([html.Hr()])
], className="mt-4")

def data_management_settings():
    """Rebuilt per visit -- see home_server_communication_settings.

    Holds the Incoming Audio Filter and Incoming Audio Storage controls,
    which is where the stale values were first noticed.
    """
    # Read here rather than at import, for the same reason as the settings
    # above. These two hid the problem better: the Interval callback refreshes
    # both a few seconds after the page opens, so a stale figure corrected
    # itself and only the first paint was wrong.
    backup_info = get_last_backup_info()
    live_status = get_live_status()
    return html.Div([
    dbc.Row([
        html.H4("Data Management (Auto-Backup)", className="mb-3")
    ]),
    dbc.Row([
        dbc.Col([
            dbc.Label("Last Backup Performed:"),
            dbc.Input(id="last_backup_time", type="text", value=backup_info.get("last_backup", "Never"), disabled=True)
        ], width=6),
        dbc.Col([
            dbc.Label("Files Archived (Last Run):"),
            dbc.Input(id="last_backup_count", type="text", value=str(backup_info.get("files_archived", 0)), disabled=True)
        ], width=6),
    ], className="mb-3"),
    # Two knobs, not one: the recordings are the disk cost (4,480 files, 838 MB)
    # while every event row together is 58 MB and is what the statistics,
    # heatmap and daily report read. Tying them together meant keeping two days
    # of audio also threw away all but two days of history.
    dbc.Row([
        dbc.Col([
            dbc.Label("Keep audio for (days)"),
            dbc.Input(id="audio_retention_days", type="number", min=1, max=3650, step=1,
                      value=parameters.get_parameters().get('housekeeper', {})
                      .get('audio_retention_days', 30)),
            html.Small(
                "Recordings older than this are zipped into the archive folder "
                "and deleted from disk. The events themselves stay.",
                className="text-muted"),
        ], width=4),
        dbc.Col([
            dbc.Label("Keep events for (days)"),
            dbc.Input(id="row_retention_days", type="number", min=1, max=3650, step=1,
                      value=parameters.get_parameters().get('housekeeper', {})
                      .get('row_retention_days', 365)),
            html.Small(
                "Rows older than this are exported to CSV inside the archive, "
                "then removed from the database. This is what the statistics "
                "and the daily report are built from -- lower it deliberately.",
                className="text-muted"),
        ], width=4),
        dbc.Col([
            dbc.Label("Archive folder"),
            dbc.Input(id="backup_dir", type="text",
                      value=parameters.get_parameters().get('housekeeper', {})
                      .get('backup_dir', 'ast_backup')),
            html.Small("Relative to the project root, or an absolute path.",
                       className="text-muted"),
        ], width=4),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Button("Save Data Management Settings", id="save_housekeeper_button",
                       color="primary", n_clicks=0),
            html.Div(id="housekeeper_save_status", className="mt-2"),
        ])
    ], className="mb-3"),

    html.Hr(),
    html.H5("Incoming Audio Filter", className="mt-3"),
    html.P("Every room has its own sentinel, so the same moment is often "
           "captured several times and the distant copies are not worth "
           "processing. A new recording is rejected if it closely matches "
           "another sentinel's capture of the same moment, or if it is simply "
           "too quiet.", className="text-muted small"),
    dbc.Row([
        dbc.Col([
            dbc.Label("Reject as duplicate above correlation"),
            dbc.Input(id="dedupe_min_correlation", type="number",
                      min=0, max=1, step=0.05,
                      value=parameters.get_parameters().get('dedupe', {})
                      .get('min_correlation', 0.25)),
            html.Small(
                "How alike two sentinels' recordings must be, 0 to 1, before "
                "they count as the same moment. The weaker copy is deleted and "
                "the clearest one kept. Lower catches more duplicates but "
                "risks discarding two people genuinely talking in two rooms.",
                className="text-muted"),
        ], width=6),
        dbc.Col([
            dbc.Label("Reject when SNR is below (dB)"),
            dbc.Input(id="dedupe_min_snr_db", type="number",
                      min=0, max=60, step=0.5,
                      value=parameters.get_parameters().get('dedupe', {})
                      .get('min_snr_db', 10.0)),
            html.Small(
                "Quiet, echoey captures are the far sentinel overhearing a "
                "room that already has its own. Rejected whether or not a "
                "duplicate exists. Applies only to recordings made after you "
                "press Save — nothing already stored is affected. Set 0 to "
                "turn this off.", className="text-muted"),
        ], width=6),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Label("Match window before processing (s)"),
            dbc.Input(id="dedupe_prefilter_window", type="number",
                      min=0.5, max=60, step=0.5,
                      value=parameters.get_parameters().get('dedupe', {})
                      .get('prefilter_window_seconds', 3.0)),
            html.Small(
                "How far apart two sentinels may start and still be compared, "
                "before anything is transcribed. Rejections here are reversible "
                "— the audio is set aside, not deleted. Two microphones on one "
                "utterance start within about a second of each other, so this "
                "does not need to be large.", className="text-muted"),
        ], width=6),
        dbc.Col([
            dbc.Label("Match window after processing (s)"),
            dbc.Input(id="dedupe_window_seconds", type="number",
                      min=0.5, max=120, step=0.5,
                      value=parameters.get_parameters().get('dedupe', {})
                      .get('window_seconds', 30.0)),
            html.Small(
                "The same comparison, run again once clips are transcribed. "
                "This one DELETES the losing row and its audio, and unlike the "
                "pass above it applies no length check — so a wide window here "
                "is the setting most likely to lose a recording for good. "
                "Lower it first if recordings are going missing.",
                className="text-muted"),
        ], width=6),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Button("Save Audio Filter Settings", id="save_dedupe_button",
                       color="primary", n_clicks=0),
            html.Div(id="dedupe_save_status", className="mt-2"),
        ])
    ], className="mb-3"),

    html.Hr(),
    html.H5("Incoming Audio Storage", className="mt-3"),
    html.P("Which recordings are kept on disk. This changes storage only: "
           "every capture is still deduplicated, classified by AST and, when "
           "it contains speech, transcribed and scored for emotion, so the "
           "event and its transcript appear in the dashboard either way. What "
           "a discarded recording loses is playback and the spectrogram, "
           "which are both read back from the audio.",
           className="text-muted small"),
    dbc.Row([
        dbc.Col([
            dbc.Label("Keep the audio for"),
            dcc.Dropdown(
                id="audio_storage_mode",
                options=[
                    {"label": "Store speech only", "value": "speech_only"},
                    {"label": "Store non-speech only", "value": "non_speech_only"},
                    {"label": "Store everything", "value": "everything"},
                    {"label": "Store nothing", "value": "nothing"},
                ],
                value=parameters.get_parameters().get('audio_storage', {})
                .get('mode', 'speech_only'),
                clearable=False,
            ),
            html.Small(
                "Speech is decided by AST's three highest-ranked labels for "
                "the clip -- the same test that decides whether to run "
                "transcription at all. Applies to recordings arriving after "
                "you press Save; nothing already on disk is deleted.",
                className="text-muted"),
        ], width=6),
        dbc.Col([
            dbc.Button("Save Storage Setting", id="save_audio_storage_button",
                       color="primary", n_clicks=0, className="mt-4"),
            html.Div(id="audio_storage_save_status", className="mt-2"),
        ], width=6),
    ], className="mb-3"),
    dbc.Row([
        dbc.Col([
            dbc.Label("Current Housekeeper Status:"),
            html.Div(id="housekeeper_status_text", children=live_status.get("status", "Idle")),
            dbc.Progress(id="housekeeper_progress_bar", value=live_status.get("progress", 0), className="mt-2", animated=True, striped=True)
        ])
    ], className="mb-3"),
    dbc.Row([html.Hr()])
], className="mt-4")

def layout(**kwargs):
    custom_childer=[
        dcc.Interval(id='housekeeper_interval', interval=5000, n_intervals=0),
        dbc.Row([
                html.H3('Settings', className="mb-4")
        ]   ),
        dbc.Row([
            home_server_communication_settings(),
        ]),
        dbc.Row([
            alert_communication_settings(),
        ]),
        dbc.Row([
            speech_settings(),
        ]),
        dbc.Row([
            data_management_settings(),
        ]),
    ]
    return html.Div(id='page_settings', children=custom_childer, style={"width": "100%"})

@callback(
    Output("housekeeper_status_text", "children"),
    Output("housekeeper_progress_bar", "value"),
    Output("last_backup_time", "value"),
    Output("last_backup_count", "value"),
    Input("housekeeper_interval", "n_intervals")
)
def update_housekeeper_progress(n):
    live = get_live_status()
    last = get_last_backup_info()
    status_text = f"{live.get('status', 'Idle')} (at {live.get('timestamp', '')})"
    return status_text, live.get("progress", 0), last.get("last_backup", "Never"), str(last.get("files_archived", 0))

@callback(
    Output("dedupe_save_status", "children"),
    Input("save_dedupe_button", "n_clicks"),
    State("dedupe_min_correlation", "value"),
    State("dedupe_min_snr_db", "value"),
    State("dedupe_prefilter_window", "value"),
    State("dedupe_window_seconds", "value"),
    prevent_initial_call=True,
)
def save_dedupe_settings(n_clicks, correlation, min_snr, prefilter_window,
                         window_seconds):
    """All four settings take effect on the dedupe thread's next pass, ~60 s.

    The SNR threshold is stamped with the moment it was saved. The filter
    refuses to judge anything recorded before that stamp, so raising or
    lowering the value can never reach back and delete recordings made under
    the previous setting -- which, at these volumes, would be thousands of
    rows from one careless keystroke.
    """
    if not n_clicks:
        return ""
    try:
        correlation = float(correlation)
        min_snr = float(min_snr)
        prefilter_window = float(prefilter_window)
        window_seconds = float(window_seconds)
    except (TypeError, ValueError):
        return html.Span("Every value must be a number.",
                         className="text-danger")
    if not 0.0 <= correlation <= 1.0:
        return html.Span("Correlation must be between 0 and 1.",
                         className="text-danger")
    if min_snr < 0:
        return html.Span("SNR threshold cannot be negative. Use 0 to disable.",
                         className="text-danger")
    if prefilter_window <= 0 or window_seconds <= 0:
        return html.Span("Both match windows must be greater than zero.",
                         className="text-danger")

    params = parameters.get_parameters()
    params.setdefault('dedupe', {})
    previous_snr = params['dedupe'].get('min_snr_db')
    params['dedupe']['min_correlation'] = correlation
    params['dedupe']['min_snr_db'] = min_snr
    params['dedupe']['prefilter_window_seconds'] = prefilter_window
    params['dedupe']['window_seconds'] = window_seconds
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # Re-stamped only when the value actually moves, so simply re-saving the
    # page does not quietly extend the rule over recordings it had spared.
    if previous_snr is None or float(previous_snr) != min_snr:
        params['dedupe']['min_snr_effective_from'] = stamp
    effective = params['dedupe'].get('min_snr_effective_from', stamp)
    parameters.set_parameters(params)
    parameters.save_parameters()

    if min_snr <= 0:
        snr_note = "The SNR filter is off; only duplicates are rejected."
    else:
        snr_note = (f"Recordings below {min_snr:g} dB made after {effective} "
                    f"will be rejected. Anything recorded earlier is untouched.")
    return html.Div([
        html.Span("Saved. ", className="text-success fw-bold"),
        html.Span(f"Duplicates rejected above correlation {correlation:g}, "
                  f"matched within {prefilter_window:g}s before processing and "
                  f"{window_seconds:g}s after. {snr_note} Active within a "
                  f"minute — no restart needed."),
    ])


@callback(
    Output("audio_storage_save_status", "children"),
    Input("save_audio_storage_button", "n_clicks"),
    State("audio_storage_mode", "value"),
    prevent_initial_call=True,
)
def save_audio_storage_settings(n_clicks, mode):
    """Takes effect on the next capture -- no restart, no dedupe pass to wait for.

    Storage only: the recogniser chain is unchanged, so a discarded clip still
    produces its row, its labels and its transcript. Nothing already written to
    disk is touched, which is why the wording below is careful to say "from now
    on" -- a setting that silently deleted a month of recordings would be a
    very expensive misreading.
    """
    if not n_clicks:
        return ""
    labels = {
        'non_speech_only': ("Storing non-speech recordings only. Clips where "
                            "AST ranks speech in its top three are "
                            "transcribed, then discarded."),
        'speech_only': ("Storing speech recordings only. Clips with no speech "
                        "in AST's top three labels are classified, then "
                        "discarded."),
        'everything': "Storing every recording, as before.",
        'nothing': ("Storing no recordings. Everything is still classified "
                    "and transcribed, but no audio is written to disk."),
    }
    if mode not in labels:
        return html.Span("Pick one of the four storage options.",
                         className="text-danger")

    params = parameters.get_parameters()
    params.setdefault('audio_storage', {})
    params['audio_storage']['mode'] = mode
    parameters.set_parameters(params)
    parameters.save_parameters()
    return html.Div([
        html.Span("Saved. ", className="text-success fw-bold"),
        html.Span(labels[mode] + " Applies from the next recording onward; "
                  "audio already on disk is left alone."),
    ])


@callback(
    Output("housekeeper_save_status", "children"),
    Input("save_housekeeper_button", "n_clicks"),
    State("audio_retention_days", "value"),
    State("row_retention_days", "value"),
    State("backup_dir", "value"),
    prevent_initial_call=True,
)
def save_housekeeper_settings(n_clicks, audio_days, row_days, folder):
    """Retention is destructive, so the message says what it will actually do."""
    if not n_clicks:
        return ""
    try:
        audio_days, row_days = int(audio_days), int(row_days)
    except (TypeError, ValueError):
        return html.Span("Both retentions must be a whole number of days.",
                         className="text-danger")
    if audio_days < 1 or row_days < 1:
        return html.Span("Retention must be at least 1 day.", className="text-danger")

    params = parameters.get_parameters()
    params.setdefault('housekeeper', {})
    params['housekeeper']['audio_retention_days'] = audio_days
    params['housekeeper']['row_retention_days'] = row_days
    params['housekeeper']['backup_dir'] = (folder or 'ast_backup').strip()
    parameters.set_parameters(params)
    parameters.save_parameters()

    # Say how much is actually affected rather than just "saved": the
    # housekeeper deletes on its next hourly tick, and the count is the
    # number the operator needs before that happens, not after.
    notes = []
    try:
        import sqlite3, os, glob
        from datetime import timedelta
        from ast_src.housekeeper import get_most_recent_recording_time, AUDIO_DIR
        reference = get_most_recent_recording_time()
        if reference:
            row_cut = reference - timedelta(days=row_days)
            audio_cut = reference - timedelta(days=audio_days)

            db_path = os.path.abspath(os.path.join(
                os.path.dirname(__file__), '..', '..',
                parameters.get_parameters()['sqlite']['db_name']))
            conn = sqlite3.connect(db_path)
            older = conn.execute(
                "SELECT COUNT(*) FROM ast_table "
                "WHERE date LIKE '____-__-__ __:__:__%' AND date < ?",
                (row_cut.strftime('%Y-%m-%d %H:%M:%S'),)).fetchone()[0]
            conn.close()

            cut_ts = audio_cut.timestamp()
            recordings = 0
            for path in glob.glob(os.path.join(AUDIO_DIR, '*')):
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cut_ts:
                        recordings += 1
                except OSError:
                    pass

            if recordings:
                notes.append(f"{recordings:,} recordings will be archived and "
                             f"removed from disk")
            if older:
                notes.append(f"{older:,} events will be exported to CSV and "
                             f"deleted from the database")
    except Exception as exc:
        print(f"[settings] could not count affected items: {exc}")

    if notes:
        return html.Span("Saved. On the next housekeeper run, " + "; ".join(notes) + ".",
                         className="text-warning")
    return html.Span(
        f"Saved. Keeping {audio_days} days of audio and {row_days} days of events; "
        f"nothing is currently older than either.", className="text-success")


@callback(
    Output("brevo_save_status", "children"),
    Input("save_brevo_button", "n_clicks"),
    State("brevo_host", "value"),
    State("brevo_port", "value"),
    State("brevo_user", "value"),
    State("brevo_key", "value"),
    State("brevo_from", "value"),
    State("brevo_to", "value"),
    prevent_initial_call=True
)
def save_brevo_settings(n_clicks, host, port, user, key, from_email, to_email):
    if n_clicks > 0:
        params = parameters.get_parameters()
        if 'brevo' not in params:
            params['brevo'] = {}

        params['brevo']['BREVO_SMTP_HOST'] = host
        params['brevo']['BREVO_SMTP_PORT'] = port
        params['brevo']['BREVO_SMTP_USER'] = user
        params['brevo']['BREVO_SMTP_KEY'] = key
        params['brevo']['FROM_EMAIL'] = from_email
        params['brevo']['ALERT_TO'] = to_email

        parameters.set_parameters(params)
        parameters.save_parameters()
        return "Settings saved successfully!"
    return ""

@callback(
    Output("comm_save_status", "children"),
    Input("save_comm_button", "n_clicks"),
    State("comm_mode", "value"),
    State("local_mqtt_host", "value"),
    State("local_mqtt_port", "value"),
    State("local_api_port", "value"),
    State("local_mqtt_user", "value"),
    State("local_mqtt_pass", "value"),
    State("local_storage_path", "value"),
    prevent_initial_call=True
)
def save_communication_settings(n_clicks, mode, host, port, api_port, user, password, storage_path):
    if n_clicks > 0:
        params = parameters.get_parameters()
        if 'communication' not in params:
            params['communication'] = {}
        if 'mqtt' not in params:
            params['mqtt'] = {}

        params['communication']['mode'] = mode
        params['communication']['api_port'] = int(api_port) if api_port else 8000
        params['communication']['local_storage_path'] = storage_path

        params['mqtt']['broker_host'] = host
        params['mqtt']['broker_port'] = int(port) if port else 1883
        params['mqtt']['username'] = user
        params['mqtt']['password'] = password
        params['mqtt']['save_sound_path'] = storage_path

        parameters.set_parameters(params)
        parameters.save_parameters()
        return "Communication settings saved successfully!"
    return ""

@callback(
    Output("speech_save_status", "children"),
    Input("save_speech_button", "n_clicks"),
    State("asr_language", "value"),
    State("vad_min_speech", "value"),
    prevent_initial_call=True,
)
def save_speech_settings(n_clicks, language, min_speech_ms):
    try:
        params = parameters.get_parameters()
        params.setdefault('asr', {})['language'] = (language or 'auto')
        params.setdefault('vad', {})['min_speech_duration_ms'] = int(min_speech_ms or 1000)
        # save_parameters() takes no argument -- it writes whatever is
        # currently held, so the dict has to be handed over first. Same
        # two-step the Brevo save above uses.
        parameters.set_parameters(params)
        parameters.save_parameters()
        # main.py reads these per file, but it caches the enabled flags at
        # import, so a running pipeline picks up language and duration
        # immediately and nothing else.
        return f"Saved. Language: {language}, minimum speech: {min_speech_ms} ms."
    except Exception as e:
        return f"Could not save: {e}"
