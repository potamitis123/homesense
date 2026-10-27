# AudioSense — ESP32-S3-BOX-3 Acoustic Monitor & Intercom

An embedded acoustic monitoring and bidirectional intercom system built on the ESP32-S3-BOX-3. Detects sound events via voice activity detection (VAD), records 20 seconds of audio, encodes to MP3 on-device, and uploads to a remote server via both MQTT (with TLS client certificates) and HTTP. Also accepts remote audio playback commands through a queue-based speaker system that never drops messages.

## Features

- **Voice Activity Detection (VAD)** — continuous RMS monitoring with configurable threshold (default: 600)
- **20-second recording** — 16 kHz, 16-bit mono PCM to SD card
- **On-device MP3 encoding** — Shine fixed-point encoder, 32 kbps, ~80 KB per recording (~2.5s encode time)
- **Dual-protocol upload** — MQTT with TLS client certs + HTTP Dash callback, both sent every time
- **Offline buffering** — MP3 files stored on SD card when WiFi is unavailable, uploaded automatically later
- **Remote speaker playback** — POST audio to `/play`, queued to SD, played when device is idle (never rejects)
- **Health check** — GET `/ping` returns JSON status
- **Live display** — RMS level bar, WiFi/MQTT status, recording progress, pending file count, current time
- **NTP time sync** — timestamped filenames
- **Connect-on-demand MQTT** — connects only during upload, disconnects after, prevents idle timeout crashes

## Hardware

