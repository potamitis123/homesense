# Sentinel Home Server Migration Specification

## Objective
Refactor the Sentinel desktop application so that it no longer acts as a client of a remote server. Instead, the laptop itself becomes the primary (and initially the only) server for all ESP32 Sentinel devices connected to the local network. The application becomes the "brain of the house."

## Architectural Goals
1. **Home Server Mode**: The desktop application manages local MQTT, recordings, telemetry, storage, APIs, and AI processing locally.
2. **Settings Tab Redesign**: A new configurable operating mode (Home Server vs. Remote Server).
3. **MQTT Abstraction**: `CommunicationManager` to abstract MQTT details from the core application.
4. **Visuals Tab**: A new Dash tab for managing real-time audiovisual communication with ESP32-P4 devices.
5. **Device Capabilities**: Automatic discovery of device features (Audio, Camera, WebRTC).
6. **REST API**: A decoupled API layer between the GUI and database, serving endpoints for devices, stats, recordings, and heatmaps.
7. **Remote Access / Tailscale Ready**: Everything uses configurable addresses and ports (no hardcoded `localhost`).
8. **WebRTC Preparation**: Define clean interfaces (`MediaRouter`, `MediaSession`, `WebRTCManager`, etc.) without implementing the actual WebRTC protocols yet.

## Deliverables
- Refactored communication architecture (`CommunicationManager`).
- Home Server configuration profile.
- New Visuals tab (`ast_dash/pages/visuals.py`).
- REST API layer (including Heatmap endpoints).
- Device capability detection.
- Future-proof WebRTC interfaces.