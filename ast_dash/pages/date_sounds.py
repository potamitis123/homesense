import dash
from dash import dcc, html, callback, dash_table, ctx
from dash.dependencies import Input, Output, State
import plotly.graph_objects as go
from datetime import datetime
import numpy as np
import pandas as pd
import sqlite3
import os
import re
from misc.load_parameters import parameters
import dash_bootstrap_components as dbc

dash.register_page(
    __name__,
    path='/daily_events',
    title='Daily Events',
    name='Daily Events'
)


def _db_path():
    return os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', '..',
        parameters.get_parameters()['sqlite']['db_name']
    ))


def _is_valid_device_name(name):
    if not name:
        return False
    name_lower = name.lower()
    if name_lower in ['manual_upload', 'unknown', 'test', 'ex', 'all', 'none', 'user', 'users', 'group', 'device_name']:
        return False
    # Names that were never a sentinel: "box3" and "esp32s3-" are truncations
    # of real device ids, and user2 is a stray. Hidden here as they are in the
    # dashboard's Sentinels panel.
    #
    # p4-sentinel-1 is NOT hidden, though the dashboard hides it. That panel
    # says which sentinels are up now, where a retired one is only noise; this
    # page browses history, and hiding a device here would make the days it
    # recorded unreachable. It is a short history -- 20 events on 3 August --
    # but the point is the principle, since the same applies to any sentinel
    # retired later with months of recordings behind it.
    if name_lower in ['box3', 'esp32s3-', 'p4-intercom-01', 'user2']:
        return False
    if re.match(r'^\d+$', name_lower):
        return False
    return True


def get_available_sentinels():
    """Sentinel names from remote_devices and distinct ast_table.device_name (Dashboard Sentinels sources)."""
    names = set()
    try:
        conn = sqlite3.connect(_db_path())
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT device_name FROM remote_devices")
            for (name,) in cursor.fetchall():
                if _is_valid_device_name(name):
                    names.add(name)
        except Exception as e:
            print(f"Error fetching remote_devices for Daily Events: {e}")
        try:
            cursor.execute("SELECT DISTINCT device_name FROM ast_table")
            for (name,) in cursor.fetchall():
                if _is_valid_device_name(name):
                    names.add(name)
        except Exception as e:
            print(f"Error fetching historical devices for Daily Events: {e}")
        conn.close()
    except Exception as e:
        print(f"DB Error listing sentinels in Daily Events: {e}")
    return sorted(names)


def _sentinel_label(name):
    """"WC esp32s3-3" -- the room first, as the Device column reads.

    A device name alone does not say where it is, and the household is
    renaming sentinels by room anyway. Underscores become spaces for the same
    reason the grid renderer does it: they exist so the value survives as an
    identifier, not because anyone wants to read them.

    Presentation only. The option's VALUE stays the bare device name, so
    everything downstream filters exactly as before.
    """
    try:
        from misc.rooms import room_for
        room = room_for(name)
    except Exception:
        room = None
    if not room or str(room).strip().lower() in ('', 'none', 'unknown'):
        return name
    return f"{str(room).replace('_', ' ')} {name}"


def get_sentinel_dropdown_options():
    options = [{'label': 'All', 'value': 'all'}]
    options.extend({'label': _sentinel_label(name), 'value': name}
                   for name in get_available_sentinels())
    return options


# How many of AST's ranked labels a detection may come from. Two is what this
# page has always counted -- sound3 was never read -- so it stays the default
# and the view does not change under anyone who does not touch the control.
DEFAULT_RANKS = 2


