from __future__ import annotations

from fastapi import APIRouter, Depends

from mobile_api.models.schemas import DevicesListResponse
from mobile_api.security.authentication import require_auth
from mobile_api.services import mobile_device_service

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])


@router.get("", response_model=DevicesListResponse)
def list_devices(_username: str = Depends(require_auth)):
    return {"devices": mobile_device_service.get_devices()}
