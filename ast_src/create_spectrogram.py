import librosa
import librosa.display
import matplotlib
matplotlib.use('Agg') # Force non-interactive backend for thread safety
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from datetime import datetime
import sys
import os # Added import os
#sys.path.append('./misc')
from misc.log_test import logger
LOG_D={'module_name':'create_spectrogram'}


def create_spectrogram(audio_data,audio_sr ,save_path=None):
    try:
        # Create figure and axes
        plt.figure(figsize=(12, 8))
        
        # Create spectrogram
        D = librosa.amplitude_to_db(np.abs(librosa.stft(audio_data)), ref=np.max)
        
        # Display spectrogram
        librosa.display.specshow(
            D,
            y_axis='hz',
            x_axis='time',
            sr=audio_sr,
            cmap='magma_r',  # Similar to the purple-orange colormap in your example
            vmin=-120,  # Adjust these values to match your desired dB range
            vmax=0
        )
        
        # Customize the plot
        plt.colorbar(format='%+2.0f dB')
        plt.title('Spectrogram',pad=10)
        
        # Add frequency axis labels
        plt.ylabel('Frequency (kHz)')
        plt.xlabel('Time (s)')
        
        # Adjust layout
        plt.tight_layout()
        
        # Save if path is provided
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            plt.close()
            return save_path
        else:
            return None
       
    except Exception as error:
        logger.error(f"Error create spectrogram error {str(error)} ",extra=LOG_D)
        return None
    

