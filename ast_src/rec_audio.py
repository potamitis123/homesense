import datetime
import pyaudio
import numpy as np
import soundfile as sd
from pydub import AudioSegment

from threading import Thread
import time
import os
from datetime import datetime
import sys
#sys.path.append('./misc')
from misc.log_test import logger
LOG_D={'module_name':'rec_audio'}

class Recorder():
    def __init__(self, record_seconds=10,save=False,rec_pause=10,save_path='audios',threshold_sound=None,call_back=None, mic_index=None):
        start_inf=datetime.now()

        self.sample_rate = 44100
        self.save=save
        self.rec_pause=rec_pause
        self.channels = 1
        self.mic_index = mic_index
        self.chunk = 1024
        self.record_seconds=record_seconds
        self.call_back=call_back
        self.save_path=save_path
        self._proc_status=False
        self.threshold_sound=threshold_sound
        if not save_path is None:
            try:
                os.makedirs(self.save_path)
            except:
                pass
        self.p = pyaudio.PyAudio()
        logger.info(f"Init microphone time: {datetime.now()-start_inf}",extra=LOG_D)
    
    def start(self):
        self.stream = self.p.open(
                rate = self.sample_rate,
                format = pyaudio.paInt16,
                channels = self.channels,
                input = True,
                input_device_index = self.mic_index,)
        self._proc_status=True
        self.proc=Thread(target=self.__record_audio)
        self.proc.start()
        
        logger.info('Start Rec',extra=LOG_D)
    
    def stop (self):
        self._proc_status=False

        if  not self.proc is None:
             # self.proc.kill() # Thread doesn't have kill
             self.proc.join()
             # self.proc.close() # Thread doesn't have close
        if not self.stream is None:
            self.stream.stop_stream()
            self.stream.close()
        if not self.p is None:
            self.p.terminate()
        logger.info('Stop Rec',extra=LOG_D)

    def __record_audio(self):
    # p = pyaudio.PyAudio()
    # for ii in range(p.get_device_count()):
    #     print(p.get_device_info_by_index(ii))
        while  self._proc_status:
            logger.info('Recording',extra=LOG_D)
            frames = []
            for i in range(0, int(self.sample_rate / self.chunk * self.record_seconds)):
                # Check status inside the loop to allow faster stop
                if not self._proc_status:
                    break
                try:
                    data = np.frombuffer(self.stream.read(self.chunk, exception_on_overflow=False),dtype=np.int16)
                    frames.extend(data)
                except:
                    pass
            
            if not self._proc_status:
                # Save partial recording if it has substantial data (e.g., > 0.5s)
                if self.save and len(frames) > (self.sample_rate * 0.5):
                    logger.info("Saving partial recording before exit", extra=LOG_D)
                    self.__save(np.array(frames))
                break
                
            frames=np.array(frames)
            logger.info("* done recording",extra=LOG_D)
            if not self.threshold_sound is None:
                if not self.__check_sound(frames):
                    logger.info('Sleeping',extra=LOG_D)
                    time.sleep(self.rec_pause)
                    continue
            if not self.call_back is None:
                self.call_back(frames,self.sample_rate)
            if self.save:
                self.__save(frames)
            logger.info('Sleeping',extra=LOG_D)
            time.sleep(self.rec_pause)
        #    return np.array(frames,dtype=np.float16)
   
    def __check_sound(self,frames):
        energy = np.sum(frames**2)
       # logger.info(energy)
        if energy< self.threshold_sound:
            return True
        else:
            return False

    def __save(self,frames):
        rec_file_name=os.path.join(self.save_path,f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_raspberry_rec.wav")
        #self.stream.write(rec_file_name,frames,self.sample_rate)
        try:
            sd.write(file=rec_file_name,data=frames,samplerate=self.sample_rate)
            logger.info(f"Save {rec_file_name}",extra=LOG_D )    
        except Exception as e:
            logger.error(f"Error save file {rec_file_name} {str(e)} ",extra=LOG_D)