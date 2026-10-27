from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from mobile_api.logging_setup import get_logger, log_extra
from mobile_api.models.schemas import (
    AccessTokenResponse,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
)
from mobile_api.security import authentication, rate_limit
from mobile_api.security.credentials_store import credentials_store

logger = get_logger()

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if rate_limit.login_rate_limiter is not None and not rate_limit.login_rate_limiter.allow(client_ip):
        logger.warning("login rate-limited", extra=log_extra("MOBILE_AUTH"))
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")

    if not credentials_store.verify(payload.username, payload.password):
        logger.warning(f"failed login for user '{payload.username}'", extra=log_extra("MOBILE_AUTH"))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if authentication.token_store is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Auth not initialised")

    access, refresh = authentication.token_store.issue_pair(payload.username)
    logger.info(f"user '{payload.username}' logged in", extra=log_extra("MOBILE_AUTH"))
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in_minutes=authentication.token_store.access_minutes,
    )


@router.post("/refresh", response_model=AccessTokenResponse)
def refresh(payload: RefreshRequest):
    if authentication.token_store is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Auth not initialised")

    access = authentication.token_store.issue_access_from_refresh(payload.refresh_token)
    if access is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    return AccessTokenResponse(
        access_token=access,
        expires_in_minutes=authentication.token_store.access_minutes,
    )
