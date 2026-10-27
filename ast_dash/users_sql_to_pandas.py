import pandas as pd
import sqlite3
import sys
import os
#sys.path.append('./misc')
#sys.path.append('./ast_sql')
from misc.load_parameters import parameters
from misc.log_test import logger
from ast_sql.users import Users
LOG_D={'module_name':'users_sql_to_pandas'}

class Users_SQL_to_Pandas():
    def __init__(self,db_file=None):
        if db_file is None:
            db_file = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', parameters.get_parameters()['sqlite']['db_name']))
        self.conn=  sqlite3.connect(db_file)
        self.users=Users(self.conn)
        self.df=None
   
    def __del__(self):
        try:
            try:
                del self.users
            except Exception as e:
                logger.error(f"Error Del  users {str(e)} ",extra=LOG_D)    
            if self.conn:
                self.conn.close()
        except Exception as e:
            logger.error(f"Error close Ast sql lite {str(e)} ",extra=LOG_D)

    def rename_columns(self,df):
        return df.rename(columns={ 
                                "device_name":"device_name",
                                 })
    def get_columns(self,df):
        if df is None:
            df = pd.read_sql_query("SELECT * FROM users", self.conn)
        columns= []
        for columnname in df.columns:
            if columnname=='username':
                tmp_field={"field": columnname, 'hide': False,"headerName": "User Name", }
            elif columnname=='password':
                tmp_field={"field": columnname, 'hide': False,"headerName": "Password", }
            elif columnname=='email':
                tmp_field={"field": columnname, 'hide': False,"headerName": "Email", }    
            elif columnname=='latitude':
                tmp_field={"field": columnname, 'hide': False,"headerName": "Latitude", }
            elif columnname=='longitude': 
                tmp_field={"field": columnname, 'hide': False,"headerName": "Longitude", }
            elif columnname=='description': 
                tmp_field={"field": columnname, 'hide': False,"headerName": "Description", }
            columns.append(tmp_field)
        columns.append({"headerName": "Delete",
                        "field": "Del",
                        "cellRenderer": "Button",
                        "cellRendererParams": {"className": "btn btn-danger"},
                        })                
        return columns
    
    def get_all_rec(self):
        self.df = pd.read_sql_query("SELECT * FROM users", self.conn)
        return self.df
        
