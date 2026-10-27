"""The 'Commands' Dash tab -- shows the last voice command heard and the
live countdown until processing resumes, if a mute is currently active.

Registered here (not physically inside ast_dash/pages/) via dash.register_page;
ast_dash/pages/commands.py just imports this module so Dash's page
auto-discovery (which only scans ast_dash/pages/) picks it up at startup.
"""

from datetime import datetime

import dash
from dash import html, dcc, callback, Output, Input
import dash_bootstrap_components as dbc

from Voice_commands.state import get_state, mute_remaining_seconds


def _fmt_time(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return iso_str


def _fmt_remaining(seconds):
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def layout(**kwargs):
    return dbc.Container([
        dbc.Row([
            dbc.Col([
                html.H2("Voice Commands", className="text-primary mb-4"),
                html.P(
                    "Say \"Kokora stop\" (or \"Κόκκορα σταμάτα\" / \"Κόκκορα μη γράφεις\") "
                    "to pause processing for 1 hour — e.g. when you have guests. "
                    "Only the most recent command is kept.",
                    className="text-muted",
                ),
            ], width=12),
        ]),

        dcc.Interval(id='commands-refresh', interval=1000, n_intervals=0),

        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader("Status", className="bg-light font-weight-bold"),
                    dbc.CardBody(id='commands-status-body'),
                ], className="shadow-sm mb-4"),

                dbc.Card([
                    dbc.CardHeader("Last Command", className="bg-light font-weight-bold"),
                    dbc.CardBody(id='commands-last-body'),
                ], className="shadow-sm"),
            ], width=12, lg=8),
        ]),
    ], fluid=True, className="p-4")


# layout is passed explicitly (not found by Dash's default module-name
# lookup) because this module is imported indirectly via the
# ast_dash/pages/commands.py loader, not directly by Dash's page scanner.
dash.register_page(
    __name__,
    path='/commands',
    title='Commands',
    name='Commands',
    layout=layout,
)


@callback(
    Output('commands-status-body', 'children'),
    Output('commands-last-body', 'children'),
    Input('commands-refresh', 'n_intervals'),
)
def refresh_commands_tab(_n):
    state = get_state()
    remaining = mute_remaining_seconds()

    if remaining is not None:
        status_body = html.Div([
            dbc.Badge("MUTED", color="danger", className="me-2"),
            html.Span("Processing paused. Resumes in "),
            html.Strong(_fmt_remaining(remaining)),
            html.Span(f" (at {_fmt_time(state['muted_until'])})."),
        ])
    else:
        status_body = html.Div([
            dbc.Badge("ACTIVE", color="success", className="me-2"),
            html.Span("Processing normally."),
        ])

    if state.get('last_command_text'):
        last_body = html.Div([
            html.Div([html.Strong("Heard: "), html.Span(f"“{state['last_command_text']}”")]),
            html.Div([html.Strong("Device: "), html.Span(state.get('last_command_device') or 'unknown')]),
            html.Div([html.Strong("At: "), html.Span(_fmt_time(state.get('last_command_time')) or 'unknown')]),
        ])
    else:
        last_body = html.Div("No commands received yet.", className="text-muted")

    return status_body, last_body
