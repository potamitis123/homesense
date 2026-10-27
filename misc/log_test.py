import os
import logging
from datetime import datetime


def init_logger(log_dir,log_name,time_per_line=True,print_log=False):
     #Init Logger
    try:
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
    except Exception as e:
        print(e.message)
        return None

    try:
        LOG=os.environ.get('LOG')
    except:
        LOG=1

    if LOG is None or LOG==1:
        level=logging.INFO
    else:
        level=logging.WARNING

    today = datetime.today().strftime("%Y%m%d")
   
    logger = logging.getLogger(log_name)
    logger.setLevel(level)
    # Third-party libraries (speechbrain, pyannote) call logging.basicConfig() at import
    # time, which would otherwise re-emit every record through the root handler.
    logger.propagate = False
    for stale in list(logger.handlers):
        logger.removeHandler(stale)
    if time_per_line:
        log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(module_name)s - %(message)s")
    else:
        log_formatter = logging.Formatter("%(levelname)s - %(module_name)s - %(message)s")

    # Use absolute path for the log file in the project root
    base_dir = os.path.dirname(os.path.abspath(__file__))
    abs_log_dir = os.path.abspath(os.path.join(base_dir, '..', log_dir))
    if not os.path.exists(abs_log_dir):
        os.makedirs(abs_log_dir)
    
    log_file_path = os.path.join(abs_log_dir, f"{log_name}.log")
    fh = logging.FileHandler(filename=log_file_path, mode='a', encoding='utf-8')
    fh.setFormatter(log_formatter)
    fh.setLevel(level)

    logger.addHandler(fh)
    if print_log:
        if time_per_line:
            console_formatter = logging.Formatter("%(asctime)s - %(module_name)s - %(message)s")
        else:
            console_formatter = logging.Formatter(" %(module_name)s - %(message)s")
        consoleHandler = logging.StreamHandler()
        consoleHandler.setFormatter(console_formatter)
        consoleHandler.setLevel(level)
        logger.addHandler(consoleHandler)
    return logger

logger=init_logger(log_dir="logs_ast_app",
                   log_name=f"logs_ast_app",
                   print_log=True
                   )