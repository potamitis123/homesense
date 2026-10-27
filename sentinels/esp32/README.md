# Audio Sentinel — ESP32-S3 Firmware

Firmware for a fleet of ESP32-S3 "audio sentinel" boxes that continuously listen through a
microphone, detect sound above an RMS threshold, and stream the captured audio up to an MQTT
broker (TLS, mutual auth) for a backend server to consume. Device configuration (WiFi, MQTT,
sample rate, RMS threshold, etc.) is persisted in NVS and can be updated remotely over MQTT.

## Contents

- [Hardware](#hardware)
- [How it works](#how-it-works)
- [Repository layout](#repository-layout)
- [Configuration](#configuration)
- [Per-device defaults](#per-device-defaults)
- [MQTT protocol](#mqtt-protocol)
- [TLS certificates](#tls-certificates)
- [Building and flashing](#building-and-flashing)
- [Troubleshooting](#troubleshooting)

## Hardware

- **MCU:** ESP32-S3-WROOM-1 (N16R8 variant — 16MB Quad flash + 8MB **Octal** SPI PSRAM).
- **Microphone (default, `MIC_TYPE == I2S`):** I2S digital MIC (e.g. INMP441 / SPH0645), wired as:

  | Signal | GPIO |
  |---|---|
  | BCLK (bit clock) | 12 |
  | WS (word select) | 11 |
  | DIN (data in) | 10 |

- **Microphone (alternate, `MIC_TYPE == USB`):** USB UAC microphone via the
  `espressif/usb_stream` managed component, driven through the native USB-OTG PHY.

Only one mic backend is compiled at a time, selected in `main/global_heads.h`.

## How it works

```
   mic (I2S or USB)
        │  raw PCM
        ▼
task_record_and_encode_audio(_usb)   [core 0]
  - reads a fixed window of audio
  - computes RMS, applies a high-pass filter
  - if RMS >= threshold: pushes PCM into a PSRAM-backed ring buffer
        │
        ▼
  RingbufHandle_t (static, allocated in PSRAM)
        │
        ▼
task_convert_and_send_audio          [core 1]
  - pulls PCM out of the ring buffer
  - encodes it (WAV header + PCM, or MP3/raw depending on ENCODER_TYPE)
  - hands it to publish_audio_chunks()
        │
        ▼
my_mqtt_client.c
  - publishes an "info" JSON message, then the payload split into CHUNK_SIZE
    chunks, then a "complete" summary — all under audio/recordings/<client_id>/...
```

In parallel:
- `wifi_manager.c` brings up WiFi (station mode, retries with backoff).
- `my_mqtt_client.c` connects to the broker (TLS + client cert, or plain TCP), announces
  presence, and subscribes to a per-device config topic so the server can push new settings.
- `task_reset_every_day` restarts the device every 24 hours as a watchdog/hygiene measure.
- Ring buffer and most large/task buffers are allocated from **PSRAM** (`MALLOC_CAP_SPIRAM`) to
  leave internal SRAM free for the WiFi/TLS stack.

## Repository layout

| File | Purpose |
|---|---|
| `main/main.c` | `app_main`, NVS/config bootstrap, ring buffer + task setup, recording→encode→publish pipeline, daily restart watchdog. |
| `main/global_heads.h` | Central compile-time configuration: device selection, mic backend, encoder, MQTT/TLS toggles, WiFi credentials, WAV header struct. |
| `main/wifi_manager.c/.h` | WiFi station init/connect with retry logic. |
| `main/my_mqtt_client.c/.h` | MQTT client setup (TLS certs, LWT/presence), chunked audio publishing, remote-config subscription handler. |
| `main/i2s_audio.c/.h` | I2S mic driver, PCM capture, RMS/high-pass processing (`MIC_TYPE == I2S`). |
| `main/usb_audio.c/.h` | USB UAC mic driver via `usb_stream`, downsampling 48k→16k (`MIC_TYPE == USB`). |
| `main/confing_file.c/.h` | `Rec_config_file_t` device config: defaults per device, JSON (cJSON) ↔ struct (de)serialization, NVS persistence. |
| `main/certs/` | `ca.crt` (broker CA), `hlias.crt` / `hlias.key` (device client cert + key) — embedded into the firmware image. |
| `main/CMakeLists.txt` | Component sources + `EMBED_TXTFILES` for the certs. |
| `sdkconfig.defaults` | Persisted PSRAM (Octal, 80MHz) + 16MB flash settings for the WROOM-1 N16R8 module. |
| `CMakeLists.txt` | Top-level ESP-IDF project file (`my_esp32_project`). |
| `main/idf_component.yml` | Component manager manifest (depends on `espressif/usb_stream`). |

## Configuration

All compile-time switches live in `main/global_heads.h`:

| Macro | Values | Meaning |
|---|---|---|
| `DEVICE_ID` | `DEVICE_ESP32S3_1..4` | Selects which credential/ID block (below) and MQTT client ID this build uses. |
| `MIC_TYPE` | `I2S` \| `USB` | Which microphone driver to compile in. |
| `ENCODER_TYPE` | `WAV` (default) \| `MP3` \| `RAW` \| `OPUS`* | Audio payload format sent over MQTT. `WAV` wraps raw PCM in a 44-byte RIFF header; `MP3` uses the LAME encoder (`esp_lame`); `RAW` sends PCM as-is. *`OPUS` is only a reserved constant — no encoder path is implemented for it. |
| `MQTT_SEND` | `0`/`1` | Enable/disable the whole MQTT pipeline. |
| `MQTT_TLS` | `0`/`1` | `1` = TLS with mutual auth (CA + client cert/key from `main/certs/`); `0` = plain TCP. |
| `MEM_STATISTICS` | `0`/`1` | Verbose heap logging (`print_free_heap`) at each pipeline stage. |
| `FIRSTBOOT` | `0`/`1` | `0` = read config from NVS on boot (normal); non-zero skips the NVS read (fresh/first flash). |
| `WIFI_SSID` / `WIFI_PASS` | string | WiFi credentials. On every boot, if NVS has different WiFi creds than these, NVS is overwritten to match — so this header is the source of truth for WiFi. |

Everything else (MQTT broker/port/client id/password, RMS threshold, sample rate/bits, record
window) lives in the runtime `Rec_config_file_t` struct (`main/confing_file.h`), seeded from
`write_default_values_to_rec_config_file()` on first boot and then persisted in NVS. It can be
replaced remotely over MQTT (see below).

## Per-device defaults

`main/confing_file.c` picks a default block based on `DEVICE_ID`:

| `DEVICE_ID` | MQTT broker | Client ID | RMS threshold | Sample rate | Record window |
|---|---|---|---|---|---|
| `DEVICE_ESP32S3_1` | `your-broker.example.com` | `esp32s3-1` | 25.0 | 16 kHz | 6 s |
| `DEVICE_ESP32S3_2` | *(placeholder — fill in)* | `esp32s3-2-name` | 25.0 | 16 kHz | 5 s |
| `DEVICE_ESP32S3_3` | `your-broker.example.com` | `esp32s3-3` | 60.0 | 48 kHz | 5 s |
| `DEVICE_ESP32S3_4` | `your-broker.example.com` | `esp32s3-4` | 60.0 | 48 kHz | 5 s |

These are only used the very first time a device boots with empty NVS; after that, whatever is
in NVS (or pushed via the config topic) wins.

## MQTT protocol

Topic prefix `MQTT_TOPIC = "audio/recordings"`, per-device client id from `Rec_config_file_t.mqtt_client_id`.

| Direction | Topic | Payload | Notes |
|---|---|---|---|
| Publish | `audio/recordings/<client_id>/info` | JSON: `total_size`, `duration`, `format`, `chunks`, `samplerate`, `bits`, `username`, `rms_threshold`, `esp_audio_duration` | Sent once before the chunks, QoS 1. |
| Publish | `audio/recordings/<client_id>/chunk/<n>` | Raw bytes (`CHUNK_SIZE` = 6144 B each) | QoS 0, not retained. |
| Publish | `audio/recordings/<client_id>/complete` | JSON: `successful_chunks`, `total_chunks` | Sent after the last chunk, QoS 1. |
| Publish (retained) | `audio/status/<client_id>` | `"online"` | Sent right after MQTT connects. |
| **LWT** (retained) | `audio/status/<client_id>` | `"offline"` | Registered as the broker's Last Will — fires automatically if the device disconnects ungracefully (power loss, WiFi drop, crash). Also published proactively before the one deliberate restart triggered by a config update. |
| Subscribe | `/audio/config/<client_id>` | JSON matching `Rec_config_file_t` | Server pushes a full new config; firmware acks with an empty retained-clear publish on the same topic, saves the new config to NVS, then reboots. |

## TLS certificates

When `MQTT_TLS == 1`, three PEM files in `main/certs/` are embedded directly into the firmware
image via `EMBED_TXTFILES` in `main/CMakeLists.txt`:

- `ca.crt` — broker's CA certificate (server verification).
- `hlias.crt` / `hlias.key` — this device's client certificate + private key (mutual auth).

ESP-IDF's build system turns each embedded file into linker symbols named after the file
(dots become underscores), which `main/my_mqtt_client.c` references directly:

```c
extern const uint8_t client_cert_pem_start[] asm("_binary_hlias_crt_start");
extern const uint8_t client_key_pem_start[] asm("_binary_hlias_key_start");
extern const uint8_t server_cert_pem_start[] asm("_binary_ca_crt_start");
```

To rotate certs: replace the files in `main/certs/` (keep the same filenames, or update both
`CMakeLists.txt`'s `EMBED_TXTFILES` list and the `asm(...)` symbol names to match), then
`idf.py fullclean && idf.py build`.

## Building and flashing

**Prerequisites:** ESP-IDF v5.5.x with the `esp32s3` target installed, and its Python virtual
environment set up (`idf_tools.py install-python-env` if you've never built with this IDF copy
before).

```powershell
# from an ESP-IDF terminal (after sourcing export.ps1 / export.sh)
idf.py set-target esp32s3   # only needed once
idf.py build
idf.py -p COMx flash monitor
```

In VS Code with the ESP-IDF extension, use the **Build** and **Flash** icons in the status bar
(or `ESP-IDF: Build your project` / `ESP-IDF: Flash your project` from the command palette),
then **Monitor** to see logs.

Notes:
- `sdkconfig.defaults` pins PSRAM to **Octal** mode at 80MHz and flash size to **16MB** — this
  matches the N16R8 WROOM-1 module. Don't switch PSRAM back to Quad mode; the N16R8 only has
  Octal PSRAM wired up, and Quad mode will fail PSRAM init at boot.
- Most large buffers (ring buffer, encoder buffers, task stacks) are allocated from PSRAM via
  `heap_caps_malloc(..., MALLOC_CAP_SPIRAM)`. If PSRAM is ever disabled these allocations return
  `NULL` and the firmware will crash/assert shortly after boot.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `assert failed: xRingbufferCreateStatic` shortly after boot | PSRAM disabled or misconfigured, so `heap_caps_malloc(..., MALLOC_CAP_SPIRAM)` returned `NULL`. | Check `CONFIG_SPIRAM=y` in `sdkconfig`/`sdkconfig.defaults`. |
| `PSRAM chip is not connected, or wrong PSRAM line mode` | PSRAM mode set to Quad on an N16R8 module (which is Octal-only). | Ensure `CONFIG_SPIRAM_MODE_OCT=y` (not `_QUAD`). |
| Linker errors for `_binary_..._start` symbols | Cert filename in `main/certs/` doesn't match the `EMBED_TXTFILES` entry or the `asm("_binary_..._start")` name in `my_mqtt_client.c` (dots → underscores). | Keep the three names in sync across `CMakeLists.txt` and `my_mqtt_client.c`. |
| Device never shows "offline" on the server after a power loss | Broker not configured to honor the client's LWT, or subscriber not listening on `audio/status/<client_id>`. | Confirm the server subscribes to `audio/status/#` (retained) and that the broker keep-alive isn't set unreasonably high. |
