# Mobile API

A thin, isolated FastAPI gateway that lets an Android app remotely read from and interact with the existing **Sentinel Home Server** (`ast_src` + `ast_dash`) — without talking to MQTT, the ESP32 Sentinels, or the Dash dashboard directly.

This module does not duplicate the existing application. It only reads from and calls into what already exists: the SQLite event database, the recording files on disk, `sentinel_status.json`, and the existing duplex audio streaming implementation (`ast_src/audio_transport`). See "How this connects to the existing app" below for exactly what's reused vs. newly written.

## 1. Architecture

```
Android App
    |  HTTPS (REST)  +  WebSocket (duplex audio)
    v
mobile_api/            <- this module, its own process, own port
    |
    +-- routes/         FastAPI routers (thin: auth + validation only)
    +-- services/        one function/class per mobile capability
    +-- adapters/         the ONLY place that touches existing-app data
    +-- security/          password hashing, bearer tokens, rate limiting
    |
    v
Existing Sentinel Home Server (ast_src / ast_dash) -- unmodified except
one small addition (SQL_to_Pandas.get_by_id) -- see below.
    |
    +-- MQTT --> ESP32 Sentinels
    +-- SQLite DB (ast_audio_db/ast_audio.db)
    +-- Recording files (assets/audio_mp3, assets/audios, assets/audiosuser)
    +-- sentinel_status.json (device presence)
```

**Why a separate process on its own port**, rather than mounted onto the existing Dash app: `ast_dash/ast_app.py` runs on Flask (WSGI). FastAPI is ASGI. The two cannot share one process the way `ast_src/webrtc/routes.py` shares Flask blueprints with Dash. `mobile_api` runs as a third supervised process alongside "AI Pipeline" and "Dashboard" in `boot_sentinel.bat`.

**Why duplex audio gets its own transport instance**: the desktop PTT session (`AudioSessionManager`/`PushToTalkController`) is a singleton living inside the Dash process. A separate process can't call into that live object. `mobile_api` instead imports and instantiates the *same class* — `StreamingAudioTransport` from `ast_src/audio_transport/streaming_transport.py` — directly. This reuses the real protocol implementation; it does not reimplement anything. **Known limitation**: a simultaneous desktop PTT press and a mobile session on the same device are not arbitrated against each other in v1.

## 2. Installation

Runs in the same `dash_env` conda environment as the rest of the app. Additional dependencies beyond what's already installed: `fastapi`, `uvicorn`, `pydantic` (already present). No new database, no new audio library, no JWT/bcrypt package.

## 3. Configuration

Everything is read from `common_files/parameters.json`'s `"mobile_api"` section, following the existing app's config convention (`misc.load_parameters.parameters`):

```json
"mobile_api": {
    "host": "0.0.0.0",
    "port": 8001,
    "https": true,
    "device_capabilities": {
        "box3-2": {"duplex": true, "video": false},
        "p4-sentinel-1": {"duplex": true, "video": true}
    },
    "access_token_minutes": 30,
    "refresh_token_days": 30,
    "audio_session_timeout_minutes": 5,
    "login_rate_limit_per_minute": 5
}
```

TLS reuses the existing cert files already used by `ast_app.py` and Mosquitto (`https.ca_certfile` / `https.ca_keyfile` in `parameters.json`) — no new certificates.

**`device_capabilities`** — which Sentinels support Push-to-Talk (`duplex`) and/or video call (`video`). No existing data source encodes this (no hardware-type column in `remote_devices`, and device names are moving towards room names like `"kitchen"`, which rules out inferring type from the name) — maintain this map yourself as devices are added or renamed. Devices absent from it default to `{"duplex": false, "video": false}`.

**Setting the first login password** (the credential store starts empty):

```bash
python -m mobile_api.security.credentials_store set <username>
```

