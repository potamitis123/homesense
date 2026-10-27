import dash
from dash.dependencies import Input, Output
import plotly.express as px
import plotly.graph_objects as go
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import numpy as np
from dash import  html,callback,get_app,Output,Input,State,dcc
import dash_bootstrap_components as dbc
import os, sys
from ast_dash.sql_to_pandas import SQL_to_Pandas
from misc import *

app=get_app()

dash.register_page(
    __name__,
    path='/statistics',
    title='Statistics',
    name='Statistics'
)

# Dark theme tokens, kept in sync with assets/sentinel_dark.css :root. These
# figures previously rendered on Plotly's default white paper/plot background
# (two set it explicitly, the other three fell back to it), while the page's
# CSS colours axis/title text light for a dark background -- so text and
# background were fighting each other and titles were nearly invisible.
# Setting these directly in Python rather than relying on CSS alone: polar
# axis lines/grid and per-figure font colour aren't reliably reachable from
# outside the SVG, so the fix has to happen where the figure is built.
SX_TEXT_DIM = "#c3cee1"
SX_MUTED = "#9aa8c0"
SX_GRID = "rgba(255,255,255,0.08)"
SX_BORDER = "rgba(255,255,255,0.16)"
SX_TRANSPARENT = "rgba(0,0,0,0)"

# Three charts here count the same events the Daily Events heatmap does, so
# clicking one opens the same list, rendered by the same code.
from ast_dash.event_popup import (fetch, rank_clause, modal, modal_body,
                                  count_detections)

# Every chart on this page expands sound1 and sound2 into detections, so the
# popup has to match that or its count will not be the number that was clicked.
STAT_RANKS = 2


def _range(str_date_from, str_date_to):
    """The from/to the charts are actually drawn over.

    load_data() silently falls back to the last 7 days when the picker is
    empty or inverted, so the popup has to apply the same rule or it would
    query a different window than the chart it was launched from.
    """
    if (str_date_from is None or str_date_to is None
            or str_to_date(str_date_from) >= str_to_date(str_date_to)):
        return (date_to_str(get_date_before(get_now_date(), 7)),
                date_to_str(get_now_date()))
    return (date_to_str(get_date_00(str_date_from)),
            date_to_str(get_date_99(str_date_to)))


def load_data(str_date_from=None, str_date_to=None):
    sql_to_pandas=SQL_to_Pandas()
    if (str_date_from is None or  str_date_to is None) or str_to_date(str_date_from)>=str_to_date(str_date_to):
        dt_date_from = get_date_before(get_now_date(), 7) # Default 7 days
        dt_date_to = get_now_date()
        str_date_from = date_to_str(dt_date_from)
        str_date_to = date_to_str(dt_date_to)
    else:
        dt_date_from = get_date_00(str_date_from)
        dt_date_to = get_date_99(str_date_to) 
        str_date_from = date_to_str(dt_date_from)
        str_date_to = date_to_str(dt_date_to)
    
    df=sql_to_pandas.get_date_from_to(str_date_from,str_date_to)
    del(sql_to_pandas)
    df['date'] = pd.to_datetime(df['date'])
    return df, str_date_from, str_date_to

