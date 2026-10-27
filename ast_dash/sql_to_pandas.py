import pandas as pd
import sqlite3
import sys
import os
# Must be set before numpy/torch BLAS runtimes collide during CLAP matmul.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
import numpy as np
import io
from misc.load_parameters import parameters
from misc.log_test import logger

LOG_D={'module_name':'sql_to_pandas'}

def convert_array(text):
    if text is None:
        return None
    if isinstance(text, np.ndarray):
        return np.ascontiguousarray(text, dtype=np.float32)
    out = io.BytesIO(text)
    out.seek(0)
    return np.ascontiguousarray(np.load(out), dtype=np.float32)

class SQL_to_Pandas():
    def __init__(self,db_file=None):
        if db_file is None:
            db_file = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', parameters.get_parameters()['sqlite']['db_name']))
        self.conn = sqlite3.connect(db_file, check_same_thread=False, timeout=30.0)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.commit()
        except:
            pass
        self.df = None

    def __del__(self):
        try:
            if hasattr(self, 'conn') and self.conn:
                self.conn.close()
        except Exception as e:
            logger.error(f"Error close Ast sql lite {str(e)} ",extra=LOG_D)

    def rename_columns(self,df):
        # Map emotions if present
        if 'emotion' in df.columns:
            emotion_map = {'neu': 'NEUTRAL', 'hap': 'HAPPY', 'ang': 'ANGRY', 'sad': 'SAD'}
            # Apply mapping: check lower() key, default to uppercase original if not found
            df['emotion'] = df['emotion'].apply(lambda x: emotion_map.get(str(x).lower(), str(x).upper()) if x is not None else x)

        return df.rename(columns={"id": "ID", 
                                "date": "Timestamp",
                                "device_name":"Device",
                                "sound1":"Class 1",
                                "conf_sound1":"Prob 1",
                                "sound2":"Class 2",
                                "conf_sound2":"Prob 2",
                                "sound3":"Class 3",
                                "conf_sound3":"Prob 3",
                                 })

    def get_columns(self,df):
        def fun_cellStyle(field):
            if field=='Prob 1' or field=='Prob 2' or  field=='Prob 3':
               return{
                    "styleConditions": [
                                             {
                                                 "condition": "params.value >= 0.3",
                                                 "style": {"backgroundColor": "red", "color": "white", "textAlign": "left"},
                                             },
                                             {
                                                 "condition": "params.value >= 0.1 && params.value < 0.3",
                                                 "style": {"backgroundColor": "yellow", "textAlign": "left"},
                                             },
                                             {
                                                 "condition": "params.value < 0.1",
                                                 "style": {"backgroundColor": "#e6f7ff", "textAlign": "left"},
                                             }
                                         ],
                }
            else:
                return {'textAlign': 'left'}
        
        if df is None:
            try:
                df = pd.read_sql_query("SELECT * FROM ast_table ORDER BY date ASC LIMIT 1", self.conn)
            except:
                return []
        
        columns= []
        columns.append({
            "headerName": "",
            "checkboxSelection": True,
            "headerCheckboxSelection": True,
            "width": 40,
            "minWidth": 40,
            "maxWidth": 40,
            "pinned": "left",
            "lockPosition": True,
            "suppressMovable": True,
            "resizable": False,
            "colId": "checkbox_col"
        })
        for i in df.columns:
            if i=='spectrogram_image' or i=='file_name' or i=='clap_embedding':
                tmp_field={"field": i, 'hide': True, 'suppressMovable': True, 'resizable': True}
            elif i=='similarity':
                tmp_field={
                    "field": i, 
                    "hide": False, 
                    "headerName": "Similarity", 
                    "suppressMovable": True, 
                    "resizable": True,
                    "width": 100,
                    "cellStyle": {'textAlign': 'left', 'fontWeight': 'bold', 'color': '#2E8B57'},
                    "valueFormatter": {"function": "params.value ? Number(params.value).toFixed(3) : ''"},
                    "pinned": "right"
                }
            elif i=='rms':
                tmp_field={
                    "field": i, 
                    "hide": False, 
                    "headerName": "RMS", 
                    "suppressMovable": True, 
                    "resizable": True,
                    "width": 80,
                    "cellStyle": {'textAlign': 'left'},
                    "valueFormatter": {"function": "params.value ? Number(params.value).toFixed(1) : ''"}
                }
            elif i=='emotion' or i=='Emotion':
                 tmp_field={
                    "field": i, 
                    "hide": False, 
                    "headerName": "Emotion", 
                    "suppressMovable": True, 
                    "resizable": True,
                    "cellStyle": {'textAlign': 'left'}
                }
            else:
                style = fun_cellStyle(i)
                if "styleConditions" in style:
                    for cond in style["styleConditions"]:
                        if "style" in cond:
                            cond["style"]["textAlign"] = "left"
                    style["defaultStyle"] = {"textAlign": "left"}
                else:
                    style.update({'textAlign': 'left'})

                tmp_field={"field": i, 'hide': False, 'cellStyle': style, 'suppressMovable': True, 'resizable': True}
                if i in ['Prob 1', 'Prob 2', 'Prob 3']:
                    tmp_field["valueFormatter"] = {"function": "params.value ? Number(params.value).toFixed(2) : ''"}
            
            tmp_field.update({
                "wrapHeaderText": False,
                "autoHeaderHeight": False,
            })
            columns.append(tmp_field)
                
        return columns
    
    def get_all_rec(self):
        self.df = pd.read_sql_query("SELECT * FROM ast_table ORDER BY date DESC", self.conn)
        return self.df
    
    def get_first_rec(self):
        self.df = pd.read_sql_query("SELECT * FROM ast_table ORDER BY date ASC LIMIT 1", self.conn)
        return self.df
    
    def get_last_rec(self):
        self.df = pd.read_sql_query("SELECT * FROM ast_table ORDER BY date DESC LIMIT 1", self.conn)
        return self.df
    
    def get_by_id(self, id):
        """Single-row lookup by primary key. Mirrors the ad hoc raw-SQL
        query previously inlined in ast_app.py's play_row_audio callback
        (SELECT ... FROM ast_table WHERE id = ?), promoted to a real
        method so other callers (e.g. mobile_api) don't have to repeat it."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM ast_table WHERE id = ?", (id,))
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))

    def get_date_from_to(self, date_from=None, date_to=None):
        try:
            if (date_from is not None and date_to is not None) and date_to > date_from:
                self.df = pd.read_sql_query(f"SELECT * FROM ast_table WHERE date BETWEEN '{date_from}' AND '{date_to}' ORDER BY date DESC", self.conn)
            else:
                self.df = pd.read_sql_query("SELECT * FROM ast_table ORDER BY date DESC", self.conn)
            return self.df
        except Exception as e:
            logger.error(f"SQL Error in get_date_from_to: {e}", extra=LOG_D)
            return pd.DataFrame()

    def get_clap_search_results(self, query_text, threshold=0.25, date_from=None, date_to=None):
        try:
            from ast_src.clap_service import CLAPService
            from misc.clap_expansions import expand_query

            df = self.get_date_from_to(date_from, date_to)
            if df.empty:
                return df

            if 'clap_embedding' not in df.columns:
                logger.warning("clap_embedding column missing from results", extra=LOG_D)
                return pd.DataFrame()

            # Filter out rows with no embedding
            df_with_embed = df.dropna(subset=['clap_embedding'])
            if df_with_embed.empty:
                return pd.DataFrame()

            clap_service = CLAPService()

            # Home-dynamics categories (shouting, anger, pain, laughter, ...) get
            # ensembled with related phrasings and penalized against opposite/noise
            # terms; queries that match no category fall back to a plain embedding.
            related_queries, negative_queries = expand_query(query_text)
            if related_queries:
                logger.info(f"CLAP query '{query_text}' expanded with related={related_queries}", extra=LOG_D)
                query_embed = clap_service.get_ensemble_query_embedding(query_text, related_queries)
            else:
                query_embed = clap_service.get_text_embedding(query_text)

            # Extract and flatten embeddings for vectorized calculation
            valid_embeddings = []
            valid_indices = []
            
            for idx, row in df_with_embed.iterrows():
                try:
                    db_embed_arr = convert_array(row['clap_embedding'])
                    if db_embed_arr is not None:
                        valid_embeddings.append(db_embed_arr.flatten())
                        valid_indices.append(idx)
                except Exception as e:
                    logger.error(f"Error extracting embedding for row {idx}: {e}", extra=LOG_D)
            
            if not valid_embeddings:
                return pd.DataFrame()

            # Convert to matrix (N, D)
            audio_embed_matrix = np.vstack(valid_embeddings)
            
            if negative_queries:
                logger.info(f"CLAP query '{query_text}' contrasted against negative={negative_queries}", extra=LOG_D)
                similarities = clap_service.search_with_contrast(
                    query_embed, audio_embed_matrix, negative_queries=negative_queries)
            else:
                similarities = clap_service.compute_similarity(query_embed, audio_embed_matrix)
            
            # Filter by threshold
            match_mask = similarities >= threshold
            matched_indices = [valid_indices[i] for i, is_match in enumerate(match_mask) if is_match]
            matched_similarities = similarities[match_mask]
            
            if not matched_indices:
                return pd.DataFrame()

            df_result = df.loc[matched_indices].copy()
            df_result['similarity'] = matched_similarities
            df_result = df_result.sort_values(by='similarity', ascending=False)
            return df_result

        except Exception as e:
            logger.error(f"Error in get_clap_search_results: {e}", extra=LOG_D)
            return pd.DataFrame()
