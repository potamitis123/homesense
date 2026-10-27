import os
import json
import time

class Parameters:
    def __init__(self,path):
        self.path=path
        self.parameters = None
        # Modification time of the copy currently in memory, and whether that
        # copy holds edits not yet written. Together they let get_parameters
        # notice a file edited from outside this process without ever throwing
        # away changes made through set_parameters.
        self._mtime = None
        self._dirty = False
        self._last_check = 0.0
        self.load_parameters()
    
    def load_parameters(self):
        try:
            # Explicit UTF-8: without it Python uses the Windows ANSI codepage,
            # and a single Greek character anywhere in the file makes the whole
            # of parameters.json fail to load -- taking every setting with it,
            # not just the one that was edited.
            with open(self.path, encoding='utf-8') as f:
                self.parameters = json.load(f)
            self._mtime = os.path.getmtime(self.path)
            self._dirty = False
        except FileNotFoundError:
            print(f"ERROR: parameters.json not found at {self.path}. Please ensure the file exists.")
            self.parameters = {} # Set to empty dict to avoid further errors
        except json.JSONDecodeError as e:
            print(f"ERROR: Could not decode parameters.json at {self.path}. Ensure it's valid JSON. Error: {e}")
            self.parameters = {} # Set to empty dict to avoid further errors
        except Exception as e:
            print(f"ERROR: An unexpected error occurred while loading parameters.json: {e}")
            self.parameters = {} # Set to empty dict to avoid further errors
    
    def get_parameters(self):
        """The current settings, re-reading the file if it changed on disk.

        The Settings tab already sees its own saves, because it saves in the
        same process that then reads them. A file edited by hand, or by another
        process, used to be invisible until a restart -- which made a setting
        look live when it was not. The stat() is throttled to once a second
        because this is called inside per-event loops.
        """
        if not self._dirty:
            now = time.monotonic()
            if now - self._last_check >= 1.0:
                self._last_check = now
                try:
                    if os.path.getmtime(self.path) != self._mtime:
                        self.load_parameters()
                except OSError:
                    pass                    # unreadable: keep what we have
        return self.parameters

    def set_parameters(self, parameters):
        # Marked dirty so the reload above cannot discard changes that are
        # still waiting for save_parameters().
        self.parameters = parameters
        self._dirty = True
    
    def save_parameters(self):
        try:
            # encoding='utf-8' is what makes ensure_ascii=False below safe;
            # without it the write raises on the first Greek character and the
            # bare except swallows it, losing the save silently.
            with open(self.path, 'w', encoding='utf-8') as f:
                # Indented and UTF-8 aware: this file is edited by hand as
                # well as by the Settings tab, and a single-line dump makes
                # that miserable. ensure_ascii=False keeps Greek readable
                # instead of turning it into \uXXXX escapes.
                json.dump(self.parameters, f, indent=2, ensure_ascii=False) 
            self.load_parameters()  
        except :
            pass


# Resolve path relative to this script's location
base_dir = os.path.dirname(os.path.abspath(__file__))
parametersfile = os.path.join(base_dir, '..', 'common_files', 'parameters.json')

# Fallback to CWD if relative path doesn't exist (legacy support)
if not os.path.exists(parametersfile):
    parametersfile = os.path.join(os.getcwd(), 'common_files', 'parameters.json')

parameters = Parameters(parametersfile)
