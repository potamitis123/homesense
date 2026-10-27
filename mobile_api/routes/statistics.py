from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from mobile_api.models.schemas import StatisticsResponse
from mobile_api.security.authentication import require_auth
from mobile_api.services import mobile_statistics_service

router = APIRouter(prefix="/api/v1/statistics", tags=["statistics"])


@router.get("/today", response_model=StatisticsResponse)
def statistics_today(
    device: Optional[str] = Query(None, description="Device id, or 'all' / omitted for every device"),
    _username: str = Depends(require_auth),
):
    return mobile_statistics_service.get_statistics_today(device=device)