def layout(**kwargs):
    return dbc.Container([
        dcc.Location(id='url_statistic', refresh=False),
        dbc.Row([
            dbc.Col(html.H2("Audio Detection Statistics", className="text-primary mb-4"), width=12)
        ]),
        
        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader("Controls", className="bg-light font-weight-bold"),
                    dbc.CardBody([
                        html.Label("Date Range", className="small text-muted mb-1"),
                        dcc.DatePickerRange(
                            id='date_picker',
                            display_format='YYYY/MM/DD',
                            className="w-100 mb-3"
                        ),
                        html.Label("Top Classes to Show", id='label_sounds_slider', className="small text-muted mb-1"),
                        dcc.Slider(
                            id='sounds_slider',
                            min=5, max=50, value=10, step=5,
                            marks={i: str(i) for i in range(5, 51, 10)},
                            className="mb-4"
                        ),
                        html.Label("Filter by Class", className="small text-muted mb-1"),
                        dcc.Dropdown(
                            id='sounds_selector',
                            options=[{'label': 'All', 'value': 'All'}],
                            value='All',
                            placeholder="Search class...",
                            className="mb-2"
                        )
                    ])
                ], className="shadow-sm border-0 sticky-top", style={"top": "100px"})
            ], width=3),
            
            dbc.Col([
                dbc.Row([
                    dbc.Col([
                        dbc.Card([
                            dbc.CardBody([
                                dcc.Graph(id='top_species_chart', config={'displayModeBar': False})
                            ])
                        ], className="shadow-sm border-0 h-100")
                    ], width=6),
                    dbc.Col([
                        dbc.Card([
                            dbc.CardBody([
                                dcc.Graph(id='daily_pattern', config={'displayModeBar': False})
                            ])
                        ], className="shadow-sm border-0 h-100")
                    ], width=6),
                ], className="g-4 mb-4"),
                
                dbc.Row([
                    dbc.Col([
                        dbc.Card([
                            dbc.CardBody([
                                dcc.Graph(id='timeline', config={'responsive': True})
                            ])
                        ], className="shadow-sm border-0")
                    ], width=12)
                ], className="mb-4"),

                dbc.Row([
                    dbc.Col([
                        dbc.Card([
                            dbc.CardBody([
                                dcc.Graph(id='emotion_radar_chart', config={'displayModeBar': False})
                            ])
                        ], className="shadow-sm border-0 h-100")
                    ], width=6),
                    dbc.Col([
                        dbc.Card([
                            dbc.CardBody([
                                dcc.Graph(id='sunburst_chart', config={'displayModeBar': False})
                            ])
                        ], className="shadow-sm border-0 h-100")
                    ], width=6),
                ])
            ], width=9)
        ], className="g-4"),
        modal('stats-cell'),
    ], fluid=True, className="p-4")

