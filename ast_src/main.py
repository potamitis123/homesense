import os, sys
import warnings
import ctypes

# Suppress Windows Error Message Boxes
try:
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except:
    pass

# 1. Suppress noisy library loading messages and tracebacks
os.environ["TORCHAUDIO_USE_TORCHCODEC"] = "0"
os.environ["TORCHCODEC_LOG_LEVEL"] = "error"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["TORCH_HUB_OFFLINE"] = "1"

warnings.filterwarnings("ignore", message="torchcodec is not installed correctly")
warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub")
warnings.filterwarnings("ignore", message=".*torchcodec.*")

import torch
import datetime
import logging
# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Import misc first to apply the SSL certificate store monkeypatch early
import misc

# Add ast_src to sys.path
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

# --- REDIRECT STDOUT/STDERR TO LOG FILE ---
# Import logger to ensure it's initialized with the correct absolute path
from misc.log_test import logger
# Access the absolute path from the first handler of the unified logger
log_file_path = logger.handlers[0].baseFilename

class LoggerWriter:
    def __init__(self, log_file):
        self.log_file = log_file

    def write(self, message):
        if message.strip():
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
                    f.write(f"{timestamp} - CONSOLE - {message.rstrip()}\n")
            except:
                pass
            # Also keep printing to real console
            sys.__stdout__.write(message)

    def flush(self):
        sys.__stdout__.flush()

# Force flushing of prints
sys.stdout = LoggerWriter(log_file_path)
sys.stderr = LoggerWriter(log_file_path)
# ------------------------------------------

from ast_model import AST
import numpy as np
from rec_audio import Recorder
import time
import glob 
from multiprocessing import Process,Pool
import datetime
from pydub import AudioSegment
from pydub.utils import mediainfo
from mutagen.mp3 import MP3

from convert_wav_to_mp3 import  *
from create_spectrogram import *
from ast_sql.ast_sql_lite import Ast_Sql_lite
from ast_src.communication import CommunicationManager
from misc.log_test import logger
from misc.load_parameters import parameters
from misc.telemetry import check_CPU_temp
from misc import *
import torch
import online_scan # Import the new module
from misc.send_alert import send_alert_email # Import email alert function

# NEW IMPORTS
from emotion_module import EmotionRecognizer, dimensions_tag
from streaming_ASR import ASRTranscriber
from clap_service import CLAPService # Import CLAPService

# Voice-command feature ("Kokora stop") -- see Voice_commands/__init__.py
import Voice_commands.detector
import Voice_commands.state
import Voice_commands.broadcaster

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

LOG_D={'module_name':'main'}

from misc.console_status import (banner, describe_duration, known_devices,
                                 quiet_third_party_loggers, status_line)

quiet_third_party_loggers()

# Define Trigger Labels (Lower case)
SPEECH_LABELS = {
    "speech", "male speech", "female speech", "child speech", 
    "conversation", "narration", "monologue", "babbling", "shouting", 
    "yelling", "whispering", "singing", "rapping"
}

# --- Global Flags for VAD / ASR / Emotion / CLAP ---
global_vad_enabled = parameters.get_parameters().get('vad', {}).get('enabled', False)
global_asr_enabled = parameters.get_parameters().get('asr', {}).get('enabled', False)
global_emotion_enabled = parameters.get_parameters().get('emotion', {}).get('enabled', False)
global_clap_enabled = parameters.get_parameters().get('clap', {}).get('enabled', False) # New CLAP flag

# --- Global CLAP Service Instance ---
# Initialize CLAPService outside the loop to manage model loading/offloading
clap_service = CLAPService() if global_clap_enabled else None

def RMS_dB(rms, num_dig=2):
    return round(20*np.log10(rms),num_dig)

def get_mp3_bitrate(file_path):
    try:
        audio = MP3(file_path)
        return int(audio.info.bitrate/1000)
    except Exception as e:
        return None
    