def create_freq_analyzer_spectrogram(audio_data, audio_sr, save_path=None, event_id=None):
    title_font=25
    axis_font=20
    tick_font=15

    try:
        start_inf=datetime.now()

        # Υπολογισμός STFT με συγκεκριμένο μέγεθος παραθύρου
        n_fft = 2048  # Μέγεθος παραθύρου FFT
        D = librosa.stft(audio_data, n_fft=n_fft)
        D_db = librosa.amplitude_to_db(np.abs(D), ref=np.max)
        
        # Υπολογισμός του μέσου φάσματος συχνοτήτων
        freq_spectrum = np.mean(np.abs(D), axis=1)
        freq_db = librosa.amplitude_to_db(freq_spectrum, ref=np.max)
        frequencies = librosa.fft_frequencies(sr=audio_sr, n_fft=n_fft)
        
        # Δημιουργία figure με τρία subplots
        fig = plt.figure(figsize=(14, 12))
        gs = plt.GridSpec(3, 1, height_ratios=[1, 1, 3], hspace=0.4)

        # Audio Waveform (πάνω subplot - NEW)
        ax_wave = fig.add_subplot(gs[0])
        duration = len(audio_data) / audio_sr
        time_axis = np.linspace(0, duration, len(audio_data))
        ax_wave.plot(time_axis, audio_data, color='#2C3E50', linewidth=0.5)
        ax_wave.set_xlim(0, duration)
        ax_wave.set_ylabel('Amplitude', fontsize=axis_font)
        ax_wave.set_xlabel('Time (s)', fontsize=axis_font)
        
        # Update title to include event_id if provided
        title_text = 'Audio Waveform'
        if event_id:
            title_text += f' (Event ID: {event_id})'
        ax_wave.set_title(title_text, pad=15, fontsize=title_font, fontweight='bold')
        
        ax_wave.grid(True, linestyle='--', alpha=0.5, color='gray')
        ax_wave.tick_params(axis='x', labelsize=tick_font)
        ax_wave.tick_params(axis='y', labelsize=tick_font)
        ax_wave.set_facecolor('#f8f9fa')

        
        # Frequency Analyzer (πάνω subplot)
        ax0 = fig.add_subplot(gs[1])
        ax0.plot(frequencies, freq_db, color='#4682B4', linewidth=2)
        ax0.set_xlim(0, 8000)  # Limit to 8 kHz
        ax0.set_ylim(freq_db.min() - 5, freq_db.max() + 10)
        ax0.set_ylabel('Magnitude (dB)', fontsize=axis_font)
        ax0.set_title('Frequency Analyzer', pad=15, fontsize=title_font, fontweight='bold')
        ax0.grid(True, linestyle='--', alpha=0.5, color='gray')
        
        # Προσθήκη kHz labels στον άξονα x του analyzer
        ax0.set_xlabel('Frequency (kHz)', fontsize=axis_font)
        x_ticks = np.linspace(0, 8000, 9)  # 0, 1k, 2k, ..., 8k
        ax0.set_xticks(x_ticks)
        ax0.set_xticklabels([f'{x/1000:.0f}' for x in x_ticks])
        ax0.tick_params(axis='x', labelsize=tick_font)
        ax0.tick_params(axis='y', labelsize=tick_font)
        ax0.set_facecolor('#f8f9fa')
            
        # Spectrogram (κάτω subplot) - FIX: Use linear frequency scale
        ax1 = fig.add_subplot(gs[2])

        img = librosa.display.specshow(
            D_db,
            y_axis='linear',  # Changed from 'hz' to 'linear'
            x_axis='time',
            sr=audio_sr,
            ax=ax1,
            cmap='magma_r',
            vmin=D_db.min(),
            vmax=D_db.max(),
        )
        
        # FIX: Set y-axis limits to 0-8000 Hz
        ax1.set_ylim(0, 8000)
        
        # Προσαρμογή των ετικετών του spectrogram
        ax1.set_ylabel('Frequency (Hz)', fontsize=axis_font)
        ax1.set_xlabel('Time (s)', fontsize=axis_font)
        ax1.set_title('Spectrogram', fontsize=title_font, pad=15, fontweight='bold')
        ax1.tick_params(axis='x', labelsize=tick_font)
        ax1.tick_params(axis='y', labelsize=tick_font)

        # FIX: Set specific y-ticks in Hz (0, 1000, 2000, ..., 8000)
        y_ticks = np.linspace(0, 8000, 9)
        ax1.set_yticks(y_ticks)
        ax1.set_yticklabels([f'{int(y)}' for y in y_ticks])
        
        # Add subtle grid on spectrogram
        ax1.grid(True, linestyle='--', alpha=0.3, color='white', linewidth=0.5)
       
        # Ρύθμιση του layout
        fig.subplots_adjust(left=0.08, right=0.95, top=0.96, bottom=0.06)
        
        logger.info(f"Create spectrogram and freq analyzer : {(datetime.now()-start_inf).total_seconds()*1000:.0f} msec",extra=LOG_D) 
                            
        # Αποθήκευση ή εμφάνιση
        if save_path:
            fig.savefig(save_path, dpi=300, bbox_inches='tight')
            plt.close(fig)
            return save_path
        else:
            return None
            
    except Exception as error:
        logger.error(f"Error create Freq Analyzer error {str(error)} ",extra=LOG_D)
        return None

