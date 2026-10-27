from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from mobile_api.models.schemas import HeatmapResponse
from mobile_api.security.authentication import require_auth
from mobile_api.services import mobile_heatmap_service

router = APIRouter(prefix="/api/v1/heatmap", tags=["heatmap"])


@router.get("/today", response_model=HeatmapResponse)
def heatmap_today(
    device: Optional[str] = Query(None, description="Device id, or 'all' / omitted for every device"),
    _username: str = Depends(require_auth),
):
    return mobile_heatmap_service.get_heatmap_today(device=device)
