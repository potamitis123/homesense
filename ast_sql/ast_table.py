import os
import sys
import sqlite3
import numpy as np
import io
from .object_tables import Object_tables
#sys.path.append('./misc')
from misc.log_test import logger
LOG_D={'module_name':'ast_table'}

# --- Numpy-BLOB Adapters ---
def adapt_array(arr):
    """
    http://stackoverflow.com/a/31312102/190597 (Shawn)
    """
    out = io.BytesIO()
    np.save(out, arr)
    out.seek(0)
    return sqlite3.Binary(out.read())

def convert_array(text):
    out = io.BytesIO(text)
    out.seek(0)
    return np.load(out)

# Converts np.array to TEXT when inserting
sqlite3.register_adapter(np.ndarray, adapt_array)
# Converts TEXT to np.array when selecting
sqlite3.register_converter("array", convert_array)

class Ast_table(Object_tables):
    def __init__(self,conn):
        super().__init__(conn)
        self.table_name="ast_table"
        self.cursor = self.conn.cursor()
        try:
            self.__create_ast_table__()
        except Exception as e:
            logger.error(f"Error Init Users sql lite {str(e)} ",extra=LOG_D)
    def __create_ast_table__(self):
        #self.cursor.execute("ALTER TABLE ast_table ADD rms FLOAT;")
        #self.cursor.execute("ALTER TABLE ast_table ADD latitude REAL;")
        #self.conn.commit()
        #self.cursor.execute("ALTER TABLE ast_table ADD longitude REAL;")
        #self.conn.commit()
        if not self.__check_exit_table__('ast_table'):
            try:
                self.cursor.execute('''CREATE TABLE ast_table (
                                                                    id INTEGER PRIMARY KEY,
                                                                    date DATETIME DEFAULT CURRENT_TIMESTAMP,
                                                                    rms FLOAT,
                                                                    file_name TEXT NOT NULL,
                                                                    device_name TEXT,
                                                                    spectrogram_image TEXT,
                                                                    sound1 TEXT,
                                                                    conf_sound1 FLOAT,
                                                                    sound2 TEXT,
                                                                    conf_sound2 FLOAT,
                                                                    sound3 TEXT,
                                                                    conf_sound3 FLOAT,
                                                                    transcription TEXT,
                                                                    emotion TEXT,
                                                                    llm_verdict TEXT,
                                                                    FOREIGN KEY (device_name) REFERENCES devices(device_name)
                                                                );
                                    '''
                                    )
                self.conn.commit()
                logger.info(f"Create  ast_table table",extra=LOG_D)
            except Exception as e:
                logger.error(f"Error create ast_table table {str(e)} ",extra=LOG_D)
        
        # Envelope dynamic range of the recording, in dB. Stored per event so
        # the dashboard can show how clean a capture was, and so the dedupe
        # pass does not have to recompute it from the audio every minute.
        try:
            self.cursor.execute("SELECT snr FROM ast_table LIMIT 1")
        except Exception:
            try:
                self.cursor.execute("ALTER TABLE ast_table ADD COLUMN snr REAL")
                self.conn.commit()
                logger.info("Added snr column", extra=LOG_D)
            except Exception as e:
                logger.error(f"Could not add snr column: {e}", extra=LOG_D)

        # Check and add new columns if they don't exist (Migration)
        try:
            self.cursor.execute("SELECT transcription FROM ast_table LIMIT 1")
        except:
            try:
                self.cursor.execute("ALTER TABLE ast_table ADD COLUMN transcription TEXT")
                self.conn.commit()
                logger.info("Added transcription column", extra=LOG_D)
            except Exception as e:
                logger.error(f"Error adding transcription column: {e}", extra=LOG_D)

        try:
            self.cursor.execute("SELECT emotion FROM ast_table LIMIT 1")
        except:
            try:
                self.cursor.execute("ALTER TABLE ast_table ADD COLUMN emotion TEXT")
                self.conn.commit()
                logger.info("Added emotion column", extra=LOG_D)
            except Exception as e:
                logger.error(f"Error adding emotion column: {e}", extra=LOG_D)

        try:
            self.cursor.execute("SELECT clap_embedding FROM ast_table LIMIT 1")
        except:
            try:
                self.cursor.execute("ALTER TABLE ast_table ADD COLUMN clap_embedding BLOB")
                self.conn.commit()
                logger.info("Added clap_embedding column", extra=LOG_D)
            except Exception as e:
                logger.error(f"Error adding clap_embedding column: {e}", extra=LOG_D)

        try:
            self.cursor.execute("SELECT llm_verdict FROM ast_table LIMIT 1")
        except:
            try:
                self.cursor.execute("ALTER TABLE ast_table ADD COLUMN llm_verdict TEXT")
                self.conn.commit()
                logger.info("Added llm_verdict column", extra=LOG_D)
            except Exception as e:
                logger.error(f"Error adding llm_verdict column: {e}", extra=LOG_D)

    def delete_ast_table(self,id=None):
        try:
            if id is not None:
                self.cursor.execute(f"DELETE FROM ast_table WHERE id={id}")
                self.conn.commit()
                logger.info(f"Delete ast_table {id} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error delete ast_table {str(e)} ",extra=LOG_D)

    def delete_ast_table_batch(self, ids_list):
        try:
            if ids_list and len(ids_list) > 0:
                # Convert list to tuple for SQL IN clause
                if len(ids_list) == 1:
                    ids_tuple = f"({ids_list[0]})"
                else:
                    ids_tuple = str(tuple(ids_list))
                
                query = f"DELETE FROM ast_table WHERE id IN {ids_tuple}"
                self.cursor.execute(query)
                self.conn.commit()
                logger.info(f"Batch deleted {len(ids_list)} records from ast_table", extra=LOG_D)
        except Exception as e:
            logger.error(f"Error batch delete ast_table {str(e)} ", extra=LOG_D)
            # Try to rollback if possible, though sqlite3 usually auto-rolls back on error in standard mode
            try:
                self.conn.rollback()
            except:
                pass

    def get_ast_table(self,id=None):
        try:
            if id is not None:
                self.cursor.execute(f"SELECT * FROM ast_table WHERE id={id}")
                return self.cursor.fetchall()
            else:
                self.cursor.execute(f"SELECT * FROM ast_table")
                return self.cursor.fetchall()
        except Exception as e:
            logger.error(f"Error create ast_table table {str(e)} ",extra=LOG_D)
            return None 
    def update_ast_table(self,id=None,date=None,device_name=None,file_name=None,spectrogram_image=None,sound1=None,conf_sound1=None,sound2=None,conf_sound2=None,sound3=None,conf_sound3=None,rms=None,transcription=None,emotion=None, clap_embedding=None, llm_verdict=None):
        try:
            if id is not None:
                if date is not None:
                    sql_query=f"UPDATE ast_table SET date='{date}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                if device_name is not None:
                    sql_query=f"UPDATE ast_table SET device_name='{device_name}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                if file_name is not None:
                    sql_query=f"UPDATE ast_table SET file_name='{file_name}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                if spectrogram_image is not None:
                    sql_query=f"UPDATE ast_table SET spectrogram_image='{spectrogram_image}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                if sound1 is not None:
                    sql_query=f"UPDATE ast_table SET sound1='{sound1}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                if conf_sound1 is not None:
                    sql_query=f"UPDATE ast_table SET conf_sound1={conf_sound1} WHERE id={id}"
                    self.cursor.execute(sql_query)
                if sound2 is not None:
                    sql_query=f"UPDATE ast_table SET sound2='{sound2}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                if conf_sound2 is not None:
                    sql_query=f"UPDATE ast_table SET conf_sound2={conf_sound2} WHERE id={id}"
                    self.cursor.execute(sql_query)
                if sound3 is not None:
                    sql_query=f"UPDATE ast_table SET sound3='{sound3}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                if conf_sound3 is not None:
                    sql_query=f"UPDATE ast_table SET conf_sound3={conf_sound3} WHERE id={id}"
                    self.cursor.execute(sql_query)
                if rms is not None:
                    sql_query=f"UPDATE ast_table SET rms={rms} WHERE id={id}"
                    self.cursor.execute(sql_query)
                if transcription is not None:
                    sql_query=f"UPDATE ast_table SET transcription='{transcription}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                if emotion is not None:
                    sql_query=f"UPDATE ast_table SET emotion='{emotion}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                if clap_embedding is not None:
                    sql_query=f"UPDATE ast_table SET clap_embedding=? WHERE id=?"
                    self.cursor.execute(sql_query, (clap_embedding, id))
                if llm_verdict is not None:
                    sql_query=f"UPDATE ast_table SET llm_verdict='{llm_verdict}' WHERE id={id}"
                    self.cursor.execute(sql_query)
                self.conn.commit()
                logger.info(f"Update ast_table {id} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error update ast_table {str(e)} ",extra=LOG_D)
    def insert_ast_table(self,date=None,device_name=None,file_name=None,spectrogram_image=None,sound1=None,conf_sound1=None,sound2=None,conf_sound2=None,sound3=None,conf_sound3=None,rms=None,transcription=None,emotion=None, clap_embedding=None, llm_verdict=None, snr=None):
        try:
            if  not file_name is None and  not spectrogram_image is None and not sound1 is None and not conf_sound1 is None and not sound2 is None and not conf_sound2 is None and not sound3 is None and not conf_sound3 is None:
                if device_name is None:
                    device_name='raspberry'
                
                # Sanitize data types for SQLite
                if isinstance(rms, (np.float32, np.float64)):
                    rms = float(rms)
                
                if isinstance(emotion, tuple) and len(emotion) == 2:
                    label, conf = emotion
                    emotion = f"{str(label).upper()} ({float(conf):.2f})"
                elif emotion is not None:
                    emotion = str(emotion)

                # Ensure clap_embedding is handled by the numpy adapter
                if clap_embedding is not None:
                    if not isinstance(clap_embedding, np.ndarray):
                        clap_embedding = np.array(clap_embedding)
                    # Ensure it's 1D for storage if it's a single embedding (e.g. from CLAP)
                    if clap_embedding.ndim > 1:
                        clap_embedding = clap_embedding.flatten()

                # Parameters for the SQL query
                params = [
                    date, # Pass None if date is None, let DB handle DEFAULT CURRENT_TIMESTAMP
                    file_name,
                    device_name,
                    spectrogram_image,
                    sound1,
                    float(conf_sound1) if conf_sound1 is not None else 0.0,
                    sound2,
                    float(conf_sound2) if conf_sound2 is not None else 0.0,
                    sound3,
                    float(conf_sound3) if conf_sound3 is not None else 0.0,
                    rms,
                    transcription,
                    emotion,
                    clap_embedding,
                    llm_verdict
                ]

                # Where the device was when this happened. Stamped now rather
                # than resolved at display time, so moving a sentinel later
                # does not retroactively relocate its old events. Unassigned
                # devices leave it NULL, as do all events recorded before
                # rooms.json existed.
                try:
                    from misc.rooms import room_for
                    params.append(room_for(device_name))
                except Exception as exc:   # never block an event over this
                    logger.warning(f"Could not resolve room for {device_name}: {exc}",
                                   extra=LOG_D)
                    params.append(None)

                # Recorded for every event, speech or not: it describes the
                # capture, not the content.
                params.append(float(snr) if snr is not None else None)

                sql_query = """
                    INSERT INTO ast_table ( date, file_name, device_name, spectrogram_image,
                                            sound1, conf_sound1, sound2, conf_sound2, sound3, conf_sound3,
                                            rms, transcription, emotion, clap_embedding, llm_verdict, room, snr )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                
                logger.info(f"DEBUG: spectrogram_image before execute: {spectrogram_image}", extra=LOG_D)
                self.cursor.execute(sql_query, tuple(params))
                self.conn.commit()
                self.cursor.execute(f"SELECT id, file_name, spectrogram_image FROM ast_table ORDER BY id DESC LIMIT 1")
                id, retrieved_file_name, retrieved_spectrogram_image = self.cursor.fetchone()
                logger.info(f"DEBUG: Retrieved from DB (after insert) - ID: {id}, file_name: {retrieved_file_name}, spectrogram_image: {retrieved_spectrogram_image}", extra=LOG_D)
                logger.info(f"Save new record to DB \n \t\t\t\t\t\t Event_Id : {id} \n \t\t\t\t\t\t Date : {date} \n \t\t\t\t\t\t Device : {device_name} \n \t\t\t\t\t\t Mp3 file name : {file_name} \n \t\t\t\t\t\t Spectrogram  : {spectrogram_image}\n \t\t\t\t\t\t Sound1 : {sound1} \n \t\t\t\t\t\t Conf Sound1 : {conf_sound1} \n \t\t\t\t\t\t Sound2 : {sound2} \n \t\t\t\t\t\t Conf Sound2 : {conf_sound2} \n \t\t\t\t\t\t Sound3 : {sound3} \n \t\t\t\t\t\t Conf Sound3 : {conf_sound3} \n \t\t\t\t\t\t RMS (dB) : {rms}",extra=LOG_D)
                return id
            return None
               
        except Exception as e:
            logger.error(f"Error DB Insert on query: {sql_query} with params: {params}. Error: {e}",extra=LOG_D)
            return None