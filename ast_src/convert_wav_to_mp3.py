from pydub import AudioSegment
import os
from datetime import datetime
import sys
from io import BytesIO
import numpy as np
#sys.path.append('./misc')
from misc.log_test import logger
LOG_D={'module_name':'convert_wav_to_mp3'}
DEFAULT_BITRATE='64k'

def RMS_dB(rms, num_dig=2):
    return round(20*np.log10(rms),num_dig)

def save_normalized_wav(data, wav_path):
    try:
        start_inf=datetime.now()
        
        # Normalize data: x / max(abs(x))
        if len(data) > 0:
            peak = np.max(np.abs(data))
            if peak > 0:
                data = data / peak

        int16_audio = (data * 32767).astype(np.int16)
        audio = AudioSegment.from_raw(BytesIO(int16_audio),
                                    sample_width=2, 
                                    channels=1,
                                    frame_rate=16000
                                    )
        # Export as WAV (very reliable, no complex encoder needed)
        audio.export(wav_path, format="wav")

        logger.info(f"Saved normalized WAV \n \t\t\t\t\t File name: {wav_path} \
                      \n \t\t\t\t\t Duration: {audio.duration_seconds:.2f}s", extra=LOG_D)
        return wav_path
        
    except Exception as e:
        logger.error(f"Error saving WAV file: {str(e)} ", extra=LOG_D)
        return None

def convert_wav_to_mp3(wav_path, mp3_path=None, bitrate=None):
   
    try:
        # If mp3_path is not provided, create it from wav_path
        # Load WAV file
        if bitrate is None:
            bitrate=DEFAULT_BITRATE
        start_inf=datetime.now()
        if '.wav' in wav_path:
            audio = AudioSegment.from_wav(wav_path)
        elif '.mp3' in wav_path:
            audio = AudioSegment.from_mp3(wav_path)
        else:
            logger.error(f"Not Supported audio file")
            return None
        audio
        # Export as MP3
        audio.export(
            mp3_path,
            format='mp3',
            bitrate=bitrate,
            tags={
                'artist': 'Converted with Python',
                'title': os.path.basename(mp3_path)
            }
        )
        logger.info(f"Successfully converted {wav_path} to {mp3_path}",extra=LOG_D)
        logger.info(f"Convert to mp3 : {(datetime.now()-start_inf).total_seconds()*1000:.0f} msec",extra=LOG_D)
        return mp3_path
        
    except Exception as e:
        logger.error(f"Error converting file: {str(e)} ",extra=LOG_D)
        return None
    

def convert_raw_data_to_mp3(data, mp3_path=None, bitrate=None):
    if bitrate is None:
            bitrate=DEFAULT_BITRATE
    try:
        start_inf=datetime.now()
        
        # Normalize data: x / max(abs(x))
        if len(data) > 0:
            peak = np.max(np.abs(data))
            if peak > 0:
                data = data / peak

        int16_audio = (data * 32767).astype(np.int16)
        audio = AudioSegment.from_raw(BytesIO(int16_audio),
                                    sample_width=2, 
                                    channels=1,
                                    frame_rate=16000
                                    )
        # Export as MP3
        audio.export(
            mp3_path,
            format='mp3',
            bitrate=bitrate,
            parameters=["-q:a", "0"],
            #tags={
            #    'title': os.path.basename(mp3_path)
            #}
        )

        logger.info(f"Convert to mp3 Final \n \t\t\t\t\t File name: {mp3_path} \
                      \n \t\t\t\t\t Sample width: {audio.sample_width} \
                      \n \t\t\t\t\t Sample rate: {audio.frame_rate} \
                      \n \t\t\t\t\t RMS: {audio.rms} \
                      \n \t\t\t\t\t RMS (dB): {RMS_dB(audio.rms)} \
                      \n \t\t\t\t\t Channels: {audio.channels} \
                      \n \t\t\t\t\t Bitrate: {bitrate} \
                      \n \t\t\t\t\t Quality 0 (best 0 worst 9)",extra=LOG_D)
                    
        logger.info(f"Convert to mp3 : {(datetime.now()-start_inf).total_seconds()*1000:.0f} msec",extra=LOG_D)
        return mp3_path
        
    except Exception as e:
        logger.error(f"Error converting file: {str(e)} ",extra=LOG_D)
        return None
    
def convert_audio_to_mp3(audio, mp3_path=None, bitrate=None):
    if bitrate is None:
            bitrate=DEFAULT_BITRATE
    try:
        start_inf=datetime.now()
        # Export as MP3
        audio.export(
            mp3_path,
            format='mp3',
            bitrate=bitrate,
            tags={
                'artist': 'Converted with Python',
                'title': os.path.basename(mp3_path)
            }
        )
        logger.info(f"Successfully converted  to {mp3_path}",extra=LOG_D)
        logger.info(f"Convert to mp3 : {(datetime.now()-start_inf).total_seconds()*1000:.0f} msec",extra=LOG_D)
        return mp3_path
        
    except Exception as e:
        logger.error(f"Error converting file: {str(e)} ",extra=LOG_D)
        return None

# Example usage
if __name__ == "__main__":
    # Single file conversion
    wav_file = "assets/55.wav"
    mp3_file = "assets/552.mp3"
    convert_wav_to_mp3(wav_file, mp3_file, bitrate='64k')#320k
    
   