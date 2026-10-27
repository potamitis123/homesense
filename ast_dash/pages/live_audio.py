import dash
from dash import html, callback, Output, Input, State, dcc, ctx
import dash_bootstrap_components as dbc
import threading
import time
import os
import sys
import datetime
import collections
import numpy as np
import pyaudio
import torch
import librosa
import plotly.graph_objects as go
from transformers import ASTForAudioClassification, ASTFeatureExtractor

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

dash.register_page(
    __name__,
    path='/live',
    title='Live Audio',
    name='Live'
)

# --- Config & Setup ---
SR = 16000

# 1. GPU Confirmation Logic
if torch.cuda.is_available():
    DEVICE = torch.device("cuda")
    print(f"GPU DETECTED: {torch.cuda.get_device_name(0)}")
else:
    DEVICE = torch.device("cpu")
    print("GPU NOT FOUND: Falling back to CPU. Performance may be lower.")

class LiveStateManager:
    def __init__(self):
        self.is_running = False
        self.thread = None
        self.stop_event = threading.Event()
        self.start_time = None
        self.log_filename = ""
        self.live_dir = os.path.join(project_root, 'live')
        
        if not os.path.exists(self.live_dir):
            os.makedirs(self.live_dir)
            
        self.audio_deque = collections.deque(maxlen=int(SR * 30))
        self.mutex = threading.Lock()
        
        self.p = pyaudio.PyAudio()
        self.stream = None
        
        self.model = None
        self.processor = None
        
        self.TARGET_LEN = 163840 # 10.24s @ 16kHz
        self.latest_results = []

    def start(self):
        if self.is_running:
            return "Already running"

        # Load model if not loaded
        if self.model is None:
            print(f"⏳ Loading AST Model onto {DEVICE}...")
            model_name = "MIT/ast-finetuned-audioset-10-10-0.4593"
            self.processor = ASTFeatureExtractor.from_pretrained(model_name)
            self.model = ASTForAudioClassification.from_pretrained(model_name).to(DEVICE)
            self.model.eval()
            print("✅ Model Ready.")

        # Prepare Logging
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_filename = os.path.join(self.live_dir, f"ast_recognition_log_{ts}.txt")
        
        # Prepare Audio
        self.audio_deque.clear()
        self.stop_event.clear()
        self.latest_results = []
        
        try:
            self.stream = self.p.open(format=pyaudio.paFloat32, channels=1, rate=SR,
                                      input=True, frames_per_buffer=1024,
                                      stream_callback=self.audio_cb)
            self.stream.start_stream()
        except Exception as e:
            return f"Audio Error: {e}"

        self.thread = threading.Thread(target=self.run_loop)
        self.thread.daemon = True
        self.thread.start()
        
        self.is_running = True
        self.start_time = time.time()
        return "Started"

    def stop(self):
        if not self.is_running:
            return "Not running"

        self.stop_event.set()
        
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
            
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None

        self.is_running = False
        return "Stopped"

    def audio_cb(self, in_data, frame_count, time_info, status):
        data = np.frombuffer(in_data, dtype=np.float32)
        with self.mutex:
            self.audio_deque.extend(data)
        return (in_data, pyaudio.paContinue)

    def run_loop(self):
        print("Backend Inference Thread Started")
        while not self.stop_event.is_set():
            self.predict_latest()
            time.sleep(0.9) # Stride: ~1 second update rate
        print("Backend Inference Thread Stopped")

    def predict_latest(self):
        with self.mutex:
            raw_data = np.array(self.audio_deque)

        if len(raw_data) < 1600: # Need at least 0.1s
            return

        # --- ZERO PADDING LOGIC ---
        if len(raw_data) < self.TARGET_LEN:
            pad_size = self.TARGET_LEN - len(raw_data)
            padding = np.zeros(pad_size, dtype=np.float32)
            chunk = np.concatenate([padding, raw_data])
        else:
            chunk = raw_data[-self.TARGET_LEN:]

        # Prepare GPU inputs
        inputs = self.processor(chunk, sampling_rate=SR, return_tensors="pt", padding="max_length")
        input_values = inputs.input_values.to(DEVICE)

        with torch.no_grad():
            outputs = self.model(input_values)
            probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
            top_probs, top_indices = torch.topk(probs, k=3)
            
            results = []
            for i in range(3):
                label = self.model.config.id2label[top_indices[0][i].item()]
                conf = top_probs[0][i].item()
                results.append((label, conf))

            self.latest_results = results
            self.log_to_disk(results)

    def log_to_disk(self, results):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        res_str = " | ".join([f"{r[0]} ({r[1]:.1%})" for r in results])
        try:
            with open(self.log_filename, "a") as f:
                f.write(f"[{timestamp}] {res_str}\n")
        except Exception as e:
            print(f"Logging Error: {e}")

    def get_audio_data(self):
        with self.mutex:
            return np.array(self.audio_deque)

state_manager = LiveStateManager()

