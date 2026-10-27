import os
import sys
from .object_tables import Object_tables
#sys.path.append('./misc')
from misc.log_test import logger
from misc import hash_password

LOG_D={'module_name':'users'}

class Users(Object_tables):
    def __init__(self,conn):
        super().__init__(conn)
        self.table_name="users"
        self.cursor = self.conn.cursor()
        try:
            self.__create_user_tables__()
        except Exception as e:
            logger.error(f"Error Init Users sql lite {str(e)} ",extra=LOG_D)
   
    def __create_user_tables__(self):
        #self.cursor.execute('''DROP TABLE IF EXISTS users;''')
        #self.conn.commit()                            
        if not self.__check_exit_table__('users'):
            try:
                self.cursor.execute("""CREATE TABLE users (
                                                            username TEXT PRIMARY KEY NOT NULL,
                                                            password TEXT NOT NULL,
                                                            email TEXT NOT NULL UNIQUE,
                                                            latitude REAL,
                                                            longitude REAL,
                                                            description TEXT
                                                        );
                                    """)
                self.conn.commit()
                logger.info(f"Create  users table",extra=LOG_D)
                self.cursor.execute(f"""INSERT INTO users (username, password, email,latitude,longitude,description)
                                        VALUES ('user1', '{hash_password('user1')}', 'user1@admin.com', 0.0, 0.0, 'user1');
                                    """)
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error create users table {str(e)} ",extra=LOG_D)
   
    def get_users(self):
        try:
            self.cursor.execute("SELECT * FROM users")
            result = self.cursor.fetchall()
            #logger.info(f"Get users {result}",extra=LOG_D)
        except Exception as e:
            result=None
            logger.error(f"Error get users {str(e)} ",extra=LOG_D)
        return result
   
    def get_user(self,user_name):
        try:
            self.cursor.execute(f"SELECT * FROM users where username='{user_name}'")
            result = self.cursor.fetchone()
            #logger.info(f"Get user {result}",extra=LOG_D)
        except Exception as e:
            result=None
            logger.error(f"Error get user {str(e)} ",extra=LOG_D)
        return result
   
    def get_password_from_username(self,user_name):
        try:
            self.cursor.execute(f"SELECT password FROM users where username='{user_name}'")
            result = self.cursor.fetchone()[0]
            #logger.info(f"Get password {result}",extra=LOG_D)
        except Exception as e:
            result=None
            logger.error(f"Error get user {str(e)} ",extra=LOG_D)
        return result
   
    def insert_user(self,user_name=None,password=None,email=None,latitude=None,longitude=None,description=None):
        try:
            self.cursor.execute(f"INSERT INTO users (username,password,email,latitude,longitude,description) VALUES ('{user_name}','{password}','{email}',{latitude},{longitude},'{description}')")
            self.conn.commit()
            try:
                self.cursor.execute(f"""INSERT INTO devices 
                                            (device_name, 
                                            device_table)
                                        VALUES 
                                            ('{user_name}',
                                            '{self.table_name}');
                                    """)
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error insert device {str(e)} ",extra=LOG_D)
            logger.info(f"Insert user {user_name} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error insert user {str(e)} ",extra=LOG_D)
    
    def update_user(self,user_name=None,password=None,email=None,latitude=None,longitude=None,description=None):
        try:
            if user_name is not None:
                if password is not None:
                    self.cursor.execute(f"UPDATE users SET password='{password}' WHERE username='{user_name}'")
                if email is not None:
                    self.cursor.execute(f"UPDATE users SET email='{email}' WHERE username='{user_name}'")
                if latitude is not None:
                    self.cursor.execute(f"UPDATE users SET latitude={latitude} WHERE username='{user_name}'")
                if longitude is not None:
                    self.cursor.execute(f"UPDATE users SET longitude={longitude} WHERE username='{user_name}'")
                if description is not None:
                    self.cursor.execute(f"UPDATE users SET description='{description}' WHERE username='{user_name}'")
            self.conn.commit()
            logger.info(f"Update user {user_name} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error update user {str(e)} ",extra=LOG_D)
   
    def delete_user(self,user_name=None):
        try:
            self.cursor.execute(f"DELETE FROM users WHERE username='{user_name}'")
            self.conn.commit()
            try:
                self.cursor.execute(f"DELETE FROM devices WHERE device_name='{user_name}'")
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error delete device {str(e)} ",extra=LOG_D)
            logger.info(f"Delete user {user_name} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error delete user {str(e)} ",extra=LOG_D)
    