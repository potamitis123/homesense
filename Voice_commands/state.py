"""Mute flag + last-command record for the 'Kokora' voice-command feature.

Written by ast_src/main.py (the live pipeline process) when a command is
heard, read by that same process on every file (the mute gate) and by the
Dash app process (the Commands tab) -- so, same as common_files/rooms.json,
this is a small JSON file re-read whenever its mtime changes rather than
kept in memory, since writer and readers are separate processes.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json')

_lock = threading.Lock()
_cache = {}
_mtime = None

_DEFAULT_STATE = {
    "muted_until": None,
    "last_command_text": None,
    "last_command_time": None,
    "last_command_device": None,
}


def _load_locked():
    global _cache, _mtime
    try:
        stamp = os.path.getmtime(_PATH)
    except OSError:
        _cache, _mtime = dict(_DEFAULT_STATE), None
        return
    if stamp == _mtime:
        return
    try:
        with open(_PATH, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # Keep serving the last good state: a half-saved file should not
        # suddenly look like the mute expired (or newly activated).
        print(f"[Voice_commands.state] cannot read {_PATH}: {exc}")
        _mtime = stamp
        return
    _cache = {**_DEFAULT_STATE, **(data or {})}
    _mtime = stamp


def _write_locked(state):
    global _cache, _mtime
    # Atomic write (temp file + replace) so a reader never sees a
    # half-written file mid-save.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(_PATH), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp_path, _PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    _cache = dict(state)
    _mtime = os.path.getmtime(_PATH)


def get_state():
    """The full state dict (muted_until / last_command_text / _time / _device)."""
    with _lock:
        _load_locked()
        return dict(_cache)


def is_muted():
    """True while a 'Kokora stop' mute is currently in effect."""
    with _lock:
        _load_locked()
        muted_until = _cache.get('muted_until')
    if not muted_until:
        return False
    try:
        return datetime.fromisoformat(muted_until) > datetime.now()
    except ValueError:
        return False


def mute_remaining_seconds():
    """Seconds left on the current mute, or None if not currently muted."""
    with _lock:
        _load_locked()
        muted_until = _cache.get('muted_until')
    if not muted_until:
        return None
    try:
        remaining = (datetime.fromisoformat(muted_until) - datetime.now()).total_seconds()
    except ValueError:
        return None
    return remaining if remaining > 0 else None


def trigger_mute(command_text, device_name, hours=1.0):
    """Record a heard command and mute processing for `hours` from now."""
    now = datetime.now()
    state = {
        "muted_until": (now + timedelta(hours=hours)).isoformat(),
        "last_command_text": command_text,
        "last_command_time": now.isoformat(),
        "last_command_device": device_name,
    }
    with _lock:
        _write_locked(state)