def load_audio_file(audio_file):
    audio_file = os.path.normpath(audio_file)
    start_load=datetime.now()
    #audio_file small letters   
    try:
        audio=None
        data=[]
        data_float=None
        sr=None
        bitrate=None
        rms=None
        dbrms=None
        if audio_file.lower().endswith('.wav'):
            try:
                audio = AudioSegment.from_wav(audio_file)
            except:
                try:
                    audio = AudioSegment.from_file(audio_file)
                except Exception as e:
                    logger.error(f"Failed to load WAV '{audio_file}': {e}", extra=LOG_D)
                    return None, None, None, None, None, None
            
            try:
                info = mediainfo(audio_file)
                bitrate = int(float(info.get('bit_rate', 0))/1000)
            except:
                # Calculate approximate bitrate if mediainfo fails
                if audio:
                    bitrate = int((audio.frame_rate * audio.sample_width * 8 * audio.channels) / 1000)
                else:
                    bitrate = None
        elif audio_file.lower().endswith('.mp3'):
            try:
                audio = AudioSegment.from_mp3(audio_file)
                bitrate = get_mp3_bitrate(audio_file)
            except Exception as e:
                logger.error(f"Failed to load MP3 '{audio_file}': {e}", extra=LOG_D)
                bitrate=None
        elif audio_file.lower().endswith('.aac'):
            try:
                audio = AudioSegment.from_file(audio_file,format='aac')
                bitrate = int(mediainfo(audio_file)['bit_rate']/1000)
            except:
                bitrate=None
        elif audio_file.lower().endswith('.flac'):
            try:
                audio = AudioSegment.from_file(audio_file,format='flac')
                bitrate = int(mediainfo(audio_file)['bit_rate']/1000)
            except:
                bitrate=None
        elif audio_file.lower().endswith('.ogg'):
            try:
                audio = AudioSegment.from_file(audio_file,format='ogg')
                bitrate = int(mediainfo(audio_file)['bit_rate']/1000)
            except:
                bitrate=None
        else:
            audio=None
            data=[]
            logger.error(f"Not Supported audio file")
        if not audio is None:
            sr=audio.frame_rate
            channels=audio.channels
            frame_width=audio.frame_width
            if sr!=16000 or channels!=1:
                audio=audio.set_frame_rate(16000)
                sr=16000
            if channels!=1:
                audio=audio.set_channels(1)
            if frame_width!=2:
                audio=audio.set_sample_width(2)
               
                logger.info(f"Resample data",extra=LOG_D)
           # while audio.get_dc_offset()!=0:
            #    offset=audio.get_dc_offset()
            #    audio=audio.remove_dc_offset(offset=offset)
            #offset=audio.get_dc_offset()

            dbrms=RMS_dB(audio.rms)
            rms=audio.rms
            data=np.array(audio.get_array_of_samples(),dtype=np.int16)
            duration=audio.duration_seconds
            data=data[2000:len(data)-2000]
            #data_float=np.array(data,dtype=np.float64)
            data_float=convert_rec_to_float(data)
            #data_rms=get_rms(data_float)
            logger.info(f"Load Original file {audio_file} \
                        \n \t\t\t\t\t BitRate: {bitrate} \
                        \n \t\t\t\t\t PCM Data: {len(data_float)} sec \
                        \n \t\t\t\t\t Kbps  Duration: {duration:.2f} sec \
                        \n \t\t\t\t\t Sample Rate: {sr} Hz \
                        \n \t\t\t\t\t RMS (dB): {dbrms} \
                        \n \t\t\t\t\t Time: {(datetime.now()-start_load).total_seconds()*1000:.0f} msec",extra=LOG_D)
    
    except Exception as e:
        data_float=None
        audio=None
        data=[]
        sr=None
        bitrate=None
        rms=None
        dbrms=None
        logger.error(f"Error Load file: {str(e)} ",extra=LOG_D)
    return data_float,audio,sr,bitrate,rms,dbrms

def get_lat_lon(ast_db,device_name):
    latitude,longitude=None,None
    cursor = ast_db.conn.cursor()
    try:
        cursor.execute(f"SELECT device_table FROM devices WHERE device_name='{device_name}'")
        result = cursor.fetchone()
        if result is not None:
            device_table=result[0]
            if device_table is not None:
                if device_table=='users':
                    cursor.execute(f"SELECT latitude,longitude FROM users WHERE username='{device_name}'")
                    result=cursor.fetchone()
                    if result:
                        latitude,longitude= result
                else:
                    cursor.execute(f"SELECT latitude,longitude FROM {device_table} WHERE device_name='{device_name}'")
                    result=cursor.fetchone()
                    if result:
                        latitude,longitude= result   
    except Exception as e:  
        logger.error(f"Error find Lat , Lon  {str(e)} ",extra=LOG_D)
    finally:
        cursor.close()
    return latitude,longitude

STATUS_INTERVAL_SEC=60


def report_pipeline_status(started_at, processed_total):
    """Print a one-line summary of broker link, sentinels and queue depth."""
    try:
        adapter=CommunicationManager.get_instance()
        link="CONNECTED" if getattr(adapter,'connected',False) else "DISCONNECTED"
        broker=f"{getattr(adapter,'host','?')}:{getattr(adapter,'port','?')}"
        in_flight=len(getattr(adapter,'rec',[]) or [])
    except Exception:
        link, broker, in_flight = "UNKNOWN", "?", 0

    devices=known_devices(project_root)
    online=[d for d,info in devices.items() if info.get('status')=='online']
    inbox=os.path.join(project_root,'assets','audios')
    pending=sum(len(glob.glob(os.path.join(inbox,f'*_rec.{ext}')))
                for ext in ('wav','mp3','flac','ogg','aac'))

    status_line(f"MQTT {link} ({broker})",
                f"sentinels online {len(online)}/{len(devices)}"
                + (f" [{', '.join(sorted(online))}]" if online else ""),
                f"receiving {in_flight}",
                f"queued {pending}",
                f"processed {processed_total}",
                f"up {describe_duration(time.time()-started_at)}")


