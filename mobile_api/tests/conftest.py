"""
Shared test fixtures. Everything that would touch the real production
database, the real credentials file, or a real MQTT broker is mocked --
these tests never require ESP32/MQTT hardware or the production DB.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from mobile_api.adapters import existing_app_adapter
from mobile_api.security.credentials_store import CredentialsStore
from mobile_api.services import mobile_audio_service, mobile_device_service


TEST_USERNAME = "testuser"
TEST_PASSWORD = "testpass123"


def _sample_events_df() -> pd.DataFrame:
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = [
        dict(id=1, date=f"{today} 08:00:00", rms=500.0, file_name="a.wav", device_name="kitchen",
             spectrogram_image="a.png", sound1="Speech", conf_sound1=0.9, sound2="Music", conf_sound2=0.1,
             sound3=None, conf_sound3=None, transcription="hello there", emotion="hap (0.80)", llm_verdict=None),
        dict(id=2, date=f"{today} 09:30:00", rms=300.0, file_name="b.wav", device_name="living_room",
             spectrogram_image="b.png", sound1="Speech", conf_sound1=0.7, sound2=None, conf_sound2=None,
             sound3=None, conf_sound3=None, transcription="No Speech Detected", emotion=None, llm_verdict=None),
        dict(id=3, date=f"{yesterday} 10:00:00", rms=200.0, file_name="c.wav", device_name="kitchen",
             spectrogram_image="c.png", sound1="Dishes", conf_sound1=0.5, sound2=None, conf_sound2=None,
             sound3=None, conf_sound3=None, transcription=None, emotion=None, llm_verdict=None),
    ]
    return pd.DataFrame(rows)


def _sample_devices_df() -> pd.DataFrame:
    return pd.DataFrame([
        dict(device_name="kitchen", latitude=0.0, longitude=0.0, description="Kitchen box",
             mqtt_broker="audiosense.hmu.gr", mqtt_port=8883, mqtt_client_id="kitchen",
             mqtt_password="super-secret", sample_rate=16000, sample_bits=16, rms_threshold=500,
             record_duration=5, wifi_ssid="home", wifi_password="wifi-secret", updated=1),
        dict(device_name="living_room", latitude=0.0, longitude=0.0, description="Living room box",
             mqtt_broker="audiosense.hmu.gr", mqtt_port=8883, mqtt_client_id="living_room",
             mqtt_password="super-secret2", sample_rate=16000, sample_bits=16, rms_threshold=500,
             record_duration=5, wifi_ssid="home", wifi_password="wifi-secret", updated=1),
    ])


class _FakeSQLToPandas:
    """Stands in for ast_dash.sql_to_pandas.SQL_to_Pandas."""

    _DF = None  # set per-test-session by the fixture below

    def __init__(self, db_file=None):
        self._df = _FakeSQLToPandas._DF.copy()

    def get_all_rec(self):
        return self._df.sort_values("date", ascending=False).reset_index(drop=True)

    def get_date_from_to(self, date_from=None, date_to=None):
        if date_from is None or date_to is None:
            return self.get_all_rec()
        mask = (self._df["date"] >= date_from) & (self._df["date"] <= date_to)
        return self._df[mask].sort_values("date", ascending=False).reset_index(drop=True)

    def get_by_id(self, id):
        row = self._df[self._df["id"] == id]
        if row.empty:
            return None
        return row.iloc[0].to_dict()

    def rename_columns(self, df):
        return df


class _FakeDevicesSQLToPandas:
    _DF = None

    def __init__(self, db_file=None):
        self._df = _FakeDevicesSQLToPandas._DF.copy()

    def get_all_rec(self):
        return self._df.copy()


@pytest.fixture(autouse=True)
def patch_data_sources(monkeypatch, tmp_path):
    """Applied to every test: existing_app_adapter never touches the real
    production DB or the real sentinel_status.json."""
    _FakeSQLToPandas._DF = _sample_events_df()
    _FakeDevicesSQLToPandas._DF = _sample_devices_df()

    monkeypatch.setattr(existing_app_adapter, "SQL_to_Pandas", _FakeSQLToPandas)
    monkeypatch.setattr(existing_app_adapter, "Devices_SQL_to_Pandas", _FakeDevicesSQLToPandas)

    status_path = tmp_path / "sentinel_status.json"
    status_path.write_text(json.dumps({
        "kitchen": {"status": "online", "last_seen": "2026-08-07 08:00:00", "ip_address": "192.168.2.11"},
        "living_room": {"status": "offline", "last_seen": "2026-08-06 20:00:00", "ip_address": None},
    }))
    monkeypatch.setattr(existing_app_adapter, "_SENTINEL_STATUS_PATH", str(status_path))

    from mobile_api.services import mobile_device_service
    monkeypatch.setattr(mobile_device_service, "load_device_capabilities", lambda: {
        "kitchen": {"duplex": True, "video": False},
    })
    yield


@pytest.fixture(autouse=True)
def isolated_credentials_store(monkeypatch, tmp_path):
    """A throwaway credentials file per test, never the real one."""
    from mobile_api.routes import auth as auth_route
    from mobile_api.security import credentials_store as store_module

    store = CredentialsStore(path=str(tmp_path / "credentials.json"))
    store.set_password(TEST_USERNAME, TEST_PASSWORD)
    monkeypatch.setattr(store_module, "credentials_store", store)
    monkeypatch.setattr(auth_route, "credentials_store", store)
    yield store


class FakeStreamingAudioTransport:
    """Stands in for ast_src.audio_transport.streaming_transport.StreamingAudioTransport
    so audio-session tests never open a real MQTT connection."""

    instances: list["FakeStreamingAudioTransport"] = []

    def __init__(self, **kwargs):
        self._connected = threading.Event()
        self._connected.set()
        self._active_session_id = None
        self._device_ids = []
        self._receive_callback = None
        self.sent_frames: list = []
        self.stopped = False
        FakeStreamingAudioTransport.instances.append(self)

    def connect(self):
        self._connected.set()

    def disconnect(self):
        pass

    def start_session(self, config):
        self._active_session_id = "fake-transport-session"
        self._device_ids = list(config.device_ids)
        return self._active_session_id

    def stop_session(self, session_id=None):
        self.stopped = True

    def send_audio_frame(self, frame, session_id=None):
        self.sent_frames.append(frame)
        return True

    def set_receive_callback(self, cb):
        self._receive_callback = cb


@pytest.fixture
def fake_transport(monkeypatch):
    FakeStreamingAudioTransport.instances.clear()
    monkeypatch.setattr(mobile_audio_service, "StreamingAudioTransport", FakeStreamingAudioTransport)
    monkeypatch.setattr(mobile_audio_service, "_default_ptt_broker_kwargs", lambda: {})
    return FakeStreamingAudioTransport


@pytest.fixture
def client(patch_data_sources, isolated_credentials_store):
    # Import app lazily so the autouse fixtures above are active before
    # mobile_api.main.create_app() runs (module-level `app = create_app()`
    # would otherwise build services against unpatched state).
    from mobile_api.main import create_app
    app = create_app()
    return TestClient(app)


@pytest.fixture
def auth_headers(client):
    resp = client.post("/api/v1/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