def get_db_data_for_date(selected_date, device='all', ranks=DEFAULT_RANKS):
    """Counts per class and hour for one day.

    `ranks` is how far down AST's ranking a label still counts as a detection.
    Rank 1 is what the classifier is most confident about, and each further
    rank is a weaker guess: the same clip can be Speech(0.89) at rank 1 and
    Silence(0.01) at rank 3. Counting all three makes the picture busier and
    less certain; counting only the first makes it sparse and confident.
    """
    if selected_date is None:
        selected_date_str = datetime.now().strftime('%Y-%m-%d')
    else:
        selected_date_str = selected_date

    try:
        ranks = max(1, min(3, int(ranks)))
    except (TypeError, ValueError):
        ranks = DEFAULT_RANKS
    columns = [f"sound{n}" for n in range(1, ranks + 1)]

    try:
        conn = sqlite3.connect(_db_path())
        query = (
            f"SELECT {', '.join(columns)}, date, device_name FROM ast_table "
            f"WHERE date LIKE '{selected_date_str}%'"
        )
        df = pd.read_sql_query(query, conn)
        conn.close()
    except Exception as e:
        print(f"DB Error in Daily Events: {e}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    # Filter by sentinel before expanding sounds (All = current behavior)
    if device and device != 'all':
        df = df[df['device_name'] == device]
        if df.empty:
            return pd.DataFrame()

    # Each ranked label becomes its own row, so one clip can contribute a
    # detection to several classes -- which is what makes the choice of rank
    # depth change the picture rather than just the totals.
    df_combined = pd.concat(
        [df[[col, 'date']].rename(columns={col: 'sound'}) for col in columns]
    ).dropna()
    # Exclude literal "None" and "Silence" class labels from counts/visualization
    sound_lower = df_combined['sound'].astype(str).str.strip().str.lower()
    df_combined = df_combined[~sound_lower.isin(['none', 'silence'])]
    if df_combined.empty:
        return pd.DataFrame()

    df_combined['hour'] = pd.to_datetime(df_combined['date']).dt.hour
    # Group by sound and hour
    df_counts = df_combined.groupby(['sound', 'hour']).size().reset_index(name='count')
    # Rename 'sound' back to 'sound1' for compatibility with create_pretty_heatmap
    df_counts = df_counts.rename(columns={'sound': 'sound1'})
    return df_counts


# The cap, the row rendering and the player live in ast_dash/event_popup so
# this page and Statistics answer the same question the same way.
from ast_dash.event_popup import (EVENT_LIMIT as CELL_EVENT_LIMIT, fetch,
                                  rank_clause, modal, modal_body)


def get_events_for_cell(selected_date, sound, hour, device='all',
                        ranks=DEFAULT_RANKS, limit=CELL_EVENT_LIMIT):
    """The clips a single heatmap cell counted.

    Repeats the heatmap's own filters exactly -- date, sentinel and rank depth
    -- because a list that does not match the number in the cell is worse than
    no list.

    Returns (rows, total) where total is the real count before the cap.
    """
    day = selected_date or datetime.now().strftime('%Y-%m-%d')
    try:
        ranks = max(1, min(3, int(ranks)))
    except (TypeError, ValueError):
        ranks = DEFAULT_RANKS

    # substr rather than strftime: the column is text and this format is what
    # the pipeline writes, so it does not depend on SQLite parsing the date.
    where = ["date LIKE ?", "substr(date, 12, 2) = ?", rank_clause(ranks)]
    args = [f"{day}%", f"{int(hour):02d}"] + [sound] * ranks
    if device and device != 'all':
        where.append("device_name = ?")
        args.append(device)
    return fetch(" AND ".join(where), args, limit)


def create_pretty_heatmap(df, selected_date, device='all'):
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No data found for this date", showarrow=False, font=dict(size=20))
        fig.update_layout(xaxis=dict(visible=False), yaxis=dict(visible=False))
        return fig

    # Create a full grid of 24 hours
    hours = list(range(24))
    all_sounds = sorted(df['sound1'].unique())

    # Pivot for heatmap
    pivot_df = df.pivot(index='sound1', columns='hour', values='count').fillna(0)

    # Ensure all hours are present
    for h in hours:
        if h not in pivot_df.columns:
            pivot_df[h] = 0
    pivot_df = pivot_df[hours]  # Sort columns

    # The per-cell counts are drawn by the Heatmap trace itself rather than as
    # one add_annotation() call per cell. Each add_annotation revalidates the
    # whole layout, so that loop was quadratic: on a busy day (90 classes, 520
    # populated cells) it took ~51 s and blocked the Dash worker for the whole
    # of it, which is what made the tab look hung. texttemplate builds the same
    # labels in one pass, in milliseconds.
    counts = pivot_df.values
    labels = np.where(counts > 0, counts.astype(int).astype(str), "")

    fig = go.Figure(data=go.Heatmap(
        z=counts,
        x=[f"{h:02d}:00" for h in hours],
        y=pivot_df.index,
        colorscale='Viridis',
        showscale=True,
        text=labels,
        texttemplate='%{text}',
        # Left unset so Plotly picks black or white per cell against the
        # Viridis background -- the same contrast rule the old loop applied
        # by hand, minus the hand.
        textfont=dict(size=11),
        # The grid between cells is a 1px gap with the (black) plot background
        # showing through, not drawn shapes: one shape per line would be the
        # same per-object layout revalidation that made add_annotation quadratic.
        xgap=1,
        ygap=1,
        hovertemplate='Class: %{y}<br>Hour: %{x}<br>Count: %{z}<extra></extra>'
    ))

    sentinel_label = "All Sentinels" if not device or device == 'all' else device
    fig.update_layout(
        title=f"Audio Events counts vs hours - {selected_date} ({sentinel_label})",
        xaxis_title="Hour of Day",
        yaxis_title="Audio Class",
        height=max(400, len(all_sounds) * 30),
        margin=dict(l=150, r=20, t=50, b=50),
        paper_bgcolor='rgba(0,0,0,0)',
        # Not transparent: this is what shows through the xgap/ygap and so is
        # the grid colour itself.
        plot_bgcolor='black',
    )

    return fig


layout = dbc.Container([
    dbc.Row([
        dbc.Col(html.H2("Daily Events Heatmap", className="text-primary mb-4"), width=12)
    ]),

    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardHeader("Select Date", className="bg-light font-weight-bold"),
                dbc.CardBody([
                    html.Div([
                        html.Label("Date", className="me-2 mb-0"),
                        dcc.DatePickerSingle(
                            id='daily-date-picker',
                            date=datetime.now().date(),
                            display_format='YYYY-MM-DD',
                            className="mb-0"
                        ),
                    ], className="d-flex align-items-center me-4"),
                    html.Div([
                        html.Label("Sentinel", className="me-2 mb-0"),
                        dcc.Dropdown(
                            id='daily-sentinel-filter',
                            options=get_sentinel_dropdown_options(),
                            value='all',
                            clearable=False,
                            style={'minWidth': '220px'},
                        ),
                    ], className="d-flex align-items-center me-3", style={'flex': '0 1 280px'}),
                    html.Div([
                        html.Label("Labels", className="me-2 mb-0"),
                        dcc.Dropdown(
                            id='daily-rank-filter',
                            options=[
                                {'label': '1st label in rank only', 'value': 1},
                                {'label': '1st and 2nd rank labels', 'value': 2},
                                {'label': 'all 3 first-rank labels', 'value': 3},
                            ],
                            value=DEFAULT_RANKS,
                            clearable=False,
                            style={'minWidth': '210px'},
                        ),
                    ], className="d-flex align-items-center me-3",
                       style={'flex': '0 1 300px'}),
                    dbc.Button(
                        [html.I(className="fas fa-sync-alt me-2"), "Refresh"],
                        id='refresh-daily',
                        color="primary",
                        className="ms-2"
                    )
                ], className="d-flex align-items-center flex-wrap gap-2")
            ], className="shadow-sm mb-4")
        ], width=12)
    ]),

    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardBody([
                    dcc.Graph(id='daily-events-graph', config={'displayModeBar': False}),
                    html.Div("Click a cell to hear the events behind it.",
                             className="text-muted small mt-2"),
                ])
            ], className="shadow-sm")
        ], width=12)
    ]),

    modal('daily-cell'),
], fluid=True, className="p-4")