def report_startup(db_path):
    """Print the effective configuration so misconfiguration is visible at boot."""
    try:
        adapter=CommunicationManager.get_instance()
        broker=f"{getattr(adapter,'host','?')}:{getattr(adapter,'port','?')}"
        link="CONNECTED" if getattr(adapter,'connected',False) else "NOT CONNECTED"
    except Exception:
        broker, link = "?", "NOT CONNECTED"

    gpu=torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU only"
    params=parameters.get_parameters()
    banner("SENTINEL BACKGROUND PIPELINE", [
        ("Mode", params.get('communication',{}).get('mode','Home Server')),
        ("MQTT broker", f"{broker}  ->  {link}"),
        ("Audio inbox", os.path.join(project_root,'assets','audios')),
        ("Database", db_path),
        ("Compute", gpu),
        ("Diarization", "on" if params.get('diarization',{}).get('enabled') else "off"),
        ("ASR", "on" if params.get('asr',{}).get('enabled') else "off"),
        ("Status heartbeat", f"every {STATUS_INTERVAL_SEC}s"),
    ])


def proc(ast,ast_db, db_path, emotion_model=None, asr_model=None):
    wait=1
    started_at=time.time()
    processed_total=0
    last_status=0.0
    while True:
        assets_path = os.path.join(project_root, 'assets')
        # Look for WAV first, then MP3
        audio_files = glob.glob(os.path.join(assets_path, 'audiosuser', '*_rec.wav'))
        audio_files.extend(glob.glob(os.path.join(assets_path, 'audiosuser', '*_rec.mp3')))
        audio_files.extend(glob.glob(os.path.join(assets_path, 'audiosuser', '*_rec.flac')))
        audio_files.extend(glob.glob(os.path.join(assets_path, 'audiosuser', '*_rec.ogg')))
        audio_files.extend(glob.glob(os.path.join(assets_path, 'audiosuser', '*_rec.aac')))
        audio_files.extend(glob.glob(os.path.join(assets_path, 'audios', '*_rec.wav')))
        audio_files.extend(glob.glob(os.path.join(assets_path, 'audios', '*_rec.mp3')))
        audio_files.extend(glob.glob(os.path.join(assets_path, 'audios', '*_rec.flac')))
        audio_files.extend(glob.glob(os.path.join(assets_path, 'audios', '*_rec.ogg')))
        audio_files.extend(glob.glob(os.path.join(assets_path, 'audios', '*_rec.acc')))

        # Add audio_examples folder
        # audio_files.extend(glob.glob(f"./audio_examples/*_rec.mp3"))

        #Path to save new mp3 files
        mp3_path=os.path.join(assets_path,'audio_mp3')
        #Path to save spectrogrm images
        specrogramm_images=os.path.join(assets_path,'specrogram_images')
        #Create folder for mp3 files
        if not os.path.exists(mp3_path):
            os.makedirs(mp3_path)
            logger.info(f"Create folder {mp3_path}",extra=LOG_D)
        #Create folder for spectrogram images
        if not os.path.exists(specrogramm_images):
            os.makedirs(specrogramm_images)
            logger.info(f"Create folder {specrogramm_images}",extra=LOG_D)
        # Which of the queued captures are duplicates of each other, decided
        # once for the whole batch before anything expensive runs.
        #
        # The housekeeper's dedupe pass still runs and still catches copies
        # that straddle two batches; what it cannot do is refund the AST, ASR,
        # emotion and CLAP time already spent on the row it deletes. During a
        # conversation that is most of the queue, which is why events were
        # surfacing up to twenty minutes late.
        #
        # A file absent from the plan is processed exactly as before, so a
        # prefilter that cannot judge something never silently drops it.
        prefilter = {}
        _dedupe_cfg = parameters.get_parameters().get('dedupe', {}) or {}
        if (_dedupe_cfg.get('enabled', True)
                and _dedupe_cfg.get('prefilter_enabled', True)
                and len(audio_files) > 1):
            try:
                from ast_src.dedupe_events import plan_pending
                prefilter = plan_pending(audio_files)
                if prefilter:
                    logger.info(
                        f"Prefilter: {len(prefilter)} of {len(audio_files)} "
                        f"queued capture(s) are duplicates another sentinel "
                        f"recorded better; skipping them before analysis",
                        extra=LOG_D)
            except Exception as e:
                logger.error(f"Prefilter failed, analysing everything: {e}",
                             extra=LOG_D)
                prefilter = {}

        # Loop for preccessed audio files
        for index,audio_file in enumerate(audio_files):
            wait=2

            # Dropped here rather than after the pipeline: before the decode,
            # before AST, ASR, emotion and CLAP. The audio is moved aside, not
            # deleted -- see shelve_duplicate.
            _skip = prefilter.get(audio_file)
            if _skip:
                from ast_src.dedupe_events import shelve_duplicate
                # Same-second skips carry no correlation (the audio was not
                # compared), so the number cannot be formatted for them.
                _how = ("was stamped with the same second as"
                        if _skip.get('rule') == 'same_second' else
                        f"correlates {_skip['correlation']:.2f} with")
                logger.info(
                    f"Skipping {os.path.basename(audio_file)}: {_how} "
                    f"{os.path.basename(_skip['winner'])}, which has the "
                    f"better SNR ({_skip['snr_winner']:.1f} dB vs "
                    f"{_skip['snr']:.1f} dB)", extra=LOG_D)
                shelve_duplicate(audio_file, _skip)
                continue

            processed_total+=1
            status_line(f"PROCESSING {index+1}/{len(audio_files)}",
                        os.path.basename(audio_file))
            logger.info(f"Proccess File {audio_file}",extra=LOG_D)
            start_inf=datetime.now()
            logger.info(f"Proccess File  : {index+1} / {len(audio_files)}",extra=LOG_D)

            # Device name from filename, parsed early so the Kokora mute-gate
            # and command-detector hooks below can use it without waiting for
            # the later DB-insert parse further down this loop body.
            try:
                _early_name_parts = os.path.basename(audio_file).split('_')
                device_name_early = 'ex' if _early_name_parts[2].startswith('ex') else _early_name_parts[2]
            except IndexError:
                device_name_early = 'unknown'

            #load audio files
            data,audio,sr,bitrate,rms,dbrms=load_audio_file(audio_file)

            #Check data len
            if data is not None and len(data)>0 and not audio is None:
                if Voice_commands.state.is_muted():
                    logger.info(f"Kokora mute active -- skipping {audio_file} without processing", extra=LOG_D)
                    try:
                        if "audio_examples" not in audio_file:
                            os.remove(audio_file)
                    except Exception as e:
                        logger.error(f"Error removing muted source file: {str(e)}", extra=LOG_D)
                    continue

                # inference audio data with AST
                pred_labels=ast.inference(waveform=data,sample_rate=sr)

                #Load reject sounds
                tmpreject_sounds=ast_db.ontology.load_ontologies('common_files/ontology_updated.txt')
                if tmpreject_sounds is not None: 
                    reject_sounds=tmpreject_sounds
                
                # Filter out rejected sounds
                pred_labels=[x for x in pred_labels if x['sound'] not in reject_sounds]
                
                # Fallback if no sounds remain after filtering
                if not pred_labels:
                    pred_labels = [{'sound': 'None', 'conf': 0.0}] * 3
                elif len(pred_labels) < 3:
                    pred_labels.extend([{'sound': 'None', 'conf': 0.0}] * (3 - len(pred_labels)))

                sound1=pred_labels[0]['sound']
                conf_sound1=pred_labels[0]['conf']
                sound2=pred_labels[1]['sound']
                conf_sound2=pred_labels[1]['conf']
                sound3=pred_labels[2]['sound']
                conf_sound3=pred_labels[2]['conf']

                # --- VAD & SPEECH ANALYSIS ---
                # Which AST labels open the door to VAD/ASR/emotion.
                #
                #   "top1"  speech must be what AST heard first. Shuts out the
                #           neighbours' dog and a cracking noise, which list
                #           speech second or third (Crack 0.07 + Speech 0.06 on
                #           18-09-2026), and saves the decode on ~7% of clips.
                #   "top3"  speech anywhere in the top three, as before.
                #
                # The price of top1, measured over the week to 18-09: of the 275
                # transcribed clips it would have skipped, 78 were boilerplate
                # or gibberish but 180 were real sentences -- mealtime talk that
                # AST led with "Dishes, pots, and pans", conversation over
                # music. The clips of the 16-09 cooking scene include one of
                # those. Diarisation stops the junk anyway: on the eleven clips
                # measured on 18-09 it found nobody in every noise clip and
                # somebody in every real one, so top3 loses nothing to the dog.
                speech_gate = parameters.get_parameters().get('asr', {}).get('speech_gate', 'top1')
                heard = [sound1] if speech_gate == 'top1' else [sound1, sound2, sound3]
                is_speech_in_ast = any((label or '').lower() in SPEECH_LABELS for label in heard)

                transcription_text = None
                emotion_tag = None
                clap_embedding = None  # Initialize CLAP embedding

                if is_speech_in_ast:
                    logger.info("AST detected speech. Proceeding with VAD/ASR/Emotion analysis.", extra=LOG_D)
                    # Create a temporary WAV file of the FULL chunk for ASR context
                    temp_chunk_for_asr = os.path.join(
                        project_root, 
                        "temp_speech_segments", 
                        f"{os.path.basename(audio_file).rsplit('.', 1)[0]}_full.wav"
                    )
                    os.makedirs(os.path.dirname(temp_chunk_for_asr), exist_ok=True)
                    from scipy.io.wavfile import write as wav_write
                    wav_write(temp_chunk_for_asr, 16000, data)

                    # This is where VAD decides if there's speech
                    audio_tensor = torch.from_numpy(data.astype(np.float32))

                    # Use parameters from JSON for VAD
                    vad_params = parameters.get_parameters().get('vad', {})
                    speech_timestamps = get_speech_timestamps(
                        audio_tensor,
                        VAD_MODEL,
                        sampling_rate=16000,
                        threshold=vad_params.get('threshold', 0.35),
                        min_silence_duration_ms=vad_params.get('silence_duration_ms', 500),
                        min_speech_duration_ms=vad_params.get('min_speech_duration_ms', 250),
                        speech_pad_ms=vad_params.get('speech_pad_ms', 150)
                    )

                    if global_vad_enabled and not speech_timestamps:
                        logger.info(f"VAD: No speech detected in {audio_file}. Skipping ASR/Emotion/CLAP.", extra=LOG_D)
                        # Keep the "No Speech Detected" prefix: several
                        # reports filter on it with a substring match. The
                        # suffix says which stage rejected the clip, so a
                        # silent row can be told apart from a mis-tuned one.
                        transcription_text = "No Speech Detected (VAD)"
                    else:
                        # Process either the VAD-filtered segment or the full audio
                        segment_to_process = None
                        if global_vad_enabled and speech_timestamps:
                            logger.info(f"VAD: Speech detected, creating speech-only segments for ASR/Emotion/CLAP...", extra=LOG_D)
                            speech_only_tensor = collect_chunks(speech_timestamps, audio_tensor)
                            
                            # Log VAD retention
                            vad_percentage = (len(speech_only_tensor) / len(audio_tensor)) * 100 if len(audio_tensor) > 0 else 0.0
                            logger.info(f"VAD passed {vad_percentage:.2f}% of signal to analysis.", extra=LOG_D)
                            
                            segment_to_process = speech_only_tensor.numpy()
                        elif not global_vad_enabled:
                            logger.info("VAD disabled, processing full audio for ASR/Emotion/CLAP.", extra=LOG_D)
                            segment_to_process = audio_tensor.numpy()

                        if segment_to_process is not None:
                            # Create a temporary WAV file from the VAD-filtered segment for Emotion/CLAP
                            temp_segment_path = os.path.join(
                                project_root, 
                                "temp_speech_segments", 
                                f"{os.path.basename(audio_file).rsplit('.', 1)[0]}_segment.wav"
                            )
                            os.makedirs(os.path.dirname(temp_segment_path), exist_ok=True)
                            
                            from scipy.io.wavfile import write as wav_write
                            wav_write(temp_segment_path, 16000, segment_to_process)

                            # --- ASR Processing (Uses FULL chunk for context) ---
                            if global_asr_enabled and asr_model:
                                # The full chunk is still what gets decoded, for
                                # the context it carries. The VAD regions ride
                                # along so ASR can fall back to the speech alone
                                # when the whole clip decodes to nothing at all --
                                # on a mostly-silent clip Whisper answers with the
                                # prompt rather than with silence. Silero reports
                                # sample offsets at 16 kHz; seconds are what
                                # transcribe() works in.
                                speech_regions = [
                                    (s['start'] / 16000.0, s['end'] / 16000.0)
                                    for s in (speech_timestamps or [])]
                                transcription_text = asr_model.transcribe(
                                    temp_chunk_for_asr,
                                    speech_regions=speech_regions or None)
                                logger.info(f"ASR Result: '{transcription_text}'", extra=LOG_D)
                                if not (transcription_text or "").strip():
                                    # VAD found speech but nothing survived
                                    # decoding and hallucination filtering.
                                    transcription_text = "No Speech Detected (ASR)"

                                try:
                                    command = Voice_commands.detector.detect_command(transcription_text)
                                    if command == "stop":
                                        logger.info(
                                            f"Kokora stop heard from {device_name_early}: '{transcription_text}'",
                                            extra=LOG_D,
                                        )
                                        Voice_commands.state.trigger_mute(
                                            transcription_text, device_name_early, hours=1.0
                                        )
                                        Voice_commands.broadcaster.announce(device_name_early)
                                except Exception as e:
                                    logger.error(f"Error handling Kokora command: {str(e)}", extra=LOG_D)

                            # --- Emotion Analysis (Uses VAD-filtered segment) ---
                            # Only when something was actually said. The AST
                            # gate opens on the mere presence of a speech label
                            # among the top three, so clips reach here that VAD
                            # and the recogniser then reject -- and this model
                            # answers anyway, with apply_vad=False. That put
                            # "angry (0.86)" on a bouncing ball and "sad" on a
                            # cupboard door, and those labels do not stop at the
                            # dashboard: they flow into L1 emotion_evidence, the
                            # per-person emotion summary at L4, and the graph.
                            spoke = "No Speech Detected" not in (transcription_text or "No Speech Detected")
                            # Emotion needs a voice, not words.
                            #
                            # Requiring a transcript was right when the
                            # alternative was scoring dog barks as SAD, and it
                            # still is for (AST) and (VAD) rejections -- in
                            # those nothing heard a voice. The (ASR) case is
                            # the opposite: AST ranked speech in its top three
                            # AND Silero found speech regions, and only the
                            # decoder came back empty. There is a voice in the
                            # clip by construction, so there is something to
                            # score, and the speech-only segment VAD produced
                            # is what gets scored.
                            voice_without_words = (transcription_text or "").startswith(
                                "No Speech Detected (ASR)")
                            if global_emotion_enabled and emotion_model and (
                                    spoke or voice_without_words):
                                emotion_label, emotion_conf = emotion_model.predict(temp_segment_path, apply_vad=False)
                                # The four-class label scores at chance against
                                # a listener; the arousal/dominance/valence it
                                # is derived from have never been kept. Stored
                                # as a suffix so both readers of this column go
                                # on working unchanged.
                                emotion_tag = (f"{emotion_label.upper()} ({emotion_conf:.2f})"
                                               + dimensions_tag(emotion_model.last_dimensions))
                                logger.info(f"Emotion: {emotion_tag}", extra=LOG_D)
                            elif global_emotion_enabled and emotion_model:
                                logger.info("Emotion skipped: no speech was transcribed.",
                                            extra=LOG_D)

                            # Say what was actually established, rather than
                            # only what failed.
                            #
                            # "No Speech Detected (ASR)" reads as silence, and
                            # for 1360 events it is the opposite: AST ranked
                            # speech first, VAD found speech regions, and the
                            # speaker was often identified by name -- all of
                            # which was then thrown away because the decoder
                            # produced no words. The findings are appended
                            # after the prefix, never in front of it, because
                            # several reports and derive_home_dynamics'
                            # _is_nullish match on that prefix with startswith:
                            # moving it would silently change what every one of
                            # them counts as speech.
                            if voice_without_words:
                                heard = getattr(asr_model, "last_speakers", []) or []
                                transcription_text = (
                                    "No Speech Detected (ASR)"
                                    f" | AST: {sound1} {float(conf_sound1):.2f}"
                                    f" | VAD: speech"
                                    f" | speaker: {', '.join(heard) if heard else 'unidentified'}"
                                    f" | emotion: {emotion_tag or 'none'}")
                                logger.info(
                                    f"Speech heard but not transcribed: "
                                    f"{transcription_text}", extra=LOG_D)

                            # --- CLAP Embedding Generation (Uses VAD-filtered segment) ---
                            if global_clap_enabled and clap_service:
                                clap_embedding = clap_service.get_audio_embedding(temp_segment_path, vad_is_valid=global_vad_enabled)
                                if clap_embedding is not None:
                                    logger.info(f"CLAP embedding generated with shape: {clap_embedding.shape}", extra=LOG_D)

                            # _segment.wav is created and removed in the same
                            # scope, so it can be cleaned up here. The full
                            # chunk cannot -- see below.
                            if os.path.exists(temp_segment_path):
                                os.remove(temp_segment_path)

                    # temp_chunk_for_asr is written as soon as AST reports
                    # speech, but its cleanup used to live inside the
                    # "segment_to_process is not None" block above -- which
                    # only runs when VAD agrees. Every clip that AST called
                    # speech and VAD then rejected left its _full.wav behind:
                    # 946 orphans had accumulated, all of them from events
                    # with no transcript, and not one _segment.wav among them.
                    # Removing it here covers both outcomes.
                    try:
                        if temp_chunk_for_asr and os.path.exists(temp_chunk_for_asr):
                            os.remove(temp_chunk_for_asr)
                    except Exception as e:
                        logger.error(f"Could not remove {temp_chunk_for_asr}: {e}",
                                     extra=LOG_D)
                else:
                    logger.info("AST did not detect speech. Skipping VAD/ASR/Emotion analysis.", extra=LOG_D)
                    # Recorded explicitly rather than left NULL, so the
                    # dashboard can say "AST never classified this as
                    # speech" instead of falling back to a placeholder that
                    # looks identical to a VAD rejection.
                    transcription_text = "No Speech Detected (AST)"
                    # transcription_text and emotion_tag are already None
                # -----------------------------------
               
                # --- PATH CONSTRUCTION FOR PROCESSED WAV AND SPECTROGRAM ---
                # Determine base name without extension
                base_name = os.path.basename(audio_file).lower()
                if base_name.endswith("_rec.wav"):
                    name_root = base_name.replace("_rec.wav", "_rec")
                elif base_name.endswith("_rec.mp3"):
                    name_root = base_name.replace("_rec.mp3", "_rec")
                elif base_name.endswith("_rec.aac"):
                    name_root = base_name.replace("_rec.aac", "_rec")
                elif base_name.endswith("_rec.ogg"):
                    name_root = base_name.replace("_rec.ogg", "_rec")
                elif base_name.endswith("_rec.flac"):
                    name_root = base_name.replace("_rec.flac", "_rec")
                else:
                    # Fallback for unexpected extensions
                    name_root = os.path.splitext(base_name)[0]

                processed_wav_file = os.path.join(mp3_path, name_root + ".wav")
                spectrogram_image = os.path.join(specrogramm_images, name_root + ".png")

                # Incoming audio storage policy, set in Settings.
                #
                # Everything above this point has already run -- the capture
                # has passed the duplicate prefilter, been classified by AST
                # and, where there was speech, transcribed and scored for
                # emotion. This decides only whether the recording itself is
                # kept, so a discarded clip still produces a full database row;
                # what it loses is playback and the live spectrogram, both of
                # which read the audio back off disk.
                #
                # The speech test is AST's top three labels, the same
                # is_speech_in_ast that gates ASR above, so a clip is stored or
                # discarded on the same evidence that decided whether to
                # transcribe it.
                #
                # An unrecognised mode stores the audio: of the two ways to be
                # wrong, keeping a recording nobody wanted is recoverable and
                # deleting one they did want is not.
                storage_mode = (parameters.get_parameters()
                                .get('audio_storage', {})
                                .get('mode', 'speech_only'))
                if storage_mode == 'nothing':
                    store_audio = False
                elif storage_mode == 'speech_only':
                    store_audio = is_speech_in_ast
                elif storage_mode == 'non_speech_only':
                    store_audio = not is_speech_in_ast
                else:
                    if storage_mode != 'everything':
                        logger.warning(
                            f"Unknown audio_storage.mode {storage_mode!r}; "
                            f"storing the recording", extra=LOG_D)
                    store_audio = True

                # Save as normalized WAV
                from ast_src.convert_wav_to_mp3 import save_normalized_wav
                # Mirrors the spectrogram's store_png below: the write is
                # skipped while the branch and the filename are left intact, so
                # the database insert nested inside it is untouched.
                audio_written = True
                if store_audio:
                    audio_written = save_normalized_wav(
                        data, processed_wav_file) is not None
                else:
                    logger.info(
                        f"Not storing {name_root}: audio_storage.mode is "
                        f"'{storage_mode}' and AST "
                        f"{'found' if is_speech_in_ast else 'found no'} speech "
                        f"in its top three labels", extra=LOG_D)
                if audio_written:
                    # The dashboard computes the spectrogram live from the audio
                    # (SPECTROGRAM_RENDER_MODE = 'plotly'), so the PNG it used to
                    # read is written and never looked at again -- 15.2 GB had
                    # accumulated, growing about 1.8 GB a day.
                    #
                    # The call is not simply deleted because everything below,
                    # including the database insert, is nested inside its success
                    # check. Skipping generation while keeping the branch leaves
                    # that flow untouched, and spectrogram_image stays a valid
                    # name so insert_ast_table's not-None guard still passes.
                    store_png = parameters.get_parameters().get(
                        'spectrogram', {}).get('store_png', False)
                    spectrogram_written = True
                    if store_png:
                        spectrogram_written = create_freq_analyzer_spectrogram(
                            np.array(data, dtype=np.float16), sr, spectrogram_image) is not None
                    if spectrogram_written:
                        try:
                            # Remove the source file (WAV or MP3) after successful processing
                            if "audio_examples" not in audio_file:
                                os.remove(audio_file)
                        except Exception as e:
                            logger.error(f"Error removing source file: {str(e)} ")
                        
                        start_inf=datetime.now()
                        array_filename=os.path.basename(audio_file).split('_')
                        str_date=array_filename[0][0:4]+"-"+array_filename[0][4:6]+"-"+array_filename[0][6:8]
                        str_time=array_filename[1][0:2]+":"+array_filename[1][2:4]+":"+array_filename[1][4:6]
                        if array_filename[2].startswith('ex'):
                            device_name='ex'
                        else:
                            device_name=array_filename[2]
                        #final_audio = AudioSegment.from_mp3(processed_wav_file)
                        #final_rms=final_audio.rms
                        #final_dbrms=RMS_dB(final_rms)

                        # Capture quality, from the samples already decoded above,
                        # so nothing is re-read from disk. Computed for every
                        # event including non-speech: it describes the recording,
                        # not what was said. ~1 ms, against seconds of ASR.
                        event_snr = None
                        try:
                            from ast_src.dedupe_events import snr_from_samples
                            event_snr = snr_from_samples(data, sr)
                        except Exception as e:
                            logger.warning(f"Could not compute SNR: {e}", extra=LOG_D)

                        # latitude,longitude=get_lat_lon(ast_db,device_name)
                        new_event_id = ast_db.ast_table.insert_ast_table(date=str_date + " " + str_time ,
                                    device_name=device_name,
                                    file_name=os.path.basename(processed_wav_file),
                                    spectrogram_image=os.path.basename(spectrogram_image),
                                    sound1=sound1,conf_sound1=round(float(conf_sound1),2),
                                    sound2=sound2,conf_sound2=round(float(conf_sound2),2),
                                    sound3=sound3,conf_sound3=round(float(conf_sound3),2),
                                    rms=dbrms,
                                    transcription=transcription_text,
                                    emotion=emotion_tag,
                                    clap_embedding=clap_embedding, # Pass the CLAP embedding
                                    snr=event_snr)
                        
                        if new_event_id:
                            try:
                                verdict = online_scan.scan_latest(db_path, new_event_id)
                                ast_db.ast_table.update_ast_table(id=new_event_id, llm_verdict=verdict)
                                
                                # Check for Hazard and Send Alert
                                if verdict and ("[Hazard]" in verdict or "YES" in verdict.upper() or ("OK" not in verdict and len(verdict) > 5)):
                                    # Simple heuristic: if it says [Hazard] or YES (from prompt rules)
                                    # or if it's NOT just "OK" (and has some length explanation)
                                    # Adjust logic based on prompt output structure.
                                    # Current prompt says: "Reply strictly as: OK or [Hazard] <brief explanation>."
                                    if "[Hazard]" in verdict:
                                         send_alert_email(str_date + " " + str_time, verdict)
                                         
                            except Exception as e_scan:
                                logger.error(f"Online scan failed: {e_scan}", extra=LOG_D)
                        
                        logger.info(f"Write rec to DB : {(datetime.now()-start_inf).total_seconds()*1000:.0f} msec",extra=LOG_D)
                    else:
                        logger.error(f"Error create spectrogram",extra=LOG_D)
                        try:    
                            logger.warning(f"Delete file {audio_file}",extra=LOG_D)
                            os.remove(audio_file)
                        except:
                            pass
                        try:
                            logger.warning(f"Delete file {mp3_file}",extra=LOG_D)
                            os.remove(mp3_file)        
                        except:
                            pass
                else:
                    logger.error(f"Error Convert to mp3")
                    try:
                        logger.warning(f"Delete file {audio_file}",extra=LOG_D)
                        os.remove(audio_file)    
                    except:
                        pass
            else:
                try:
                    logger.error(f"Zero audio file",extra=LOG_D)
                    logger.warning(f"Delete file {audio_file}",extra=LOG_D)
                    os.remove(audio_file)
                except:
                    pass
            temperature=check_CPU_temp()
            if not temperature  is None and temperature>90:
                logger.warning(f"Warning Hi Temperature {temperature}",extra=LOG_D)
                time.sleep(10)
            
            # Free up memory after each file processing iteration
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                
        if wait>0:    
            logger.info(f"Nothing in loop wait ",extra=LOG_D) 
            wait=0

        # Periodic heartbeat so an idle console still shows the pipeline is alive
        # and whether the sentinels are actually reaching the broker.
        if time.time()-last_status >= STATUS_INTERVAL_SEC:
            last_status=time.time()
            report_pipeline_status(started_at, processed_total)

        if os.environ.get("RUN_ONCE"):
            logger.info("RUN_ONCE set, exiting loop.", extra=LOG_D)
            break
            
        time.sleep(10)

        

if __name__ == "__main__":
    # Fix DB Path to be project-relative
    db_name = parameters.get_parameters()['sqlite']['db_name']
    if not os.path.isabs(db_name):
        db_path = os.path.join(project_root, db_name)
    else:
        db_path = db_name
        
    ast_db=Ast_Sql_lite(db_path)
    ast=AST()
    
    # Initialize Speech & Emotion Models
    emotion_model = EmotionRecognizer()
    asr_model = ASRTranscriber()

    # speechbrain/pyannote call logging.basicConfig() while their models load,
    # which re-enables root DEBUG output. Re-apply the quiet levels afterwards.
    quiet_third_party_loggers()

    try:
        # Initialize and start the CommunicationManager
        receiver = CommunicationManager.get_instance()
        receiver.start()
    except Exception as e:
        logger.error(f"Failed to start Communication Manager: {e}", extra=LOG_D)
        banner("MQTT INGESTION UNAVAILABLE", [
            ("Error", e),
            ("Consequence", "No sentinel audio will be received"),
            ("Check", "Is the Mosquitto broker window running?"),
        ], char="!")

    report_startup(db_path)
    proc(ast,ast_db, db_path, emotion_model, asr_model)
    ast_db.close()
    