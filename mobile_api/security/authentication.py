"""
Password hashing and bearer-token session management for the mobile API.

Deliberately minimal, per the "research project, not heavy security libs"
requirement: everything here is Python standard library (hashlib, secrets).

Password hashing: PBKDF2-HMAC-SHA256 with a random per-user salt and 100k
iterations. Meaningfully stronger than the existing app's unsalted-SHA256
`users` table (ast_sql/users.py, misc.hash_password), with zero new
dependencies.

Tokens: opaque random strings (secrets.token_urlsafe), not JWT -- there is
nothing to sign/verify cryptographically, just a server-side lookup table.
Held in memory only: restarting the mobile API process invalidates every
session, which is an accepted tradeoff for a personal/home project (see
mobile_api/README.md). Do not extend this to a multi-instance deployment
without moving the token store somewhere shared.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mobile_api.logging_setup import get_logger, log_extra

logger = get_logger()

_PBKDF2_ITERATIONS = 100_000
_SALT_BYTES = 16


def hash_password(password: str) -> tuple[str, str]:
    """Returns (salt_hex, hash_hex). Never log either value."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return secrets.compare_digest(digest.hex(), hash_hex)


@dataclass
class _TokenInfo:
    username: str
    kind: str  # "access" | "refresh"
    expires_at: float


class TokenStore:
    """In-memory bearer-token issue/verify/revoke."""

    def __init__(self, access_minutes: int, refresh_days: int):
        self.access_minutes = access_minutes
        self._access_seconds = access_minutes * 60
        self._refresh_seconds = refresh_days * 24 * 60 * 60
        self._tokens: dict[str, _TokenInfo] = {}
        self._lock = threading.Lock()

    def issue_pair(self, username: str) -> tuple[str, str]:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[access] = _TokenInfo(username, "access", now + self._access_seconds)
            self._tokens[refresh] = _TokenInfo(username, "refresh", now + self._refresh_seconds)
        return access, refresh

    def issue_access_from_refresh(self, refresh_token: str) -> Optional[str]:
        info = self._get_valid(refresh_token, kind="refresh")
        if info is None:
            return None
        now = time.time()
        access = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[access] = _TokenInfo(info.username, "access", now + self._access_seconds)
        return access

    def verify_access(self, token: str) -> Optional[str]:
        info = self._get_valid(token, kind="access")
        return info.username if info else None

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def _get_valid(self, token: str, kind: str) -> Optional[_TokenInfo]:
        with self._lock:
            info = self._tokens.get(token)
            if info is None or info.kind != kind:
                return None
            if info.expires_at < time.time():
                self._tokens.pop(token, None)
                return None
            return info


# Populated by mobile_api.main at startup from MobileApiConfig.
token_store: Optional[TokenStore] = None

_bearer_scheme = HTTPBearer(auto_error=False)


def require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme)) -> str:
    """FastAPI dependency: returns the authenticated username or raises 401."""
    if credentials is None or token_store is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    username = token_store.verify_access(credentials.credentials)
    if username is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return username