@callback(
     Output('top_species_chart', 'figure'),
     Output('daily_pattern', 'figure'),
     Output('timeline', 'figure'),
     Output('emotion_radar_chart', 'figure'),
     Output('sunburst_chart', 'figure'),
     Output('sounds_selector', 'options'),
     Output('label_sounds_slider','children'),
     Input('date_picker', 'start_date'),
     Input('date_picker', 'end_date'),
     Input('sounds_selector', 'value'),
     Input('sounds_slider', 'value'),
)
def update_graphs(start_date, end_date, selected_sound, num_sounds):
    df, _, _ = load_data(start_date, end_date)
    
    def _empty_fig(message):
        fig = go.Figure()
        fig.add_annotation(text=message, showarrow=False,
                           font=dict(size=16, color=SX_MUTED))
        fig.update_layout(paper_bgcolor=SX_TRANSPARENT, plot_bgcolor=SX_TRANSPARENT,
                          xaxis=dict(visible=False), yaxis=dict(visible=False))
        return fig

    if df.empty:
        empty_fig = _empty_fig("No data found for this range")
        return empty_fig, empty_fig, empty_fig, empty_fig, empty_fig, [{'label': 'All', 'value': 'All'}], "No data"

    # Handle Top 2 Labels: Expand sound1 and sound2 into a unified sound column
    df_s1 = df[['sound1', 'conf_sound1', 'date']].rename(columns={'sound1': 'sound', 'conf_sound1': 'conf'})
    df_s2 = df[['sound2', 'conf_sound2', 'date']].rename(columns={'sound2': 'sound', 'conf_sound2': 'conf'})
    df_expanded = pd.concat([df_s1, df_s2]).dropna(subset=['sound'])

    # Drop the non-classes before anything counts them. dropna() above only
    # removes real NULLs; "None" and "Silence" arrive as literal strings, and
    # every chart plus the class dropdown is derived from df_expanded, so
    # filtering once here keeps them out of all six at the same time.
    sound_lower = df_expanded['sound'].astype(str).str.strip().str.lower()
    df_expanded = df_expanded[~sound_lower.isin(['none', 'silence'])]

    if df_expanded.empty:
        # A range can hold rows that are all "None"/"Silence" -- feeding the
        # resulting empty frame to px.sunburst raises rather than drawing.
        empty_fig = _empty_fig("No audio classes detected in this range")
        return empty_fig, empty_fig, empty_fig, empty_fig, empty_fig, [{'label': 'All', 'value': 'All'}], "No data"

    df_expanded['hour'] = df_expanded['date'].dt.hour

    # Filter by selected sound
    if selected_sound != 'All':
        df_filtered = df_expanded[df_expanded['sound'] == selected_sound]
    else:
        df_filtered = df_expanded

    # 1. Top Classes Chart
    top_counts = df_expanded['sound'].value_counts().head(num_sounds).sort_values(ascending=True)
    top_fig = go.Figure(go.Bar(
        x=top_counts.values,
        y=top_counts.index,
        orientation='h',
        marker=dict(color='#2E8B57', line=dict(color='white', width=1))
    ))
    top_fig.update_layout(
        title=f"Top {num_sounds} Classes",
        margin=dict(l=10, r=10, t=40, b=10),
        paper_bgcolor=SX_TRANSPARENT,
        plot_bgcolor=SX_TRANSPARENT,
        font=dict(color=SX_TEXT_DIM),
        xaxis_title="Detections",
        xaxis=dict(gridcolor=SX_GRID, zerolinecolor=SX_BORDER, color=SX_MUTED),
        yaxis=dict(color=SX_MUTED),
    )

    # 2. Daily Activity Pattern (Radar/Polar)
    hourly_counts = df_filtered.groupby('hour').size().reindex(range(24), fill_value=0)
    pattern_fig = go.Figure(go.Barpolar(
        r=hourly_counts.values,
        theta=np.linspace(0, 360, 24, endpoint=False),
        marker_color='#2E8B57',
        opacity=0.8,
        width=360/24
    ))
    pattern_fig.update_layout(
        title='Activity Pattern (24h)',
        paper_bgcolor=SX_TRANSPARENT,
        font=dict(color=SX_TEXT_DIM),
        polar=dict(
            bgcolor=SX_TRANSPARENT,
            angularaxis=dict(
                ticktext=[f"{h:02d}:00" for h in range(24)],
                tickvals=list(range(0, 360, 15)),
                direction='clockwise', period=24,
                gridcolor=SX_GRID, linecolor=SX_BORDER, color=SX_MUTED,
            ),
            radialaxis=dict(gridcolor=SX_GRID, linecolor=SX_BORDER, color=SX_MUTED),
        ),
        margin=dict(l=30, r=30, t=40, b=30)
    )

    # 3. Responsive Timeline
    # Group by a dynamic frequency based on date range
    if start_date and end_date:
        days = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days
        freq = 'h' if days <= 2 else '6h' if days <= 7 else 'D'
    else:
        freq = 'h'

    timeline_df = df_filtered.groupby(pd.Grouper(key='date', freq=freq)).size().reset_index(name='count')
    
    timeline_fig = go.Figure()
    timeline_fig.add_trace(go.Scatter(
        x=timeline_df['date'], y=timeline_df['count'],
        mode='lines+markers',
        fill='tozeroy',
        fillcolor='rgba(46, 139, 87, 0.1)',
        line=dict(color='#2E8B57', width=2),
        marker=dict(size=6, symbol='circle')
    ))
    timeline_fig.update_layout(
        title=f"Detection Timeline (Resolution: {freq})",
        xaxis_title="Date & Time",
        yaxis_title="Detections",
        hovermode="x unified",
        margin=dict(l=10, r=10, t=40, b=10),
        paper_bgcolor=SX_TRANSPARENT,
        plot_bgcolor=SX_TRANSPARENT,
        font=dict(color=SX_TEXT_DIM),
        height=350
    )
    timeline_fig.update_xaxes(gridcolor=SX_GRID, zerolinecolor=SX_BORDER, color=SX_MUTED)
    timeline_fig.update_yaxes(gridcolor=SX_GRID, zerolinecolor=SX_BORDER, color=SX_MUTED)

    # Prepare data for Radar and Sunburst
    df_counts = df_expanded['sound'].value_counts().reset_index()
    df_counts.columns = ['sound', 'count']

    # 4. Emotion Radar Chart (Scatterpolar)
    target_emotions = ['HAPPY', 'SAD', 'NEUTRAL', 'ANGRY']
    emotion_map = {
        'HAP': 'HAPPY', 'HAPPY': 'HAPPY',
        'SAD': 'SAD',
        'NEU': 'NEUTRAL', 'NEUTRAL': 'NEUTRAL',
        'ANG': 'ANGRY', 'ANGRY': 'ANGRY'
    }
    
    if 'emotion' in df.columns:
        import re
        def parse_emotion(text):
            if not text or pd.isna(text):
                return None
            # Extract letters before optional parenthesis: e.g. "SAD(0.97)" -> "SAD"
            match = re.search(r'^([A-Za-z]+)', str(text))
            if match:
                emo_part = match.group(1).upper()
                return emotion_map.get(emo_part, emo_part)
            return None

        standardized_emotions = df['emotion'].apply(parse_emotion)
        emotion_counts = standardized_emotions.value_counts()
        print(f"DEBUG: Emotion counts found: {emotion_counts.to_dict()}")
        radar_values = [int(emotion_counts.get(e, 0)) for e in target_emotions]
        # Close the radar line by appending the first value
        radar_values_closed = radar_values + [radar_values[0]]
        radar_labels_closed = target_emotions + [target_emotions[0]]
    else:
        radar_values_closed = [0, 0, 0, 0, 0]
        radar_labels_closed = target_emotions + [target_emotions[0]]

    radar_fig = go.Figure(data=go.Scatterpolar(
        r=radar_values_closed,
        theta=radar_labels_closed,
        fill='toself',
        line=dict(color='#2E8B57'),
        marker=dict(color='#2E8B57')
    ))
    radar_fig.update_layout(
        paper_bgcolor=SX_TRANSPARENT,
        font=dict(color=SX_TEXT_DIM),
        polar=dict(
            bgcolor=SX_TRANSPARENT,
            radialaxis=dict(visible=True, gridcolor=SX_GRID, linecolor=SX_BORDER, color=SX_MUTED),
            angularaxis=dict(gridcolor=SX_GRID, linecolor=SX_BORDER, color=SX_MUTED),
        ),
        showlegend=False,
        title='Emotion Distribution (Radar)',
        margin=dict(l=40, r=40, t=40, b=40)
    )

    # 5. Sunburst
    sunburst_fig = px.sunburst(
        df_counts, 
        path=['sound'], 
        values='count',
        color='count',
        color_continuous_scale='Greens',
        title='Event Frequency (Sunburst)'
    )
    sunburst_fig.update_layout(
        margin=dict(l=10, r=10, t=40, b=10),
        paper_bgcolor=SX_TRANSPARENT,
        # Per-segment wedge labels are left alone: Plotly auto-contrasts them
        # against each wedge's own fill, so they stay readable regardless of
        # page theme. Only the title/colorbar chrome needs a colour here.
        font=dict(color=SX_TEXT_DIM),
    )

    # Dropdown Options
    unique_sounds = sorted(df_expanded['sound'].unique())
    options = [{'label': 'All', 'value': 'All'}] + [{'label': s, 'value': s} for s in unique_sounds]

    return top_fig, pattern_fig, timeline_fig, radar_fig, sunburst_fig, options, f"Show Top {num_sounds} Classes"

    # Dropdown Options
    unique_sounds = sorted(df_expanded['sound'].unique())
    options = [{'label': 'All', 'value': 'All'}] + [{'label': s, 'value': s} for s in unique_sounds]

    return top_fig, pattern_fig, timeline_fig, treemap_fig, sunburst_fig, options, f"Show Top {num_sounds} Classes"

