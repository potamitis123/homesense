import os,sys
import platform
import subprocess
#sys.path.append('./misc')
from .log_test import logger
LOG_D={'module_name':'telemetry'}

def check_CPU_temp():
    temp=None
    
    # Try Linux path
    if platform.system() != "Windows":
        try:
            with open('/sys/class/thermal/thermal_zone0/temp','r') as f:
                temp=f.readline().replace('\n','')
                temp=float(temp)/1000
            return temp
        except (ValueError, FileNotFoundError):
            pass 
        except Exception as e:
            logger.error(f'Error CPU temp (Linux): {e}',extra=LOG_D) 

    # Try Windows WMI via PowerShell
    if platform.system() == "Windows":
        try:
            # MSAcpi_ThermalZoneTemperature requires Administrator privileges
            cmd = "Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace \"root/wmi\" | Select-Object -ExpandProperty CurrentTemperature"
            res = subprocess.run(["powershell", "-Command", cmd], capture_output=True, text=True)
            output = res.stdout.strip()
            if output:
                # WMI returns Decikelvin (K * 10)
                lines = output.split('\n')
                if lines:
                    kelvin_x10 = float(lines[0].strip())
                    celsius = (kelvin_x10 / 10.0) - 273.15
                    return round(celsius, 2)
        except Exception as e:
            logger.debug(f'Error CPU temp (Windows/WMI): {e}', extra=LOG_D)
            
    return temp