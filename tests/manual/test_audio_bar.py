import dash
from dash import html, dcc, Input, Output, State
import dash_bootstrap_components as dbc
import os

# Create simple app
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])

# Path to a known good file from your directory
TEST_WAV = "/assets/audios/20260310_124814_esp32s3-1_rec.wav"

app.layout = dbc.Container([
    html.H1("Audio Bar Test"),
    html.P(f"Testing with file: {TEST_WAV}"),
    
    dbc.Button("Load and Play Audio", id="test-play-btn", color="primary", className="mb-3"),
    
    html.Div([
        html.Audio(
            id='test-audio-player',
            controls=True,
            style={'width': '100%'}
        )
    ], style={'padding': '20px', 'border': '1px solid #ccc'}),
    
    html.Div(id="test-status", className="mt-3")
], fluid=True)

@app.callback(
    Output('test-audio-player', 'src'),
    Output('test-status', 'children'),
    Input('test-play-btn', 'n_clicks'),
    prevent_initial_call=True
)
def play_test_audio(n):
    print(f"Button clicked, setting src to: {TEST_WAV}")
    return TEST_WAV, f"Setting source to {TEST_WAV} at {dash.callback_context.triggered[0]['value']}"

if __name__ == '__main__':
    # We must ensure Dash can see the assets folder
    # In this test script, assets is at the parent level
    app.run_server(debug=True, port=8051)
