"""
Recording ID -> file path resolution, and HTTP range-request streaming.

The path-probing logic mirrors ast_dash/pages/audio_details.py's
prepare_spectrogram_fig() (lines ~60-98) exactly (same three audio
folders, same .wav/.mp3 fallback) rather than importing that module: it's
a dash.register_page page that also instantiates AudioSessionManager at
import time (opens MQTT + starts mic capture as a side effect of import),
which would be actively harmful to import from a separate process.

Uses the existing recording storage under assets/ as-is -- no files are
copied or duplicated into any mobile_api-specific location.
"""

from __future__ import annotations

import os
from typing import Optional

from mobile_api.config import PROJECT_ROOT

_AUDIO_FOLDERS = ["audio_mp3", "audios", "audiosuser"]
_BASE_ASSETS = os.path.join(PROJECT_ROOT, "assets")


def resolve_audio_path(file_name: Optional[str]) -> Optional[str]:
    """Given ast_table.file_name (a basename), return the absolute path to
    the actual audio file on disk, or None if it can't be found."""
    if not file_name:
        return None

    basename = os.path.basename(file_name)
    search_names = [basename]
    if basename.endswith(".mp3"):
        search_names.append(basename[:-4] + ".wav")
    elif basename.endswith(".wav"):
        search_names.append(basename[:-4] + ".mp3")

    for name in search_names:
        for folder in _AUDIO_FOLDERS:
            path = os.path.join(_BASE_ASSETS, folder, name)
            if os.path.exists(path):
                return path
    return None


def guess_media_type(path: str) -> str:
    return "audio/mpeg" if path.lower().endswith(".mp3") else "audio/wav"