@callback(
    Output('daily-sentinel-filter', 'options'),
    Input('refresh-daily', 'n_clicks'),
)
def refresh_sentinel_options(n_clicks):
    return get_sentinel_dropdown_options()


@callback(
    Output('daily-events-graph', 'figure'),
    Input('daily-date-picker', 'date'),
    Input('daily-sentinel-filter', 'value'),
    Input('daily-rank-filter', 'value'),
    Input('refresh-daily', 'n_clicks')
)
def update_daily_graph(selected_date, selected_sentinel, ranks, n_clicks):
    device = selected_sentinel or 'all'
    df = get_db_data_for_date(selected_date, device=device,
                              ranks=ranks or DEFAULT_RANKS)
    return create_pretty_heatmap(df, selected_date, device=device)


@callback(
    Output('daily-cell-modal', 'is_open'),
    Output('daily-cell-title', 'children'),
    Output('daily-cell-body', 'children'),
    Input('daily-events-graph', 'clickData'),
    Input('daily-cell-close', 'n_clicks'),
    State('daily-date-picker', 'date'),
    State('daily-sentinel-filter', 'value'),
    State('daily-rank-filter', 'value'),
    prevent_initial_call=True,
)
def show_cell_events(click_data, _close, selected_date, selected_sentinel, ranks):
    """Open the clips behind a clicked cell; the Close button shuts it again."""
    if ctx.triggered_id == 'daily-cell-close' or not click_data:
        return False, dash.no_update, dash.no_update

    try:
        point = click_data['points'][0]
        sound = point['y']
        hour = int(str(point['x'])[:2])
    except (KeyError, IndexError, TypeError, ValueError):
        return False, dash.no_update, dash.no_update

    device = selected_sentinel or 'all'
    ranks = ranks or DEFAULT_RANKS
    rows, total = get_events_for_cell(selected_date, sound, hour,
                                      device=device, ranks=ranks)

    where = _sentinel_label(device) if device != 'all' else "all sentinels"
    title = (f"{sound} · {hour:02d}:00–{hour:02d}:59 · {selected_date} · {where}")
    return True, title, modal_body(
        rows, total, sound,
        empty_note="No events found for this cell. If the heatmap shows a count "
                   "here, the rank-depth selection has changed since it was drawn.")
