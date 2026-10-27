import os
os.environ["TORCH_HUB_OFFLINE"] = "1"

import dash
from dash import  html,callback,get_app,Output,Input,State,dcc
import dash_bootstrap_components as dbc
import os, sys
from datetime import datetime
import base64
import time 
import numpy as np
from pydub import AudioSegment
import io
import uuid
import traceback

# AI Imports
# Add project root to sys.path if not present
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from ast_src.ast_model import AST
from ast_src.convert_wav_to_mp3 import convert_raw_data_to_mp3
from ast_src.create_spectrogram import create_freq_analyzer_spectrogram
from ast_sql.ast_sql_lite import Ast_Sql_lite
from misc.load_parameters import parameters
from misc import convert_rec_to_float

# NEW IMPORTS
from ast_src.emotion_module import EmotionRecognizer
from ast_src.streaming_ASR import ASRTranscriber
from ast_src.clap_service import CLAPService
import torch

# Load Silero VAD at startup (stays on CPU) - Load from local cache offline if available
local_vad_path = os.path.join(os.path.expanduser("~"), ".cache", "torch", "hub", "snakers4_silero-vad_master")
if os.path.exists(local_vad_path):
    VAD_MODEL, VAD_UTILS = torch.hub.load(repo_or_dir=local_vad_path,
                                          model='silero_vad',
                                          source='local',
                                          onnx=False)
else:
    VAD_MODEL, VAD_UTILS = torch.hub.load(repo_or_dir='snakers4/silero-vad',
                                          model='silero_vad',
                                          force_reload=False,
                                          onnx=False)
(get_speech_timestamps, _, _, _, collect_chunks) = VAD_UTILS

app=get_app()

UPLOAD_DIRECTORY = os.path.join(project_root, "assets", "audiosuser")
ASSETS_MP3 = os.path.join(project_root, "assets", "audio_mp3")
ASSETS_SPEC = os.path.join(project_root, "assets", "specrogram_images")

# Global Cache for AI Models to prevent re-loading on every upload
GLOBAL_MODELS = {
    'ast': None,
    'emotion': None,
    'asr': None,
    'clap': None
}

dash.register_page(
    __name__,
    path='/upload_files',
    title='Upload & Process',
    name='Upload & Process'
)

def layout(**kwargs):
    return dbc.Container([
        dbc.Row([
            dbc.Col([
                html.H2("Upload & AI Process", className="text-primary mb-4"),
                html.P("Upload audio files (mp3, wav, flac, ogg) to be automatically classified by the AST model and added to the dashboard.", className="text-muted")
            ], width=12)
        ]),
        
        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader("Drop Files Here", className="bg-light font-weight-bold"),
                    dbc.CardBody([
                        dcc.Upload(
                            id='upload_audio_file',
                            children=html.Div([
                                html.I(className="fas fa-cloud-upload-alt fa-3x mb-3",
                                       style={'color': 'var(--sx-accent, #7c5cff)'}),
                                html.Div([
                                    'Drag and Drop or ',
                                    # A lighter tint of the accent than the icon
                                    # uses: at this size the link counts as normal
                                    # text, where the accent itself reaches only
                                    # 3.8:1 against the panel. This is 6.1:1.
                                    html.A('Select Files',
                                           style={'color': '#a48bff',
                                                  'textDecoration': 'underline'})
                                ], className="fs-5",
                                   style={'color': 'var(--sx-text, #e8eef9)'})
                            ]),
                            # Colours come from the dark theme's tokens rather than
                            # being hard-coded. The previous '#f8f9fa' was an inline
                            # style, so it beat sentinel_dark.css and left the panel
                            # near-white while the theme painted the label and icon
                            # light -- invisible on invisible. The fallbacks after
                            # each var() keep it legible if that stylesheet is ever
                            # removed.
                            style={
                                'width': '100%',
                                'height': '200px',
                                'lineHeight': '60px',
                                'borderWidth': '2px',
                                'borderStyle': 'dashed',
                                'borderRadius': '10px',
                                'textAlign': 'center',
                                'display': 'flex',
                                'flexDirection': 'column',
                                'justifyContent': 'center',
                                'cursor': 'pointer',
                                'backgroundColor': 'var(--sx-raised, #161e30)',
                                'borderColor': 'var(--sx-border-str, rgba(255,255,255,0.13))',
                                'color': 'var(--sx-text, #e8eef9)'
                            },
                            multiple=True
                        ),
                    ])
                ], className="shadow-sm mb-4"),
                
                dbc.Card([
                    dbc.CardHeader("Processing Results", className="bg-light font-weight-bold"),
                    dbc.CardBody([
                        dbc.Spinner(html.Div(id='output_audio_upload'), color="primary"),
                        html.Div(id='results', className="mt-3")
                    ])
                ], className="shadow-sm")
            ], width=12)
        ])
    ], fluid=True, className="p-4")

