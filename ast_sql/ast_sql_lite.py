import sqlite3
import os
import sys
from .ontology import Ontology
from .users import Users
from .object_tables import Object_tables 
from .remote_devices import Remote_Devices
from .ast_table import Ast_table
from .devices import Devices
from .group_ofclasses import Group_of_Classes
#sys.path.append('./misc')
from misc.log_test import logger
LOG_D={'module_name':'ast_sql_lite'}

class Ast_Sql_lite():
    def __init__(self,db_file):
        try:
            self.conn = sqlite3.connect(db_file, check_same_thread=False, timeout=30.0)
            self.conn.execute("PRAGMA journal_mode=WAL;") # Enable Write-Ahead Logging
            self.conn.commit()
            self.just_load=True
            self.ontology=Ontology(self.conn)
            self.users=Users(self.conn)
            self.object_tables=Object_tables(self.conn)
            self.remote_devices=Remote_Devices(self.conn)
            self.devices=Devices(self.conn)
            self.ast_table=Ast_table(self.conn)
            self.group_of_classes=Group_of_Classes(self.conn)
        except Exception as e:
            logger.error(f"Error Init Ast sql lite {str(e)} ",extra=LOG_D)
        
    def __del__(self):
        try:
            self.conn.close()
            #logger.info(f"Close Ast sql lite",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error close Ast sql lite {str(e)} ",extra=LOG_D)    

    def close(self):
        self.__del__()
        