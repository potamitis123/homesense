import os
import sys
from .object_tables import Object_tables
#sys.path.append('./misc')
from misc.log_test import logger
LOG_D={'module_name':'devices'}

class Devices(Object_tables):
    def __init__(self,conn):
        super().__init__(conn)
        self.table_name="devices"
        self.cursor = self.conn.cursor()
        try:
            self.__create_devices_table__()
        except Exception as e:
            logger.error(f"Error Init Users sql lite {str(e)} ",extra=LOG_D)    
    def __create_devices_table__(self):
        #self.cursor.execute("""DROP TABLE IF EXISTS devices;""")
        #self.conn.commit()
        if not self.__check_exit_table__('devices'):
            try:
                self.cursor.execute("""CREATE TABLE devices (
                                                            device_name TEXT PRIMARY KEY,
                                                            device_table TEXT NOT NULL 
                                                        );
                                    """)
                self.conn.commit()
                logger.info(f"Create  device table",extra=LOG_D)
                self.cursor.execute("""INSERT INTO devices 
                                            (device_name, 
                                            device_table)
                                        VALUES 
                                            ('user1',
                                            'users');
                                    """)
                self.conn.commit()
                logger.info(f"Add records to  device table",extra=LOG_D)
            except Exception as e:
                logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
    def delete_device(self,device_name=None):
        try:
            self.cursor.execute(f"DELETE FROM devices WHERE device_name='{device_name}'")
            self.conn.commit()
            logger.info(f"Delete device {device_name} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error delete device {str(e)} ",extra=LOG_D)

    def get_device(self,device_name=None):
        try:
            self.cursor.execute(f"SELECT * FROM devices WHERE device_name='{device_name}'")
            return self.cursor.fetchall()
        except Exception as e:
            logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
            return None

    def get_devices(self):
        try:
            self.cursor.execute(f"SELECT * FROM devices")
            return self.cursor.fetchall()
        except Exception as e:
            logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
            return None
    def insert_device(self,device_name=None,device_table=None):
        try:
            self.cursor.execute(f"INSERT INTO devices (device_name,device_table) VALUES ('{device_name}','{device_table}')")
            self.conn.commit()
            logger.info(f"Insert device {device_name} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error insert device {str(e)} ",extra=LOG_D)
    def update_device(self,device_name=None,device_table=None):
        try:
            self.cursor.execute(f"UPDATE devices SET device_table='{device_table}' WHERE device_name='{device_name}'")
            self.conn.commit()
            logger.info(f"Update device {device_name} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error update device {str(e)} ",extra=LOG_D)