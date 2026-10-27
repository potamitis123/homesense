import pandas as pd
import sqlite3
import sys
import os
#sys.path.append('./misc')
#sys.path.append('./ast_sql')
from misc.load_parameters import parameters
from misc.log_test import logger
from ast_sql.remote_devices import Remote_Devices
LOG_D={'module_name':'devices_sql_to_pandas'}

class Devices_SQL_to_Pandas():
    def __init__(self,db_file=None):
        if db_file is None:
            db_file = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', parameters.get_parameters()['sqlite']['db_name']))
        self.conn=  sqlite3.connect(db_file)
        self.remote_devices=Remote_Devices(self.conn)
        self.df=None
    def __del__(self):
        try:
            del self.remote_devices
            self.conn.commit()
            self.conn.close()
        except Exception as e:
            logger.error(f"Error close Ast sql lite {str(e)} ",extra=LOG_D)
    def rename_columns(self,df):
        return df.rename(columns={ 
                                "device_name":"device_name",
                                 })
    def get_columns(self,df):
        def fun_cellStyle(field):
            if field=='prob class 1' or field=='prob class 2' or  field=='prob class 3':
               return{
                    "styleConditions": [
                                             {
                                                 "condition": "params.value >= 0.5",
                                                 "style": {"backgroundColor": "pink"},
                                             },
                                             {
                                                 "condition": "params.value > 0.3 && params.value < 0.5 ",
                                                 "style": {"backgroundColor": "yellow"},
                                             }
                                         ],
                   # "defaultStyle": {"backgroundColor": "white"},
                }
            else:
                return {
                    #"defaultStyle": {"backgroundColor": "white"}
                    }
        
        
        
        if df is None:
            df = pd.read_sql_query("SELECT * FROM remote_devices", self.conn)
        columns= []
        for columnname in df.columns:
            if columnname=='type_of_user' :
                #tmp_field={"field": i, 'hide': True, 'cellStyle':fun_cellStyle(i)}
                tmp_field={"field": columnname, 'hide': True , 'type':'text'}
            elif columnname=='mqtt_broker' or columnname=='mqtt_port':
                tmp_field={"field": columnname, 'hide': True , 'type':'text'}
            elif columnname=='wifi_ssid' or columnname=='wifi_password':
                tmp_field={"field": columnname, 'hide': True , 'type':'text'}    
            elif columnname=='mqtt_client_id' or columnname=='mqtt_password':
                tmp_field={"field": columnname, 'hide': True , 'type':'text'}    
            elif columnname=='sample_rate' or columnname=='sample_bits':
                tmp_field={"field": columnname, 'hide': True , 'type':'text'}
            elif columnname=='device_name':
                tmp_field={"field": columnname, 'hide': False,"headerName": "Device Name", }
            elif columnname=='rms_threshold':
                tmp_field={"field": columnname, 'hide': False,"headerName": "RMS (dB)", }
            elif columnname=='record_duration':
                tmp_field={"field": columnname, 'hide': False,"headerName": "Duration (sec)", }    
            elif columnname=='updated':
                #tmp_field={"field": columnname, 'hide': False }
                tmp_field={"field": columnname, 'hide': False,  
                           "cellRenderer": "Checkbox",
                           
                           }
            else:
                #tmp_field={"field": i, 'hide': False,  'cellStyle':fun_cellStyle(i)}
                tmp_field={"field": columnname, 'hide': False, 'type':'text'}
            columns.append(tmp_field)
        columns.append({"headerName": "Delete",
                        "field": "Del",
                        "cellRenderer": "Button",
                        "cellRendererParams": {"className": "btn btn-danger"},
                        })                
        return columns
    
    def get_all_rec(self):
        self.df = pd.read_sql_query("SELECT * FROM remote_devices", self.conn)
        return self.df
        
