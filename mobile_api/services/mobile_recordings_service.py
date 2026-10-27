"""Resolve a recording ID to a real file on disk, using the existing
recording storage as-is (no copying, no second storage location)."""

from __future__ import annotations

from typing import Optional

from mobile_api.adapters import existing_app_adapter as existing
from mobile_api.adapters import recordings_files


def resolve_recording(recording_id: int) -> Optional[dict]:
    row = existing.get_event_by_id(recording_id)
    if row is None:
        return None
    file_name = row.get("file_name")
    path = recordings_files.resolve_audio_path(file_name)
    if path is None:
        return None
    return {"path": path, "media_type": recordings_files.guess_media_type(path)}
