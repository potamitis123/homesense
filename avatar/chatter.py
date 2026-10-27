# -*- coding: utf-8 -*-
"""
Created on Sun Aug 16 17:13:27 2026

@author: potam
"""

import os
import torch
import torchaudio as ta
from chatterbox.tts import ChatterboxTTS

def main():
    # 1. Choose hardware acceleration ('cuda' for NVIDIA GPU, 'cpu' for default)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"🤖 Loading Chatterbox TTS on: {device.upper()}...")
    
    # 2. Initialize the model (weights will automatically download on first run)
    model = ChatterboxTTS.from_pretrained(device=device)
    print("✅ Model loaded successfully!")

    # 3. Define the text you want to synthesize
    text_prompt = "Hello! I am testing Chatterbox TTS directly from my Anaconda environment. It sounds surprisingly natural!"
    
    # 4. Optional: Path to a 5-10 second clean audio clip for voice cloning
    # Replace this with your own .wav file path to clone a specific voice
    reference_voice_path = "my_voice_sample.wav" 

    print("🎙️ Synthesizing speech...")
    
    if os.path.exists(reference_voice_path):
        print(f"✨ Found reference voice! Cloning voice from: {reference_voice_path}")
        # Generate audio using the cloned voice sample
        wav = model.generate(text_prompt, audio_prompt_path=reference_voice_path)
        output_filename = "chatterbox_cloned_output.wav"
    else:
        print("💡 No reference voice sample found. Generating with the default base voice.")
        # Generate audio using the default base model voice
        wav = model.generate(text_prompt)
        output_filename = "chatterbox_default_output.wav"

    # 5. Save the generated audio file to disk
    ta.save(output_filename, wav, model.sr)
    print(f"🎉 Success! Audio saved as '{output_filename}' in your current directory.")

if __name__ == "__main__":
    main()
