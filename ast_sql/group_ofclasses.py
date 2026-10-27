import sys
from .object_tables import Object_tables
#sys.path.append('./misc')
from misc.log_test import logger
LOG_D={'module_name':'devices'}

class Group_of_Classes(Object_tables):
    def __init__(self,conn):
        super().__init__(conn)
        self.table_name="group_of_clases"
        self.second_table_name="clases_of_group"
        self.cursor = self.conn.cursor()
        try:
            self.__create_devices_table__()
        except Exception as e:
            logger.error(f"Error Init Users sql lite {str(e)} ",extra=LOG_D)    
    def __create_devices_table__(self):
        #self.cursor.execute("""DROP TABLE IF EXISTS group_of_clases;""")
        #self.conn.commit()
        #self.cursor.execute("""DROP TABLE IF EXISTS clases_of_group;""")
        #self.conn.commit()
        if not self.__check_exit_table__('group_of_clases'):
            groups=[
                {"name":"Indoors safety","ontologies":[2, 7, 11, 12, 13, 15, 16, 21, 22, 25, 27, 28, 46, 50, 51, 52, 53, 57, 60, 63, 64, 67, 70, 74, 75, 79, 85, 86, 87, 88, 89, 91, 92, 93, 95, 96, 100, 101, 102, 103, 104, 107, 108, 109, 110, 111, 113, 114, 115, 117, 120, 121, 122, 126, 128, 129, 130, 131, 132, 133, 137, 199, 140, 142, 143, 144, 145, 146, 147, 149, 150, 151, 159, 170, 171, 172, 173, 174, 175, 177, 178, 179, 180, 184, 185, 186, 187, 189, 200, 204, 205, 206, 207, 208, 215, 216, 217, 218, 220, 221, 222, 223, 224, 226, 234, 240, 241, 246, 247, 248, 253, 254, 255, 256, 258, 260, 261, 262, 263, 264, 265, 266, 267, 268, 271, 272, 273, 274, 280, 283, 284, 289, 298, 302, 311, 322, 327, 329, 332, 341, 342, 343, 349, 350, 353, 361, 362, 363, 364, 371, 376, 378, 379, 380, 384, 385, 386, 388, 389, 391, 394, 395, 402, 403, 405, 406, 409, 411, 412, 413, 415, 419, 446, 448, 449, 450, 451, 455, 457, 458, 459, 461, 462, 465, 466, 468, 483, 484, 486, 490, 491, 494, 495, 496, 497, 498, 505, 506, 507, 508, 509, 510, 519, 520, 521, 522, 523, 524, 525, 526, 527]},
                {"name":"Elderly care","ontologies":[7, 11, 12, 13, 14, 15, 16, 17, 20, 21, 22, 25, 27, 32, 35,36, 38, 41, 46, 54, 57, 58, 60, 74, 81, 83, 84, 87, 88, 89, 90, 100, 101, 103, 104, 106, 107, 108, 109, 110, 111, 113, 114, 115, 117, 120, 121, 122, 126, 128, 129, 130, 131, 132, 137, 139, 140, 142, 144, 145, 146, 147, 149, 151, 163, 164, 170, 178, 179, 180, 182, 183, 184, 185, 186, 187, 200, 215, 220, 222, 223, 224, 226, 227, 238, 248, 253, 254, 255, 257, 259, 260, 261, 262, 263, 264, 265, 267, 268, 272, 273, 274, 280, 281, 284, 292, 293, 298, 299, 300, 324, 338, 353, 354,355, 361, 362, 363, 364, 366, 370, 371, 378, 379, 380, 381, 384, 385, 388, 389, 391, 395, 405, 406, 409, 411, 412, 413, 415, 419, 434, 448, 449, 450, 451, 455, 456, 457, 461, 462, 466, 467, 468, 483, 484, 486, 494, 495, 496, 497, 498, 503, 504, 507, 508, 509, 510, 521, 525, 527]},
                {"name":"Biodiversity","ontologies":[27, 34, 41, 42, 65, 67, 74, 76, 77, 85, 97, 112, 119, 123, 124, 125, 127, 144, 145, 158, 199, 210, 213, 242, 243, 252, 283, 286, 287, 291, 310, 315, 316, 354, 355, 358, 381, 407, 482, 502, 505]},
                {"name":"Machines","ontologies":[2, 6, 7, 8, 9, 10, 14, 25, 35, 36, 46, 49, 52, 68, 69, 70, 79, 136, 147, 150, 155, 170, 171, 172, 173, 177, 187, 288, 289, 290, 322, 326, 329, 340, 382, 471, 472, 473, 474, 478, 486, 487, 488, 490]},
                {"name":"Outdoors","ontologies":[2, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20, 21, 22, 23, 24, 25, 27, 28, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 49, 50, 51, 52, 53, 54, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 79, 80, 81, 82, 83, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 99, 100, 101, 102, 103, 104, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 135, 136, 137, 138, 139, 140, 141, 143, 144, 145, 146, 147, 148, 150, 151, 156, 157, 158, 159, 160, 165, 170, 171, 172, 173, 174, 175, 177, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 192, 194, 197, 199, 200, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 215, 216, 217, 218, 220, 221, 222, 224, 225, 226, 227, 228, 230, 234, 235, 236, 240, 241, 242, 245, 246, 247, 248, 249, 250, 251, 252, 254, 256, 258, 259, 260, 261, 263, 264, 265, 266, 267, 268, 269, 270, 271, 274, 280, 281, 282, 283, 287, 288, 289, 290, 292, 293, 302, 304, 308, 309, 310, 311, 312, 315, 316, 317, 318, 319, 320, 322, 323, 324, 325, 326, 327, 328, 329, 331, 332, 333, 334, 335, 336, 337, 338, 339, 340, 341, 342, 343, 344, 345, 346, 348, 349, 350, 351, 352, 353, 354, 355, 356, 357, 358, 359, 360, 362, 363, 364, 365, 366, 370, 371, 373, 375, 376, 377, 378, 379, 380, 381, 382, 383, 384, 385, 386, 387, 389, 390, 391, 392, 393, 394, 395, 396, 397, 398, 399, 400, 401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411, 412, 413, 415, 416, 417, 418, 419, 421, 422, 423, 424, 425, 426, 427, 428, 429, 430, 432, 433, 435, 436, 437, 438, 439, 441, 442, 444, 446, 447, 448, 449, 450, 451, 453, 455, 456, 457, 458, 459, 460, 461, 462, 464, 466, 467, 468, 469, 470, 471, 472, 473, 474, 475, 476, 477, 478, 479, 480, 481, 482, 484, 486, 487, 488, 490, 491, 492, 493, 494, 495, 496, 497, 498, 499, 500, 501, 503, 504, 505, 506, 507, 508, 509, 510, 511, 512, 513, 514, 515, 516, 517, 518, 519, 520, 522, 523, 524, 525]},
                {"name":"Drone Detection","ontologies":[2, 9, 10, 34, 46, 63, 150, 163, 164, 194, 223, 236, 252, 280, 287, 289, 329, 466, 487, 490]}
            ]
            try:
                self.cursor.execute("""CREATE TABLE group_of_clases (
                                                            group_name TEXT PRIMARY KEY,
                                                            group_enable BOOLEAN  DEFAULT 0,
                                                            FOREIGN KEY (group_name) REFERENCES clases_of_group(group_name) 
  
                                                        );
                                    """)
                self.conn.commit()
                logger.info(f"Create  group_of_clases table",extra=LOG_D)
                self.cursor.execute("""CREATE TABLE clases_of_group (
                                                            id INTEGER PRIMARY KEY,
                                                            group_name TEXT NOT NULL,
                                                            class_id int  NOT NULL
                                                        );
                                    """)
                self.conn.commit()
                logger.info(f"Create  clases_of_group table",extra=LOG_D)
                for group in groups:
                    self.cursor.execute(f"""INSERT INTO group_of_clases 
                                                (group_name, 
                                                group_enable)
                                            VALUES 
                                                ('{group['name']}',
                                                0);
                                        """)
                    self.conn.commit()
                    for class_id in group['ontologies']:
                        self.cursor.execute(f"""INSERT INTO clases_of_group 
                                                (group_name, 
                                                class_id)
                                            VALUES 
                                                ('{group['name']}',
                                                {class_id});
                                        """)
                    self.conn.commit()

                logger.info(f"Add records to  device table",extra=LOG_D)
            except Exception as e:
                logger.error(f"Error create devices table {str(e)} ",extra=LOG_D)
    def delete_group(self,group_name=None):
        try:
            self.cursor.execute(f"DELETE FROM group_of_clases WHERE group_name='{group_name}'")
            self.conn.commit()
            self.cursor.execute(f"DELETE FROM clases_of_group WHERE group_name='{group_name}'")
            self.conn.commit()
            logger.info(f"Delete group_of_clases and  {group_name} ",extra=LOG_D)
        except Exception as e:
            logger.error(f"Error delete device {str(e)} ",extra=LOG_D)
    
    def get_class_id_from_group_name(self,group_name=None):
        try:
            if id is not None:
                self.cursor.execute(f"SELECT * FROM clases_of_group WHERE group_name={group_name}")
                return self.cursor.fetchall()
            else:
               return None
        except Exception as e:
            logger.error(f"Error create ast_table table {str(e)} ",extra=LOG_D)
            return None 
