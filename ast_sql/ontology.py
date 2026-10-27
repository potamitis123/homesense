import os
import sys
import json
from .object_tables import Object_tables

#sys.path.append('./misc')
# Removed sys.path.append('./ast_src') as project root is now handled
from misc.log_test import logger
LOG_D={'module_name':'ontology'}

class Ontology(Object_tables):
    def __init__(self,conn):
        super().__init__(conn)
        self.cursor = self.conn.cursor()
        self.__create_tables__()
        self.just_load=True
    def __del__(self):
        pass
    def __create_tables__(self):
        if not self.__check_exit_table__('ontology'):
            from ast_src.ast_model import AST
            ast = AST()
            try:
                self.cursor.execute("""CREATE TABLE ontology (
                                                            id TEXT PRIMARY KEY NOT NULL,
                                                            name TEXT NOT NULL,
                                                            description TEXT,
                                                            child_ids TEXT,
                                                            restrictions TEXT,
                                                            used BOOLEAN  DEFAULT 1  
                                                        );
                                    """)
                self.conn.commit()
                logger.info(f"Create ontology table", extra=LOG_D)
                
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                ontology_path = os.path.join(project_root, 'ontology.json')
                
                with open(ontology_path, 'r') as f:
                    content = f.read().strip()
                
                # Robust JSON wrapping
                if not content.startswith('['):
                    content = '[' + content + ']'
                
                ontology_data = json.loads(content)
                ontology_map = {item['name']: item for item in ontology_data}
                
                list_classes = ast.get_classes()
                num_ontologies = 0
                for class_name in list_classes:
                    # Look up class in ontology.json, or use defaults
                    item = ontology_map.get(class_name, {
                        'id': f'custom_{num_ontologies}',
                        'name': class_name,
                        'description': 'No description available',
                        'child_ids': [],
                        'restrictions': []
                    })
                    
                    desc = item.get('description', '').replace('"', "'")
                    child_ids = json.dumps(item.get('child_ids', []))
                    restrictions = json.dumps(item.get('restrictions', []))
                    
                    # Set all to 1 (selected) by default
                    self.cursor.execute('''INSERT OR REPLACE INTO ontology (id, name, description, child_ids, restrictions, used) 
                                           VALUES (?, ?, ?, ?, ?, ?)''',
                                        (item['id'], class_name, desc, child_ids, restrictions, 1))
                    num_ontologies += 1
                    
                self.conn.commit()
                logger.info(f"Inserted {num_ontologies} AST-supported entries into ontology table", extra=LOG_D)
            except Exception as e:
                logger.error(f"Error create ontology table {str(e)} ", extra=LOG_D)
    def get_ontologies(self):
        try:
            self.cursor.execute("SELECT * FROM ontology")
            result = self.cursor.fetchall()
            logger.info(f"Get ontologies {result}",extra=LOG_D)
        except Exception as e:
            result=None
            logger.error(f"Error get ontologies {str(e)} ",extra=LOG_D)
        return result

    def load_ontologies(self, ontology_update_path=None):
        if not ontology_update_path is None or self.just_load:
            if (ontology_update_path and os.path.exists(ontology_update_path)) or self.just_load:
                # Get names where used = 0 (unselected)
                sql_query = "SELECT name FROM ontology WHERE used = 0 ORDER BY name"
                results = self.cursor.execute(sql_query)
                output = [row[0] for row in results.fetchall()]
                self.just_load = False
                try:
                    if ontology_update_path and os.path.exists(ontology_update_path):
                        os.remove(ontology_update_path)
                except Exception as e:  
                    logger.error(f"Error remove {str(e)} ", extra=LOG_D)
                logger.info(f"Ontology update: {len(output)} classes filtered out", extra=LOG_D)
                return output
            else:
                return None
        else:
            return None