def create_freq_analyzer_spectrogram2(audio_data, audio_sr, save_path=None):
    try:
        start_inf=datetime.now()

        # Υπολογισμός STFT
        n_fft = 2048
        D = librosa.stft(audio_data, n_fft=n_fft)
        D_db = librosa.amplitude_to_db(np.abs(D), ref=np.max)
        
        # Υπολογισμός του μέσου φάσματος συχνοτήτων
        freq_spectrum = np.mean(np.abs(D), axis=1)
        freq_db = librosa.amplitude_to_db(freq_spectrum, ref=np.max)
        
        # Συχνότητες για τον άξονα x
        freq_bands = ['31.5', '63', '125', '250', '500', '1k', '2k', '4k', '8k', '16k']
        frequencies = librosa.fft_frequencies(sr=audio_sr, n_fft=n_fft)
        
        # Ομαδοποίηση συχνοτήτων σε μπάντες
        band_magnitudes = []
        freq_values = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
        for i in range(len(freq_values)-1):
            mask = (frequencies >= freq_values[i]) & (frequencies < freq_values[i+1])
            if np.any(mask):
                magnitude = np.mean(freq_db[mask])
                band_magnitudes.append(magnitude)
            else:
                band_magnitudes.append(freq_db.min())

        # Δημιουργία figure
        plt.style.use('dark_background')
        fig = plt.figure(figsize=(12, 6))
        
        # Frequency Analyzer
        ax = plt.gca()
        ax.set_facecolor('black')
        fig.patch.set_facecolor('black')
        
        # Προσθήκη οριζόντιων γραμμών
        for db in range(-100, 20, 20):
            ax.axhline(y=db, color='green', linestyle='-', alpha=0.3)
        
        # Κάθετες γραμμές πλέγματος
        for x in range(len(freq_bands)):
            ax.axvline(x, color='green', linestyle='-', alpha=0.3)
        
        # Σχεδίαση των μπαρών
        bars = ax.bar(range(len(band_magnitudes)), 
                     band_magnitudes,
                     width=0.8,
                     color='lime')
        
        # Ρύθμιση των αξόνων
        ax.set_ylim(-100, 100)
        ax.set_xlim(-0.5, len(freq_bands) - 0.5)
        
        # Προσθήκη ετικετών στον άξονα x (συχνότητες)
        plt.xticks(range(len(freq_bands)), freq_bands)
        
        # Προσθήκη ετικετών στον άξονα y (dB)
        y_ticks = range(-100, 120, 20)
        plt.yticks(y_ticks, [f'{db}.0' for db in y_ticks])
        
        # Προσθήκη τίτλου και πληροφοριών στο πάνω μέρος
        current_time = "0m 0m 19.22s"  # Προσαρμόστε ανάλογα
        plt.title(f'Ch1  Overall: 82.51 dB  (Time: {current_time})\n'
                 f'Freq: 250.00 Hz  Level: 82.46 dB\n'
                 f'Max: 84.91 dB  Peak: 88.82 dB', 
                 loc='left', pad=10, color='cyan')
        
        # Προσθήκη ετικέτας dB Fast στον άξονα y
        plt.ylabel('dB Fast')
        
        # Προσθήκη ετικέτας Frequency (Hz) στον άξονα x
        plt.xlabel('Frequency (Hz)')
        
        # Ρύθμιση των χρωμάτων για τις ετικέτες και τους άξονες
        ax.xaxis.label.set_color('white')
        ax.yaxis.label.set_color('white')
        ax.tick_params(colors='white')
        
        plt.tight_layout()
        logger.info(f"Create spectrogram and freq analyzer : {(datetime.now()-start_inf).total_seconds()*1000:.0f} msec",extra=LOG_D) 
                            
        # Αποθήκευση ή εμφάνιση
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight', facecolor='black')
            plt.close()
            return save_path
        else:
            return None
            
    except Exception as error:
        logger.error(f"Error create Freq Analyzer error {str(error)} ",extra=LOG_D)
        return None

def generate_spectrogram_from_audio_file(audio_filepath, output_dir="../assets/specrogram_images", sr=None):
    """
    Loads an audio file, generates its spectrogram using create_freq_analyzer_spectrogram,
    and saves it to the specified output directory.

    Args:
        audio_filepath (str): The full path to the audio file.
        output_dir (str): The directory to save the spectrogram image.
        sr (int, optional): The sample rate to resample the audio to. If None, uses librosa's default.

    Returns:
        str: The path to the saved spectrogram image, or None if an error occurred.
    """
    try:
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # Load the audio file
        audio_data, audio_sr = librosa.load(audio_filepath, sr=sr)

        # Construct save path
        spectrogram_filename = os.path.splitext(os.path.basename(audio_filepath))[0] + ".png"
        save_path = os.path.join(output_dir, spectrogram_filename)

        logger.info(f"Generating spectrogram for {audio_filepath} to {save_path}", extra=LOG_D)
        return create_freq_analyzer_spectrogram(audio_data, audio_sr, save_path)

    except Exception as e:
        logger.error(f"Error generating spectrogram from file {audio_filepath}: {e}", extra=LOG_D)
        return None