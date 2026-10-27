import numpy as np
import matplotlib.pyplot as plt
import librosa
import librosa.display
import sounddevice as sd
from matplotlib.widgets import Button
import scipy.io.wavfile as wav
import time

# 1. Setup Audio Data (5 seconds)
sr = 22050
duration = 5.0
t = np.linspace(0, duration, int(sr * duration))
y = (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 659 * t)) * 0.5

# 2. Compute Spectrogram
S = librosa.feature.melspectrogram(y=y, sr=sr)
S_dB = librosa.power_to_db(S, ref=np.max)

# 3. Initialize the UI
fig, ax = plt.subplots(figsize=(12, 7))
plt.subplots_adjust(bottom=0.25)

# Display spectrogram WITHOUT colorbar
img = librosa.display.specshow(S_dB, x_axis='time', y_axis='mel', sr=sr, ax=ax, cmap='viridis')
ax.set_title('Synchronized Spectrogram Viewer', fontsize=14, fontweight='bold')
ax.set_xlabel('Time (s)', fontsize=11)
ax.set_ylabel('Frequency (Hz)', fontsize=11)

# Add grid
ax.grid(True, which='both', color='white', linestyle='--', linewidth=0.5, alpha=0.4)

# Moving progress bar (white vertical line)
playhead = ax.axvline(x=0, color='white', linewidth=3, alpha=0.9, linestyle='-')

# 4. Audio Control Logic
class AudioPlayer:
    def __init__(self, audio_data, samplerate):
        self.data = audio_data
        self.sr = samplerate
        self.playing = False
        self.start_clock = 0
        self.timer = None

    def play(self, event):
        if not self.playing:
            self.playing = True
            self.start_clock = time.time()
            sd.play(self.data, self.sr)
            # Start animation
            self.timer = fig.canvas.new_timer(interval=50)  # Update every 50ms
            self.timer.add_callback(self.update_bar)
            self.timer.start()

    def stop(self, event):
        self.playing = False
        sd.stop()
        if self.timer:
            self.timer.stop()
        playhead.set_xdata([0])
        fig.canvas.draw_idle()

    def save_file(self, event):
        filename = "exported_audio.wav"
        wav.write(filename, self.sr, (self.data * 32767).astype(np.int16))
        print(f"Audio saved as {filename}")

    def update_bar(self):
        if self.playing:
            elapsed = time.time() - self.start_clock
            if elapsed >= duration:
                self.stop(None)
            else:
                playhead.set_xdata([elapsed])
                fig.canvas.draw_idle()

player = AudioPlayer(y, sr)

# 5. UI Buttons
ax_play = plt.axes([0.25, 0.05, 0.15, 0.08])
ax_stop = plt.axes([0.45, 0.05, 0.15, 0.08])
ax_save = plt.axes([0.65, 0.05, 0.18, 0.08])

btn_play = Button(ax_play, '▶ PLAY', color='#2ecc71', hovercolor='#27ae60')
btn_stop = Button(ax_stop, '■ STOP', color='#e74c3c', hovercolor='#c0392b')
btn_save = Button(ax_save, '💾 SAVE', color='#3498db', hovercolor='#2980b9')

btn_play.on_clicked(player.play)
btn_stop.on_clicked(player.stop)
btn_save.on_clicked(player.save_file)

plt.tight_layout()
plt.show()