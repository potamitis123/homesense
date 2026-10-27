# --- SSL Windows Store Certificate Load Error Monkeypatch ---
import ssl
try:
    import certifi
    orig_load_default_certs = ssl.SSLContext.load_default_certs
    def patched_load_default_certs(self, purpose=ssl.Purpose.SERVER_AUTH):
        try:
            orig_load_default_certs(self, purpose)
        except Exception:
            # Fallback to certifi CA bundle if Windows cert store loading fails
            self.load_verify_locations(certifi.where())
    ssl.SSLContext.load_default_certs = patched_load_default_certs
except Exception:
    pass
# -------------------------------------------------------------

from datetime import datetime,timedelta
import numpy as np
import math
import hashlib

def convert_rec_to_float(data):
    if data.dtype == np.uint8:
        data = (data - 128) / 128.
    elif data.dtype == np.int16:
        data = data / 32768.
    elif data.dtype == np.int32:
        data = data / 2147483648.
    mean_data=np.mean(data,dtype=np.float64)
    data = data - mean_data
    return data

def convert_rec_to_float_get_rms(data):
    data=convert_rec_to_float(data)
    return get_rms(data)

def get_rms(frames,db=False):
    pow_frames=np.power(frames,2,dtype=np.float32)
    mean=np.mean(pow_frames)
    if mean>0:
        rms_energy= math.sqrt(mean)
        if db:
            rms_energy=20 * np.log10(rms_energy)
    else:
        rms_energy=0
    return round(rms_energy,1)

def hash_password(password):
   password_bytes = password.encode('utf-8')
   hash_object = hashlib.sha256(password_bytes)
   return hash_object.hexdigest()

def get_date_after(dt_date,days=30):
    dt_delta = timedelta(days=days)
    new_date=(dt_date + dt_delta)
    new_date=new_date.replace(hour=23, minute=59, second=59,microsecond=999)
    return new_date

def get_date_before(dt_date,days=30):
    dt_delta = timedelta(days=days)
    new_date=(dt_date - dt_delta)
    new_date=new_date.replace(hour=0, minute=0, second=0,microsecond=0)
    return new_date

def get_date_00(str_date,format='%Y-%m-%d %H:%M:%S'):
    old_date=str_to_date(str_date,format)
    new_date=old_date.replace(hour=0, minute=0, second=0,microsecond=0)
    return new_date

def get_date_99(str_date,format='%Y-%m-%d %H:%M:%S'):
    old_date=str_to_date(str_date,format)
    new_date=old_date.replace(hour=23, minute=59, second=59,microsecond=999)
    return new_date

def str_to_date(str_date,format='%Y-%m-%d %H:%M:%S'):
    str_date=str_date.replace('T',' ')
    pos_=str_date.find(':')
    if pos_== -1:
        return datetime.strptime(str_date,format[0:8])
        
    return datetime.strptime(str_date,format)

def date_to_str(dt_date,format='%Y-%m-%d %H:%M:%S'):

    return dt_date.strftime(format)

def get_now_str():
    str_date=date_to_str(datetime.now())
    return str_date

def get_now_date():
    return datetime.now()


    
    