@callback(
    Output('output_audio_upload', 'children', allow_duplicate=True),
    Output('results', 'children'),
    Input('upload_audio_file', 'filename'),
    State('upload_audio_file', 'contents'),
    prevent_initial_call=True,
)
def update_output(list_of_names, list_of_contents):
    if not list_of_contents:
        return None, None

    # Ensure directories exist
    for d in [ASSETS_MP3, ASSETS_SPEC, UPLOAD_DIRECTORY]:
        if not os.path.exists(d):
            os.makedirs(d)

    # Initialize AI Components (using cache)
    try:
        if GLOBAL_MODELS['ast'] is None: GLOBAL_MODELS['ast'] = AST()
        if GLOBAL_MODELS['emotion'] is None: GLOBAL_MODELS['emotion'] = EmotionRecognizer()
        if GLOBAL_MODELS['asr'] is None: GLOBAL_MODELS['asr'] = ASRTranscriber()
        if GLOBAL_MODELS['clap'] is None: GLOBAL_MODELS['clap'] = CLAPService()
        
        ast_model = GLOBAL_MODELS['ast']
        emotion_model = GLOBAL_MODELS['emotion']
        asr_model = GLOBAL_MODELS['asr']
        clap_service = GLOBAL_MODELS['clap']
        
        db_path = os.path.join(project_root, parameters.get_parameters()['sqlite']['db_name'])
        ast_db = Ast_Sql_lite(db_path)
    except Exception as e:
        traceback.print_exc()
        return html.Div(f"Failed to initialize AI: {e}", className="text-danger"), None

    status_messages = []
    CHUNK_LENGTH_MS = 10000 # 10 seconds
    CHUNK_STEP_MS = 5000    # 5 seconds step (50% overlap)
    
    for name, content in zip(list_of_names, list_of_contents):
        try:
            # Decode and Save original upload temporarily
            content_type, content_string = content.split(',')
            decoded = base64.b64decode(content_string)
            
            original_ext = os.path.splitext(name)[1]
            temp_orig_path = os.path.join(UPLOAD_DIRECTORY, f"orig_{uuid.uuid4()}{original_ext}")
            with open(temp_orig_path, "wb") as f:
                f.write(decoded)
            
            # Load with pydub
            try:
                full_audio = AudioSegment.from_file(temp_orig_path)
            except Exception as e:
                status_messages.append(dbc.Alert(f"Failed to read {name}: {e}", color="danger"))
                if os.path.exists(temp_orig_path): os.remove(temp_orig_path)
                continue

            # Standardize audio (16kHz, mono, 16-bit)
            if full_audio.frame_rate != 16000: full_audio = full_audio.set_frame_rate(16000)
            if full_audio.channels != 1: full_audio = full_audio.set_channels(1)
            if full_audio.sample_width != 2: full_audio = full_audio.set_sample_width(2)
            
            total_duration_ms = len(full_audio)
            
            # Calculate number of chunks with overlap
            # A 10s chunk with 5s step means a chunk starts every 5s
            # For 20s: 0-10, 5-15, 10-20, 15-25 (pushed to 20)
            # Actually, we stop when start_ms reaches total_duration_ms
            chunk_starts = list(range(0, total_duration_ms, CHUNK_STEP_MS))
            num_chunks = len(chunk_starts)
            
            status_messages.append(html.H5(f"Processing {name} ({total_duration_ms/1000:.1f}s) in {num_chunks} overlapping chunks:"))

            for i, start_ms in enumerate(chunk_starts):
                end_ms = min(start_ms + CHUNK_LENGTH_MS, total_duration_ms)
                chunk = full_audio[start_ms:end_ms]
                
                # Skip very short chunks (e.g., less than 0.5s)
                if len(chunk) < 500: continue
                
                # Padding short chunks to 10s for AST/CLAP if needed (last chunk)
                actual_chunk_len = len(chunk)
                if actual_chunk_len < CHUNK_LENGTH_MS:
                    chunk = chunk + AudioSegment.silent(duration=CHUNK_LENGTH_MS - actual_chunk_len, frame_rate=16000)

                temp_chunk_wav = os.path.join(UPLOAD_DIRECTORY, f"chunk_{uuid.uuid4()}.wav")
                chunk.export(temp_chunk_wav, format="wav")

                # Prepare data for AI
                samples = np.array(chunk.get_array_of_samples(), dtype=np.int16)
                data_float = convert_rec_to_float(samples)
                
                # Normalize
                peak = np.max(np.abs(data_float))
                if peak > 0.001:
                    data_float = data_float / peak

                # 1. AST Inference
                predictions = ast_model.inference(waveform=data_float, sample_rate=16000)
                reject_sounds = ast_db.ontology.load_ontologies() or []
                filtered_preds = [p for p in predictions if p['sound'] not in reject_sounds]
                if not filtered_preds: filtered_preds = [{'sound': 'None', 'conf': 0.0}] * 3
                elif len(filtered_preds) < 3: filtered_preds.extend([{'sound': 'None', 'conf': 0.0}] * (3 - len(filtered_preds)))

                # 2. VAD & SPEECH ANALYSIS
                transcription_text = None
                emotion_tag = None
                clap_embedding = None

                audio_tensor = torch.from_numpy(data_float.astype(np.float32))
                
                # Use parameters from JSON for VAD
                vad_params = parameters.get_parameters().get('vad', {})
                speech_timestamps = get_speech_timestamps(
                    audio_tensor, 
                    VAD_MODEL, 
                    sampling_rate=16000,
                    threshold=vad_params.get('threshold', 0.5),
                    min_silence_duration_ms=vad_params.get('silence_duration_ms', 500),
                    min_speech_duration_ms=vad_params.get('min_speech_duration_ms', 250),
                    speech_pad_ms=vad_params.get('speech_pad_ms', 150)
                )

                # --- NEW ASR Logic: Pass full chunk to ASR/Diarization for context ---
                # Whisper/Pyannote work better with original timing context.
                transcription_text = asr_model.transcribe(temp_chunk_wav)

                if not speech_timestamps:
                    # Generate CLAP embedding on full chunk if no speech (background sound)
                    clap_embedding = clap_service.get_audio_embedding(temp_chunk_wav, vad_is_valid=False)
                else:
                    speech_only_tensor = collect_chunks(speech_timestamps, audio_tensor)
                    temp_speech_wav = temp_chunk_wav.replace('.wav', '_speech.wav')
                    
                    from scipy.io.wavfile import write as wav_write
                    wav_write(temp_speech_wav, 16000, speech_only_tensor.numpy())
                    
                    emotion_result = emotion_model.predict(temp_speech_wav)
                    if isinstance(emotion_result, tuple):
                        label, conf = emotion_result
                        emotion_tag = f"{label.upper()} ({conf:.2f})"
                    else:
                        emotion_tag = str(emotion_result)

                    clap_embedding = clap_service.get_audio_embedding(temp_speech_wav, vad_is_valid=True)
                    
                    if os.path.exists(temp_speech_wav): os.remove(temp_speech_wav)
                # Save Assets
                chunk_ts = datetime.now()
                base_fn = f"{chunk_ts.strftime('%Y%m%d_%H%M%S')}_{i}_upload"
                wav_fn = f"{base_fn}.wav"
                png_fn = f"{base_fn}.png"
                
                wav_path = os.path.join(ASSETS_MP3, wav_fn)
                png_path = os.path.join(ASSETS_SPEC, png_fn)
                
                from ast_src.convert_wav_to_mp3 import save_normalized_wav
                save_normalized_wav(data_float, wav_path)
                create_freq_analyzer_spectrogram(np.array(data_float, dtype=np.float16), 16000, png_path)
                
                # Insert to DB
                ast_db.ast_table.insert_ast_table(
                    date=chunk_ts.strftime('%Y-%m-%d %H:%M:%S'),
                    device_name="manual_upload",
                    file_name=wav_fn,
                    spectrogram_image=png_fn,
                    sound1=filtered_preds[0]['sound'], conf_sound1=filtered_preds[0]['conf'],
                    sound2=filtered_preds[1]['sound'], conf_sound2=filtered_preds[1]['conf'],
                    sound3=filtered_preds[2]['sound'], conf_sound3=filtered_preds[2]['conf'],
                    rms=round(20 * np.log10(chunk.rms) if chunk.rms > 0 else 0, 2),
                    transcription=transcription_text,
                    emotion=emotion_tag,
                    clap_embedding=clap_embedding
                )
                
                status_messages.append(html.Div(f"  - Chunk {i+1}/{num_chunks}: {filtered_preds[0]['sound']} (AST) | {emotion_tag or 'N/A'} (Emotion)", className="ms-4 small"))
                
                if os.path.exists(temp_chunk_wav): os.remove(temp_chunk_wav)

            if os.path.exists(temp_orig_path): os.remove(temp_orig_path)
            status_messages.append(dbc.Alert(f"Finished processing {name}", color="success", className="mt-2"))
                
        except Exception as e:
            traceback.print_exc()
            status_messages.append(dbc.Alert(f"Error processing {name}: {e}", color="danger"))
            if 'temp_orig_path' in locals() and os.path.exists(temp_orig_path):
                os.remove(temp_orig_path)

    ast_db.close()
    return None, html.Div(status_messages)
