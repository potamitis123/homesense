"""Human-readable console reporting for the Sentinel home-server processes.

The background pipeline and the Dash frontend are launched in their own consoles
by ``boot_sentinel.bat``. These helpers make those consoles readable at a glance
instead of a wall of interleaved library log lines.
"""

import logging
import os
import sys
from datetime import datetime

WIDTH = 78

# Libraries that call logging.basicConfig() or log at DEBUG on import and would
# otherwise bury the pipeline's own messages.
NOISY_LOGGERS = (
    "speechbrain",
    "pyannote",
    "pytorch_lightning",
    "lightning_fabric",
    "urllib3",
    "filelock",
    "huggingface_hub",
    "matplotlib",
    "numba",
    "asyncio",
    "werkzeug",
    "opentelemetry",
    "httpx",
    "PIL",
)


def quiet_third_party_loggers(level=logging.WARNING):
    """Silence import-time DEBUG chatter from ML libraries.

    Claiming the root handler matters as much as setting the level: several of
    these libraries call ``logging.basicConfig()`` while their models load, and
    that call is a no-op only if the root logger already has a handler.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s - %(name)s - %(message)s"))
        handler.setLevel(level)
        root.addHandler(handler)
    root.setLevel(level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(level)


def _write(line):
    # main.py replaces sys.stdout with a tee that also writes to the log file.
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def banner(title, rows=None, char="="):
    """Print a boxed title plus optional ``(label, value)`` rows."""
    _write("")
    _write(char * WIDTH)
    _write(f" {title}")
    _write(char * WIDTH)
    for label, value in rows or []:
        _write(f"  {label:<26}{value}")
    if rows:
        _write(char * WIDTH)
    _write("")


def status_line(*parts):
    """Print a single timestamped status line, e.g. during the polling loop."""
    stamp = datetime.now().strftime("%H:%M:%S")
    _write(f"[{stamp}] " + "  |  ".join(str(p) for p in parts))


def describe_duration(seconds):
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def known_devices(project_root):
    """Return ``{device_id: info}`` from the sentinel status file, or ``{}``."""
    import json

    path = os.path.join(project_root, "sentinel_status.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}