@callback(
     Output('date_picker', 'start_date'),
     Output('date_picker', 'end_date'),
     Input('url_statistic', 'pathname'),
)
def init_dates(url):
    dt_from = get_date_before(get_now_date(), 7)
    return date_to_str(dt_from), get_now_str()


@callback(
    Output('stats-cell-modal', 'is_open'),
    Output('stats-cell-title', 'children'),
    Output('stats-cell-body', 'children'),
    Input('top_species_chart', 'clickData'),
    Input('daily_pattern', 'clickData'),
    Input('timeline', 'clickData'),
    Input('stats-cell-close', 'n_clicks'),
    State('date_picker', 'start_date'),
    State('date_picker', 'end_date'),
    State('sounds_selector', 'value'),
    prevent_initial_call=True,
)
def show_stat_events(top_click, pattern_click, timeline_click, _close,
                     start_date, end_date, selected_sound):
    """The clips behind a bar, an hour of the dial, or a point on the timeline.

    One callback for three charts because they differ only in how the click
    narrows the same query: a class, an hour of the day, or a slice of time.
    Each reads its selection off the point the user actually hit, so a chart
    redrawn under a different date range or class filter cannot answer with a
    stale window.
    """
    which = dash.ctx.triggered_id
    if which == 'stats-cell-close':
        return False, dash.no_update, dash.no_update

    date_from, date_to = _range(start_date, end_date)
    where = ["date BETWEEN ? AND ?"]
    args = [date_from, date_to]
    matched = None
    title = ""

    try:
        if which == 'top_species_chart':
            # Horizontal bars: the class is on the y axis. This chart ignores
            # the class selector -- it is drawn from every class -- so the
            # popup does too, or the number would not match the bar.
            matched = top_click['points'][0]['y']
            where.append(rank_clause(STAT_RANKS))
            args.extend([matched] * STAT_RANKS)
            title = f"{matched} · {date_from[:10]} to {date_to[:10]}"

        elif which == 'daily_pattern':
            # Barpolar: theta comes back in degrees over 24 wedges.
            theta = float(pattern_click['points'][0]['theta'])
            hour = int(round(theta / 15.0)) % 24
            where.append("substr(date, 12, 2) = ?")
            args.append(f"{hour:02d}")
            title = f"{hour:02d}:00–{hour:02d}:59 across {date_from[:10]} to {date_to[:10]}"

        elif which == 'timeline':
            # The bucket the point stands for, whose width is the resolution
            # the chart chose for this range -- so the popup covers exactly the
            # span that was summed into the point, not the hour around it.
            start = pd.to_datetime(timeline_click['points'][0]['x'])
            days = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days \
                if (start_date and end_date) else 0
            step = pd.Timedelta(hours=1) if days <= 2 else \
                pd.Timedelta(hours=6) if days <= 7 else pd.Timedelta(days=1)
            where = ["date >= ? AND date < ?"]
            args = [start.strftime('%Y-%m-%d %H:%M:%S'),
                    (start + step).strftime('%Y-%m-%d %H:%M:%S')]
            title = (f"{start:%Y-%m-%d %H:%M} + {step} "
                     f"({'hourly' if step.seconds == 3600 else '6-hourly' if step.seconds else 'daily'})")
        else:
            return False, dash.no_update, dash.no_update
    except (KeyError, IndexError, TypeError, ValueError):
        return False, dash.no_update, dash.no_update

    # The class selector narrows the pattern and timeline charts but not the
    # top-classes one, so it is applied to exactly the same two here.
    if which in ('daily_pattern', 'timeline') and selected_sound and selected_sound != 'All':
        where.append(rank_clause(STAT_RANKS))
        args.extend([selected_sound] * STAT_RANKS)
        matched = selected_sound
        title += f" · {selected_sound}"

    clause = " AND ".join(where)
    rows, total = fetch(clause, args)
    # The chart's number and the number of clips are not the same thing when
    # the selection is not about one class; the header explains the gap.
    detections = count_detections(clause, args, STAT_RANKS, sound=matched)
    return True, title, modal_body(
        rows, total, matched, detections=detections,
        empty_note="No events found for this selection. The charts count "
                   "sound1 and sound2, so a class shown here is one of an "
                   "event's two strongest labels.")
