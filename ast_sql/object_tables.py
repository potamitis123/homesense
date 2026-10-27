import sqlite3
import os
import sys
import json
#sys.path.append('../misc')
from misc.log_test import logger
LOG_D={'module_name':'Object_table'}

class Object_tables():
    def __init__(self,conn):
        self.conn=conn
        self.cursor = self.conn.cursor()
    
    def __del__(self):
        try:
            #self.conn.commit()
            self.conn.close()
        except Exception as e:
            logger.error(f"Error close Ast sql lite {str(e)} ",extra=LOG_D)
    
    def __check_exit_table__(self,table_name):
        try:
            self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
            result = self.cursor.fetchone()
           # logger.info(f"Table {table_name} table exist {result}",extra=LOG_D)
        except Exception as e:
            result=None
            logger.error(f"Error checkexist table {str(e)} ",extra=LOG_D)
        return result is not None