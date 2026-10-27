import os
import sys
from .object_tables import Object_tables
#sys.path.append('./misc')
from misc.log_test import logger
LOG_D={'module_name':'remote_devices'}

class Remote_Devices (Object_tables):
    def __init__(self,conn):
        super().__init__(conn)
        self.table_name="remote_devices"
        self.cursor = self.conn.cursor()
        try:
            self.__create_remote_devices_table__()
        except Exception as e:
            logger.error(f"Error Init Users sql lite {str(e)} ",extra=LOG_D)

    def __del__(self):
        try:
            self.conn.commit()
            self.cursor.close()
        except Exception as e:
            pass
            #logger.error(f"Error close Ast sql lite {str(e)} ",extra=LOG_D)  
    def __create_remote_devices_table__(self):
        #self.cursor.execute('''DROP TABLE IF EXISTS remote_devices;''')
        #self.conn.commit()
        if not self.__check_exit_table__('remote_devices'):
            try:
                self.cursor.execute("""CREATE TABLE remote_devices (
                                                            device_name TEXT PRIMARY KEY,
                                                            latitude REAL,
                                                            longitude REAL,
                                                            description TEXT,
                                                            mqtt_broker TEXT,
                                                            mqtt_port SMALLINT DEFAULT 1883,
                                                            mqtt_client_id TEXT,
                                                            mqtt_password TEXT,
                                                            sample_rate INT DEFAULT 16000,
                                                            sample_bits INT DEFAULT 16,
                                                            rms_threshold INT DEFAULT 0,
                                                            record_duration INT DEFAULT 5,
                                                            wifi_ssid TEXT,
                                                            wifi_password TEXT,
                                                            updated BOOLEAN  DEFAULT 1,
                                                            type_of_user SMALLINT DEFAULT 0
   
                                                        );

                                    """)
                self.conn.commit()
                logger.info(f"Create  device table",extra=LOG_D)
            except Exception as e:
                logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
    
    def insert_remote_devices_rec(self,device_name=None,
                                    latitude=None,
                                    longitude=None,
                                    description=None,
                                    type_of_user=None,
                                    mqtt_broker=None,
                                    mqtt_port=None,
                                    mqtt_client_id=None,
                                    mqtt_password=None,
                                    rms_threshold=None,
                                    sample_rate=None,
                                    sample_bits=None,
                                    record_duration=None,
                                    wifi_ssid=None,
                                    wifi_password=None,
                                    updated=None):
        try:
            self.cursor.execute(f"""INSERT INTO remote_devices 
                                            (device_name, 
                                            latitude, 
                                            longitude, 
                                            description, 
                                            type_of_user, 
                                            mqtt_broker, 
                                            mqtt_port, 
                                            mqtt_client_id, 
                                            mqtt_password, 
                                            rms_threshold,
                                            sample_rate,
                                            sample_bits,
                                            record_duration,
                                            wifi_ssid,
                                            wifi_password,
                                            updated)
                                        VALUES 
                                            ('{device_name}',
                                            {latitude}, 
                                            {longitude}, 
                                            '{description}', 
                                              0,
                                            '{mqtt_broker}',
                                             {mqtt_port},
                                            '{mqtt_client_id}',
                                            '{mqtt_password}',
                                            '{rms_threshold}',
                                            {sample_rate},
                                            {sample_bits},
                                            {record_duration},
                                            '{wifi_ssid}',
                                            '{wifi_password}',
                                            {int(updated)}
                                            );
                                    """)
            self.conn.commit()
            logger.info(f"Add records to  device table",extra=LOG_D)
            try:
                self.cursor.execute(f"""INSERT INTO devices 
                                            (device_name, 
                                            device_table)
                                        VALUES 
                                            ('{device_name}',
                                            '{self.table_name}');
                                    """)
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error insert device {str(e)} ",extra=LOG_D)
                
        except Exception as e:
            logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
    def delete_remote_devices_rec(self,device_name=None):
        try:
            self.cursor.execute(f"DELETE FROM remote_devices WHERE device_name='{device_name}'")
            self.conn.commit()
            try:
                self.cursor.execute(f"DELETE FROM devices WHERE device_name='{device_name}'")
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error delete device {str(e)} ",extra=LOG_D)
            logger.info(f"Delete device {device_name} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)

    def update_remote_devices_rec(self,device_name=None,
                                    latitude=None,
                                    longitude=None,
                                    description=None,
                                    type_of_user=None,
                                    mqtt_broker=None,
                                    mqtt_port=None,
                                    mqtt_client_id=None,
                                    mqtt_password=None,
                                    rms_threshold=None,
                                    sample_rate=None,
                                    sample_bits=None,
                                    record_duration=None,
                                    wifi_ssid=None,
                                    wifi_password=None,
                                    updated=None):
        try:
            if device_name is not None:
                if latitude is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET latitude={latitude} WHERE device_name='{device_name}'")
                if longitude is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET longitude={longitude} WHERE device_name='{device_name}'")
                if description is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET description='{description}' WHERE device_name='{device_name}'")
                if mqtt_broker is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET mqtt_broker='{mqtt_broker}' WHERE device_name='{device_name}'")
                if mqtt_port is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET mqtt_port={mqtt_port} WHERE device_name='{device_name}'")
                if mqtt_client_id is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET mqtt_client_id='{mqtt_client_id}' WHERE device_name='{device_name}'")
                if mqtt_password is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET mqtt_password='{mqtt_password}' WHERE device_name='{device_name}'")
                if rms_threshold is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET rms_threshold={rms_threshold} WHERE device_name='{device_name}'")
                if sample_rate is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET sample_rate={sample_rate} WHERE device_name='{device_name}'")
                if sample_bits is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET sample_bits={sample_bits} WHERE device_name='{device_name}'")
                if record_duration is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET record_duration={record_duration} WHERE device_name='{device_name}'")
                if wifi_ssid is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET wifi_ssid='{wifi_ssid}' WHERE device_name='{device_name}'")
                if wifi_password is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET wifi_password='{wifi_password}' WHERE device_name='{device_name}'")
                if updated is not None:
                    self.cursor.execute(f"UPDATE remote_devices SET updated='{int(updated)}' WHERE device_name='{device_name}'")
                # self.cursor.execute(f"""UPDATE remote_devices SET 
                #                 latitude='{latitude}',
                #                 longitude='{longitude}',
                #                 description='{description}',
                #                 mqtt_broker='{mqtt_broker}',
                #                 mqtt_port='{mqtt_port}',
                #                 mqtt_client_id='{mqtt_client_id}',
                #                 mqtt_password='{mqtt_password}',
                #                 rms_threshold='{rms_threshold}',
                #                 sample_rate='{sample_rate}',
                #                 sample_bits='{sample_bits}',
                #                 record_duration='{record_duration}',
                #                 wifi_ssid='{wifi_ssid}',
                #                 wifi_password='{wifi_password}',
                #                 updated='{int(updated)}' 
                #                 WHERE device_name='{device_name}'""")
                self.conn.commit()
        except Exception as e:
            logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
    def update_remote_devices_rec_updated(self,device_name=None,
                                    updated=None):
        try:
            self.cursor.execute(f"""UPDATE remote_devices SET 
                                updated='{int(updated)}' 
                                WHERE device_name='{device_name}'""")
            self.conn.commit()
        except Exception as e:
            logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
    def get_remote_devices_rec(self,device_name=None):
        try:
            self.cursor.execute(f"SELECT * FROM remote_devices WHERE device_name='{device_name}'")
            return self.cursor.fetchall()
        except Exception as e:
            logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
            return None
    def get_updated_remote_devices_rec(self):
        try:
            self.cursor.execute(f"SELECT * FROM remote_devices WHERE updated=1")
            return self.cursor.fetchall()
        except Exception as e:
            logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
            return None
    def get_remote_devices(self):
        try:
            self.cursor.execute(f"SELECT * FROM remote_devices")
            return self.cursor.fetchall()
        except Exception as e:
            logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
            return None