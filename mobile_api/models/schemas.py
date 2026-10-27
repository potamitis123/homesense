"""Pydantic response/request models for the mobile API. Kept small and
mobile-friendly -- never a passthrough of internal DB rows."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


# ── Auth ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


# ── Dashboard ───────────────────────────────────────────────────────

class LastEvent(BaseModel):
    time: Optional[str] = None
    device: Optional[str] = None
    type: Optional[str] = None


class DashboardToday(BaseModel):
    date: str
    total_events: int
    devices_online: int
    devices_total: int
    last_event: Optional[LastEvent] = None
    most_active_location: Optional[str] = None


# ── Events ──────────────────────────────────────────────────────────

class EventDetection(BaseModel):
    label: str
    confidence: float


class EventTranscription(BaseModel):
    speaker: Optional[str] = None
    text: Optional[str] = None
    emotion: Optional[str] = None


class EventOut(BaseModel):
    id: int
    timestamp: str
    device_id: str
    location: str
    transcription: Optional[EventTranscription] = None
    detections: List[EventDetection] = []
    audio_available: bool = False


class EventsListResponse(BaseModel):
    date: Optional[str] = None
    total: int
    limit: int
    offset: int
    events: List[EventOut]


# ── Heatmap ─────────────────────────────────────────────────────────

class ClassHourlyActivity(BaseModel):
    class_name: str
    hourly_activity: List[int]


class HeatmapResponse(BaseModel):
    date: str
    device: str  # a device_id, or "all"
    classes: List[ClassHourlyActivity]


# ── Statistics ──────────────────────────────────────────────────────

class StatisticsResponse(BaseModel):
    date: str
    device: str  # a device_id, or "all"
    activity: dict[str, int]
    available_classes: List[str]


# ── Devices ─────────────────────────────────────────────────────────

class DeviceCapabilities(BaseModel):
    duplex: bool = False
    video: bool = False


class DeviceOut(BaseModel):
    device_id: str
    name: str
    online: bool
    last_seen: Optional[str] = None
    ip_address: Optional[str] = None
    capabilities: DeviceCapabilities = DeviceCapabilities()


class DevicesListResponse(BaseModel):
    devices: List[DeviceOut]


# ── Audio / PTT session ─────────────────────────────────────────────

class AudioSessionCreateRequest(BaseModel):
    device_id: str


class AudioSessionCreateResponse(BaseModel):
    session_id: str
    device_id: str
    status: str


# ── WebRTC video call signaling ─────────────────────────────────────
# Message bodies are opaque SDP/ICE dicts relayed verbatim between the
# app and the P4 sentinel; nothing here parses them (see
# mobile_api/services/mobile_webrtc_service.py).

class WebrtcVideoProfile(BaseModel):
    width: int
    height: int
    fps: int
    quality: float


class WebrtcSessionCreateRequest(BaseModel):
    device_id: str


class WebrtcSessionCreateResponse(BaseModel):
    session_id: str
    device_id: str
    ice_servers: List[dict]
    video: WebrtcVideoProfile


class WebrtcSignalRequest(BaseModel):
    session_id: str
    message: dict


class WebrtcPollResponse(BaseModel):
    messages: List[dict]


class WebrtcCloseRequest(BaseModel):
    session_id: str