layout = dbc.Container([
    dbc.Row([
        dbc.Col([
            html.H2("Live Audio Recognition (Backend)", className="text-primary mb-4"),
            html.P("This module runs the AST model locally on the captured audio. Results are logged to disk and displayed below.", className="text-muted")
        ], width=12)
    ]),

    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardHeader("Controls", className="bg-light font-weight-bold"),
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            dbc.Button("Start Monitoring", id="btn-start-live", color="success", className="me-2", n_clicks=0),
                            dbc.Button("Stop Monitoring", id="btn-stop-live", color="danger", className="me-2", disabled=True, n_clicks=0),
                        ], width="auto"),
                    ], className="mb-3"),
                    
                    html.Div(id="live-status", children="Status: Ready", className="lead text-info")
                ])
            ], className="shadow-sm mb-4")
        ], width=12)
    ]),
    
    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardHeader("Live Visualization", className="bg-dark text-white font-weight-bold"),
                dbc.CardBody([
                    html.Div(id="live-ai-labels", className="mb-2 text-success font-weight-bold", style={"font-family": "Consolas", "font-size": "1.2em", "background": "#1a1a1a", "padding": "10px", "border": "1px solid #333"}),
                    dcc.Graph(id="live-spectrogram", style={"height": "400px"}, config={'displayModeBar': False}),
                ], style={"background-color": "#0c0c0c"})
            ], className="shadow-sm")
        ], width=12)
    ]),

    dcc.Interval(id="live-status-interval", interval=1000, n_intervals=0),
    dcc.Interval(id="live-graph-interval", interval=500, n_intervals=0, disabled=True) 
], fluid=True, className="p-4")

@callback(
    Output("live-status", "children"),
    Output("btn-start-live", "disabled"),
    Output("btn-stop-live", "disabled"),
    Output("live-graph-interval", "disabled"),
    Input("btn-start-live", "n_clicks"),
    Input("btn-stop-live", "n_clicks"),
    prevent_initial_call=True
)
def toggle_live_recording(start_clicks, stop_clicks):
    trigger = ctx.triggered_id
    
    if trigger == "btn-start-live":
        msg = state_manager.start()
        return f"Status: {msg}", True, False, False
    
    elif trigger == "btn-stop-live":
        msg = state_manager.stop()
        return f"Status: {msg}", False, True, True
    
    return dash.no_update

@callback(
    Output("live-status", "children", allow_duplicate=True),
    Output("btn-start-live", "disabled", allow_duplicate=True),
    Output("btn-stop-live", "disabled", allow_duplicate=True),
    Output("live-graph-interval", "disabled", allow_duplicate=True),
    Input("live-status-interval", "n_intervals"),
    prevent_initial_call=True
)
def update_status_on_interval(n):
    if state_manager.is_running and state_manager.start_time:
        elapsed = int(time.time() - state_manager.start_time)
        return f"Status: Monitoring Active ({elapsed} s)", True, False, False
    else:
        return "Status: Ready", False, True, True

@callback(
    Output("live-spectrogram", "figure"),
    Output("live-ai-labels", "children"),
    Input("live-graph-interval", "n_intervals"),
    prevent_initial_call=True
)
def update_spectrogram(n):
    if not state_manager.is_running:
        return dash.no_update, dash.no_update
        
    # 1. Update Labels
    results = state_manager.latest_results
    if results:
        res_str = " | ".join([f"{r[0].upper()} ({r[1]:.1%})" for r in results])
        label_text = f"LIVE DETECT: {res_str}"
    else:
        label_text = "LIVE DETECT: WAITING FOR AUDIO..."

    # 2. Update Spectrogram
    samples = state_manager.get_audio_data()
    if len(samples) < 512:
        return dash.no_update, label_text
        
    # Visual: Last 10s
    view_len = int(10 * SR)
    chunk = samples[-view_len:]
    S = np.abs(librosa.stft(chunk, n_fft=1024, hop_length=512))
    S_db = librosa.amplitude_to_db(S, ref=np.max)
    
    # Axes
    freqs = librosa.fft_frequencies(sr=SR, n_fft=1024)
    
    elapsed = time.time() - state_manager.start_time if state_manager.start_time else 0
    n_frames = S_db.shape[1]
    # Time axis: ends at 'elapsed', spans roughly chunk duration (max 10s)
    chunk_duration = len(chunk) / SR
    times = np.linspace(max(0, elapsed - chunk_duration), elapsed, num=n_frames)
    
    fig = go.Figure(data=go.Heatmap(
        z=S_db,
        x=times,
        y=freqs,
        colorscale='Inferno',
        showscale=False,
        zmin=-70, 
        zmax=0
    ))
    
    fig.update_layout(
        margin=dict(l=40, r=10, t=10, b=30),
        xaxis_title="Recording Time (s)",
        yaxis_title="Frequency (Hz)",
        paper_bgcolor='#0c0c0c',
        plot_bgcolor='#0c0c0c',
        font=dict(color='#EEE'),
        xaxis=dict(showgrid=True, gridcolor='#333'),
        yaxis=dict(showgrid=True, gridcolor='#333')
    )
    
    return fig, label_text
