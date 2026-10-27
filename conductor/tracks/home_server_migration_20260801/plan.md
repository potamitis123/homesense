# Implementation Plan: Home Server Migration

## Objective
Execute the "Top-Down" migration strategy to transform the Sentinel application into a Home Server, introducing MQTT abstraction, a configurable Settings UI, a REST API layer, and WebRTC architectural hooks.

## Phase 1: MQTT Abstraction & Communication Manager
- [x] Create `ast_src/communication/manager.py` with the `CommunicationManager` interface (`publish`, `subscribe`, `requestRecording`, `sendCommand`).
- [x] Implement `LocalMQTTAdapter` that wraps the existing `paho-mqtt` logic currently found in `mqtt_client.py`.
- [x] Refactor `ast_src/main.py` and `mqtt_clent/mqtt_client.py` to use `CommunicationManager` instead of direct MQTT client instances.
- [x] Test that live audio chunks are still correctly received and processed via the abstraction.

## Phase 2: Settings Tab & Home Server Configuration
- [x] Update `common_files/parameters.json` schema to support a `communication_mode` ("Home Server" vs "Remote Server") and local API ports.
- [x] Update `misc/load_parameters.py` to handle the new configuration schema gracefully (with fallbacks).
- [x] Redesign `ast_dash/pages/settings.py` to replace old broker hardcoding with the new "Communication Mode" UI.
- [x] Add form fields for Local MQTT Broker Address, API Listening Port, and Local Storage Path when in "Home Server" mode.

## Phase 3: REST API Layer (FastAPI/Flask)
- [x] Integrate a lightweight API framework (e.g., FastAPI or Flask) to run alongside the Dash app and background processes.
- [x] Define standard endpoints: `/api/devices`, `/api/statistics`, `/api/recordings`, `/api/events`, `/api/status`, `/api/device/{id}`.
- [x] Implement the Heatmap API endpoints: `/api/heatmap`, `/api/heatmap?device=Kitchen&from=X&to=Y`.
- [x] Ensure the API binds to `0.0.0.0` and uses the configurable port from Phase 2 to ensure Tailscale/Remote access compatibility.
- [x] (Optional) Begin migrating 1-2 Dash GUI components to fetch data via REST instead of direct `Ast_Sql_lite` calls to prove the architecture.

## Phase 4: Device Capabilities & Visuals Tab
- [x] Update `CommunicationManager` to listen for and parse "capabilities" metadata from connecting Sentinels (e.g., Audio Capture, Display, Camera, WebRTC).
- [x] Create `ast_dash/pages/visuals.py`.
- [x] Build the Visuals UI: List online ESP32-P4 devices (filtered by capabilities), connection status, room selection, and mic/speaker routing dropdowns.
- [x] Add placeholders for future Avatar and Live Camera modes.

## Phase 5: WebRTC Interfaces & Future-Proofing
- [x] Create `ast_src/webrtc/interfaces.py`.
- [x] Define Abstract Base Classes (ABCs): `VideoSource`, `AudioSource`, `MediaSession`, `MediaRouter`, `DeviceConnection`.
- [x] Define `WebRTCManager`, `CameraController`, and `AvatarController` stubs.
- [x] Wire these stubs into the Visuals tab logic so they are ready to be implemented in a future track.

## Phase 6: Final Verification & Testing
- [x] Run full pipeline tests (audio recording -> DB -> Dashboard).
- [x] Verify REST API endpoints via `curl` or Postman.
- [x] Verify Tailscale/Remote access simulation (accessing via network IP instead of localhost).
- [x] Check code style and documentation.