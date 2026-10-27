from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from mobile_api.models.schemas import EventOut, EventsListResponse
from mobile_api.security.authentication import require_auth
from mobile_api.services import mobile_events_service

router = APIRouter(prefix="/api/v1/events", tags=["events"])


@router.get("", response_model=EventsListResponse)
def list_events(
    date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    label: Optional[str] = Query(None, description="Filter to a detected class, e.g. 'Speech'"),
    _username: str = Depends(require_auth),
):
    return mobile_events_service.get_events(date=date, limit=limit, offset=offset, label=label)


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: int, _username: str = Depends(require_auth)):
    event = mobile_events_service.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return event
