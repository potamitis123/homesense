from transformers import WhisperProcessor, WhisperForConditionalGeneration
import torch
import torch.quantization
from datetime import datetime
import sys
#sys.path.append('./misc')
from misc.log_test import logger
from common_files.device_handler import device # Use the global device handler
LOG_D={'module_name':'whisper'}

class Whisper():
    def __init__(self):
        init_start_inf = datetime.now()
        self.model_id = "openai/whisper-medium"
        torch_dtype = torch.float16 if device.type == "cuda" else torch.float32
        self.processor = WhisperProcessor.from_pretrained(self.model_id)
        self.device = device

        self.model = WhisperForConditionalGeneration.from_pretrained(
            self.model_id, 
            torch_dtype=torch_dtype, 
            low_cpu_mem_usage=True, 
            use_safetensors=True
        )
        self.model.eval()
        self.model.to(self.device)
        total_start_inf = datetime.now()
        logger.info(f"Init Whisper : {(datetime.now()-init_start_inf).total_seconds()*1000:.0f} msec", extra=LOG_D)
    
    def inference(self, waveform, sample_rate, language=None, task="transcribe"):
        init_start_inf = datetime.now()
        
        # Convert to float32 and normalize if not already
        if waveform.dtype != np.float32:
            waveform = waveform.astype(np.float32) / 32768.0
        
        input_features = self.processor(
            waveform, 
            sampling_rate=sample_rate, 
            return_tensors="pt",
            padding=True
        ).input_features
    
        # Move input to same device as model
        input_features = input_features.to(self.device)
        attention_mask = torch.ones_like(input_features).to(self.device)

        generate_kwargs = {
            "attention_mask": attention_mask,
            "no_repeat_ngram_size": 3,
            "length_penalty": 1.0,
            "num_beams": 1
        }
    
        if language:
            forced_decoder_ids = self.processor.get_decoder_prompt_ids(
                language=language,
                task=task
            )
            generate_kwargs["forced_decoder_ids"] = forced_decoder_ids
    
        with torch.no_grad():
            predicted_ids = self.model.generate(
                input_features,
                **generate_kwargs
            )
        
        transcription = self.processor.batch_decode(
            predicted_ids, 
            skip_special_tokens=True
        )[0]
        
        # Free up VRAM
        del input_features
        del predicted_ids
        if self.device.type == 'cuda':
            torch.cuda.empty_cache()
            
        total_start_inf = datetime.now()
        logger.info(f"Total Whisper inference: {(datetime.now()-init_start_inf).total_seconds()*1000:.0f} msec", extra=LOG_D)
        return transcription

if __name__ == "__main__":
    from pydub import AudioSegment
    import sqlite3
    import os
    import numpy as np
    
    whisper = Whisper()
    conn = sqlite3.connect("/home/giannis/ast_audio_net/ast_audio_db/ast_audio.db")
    cursor = conn.cursor()
    cursor.execute("SELECT file_name FROM ast_table WHERE sound1='Speech' ORDER BY date DESC")
    speech_files = cursor.fetchall()
    
    for speech_file in speech_files:
        speech_full_path = os.path.join('/home/giannis/ast_audio_net/assets/audio_mp3', speech_file[0])
        audio = AudioSegment.from_mp3(speech_full_path)
        data = np.array(audio.get_array_of_samples(), dtype=np.float32) / 32768.0
        
        # Handle stereo audio
        if audio.channels == 2:
            data = data.reshape(-1, 2).mean(axis=1)
            
        sr = audio.frame_rate
        speech_text = whisper.inference(waveform=data, sample_rate=sr, language='el')
        print(speech_full_path, speech_text)
        
    conn.close()