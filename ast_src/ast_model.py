from transformers import AutoFeatureExtractor, ASTForAudioClassification
import torch
import librosa
import numpy as np
from datetime import datetime
import sys
#sys.path.append('./misc')
from misc.log_test import logger
from misc.load_parameters import parameters
from common_files.device_handler import device # Use the global device handler

LOG_D = {'module_name': 'ast_model'}

class AST():
    def __init__(self, model_name=None):
        if model_name is None:
            model_name = parameters.get_parameters().get('audio', {}).get('ast_model_name', "bookbot/distil-ast-audioset")
        
        self.model_name = model_name
        self._samplerate_ast = 16000
        
        try:
            self.device = device # Use the global device
            self.feature_extractor = AutoFeatureExtractor.from_pretrained(self.model_name)
            self.model = ASTForAudioClassification.from_pretrained(self.model_name)
            self.model.to(self.device)
            self.model.eval()
            logger.info(f"AST model {self.model_name} loaded successfully on {self.device}.", extra=LOG_D)
        except Exception as e:
            logger.error(f"Error loading AST model: {e}", extra=LOG_D)
            # Fallback to dummy behavior if model fails to load
            self.model = None
            self.device = torch.device('cpu') # Fallback explicitly to CPU
        
    def inference(self, waveform, chunk_duration=10.0, sample_rate=16000):
        if self.model is None:
            logger.warning("AST model not loaded. Returning dummy data.", extra=LOG_D)
            return [
                {'sound': 'Model not loaded', 'conf': 0.0},
                {'sound': 'None', 'conf': 0.0},
                {'sound': 'None', 'conf': 0.0},
            ]

        try:
            # 1. Resample entire waveform to 16kHz first
            # This is critical so that chunk calculations match the model's expectation
            if sample_rate != self._samplerate_ast:
                waveform = librosa.resample(waveform, orig_sr=sample_rate, target_sr=self._samplerate_ast)
            
            # 2. Setup Chunking
            chunk_samples = int(chunk_duration * self._samplerate_ast) # 16000 * 10 = 160000 samples
            total_length = len(waveform)
            
            # Tensor to hold the max probabilities across all chunks
            aggregated_logits = None
            
            # 3. Iterate through chunks
            for i in range(0, total_length, chunk_samples):
                # Slice the chunk
                chunk = waveform[i : i + chunk_samples]
                
                # 4. PAD WITH ZEROS if the chunk is smaller than 10 seconds
                if len(chunk) < chunk_samples:
                    padding_needed = chunk_samples - len(chunk)
                    # (0, padding_needed) means: add 0 items to start, 'padding_needed' items to end
                    chunk = np.pad(chunk, (0, padding_needed), mode='constant', constant_values=0)
                
                # Prepare inputs for this specific chunk
                # return_tensors="pt" creates a PyTorch tensor automatically
                inputs = self.feature_extractor(chunk, sampling_rate=self._samplerate_ast, return_tensors="pt")
                
                # Move inputs to GPU/CPU
                inputs = {k: v.to(self.device) for k, v in inputs.items()}

                with torch.no_grad():
                    outputs = self.model(**inputs)
                    logits = outputs.logits # Shape: [1, 527]

                # 5. Perform Max Pooling
                # We compare the current chunk's logits with the best we've seen so far
                if aggregated_logits is None:
                    aggregated_logits = logits
                else:
                    # element-wise maximum
                    aggregated_logits = torch.max(aggregated_logits, logits)

            # 6. Final Prediction based on the "Max Pooled" logits
            return self.__show_prediction(aggregated_logits)

        except Exception as e:
            logger.error(f"Inference error: {e}", extra=LOG_D)
            return [{'sound': 'Error', 'conf': 0.0}] * 3
    
    def __show_best_prediction(self, logits):
        predictions = self.__show_prediction(logits)
        return predictions[0] if predictions else {'sound': 'None', 'conf': 0.0}
            
    def __show_prediction(self, logits, top_k=3):
        if logits is None:
            return [{'sound': 'None', 'conf': 0.0}] * top_k

        # Apply softmax to get probabilities
        probs = torch.nn.functional.softmax(logits, dim=-1)
        # Get top k results
        top_results = torch.topk(probs, top_k, dim=-1)
        
        scores = top_results.values[0].tolist()
        indices = top_results.indices[0].tolist()
        
        results = []
        for score, idx in zip(scores, indices):
            label = self.model.config.id2label[idx]
            results.append({'sound': label, 'conf': round(float(score), 4)})
            
        return results

    def get_classes(self):
        if self.model:
            return list(self.model.config.label2id.keys())
        return [
            'Chainsaw', 'Engine', 'Drill', 'Siren', 'Speech', 'Music', 'Dog Bark',
            'Squeak', 'Water', 'Alarm', 'Baby cry', 'Fire alarm', 'Bird song',
            'Cat purr', 'Clock alarm', 'Doorbell', 'Gunshot', 'Knock', 'Telephone',
            'Vehicle', 'Wind noise'
        ]

    def get_device_name(self):
        if self.device.type == 'cuda':
            return f"GPU: {torch.cuda.get_device_name(0)}"
        return "CPU"