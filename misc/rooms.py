"""Which room each sentinel sits in.

The mapping lives in common_files/rooms.json and is the single source of
truth. It is read here and stamped onto each event as it is written, so the
room recorded against an event is where the device was *at the time*: moving
a sentinel later changes where new events are filed without rewriting the
history of the old ones.

Re-read whenever the file's timestamp changes, so editing rooms.json takes
effect without restarting anything.
"""

import json
import os
import threading

_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'common_files', 'rooms.json')

_lock = threading.Lock()
_cache = {}
_mtime = None


def _load_locked():
    global _cache, _mtime
    try:
        stamp = os.path.getmtime(_PATH)
    except OSError:
        _cache, _mtime = {}, None
        return
    if stamp == _mtime:
        return
    try:
        with open(_PATH, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # Keep serving the last good mapping: a half-saved file should not
        # start filing events under no room at all.
        print(f"[rooms] cannot read {_PATH}: {exc}")
        _mtime = stamp
        return
    _cache = {
        str(k): str(v).strip()
        for k, v in (data or {}).items()
        # Keys beginning with _ are notes to the reader, not devices.
        if not str(k).startswith('_') and str(v).strip()
    }
    _mtime = stamp


def room_for(device_name):
    """The room this device is in, or None if it has not been assigned one.

    None rather than "" so the column stays NULL for unassigned devices,
    which is what distinguishes them from events recorded before rooms
    existed at all.
    """
    if not device_name:
        return None
    with _lock:
        _load_locked()
        return _cache.get(str(device_name)) or None


def all_rooms():
    """The whole mapping, for anything that wants to list or display it."""
    with _lock:
        _load_locked()
        return dict(_cache)