- [ESP32-S3-BOX-3](https://github.com/espressif/esp-box) development kit
- microSD card (FAT32 formatted)

No additional hardware required. The BOX-3 includes dual microphones (ES7210), speaker (ES8311), 2.4" IPS display, and WiFi.

## Project Structure

```
audiosense-box3/
├── README.md
├── CMakeLists.txt                  # Project-level CMake
├── sdkconfig.defaults              # Build defaults (PSRAM, TLS, fonts, LFN)
├── partitions.csv                  # 3MB app partition for 16MB flash
├── .gitignore
├── main/
│   ├── CMakeLists.txt              # Registers sources + embeds TLS certs
│   ├── main.c                      # Application code
│   ├── audiosense_mqtt.h           # MQTT interface
│   ├── audiosense_mqtt.c           # MQTT with TLS client certs
│   ├── idf_component.yml           # ESP-BSP + codec dependencies
│   └── certs/                      # TLS certificates (user must supply)
│       ├── ca.crt                  # CA certificate
│       ├── hlias.crt               # Client certificate
│       └── hlias.key               # Client private key
├── components/
│   └── shine/
│       ├── CMakeLists.txt          # Shine MP3 encoder component
│       └── src/                    # Shine source files (see setup)
└── scripts/
    ├── play_on_box3.py             # Send audio to BOX-3 speaker
    └── send_audiosense2.py         # Upload MP3 from PC (MQTT + HTTP)
```

## Prerequisites

- **ESP-IDF v5.3+** — [installation guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/get-started/)
- **Python 3.8+** — for companion scripts
- **ffmpeg** — required by `play_on_box3.py` for audio conversion

## Setup

### 1. Clone this repository

```bash
git clone <repo-url>
cd audiosense-box3
```

### 2. Download Shine MP3 encoder source

```bash
git clone https://github.com/toots/shine.git /tmp/shine
cp /tmp/shine/src/lib/*.c /tmp/shine/src/lib/*.h components/shine/src/
```

You should have ~17 files in `components/shine/src/` including `layer3.c`, `layer3.h`, `bitstream.c`, `tables.c`, etc.

### 3. Supply TLS certificates

Place your MQTT broker certificates in `main/certs/`:

```
main/certs/
├── ca.crt          # CA certificate
├── hlias.crt       # Client certificate
└── hlias.key       # Client private key
```

All three files must be PEM format (text, starting with `-----BEGIN`).

### 4. Configure WiFi and server credentials

Edit `main/main.c` — update the defines at the top:

```c
#define WIFI_SSID             "your_wifi_ssid"
#define WIFI_PASS             "your_wifi_password"
#define HTTP_SERVER           "http://your-server:8050"
#define HTTP_USER             "your_http_user"
#define HTTP_PASSWORD         "your_http_pass"
```

Edit `main/audiosense_mqtt.h` — update MQTT settings:

```c
#define MQTT_BROKER_HOST      "your-server.example.com"
#define MQTT_BROKER_PORT      8883
#define MQTT_CLIENT_ID        "your-device-id"
#define MQTT_USER             "your_mqtt_user"
#define MQTT_PASS             "your_mqtt_pass"
```

### 5. Build and flash

```bash
idf.py set-target esp32s3
idf.py build
idf.py -p COMx flash monitor
```

Replace `COMx` with your serial port (`/dev/ttyUSB0` on Linux, `COM3` on Windows, etc.).

If using VS Code with the ESP-IDF extension:
- F1 → ESP-IDF: Build your project
- F1 → ESP-IDF: Flash your project
- F1 → ESP-IDF: Monitor your device

## Usage

### Automatic recording and upload

Once booted, the device enters idle monitoring mode. When the microphone RMS level exceeds the threshold:

1. Records 20 seconds of PCM audio to SD card (WAV)
2. Encodes the WAV to MP3 using Shine (32 kbps, ~2.5 seconds)
3. Deletes the temporary WAV
4. Uploads the MP3 via **both** MQTT and HTTP simultaneously
5. Enters cooldown to drain the I2S buffer
6. Returns to monitoring

If WiFi is unavailable, MP3 files are stored on the SD card and uploaded when connectivity is restored.

### Remote playback

Send audio to the BOX-3 speaker from any machine on the same network:

```bash
pip install requests pydub
python scripts/play_on_box3.py --file message.mp3 --ip <box3-ip>
```

The script converts any audio format (MP3, WAV, OGG, FLAC) to 16 kHz mono WAV and POSTs it to the device's `/play` endpoint. Audio is queued to SD and played when the device is idle — it **never rejects** due to being busy. The IP address is shown on the BOX-3 display during boot.

### Health check

```bash
curl http://<box3-ip>/ping
# Returns: {"status":"ok","device":"esp32-s3-box3"}
```

### Upload from PC

To upload an MP3 file to the server using the same protocol as the device:

```bash
pip install paho-mqtt mutagen requests
python scripts/send_audiosense2.py --file recording.mp3 --user esp32s3-2
```

## Configuration

Key parameters in `main/main.c`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `RMS_THRESHOLD` | 600 | VAD trigger level |
| `RECORD_DURATION_MS` | 20000 | Recording length (ms) |
| `SAMPLE_RATE` | 16000 | Audio sample rate (Hz) |
| `MP3_BITRATE` | 32 | MP3 encoding bitrate (kbps) |
| `DISCARD_SAMPLES` | 3000 | Initial samples discarded (codec artifact) |
| `COOLDOWN_DRAIN_MS` | 3000 | Post-record buffer drain time (ms) |
| `COOLDOWN_RMS_CHECKS` | 10 | Consecutive quiet reads before resuming |

Key parameters in `main/audiosense_mqtt.h`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MQTT_BROKER_HOST` | your-broker.example.com | MQTT broker hostname |
| `MQTT_BROKER_PORT` | 8883 | MQTT broker TLS port |
| `MQTT_CHUNK_SIZE` | 4096 | MQTT publish chunk size (bytes) |

## Architecture

### Data Flow

```
                    ┌─────────────────────────────────────┐
                    │          ESP32-S3-BOX-3              │
                    │                                     │
  Sound ──► Mic ──► │ VAD ──► Record ──► MP3 Encode ──►  │
                    │         (WAV/SD)    (Shine)         │
                    │                        │            │
                    │              ┌─────────┴──────┐     │
                    │              ▼                ▼     │
                    │           MQTT            HTTP      │──► Server
                    │      (TLS+certs)      (Dash JSON)   │
                    │                                     │
  Network ──► HTTP ─┤──► /play ──► SD queue ──► Speaker  │
              POST  │                                     │
                    └─────────────────────────────────────┘
```

### State Machine

```
MONITORING ──(RMS > threshold)──► RECORDING ──► COOLDOWN ──► MONITORING
     │                                              ▲
     └──(play queue found)──► PLAYING ──────────────┘
```

### MQTT Connection Pattern

Unlike a persistent connection, this device uses **connect-on-demand** (same as the Python scripts):
1. Connect to broker with TLS client certificates
2. Publish info → chunks → complete
3. Disconnect immediately

This avoids idle-timeout disconnections from the broker that cause crash loops.

### Key Design Decisions

- **Mic stays open** — the codec device is opened once at boot and stays open. Avoids stale I2S FIFO data from open/close cycles.
- **Cooldown phase** — after recording, reads and discards mic data until silence is confirmed. Prevents immediate re-triggering.
- **Play queue on SD** — incoming audio is always saved to SD first, played when idle. HTTP handler never blocks or rejects.
- **mbedTLS on PSRAM** — the BOX-3's display (LVGL) consumes most internal RAM. TLS allocations are routed to the 16MB PSRAM via `CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC=y`.
- **Both MQTT and HTTP** — every recording is sent via both protocols simultaneously for redundancy.

## Performance

| Parameter | Value |
|---|---|
| Sample rate | 16,000 Hz |
| Resolution | 16-bit mono |
| Recording duration | 20 seconds |
| WAV file size | 640 KB |
| MP3 file size | ~80 KB (32 kbps) |
| Compression ratio | 8:1 |
| MP3 encode time | ~2.5 seconds |
| MQTT chunks per upload | ~20 |
| Total boot time | ~10 seconds |
| BOM cost | <$50 USD |

## Troubleshooting

**"SD FAILED!" at boot** — Check the microSD card is FAT32 formatted and inserted properly. Erase 0 bytes files if there

**"Cannot create file"** — Long filenames are disabled. Ensure `CONFIG_FATFS_LFN_HEAP=y` is in `sdkconfig.defaults`, delete `sdkconfig`, and do a clean build.

**MQTT "not authorized"** — Check username/password in `audiosense_mqtt.h` match your broker config. The username must match the certificate identity.

**MQTT "Failed to allocate memory"** — Ensure `CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC=y` is in `sdkconfig.defaults`. The BOX-3's display uses most internal RAM; TLS needs PSRAM.

**MQTT connects then disconnects** — The connect-on-demand pattern handles this. If it persists, check broker logs for connection limits.

**Device re-triggers after recording** — Increase `COOLDOWN_DRAIN_MS` or `COOLDOWN_RMS_CHECKS` in `main.c`.

**"Device busy" on /play** — This should not happen with the queue system. If it does, check that the SD card has free space.

**VAD task FAILED** — Internal RAM exhausted. This should not occur with the current configuration. If it does, reduce other task stack sizes or enable `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY=y`.

**Build error: partition too small** — Ensure `partitions.csv` is in the project root and `sdkconfig.defaults` contains `CONFIG_PARTITION_TABLE_CUSTOM=y`.

**Build error: font not found** — Ensure `CONFIG_LV_FONT_MONTSERRAT_16=y` and `CONFIG_LV_FONT_MONTSERRAT_24=y` are in `sdkconfig.defaults`. Delete `sdkconfig` and clean build.

## sdkconfig.defaults Reference

The following settings are critical and must all be present:

```
CONFIG_IDF_TARGET="esp32s3"
CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_OCT=y
CONFIG_SPIRAM_SPEED_80M=y
CONFIG_ESP_DEFAULT_CPU_FREQ_240=y
CONFIG_FREERTOS_HZ=1000
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
CONFIG_LV_FONT_MONTSERRAT_16=y
CONFIG_LV_FONT_MONTSERRAT_24=y
CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC=y
CONFIG_FATFS_LFN_HEAP=y
```

If any of these are missing, delete `sdkconfig`, delete `build/`, and rebuild.

## License

Shine MP3 encoder is licensed under GPL. See [shine/COPYING](https://github.com/toots/shine/blob/main/COPYING).

ESP-IDF and ESP-BSP components are licensed under Apache 2.0.

## References

- [ESP32-S3-BOX-3 BSP](https://github.com/espressif/esp-bsp/tree/master/bsp/esp-box-3)
- [esp_codec_dev API](https://components.espressif.com/components/espressif/esp_codec_dev)
- [Shine MP3 Encoder](https://github.com/toots/shine)
- [ESP-IDF Programming Guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/)
