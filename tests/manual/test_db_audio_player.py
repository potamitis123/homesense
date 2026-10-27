import dash
from dash import html, dcc, Input, Output
import dash_bootstrap_components as dbc
import os
import sqlite3
import sys

# 1. SETUP
# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from misc.load_parameters import parameters

db_name = parameters.get_parameters()['sqlite']['db_name']
db_path = os.path.join(project_root, db_name)

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])

def get_first_recording():
    """Access database and find the filename of the first recording."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Get the most recent recording first (as the dashboard does)
        cursor.execute("SELECT id, file_name FROM ast_table ORDER BY date DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        return row # (id, file_name)
    except Exception as e:
        print(f"DB Error: {e}")
        return None

app.layout = dbc.Container([
    html.H1("DB Audio Player Test"),
    html.P(f"Database: {db_name}"),
    
    dbc.Button("Load First Recording from DB", id="load-db-btn", color="success", className="mb-3"),
    
    html.Div(id="info-display", className="mb-3 fw-bold"),
    
    html.Div(id='player-container', children=[
        # Audio element will be inserted here
        html.P("Audio player will appear here after loading.")
    ], style={'padding': '20px', 'border': '1px solid #ccc', 'borderRadius': '10px'}),
    
], fluid=True, className="p-5")

@app.callback(
    Output('player-container', 'children'),
    Output('info-display', 'children'),
    Input('load-db-btn', 'n_clicks'),
    prevent_initial_call=True
)
def load_and_play(n):
    result = get_first_recording()
    if not result:
        return html.P("No recordings found in DB."), "Status: Empty"
    
    evt_id, file_name = result
    
    # Construct the URL
    # We search in common folders
    audio_src = None
    for folder in ['audio_mp3', 'audios', 'audiosuser']:
        if os.path.exists(os.path.join(project_root, "assets", folder, file_name)):
            audio_src = f"/assets/{folder}/{file_name}"
            break
    
    if not audio_src:
        return html.P(f"File not found on disk: {file_name}"), f"Event ID: {evt_id} (File Missing)"

    print(f"Found recording: {file_name}, creating player...")
    
    # Create a fresh audio component
    player = html.Audio(
        id='test-audio-details',
        src=audio_src,
        controls=True,
        autoPlay=True,
        style={'width': '100%'}
    )
    
    return player, f"Loaded Event ID: {evt_id} | File: {file_name}"

if __name__ == '__main__':
    app.run_server(debug=True, port=8052)