This prompts for a password (not visible on screen, not passed as an argv value) and writes a PBKDF2 hash to `mobile_api/security/credentials.json` (gitignored — add it to your `.gitignore` if it isn't already).

## 4. Starting the API

Standalone:

```bash
python -u mobile_api/main.py
```

Or via `boot_sentinel.bat`, which now starts it as a third window ("Sentinel - Mobile API") alongside AI Pipeline and Dashboard.

Programmatic single integration point:

```python
from mobile_api import start_mobile_api
start_mobile_api()
```

## 5. Authentication

Deliberately simple, per this being a personal/research project rather than something requiring an external security stack:

- **Password hashing**: PBKDF2-HMAC-SHA256, 100k iterations, random per-user salt. Standard library only (`hashlib`, `secrets`) — not the existing app's `ast_sql/users.py` table, which hashes with unsalted SHA-256 and looks purpose-built for a different feature (device/location registration), not login.
- **Tokens**: opaque random bearer tokens (`secrets.token_urlsafe`), not JWT. Held in memory only — **restarting the mobile API process logs everyone out**. Acceptable tradeoff for a home server; if that changes, swap `TokenStore`'s in-memory dict for a small persisted store without touching the route layer.
- **Access token**: short-lived (default 30 min). **Refresh token**: longer-lived (default 30 days), only usable against `/api/v1/auth/refresh`.
- **Rate limiting**: sliding-window, per-IP, applied only to `/api/v1/auth/login`.
- Never logged: passwords, tokens, MQTT credentials, raw audio bytes.

Flow:
```
POST /api/v1/auth/login    {username, password} -> {access_token, refresh_token}
POST /api/v1/auth/refresh  {refresh_token}       -> {access_token}
```
All other `/api/v1/*` routes require `Authorization: Bearer <access_token>`.

## 6. API endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/auth/login` | — | Exchange username/password for token pair |
| POST | `/api/v1/auth/refresh` | — | Exchange refresh token for a new access token |
| GET | `/api/v1/dashboard/today` | ✓ | Compact daily overview |
| GET | `/api/v1/events?date=&limit=&offset=&label=` | ✓ | Paginated, newest-first event list; `label` filters to a detected class (e.g. `Speech`) |
| GET | `/api/v1/events/{id}` | ✓ | Single event detail |
| GET | `/api/v1/recordings/{id}` | ✓ | Stream a recording's audio file (HTTP range supported) |
| GET | `/api/v1/statistics/today?device=` | ✓ | Today's activity-by-class counts + `available_classes`, for the circular chart. `device` is a device_id or `all` (default) |
| GET | `/api/v1/heatmap/today?device=` | ✓ | Today's class×hour activity for one device (a room) or `all` (default) — mirrors the "Daily Events" Dash page |
| GET | `/api/v1/devices` | ✓ | Registered Sentinels + online/offline/last_seen + `capabilities.{duplex,video}` |
| POST | `/api/v1/audio/session` | ✓ | Open a duplex audio session against one Sentinel |
| WS | `/api/v1/audio/ws/{session_id}` | ✓ (token query param or header) | Realtime duplex audio |
| GET | `/api/v1/health` | — | Liveness check |

## 7. WebSocket / audio session protocol

```
1. POST /api/v1/audio/session  {"device_id": "kitchen"}
   -> {"session_id": "...", "device_id": "kitchen", "status": "starting"}

2. Open WS /api/v1/audio/ws/{session_id}?token=<access_token>
   (native WebSocket clients that can't set an Authorization header can
   pass the access token as a query parameter instead)

3. Android -> server: raw PCM16 mono 16kHz binary frames (any chunk size;
   the server re-chunks to the 960-byte/30ms frames the Sentinel firmware
   expects, mirroring PushToTalkController._on_mic_frame)

4. Server -> Android: PCM16 mono 16kHz bytes, decoded server-side from the
   Sentinel's Opus stream (reusing the same decoder speaker_sink.py
   already uses for desktop PTT playback) so the Android client needs no
   native/JNI Opus dependency -- just play it directly through
   `AudioTrack`. Costs ~10x more WebSocket bandwidth than raw Opus during
   a call; a reasonable tradeoff for short PTT conversations in exchange
   for a much simpler client.

5. Stop by closing the WebSocket, or sending the text message "stop".
   A session with no activity for audio_session_timeout_minutes
   (default 5) is closed automatically, so a dropped connection can't
   leave a Sentinel stuck in Live Audio Mode.
```

## 8. How this connects to the existing app

| Mobile need | Reused from | Notes |
|---|---|---|
| Events / dashboard / statistics source | `ast_dash/sql_to_pandas.py: SQL_to_Pandas` | `get_date_from_to`, `get_all_rec`; `get_by_id` added (only existing-file change) |
| Device list | `ast_dash/devices_sql_to_pandas.py: Devices_SQL_to_Pandas` | Secret columns (`mqtt_password`, `wifi_password`, etc.) stripped before returning to a client |
| Device online/offline | `sentinel_status.json` | Read, never written, by mobile_api |
| Duplex audio | `ast_src/audio_transport/streaming_transport.py: StreamingAudioTransport` | Same class, fresh instance per mobile session (see Architecture) |
| Recording files | `assets/audio_mp3`, `assets/audios`, `assets/audiosuser` | Read directly; no copying, no second storage location |

A couple of small presentational aggregations (which class×hour, hourly counts, emotion-string parsing) live inside `ast_dash/pages/*.py` modules that call `dash.register_page(...)` at import time — importing those from a separate process would trigger unwanted Dash-registry side effects (one of them, `audio_details.py`, even opens an MQTT connection and starts mic capture at import time). Those specific one-liners are re-expressed locally in `mobile_api/adapters/existing_app_adapter.py`, each commented with the exact original location it mirrors. The actual query/aggregation engine (SQL + pandas) is always reused, never reimplemented.

`ast_src/api_server.py`, an earlier, unfinished, never-wired-in FastAPI skeleton with overlapping endpoints, was removed in favor of this module.

## 9. Testing locally

```bash
pytest mobile_api/tests
```

Everything is mocked (`mobile_api/tests/conftest.py`): a fake `SQL_to_Pandas`/`Devices_SQL_to_Pandas` backed by an in-memory DataFrame, a fake `sentinel_status.json`, a fake `StreamingAudioTransport`, and an isolated credentials file. No ESP32/MQTT hardware and no production database are touched.

## 10. How Android should consume it

1. `POST /auth/login` once, store `access_token` + `refresh_token` securely (Android Keystore-backed storage, not plain SharedPreferences).
2. Attach `Authorization: Bearer <access_token>` to every REST call.
3. On a 401, try `/auth/refresh`; if that also fails, send the user back to login.
4. For recordings, use a media player that supports HTTP range requests and can set a custom header (e.g. ExoPlayer with a custom `DataSource.Factory` that adds the Authorization header) — the endpoint supports `Range` for scrubbing.
5. For PTT: `POST /audio/session`, open the WS with `?token=`, stream mic PCM in, play received Opus bytes out (decode locally), close on release.

## 11. Recommended secure remote-access setup

Do **not** port-forward `mobile_api`'s port directly to the internet. Recommended, in order of preference:

1. **Tailscale** (or another WireGuard-based mesh VPN) between the phone and the laptop — the phone reaches the laptop's Tailscale IP as if on the same LAN, `mobile_api` never needs to be internet-facing at all. This is the same trust model the rest of this codebase already assumes (see the CORS comment in the now-removed `api_server.py`, and `boot_sentinel.bat`'s use of a DNS-redirected hostname for the LAN).
2. A reverse proxy (e.g. Caddy/nginx) on a machine you control, terminating TLS and forwarding only `mobile_api`'s port, if you specifically need access without installing a VPN app.
3. Plain router port-forwarding is explicitly **not** recommended — it exposes the API surface directly to internet scanners with no network-level protection beyond this module's own (deliberately lightweight) auth.

## 12. Adding future functionality without touching the existing app

- New read-only capability: add an adapter function in `mobile_api/adapters/existing_app_adapter.py` that calls the real existing function, a service function that shapes it for mobile, a route, and a Pydantic model. Nothing outside `mobile_api/` changes.
- New existing-app capability mobile needs to reuse: if it's a plain function (like `SQL_to_Pandas`), import it directly. If it's trapped inside a `dash.register_page` module, add one small, focused method to the underlying class it already delegates to (as done for `get_by_id`) rather than importing the Dash page — keep that addition to the smallest possible diff, matching the one already made.
- New auth needs (multi-user roles, OAuth, etc.): `mobile_api/security/authentication.py` is the only place that would need to change; `require_auth` is the single dependency every route already goes through.
