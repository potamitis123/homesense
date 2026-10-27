import sys
import warnings
import time
import collections
import datetime
import numpy as np
import pyqtgraph as pg
import librosa
import pyaudio
import torch
from PyQt5 import QtWidgets, QtCore, QtGui
from ast_model import AST

# --- Config & Setup ---
warnings.filterwarnings("ignore")
pg.setConfigOptions(useOpenGL=True, antialias=True)

class AIWorker(QtCore.QThread):
    prediction_ready = QtCore.pyqtSignal(list)

    def __init__(self, audio_deque, mutex):
        super().__init__()
        self.audio_deque = audio_deque
        self.mutex = mutex
        self.running = False
        self.log_filename = ""
        self.ast = None

    def start_session(self, log_name):
        self.log_filename = log_name
        self.running = True
        self.start()

    def run(self):
        print(f"⏳ Loading Unified AST Model...")
        self.ast = AST()
        print(f"✅ Model Ready on {self.ast.device}.")

        while self.running:
            self.predict_latest()
            # Stride: ~1 second update rate (High Overlap)
            self.msleep(900) 

    def predict_latest(self):
        self.mutex.lock()
        try:
            raw_data = np.array(self.audio_deque)
        finally:
            self.mutex.unlock()

        if len(raw_data) < 1600: # Need at least 0.1s to try
            return

        # AST.inference handles padding and feature extraction
        # It expects a waveform (numpy array) and sample_rate
        # We use chunk_duration=10.24 to match previous behavior if desired, 
        # but default 10.0 is fine.
        preds = self.ast.inference(waveform=raw_data, sample_rate=16000)
        
        # Convert [{'sound': label, 'conf': score}, ...] to [(label, score), ...]
        results = [(p['sound'], p['conf']) for p in preds]

        self.log_to_disk(results)
        self.prediction_ready.emit(results)

    def log_to_disk(self, results):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        res_str = " | ".join([f"{r[0]} ({r[1]:.1%})" for r in results])
        with open(self.log_filename, "a") as f:
            f.write(f"[{timestamp}] {res_str}\n")

    def stop_session(self):
        self.running = False
        self.wait()

class BioAcousticViewer(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Live Bioacoustic Monitor (GPU Accelerated)")
        self.resize(1200, 750)
        self.setStyleSheet("background-color: #0c0c0c; color: #EEE;")

        self.SR = 16000
        self.is_recording = False
        self.start_time = 0
        self.elapsed = 0.0
        
        self.mutex = QtCore.QMutex()
        self.audio_deque = collections.deque(maxlen=int(self.SR * 30)) 
        self.p = pyaudio.PyAudio()
        
        self.ai_worker = AIWorker(self.audio_deque, self.mutex)
        self.ai_worker.prediction_ready.connect(self.update_ui_labels)

        self.setup_ui()

        self.timer_ui = QtCore.QTimer()
        self.timer_ui.setInterval(20) # 50 FPS
        self.timer_ui.timeout.connect(self.update_loop)
        self.timer_ui.start()

    def setup_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # AI HUD
        self.lbl_ai = QtWidgets.QLabel("SYSTEM READY - PRESS START")
        self.lbl_ai.setFont(QtGui.QFont("Consolas", 13, QtGui.QFont.Bold))
        self.lbl_ai.setStyleSheet("color: #00ff00; background: #1a1a1a; padding: 12px; border: 1px solid #333;")
        layout.addWidget(self.lbl_ai)

        # Plot
        self.plot = pg.PlotWidget()
        self.plot.setLabel('bottom', 'Recording Time', units='s')
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        layout.addWidget(self.plot)

        self.img = pg.ImageItem()
        self.img.setLookupTable(pg.colormap.get('inferno').getLookupTable())
        self.img.setLevels([-70, 0])
        self.plot.addItem(self.img)

        # Controls
        ctrl = QtWidgets.QHBoxLayout()
        self.btn = QtWidgets.QPushButton("START MONITORING")
        self.btn.setFixedHeight(50)
        self.btn.setStyleSheet("background: #1b5e20; font-weight: bold; border-radius: 4px;")
        self.btn.clicked.connect(self.toggle)
        
        self.lbl_time = QtWidgets.QLabel("Elapsed: 0.0s")
        self.lbl_time.setFont(QtGui.QFont("Consolas", 12))
        
        ctrl.addWidget(self.btn, 4)
        ctrl.addWidget(self.lbl_time, 1)
        layout.addLayout(ctrl)

    def toggle(self):
        if not self.is_recording:
            # START
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.log_name = f"ast_recognition_log_{ts}.txt"
            
            self.audio_deque.clear()
            self.start_time = time.time()
            self.elapsed = 0.0
            
            self.stream = self.p.open(format=pyaudio.paFloat32, channels=1, rate=self.SR,
                                      input=True, frames_per_buffer=1024,
                                      stream_callback=self.audio_cb)
            
            self.ai_worker.start_session(self.log_name)
            self.btn.setText("STOP MONITORING")
            self.btn.setStyleSheet("background: #b71c1c; font-weight: bold;")
            self.is_recording = True
        else:
            # STOP
            self.is_recording = False
            self.ai_worker.stop_session()
            self.stream.stop_stream()
            self.stream.close()
            self.btn.setText("START MONITORING")
            self.btn.setStyleSheet("background: #1b5e20; font-weight: bold;")

    def audio_cb(self, in_data, frame_count, time_info, status):
        data = np.frombuffer(in_data, dtype=np.float32)
        self.mutex.lock()
        self.audio_deque.extend(data)
        self.mutex.unlock()
        return (in_data, pyaudio.paContinue)

    def update_loop(self):
        if not self.is_recording: return

        self.elapsed = time.time() - self.start_time
        self.lbl_time.setText(f"Elapsed: {self.elapsed:.1f}s")

        self.mutex.lock()
        samples = np.array(self.audio_deque)
        self.mutex.unlock()

        if len(samples) < 512: return

        # Visual: Last 10s
        view_len = int(10 * self.SR)
        chunk = samples[-view_len:]
        S = np.abs(librosa.stft(chunk, n_fft=1024, hop_length=512))
        S_db = librosa.amplitude_to_db(S, ref=np.max)
        
        self.img.setImage(S_db.T, autoLevels=False)
        
        # X-axis Logic: Use Elapsed Time
        x_end = self.elapsed
        x_start = max(0, x_end - 10)
        self.img.setRect(QtCore.QRectF(x_start, 0, (x_end - x_start), self.SR/2000))
        self.plot.setXRange(x_start, x_end, padding=0)

    def update_ui_labels(self, results):
        res_str = " | ".join([f"{r[0].upper()} ({r[1]:.1%})" for r in results])
        self.lbl_ai.setText(f"LIVE DETECT: {res_str}")

    def closeEvent(self, event):
        if self.is_recording: self.toggle()
        self.p.terminate()
        event.accept()

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = BioAcousticViewer()
    win.show()
    sys.exit(app.exec_())