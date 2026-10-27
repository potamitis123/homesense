"""Stream an existing recording from disk, with HTTP range-request support
so Android can seek/scrub without downloading the whole file. Uses the
existing recording storage under assets/ directly -- nothing is copied."""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from mobile_api.security.authentication import require_auth
from mobile_api.services import mobile_recordings_service

router = APIRouter(prefix="/api/v1/recordings", tags=["recordings"])

_CHUNK_SIZE = 1024 * 256
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@router.get("/{recording_id}")
def stream_recording(recording_id: int, request: Request, _username: str = Depends(require_auth)):
    resolved = mobile_recordings_service.resolve_recording(recording_id)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")

    path = resolved["path"]
    media_type = resolved["media_type"]
    file_size = os.path.getsize(path)

    range_header = request.headers.get("range")
    start, end = 0, file_size - 1

    if range_header:
        match = _RANGE_RE.match(range_header)
        if not match:
            raise HTTPException(status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE, detail="Invalid Range header")
        start_str, end_str = match.groups()
        if start_str:
            start = int(start_str)
        if end_str:
            end = int(end_str)
        if start > end or end >= file_size:
            raise HTTPException(status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE, detail="Range not satisfiable")

    length = end - start + 1

    def iterfile():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    status_code = status.HTTP_200_OK
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        status_code = status.HTTP_206_PARTIAL_CONTENT

    return StreamingResponse(iterfile(), status_code=status_code, media_type=media_type, headers=headers)
