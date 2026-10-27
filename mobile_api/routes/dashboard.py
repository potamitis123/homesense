from __future__ import annotations

from fastapi import APIRouter, Depends

from mobile_api.models.schemas import DashboardToday
from mobile_api.security.authentication import require_auth
from mobile_api.services import mobile_dashboard_service

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("/today", response_model=DashboardToday)
def dashboard_today(_username: str = Depends(require_auth)):
    return mobile_dashboard_service.get_dashboard_today()
