# AudioSense Intercom — Waveshare ESP32-P4-WIFI6-Touch-LCD-5

A port of the working ESP32-S3-BOX-3 intercom in the parent directory to the
Waveshare ESP32-P4-WIFI6-Touch-LCD-5 board.

The hardware facts below were taken from the board's own BSP and examples in
[waveshareteam/ESP32-P4-WIFI6-Touch-LCD-5](https://github.com/waveshareteam/ESP32-P4-WIFI6-Touch-LCD-5/tree/main/examples/esp-idf),
not inferred.

## Why this port is mostly mechanical

Waveshare ships a real BSP for this board
(`examples/esp-idf/08_lvgl_demo_v9/components/esp32_p4_wifi6_touch_lcd_5`), and
it exposes the *same* API surface as the `espressif/esp-box-3` BSP the S3 build
uses:

| Call the app already makes | Provided by the Waveshare BSP |
| --- | --- |
| `bsp_i2c_init()` | yes |
| `bsp_audio_codec_speaker_init()` → `esp_codec_dev_handle_t` | yes |
| `bsp_audio_codec_microphone_init()` → `esp_codec_dev_handle_t` | yes |
| `bsp_sdcard_mount()` / `BSP_SD_MOUNT_POINT` | yes |
| `bsp_display_start()`, `bsp_display_lock/unlock()` | yes |
| `bsp_display_backlight_on()` | yes (`bsp/display.h`) |

Playback and capture still go through `esp_codec_dev` exactly as before, so the
audio logic — including the `spk_open()` / `spk_write()` hardening that fixed
the silent-loudspeaker bug — carries over unchanged.

## Setup

```powershell
cd esp32-p4-intercom
.\setup.ps1              # copies app sources from ../main, vendors the BSP
idf.py set-target esp32p4
idf.py build
```

`setup.ps1` exists because the BSP is not published on the ESP component
registry — it only lives inside an example directory in that git repo, so it
has to be vendored into `./components`.

If the application sources are already in `main\` and only the BSP is missing,
vendor it on its own:

```powershell
$ws = "$env:TEMP\ws-p4-bsp"
git clone --depth 1 --filter=blob:none --sparse https://github.com/waveshareteam/ESP32-P4-WIFI6-Touch-LCD-5.git $ws
git -C $ws sparse-checkout set examples/esp-idf/08_lvgl_demo_v9/components
mkdir components -Force
Copy-Item "$ws\examples\esp-idf\08_lvgl_demo_v9\components\esp32_p4_wifi6_touch_lcd_5" components -Recurse -Force
Copy-Item "$ws\examples\esp-idf\08_lvgl_demo_v9\components\esp_lcd_hx8394"            components -Recurse -Force
Remove-Item $ws -Recurse -Force
```

`bsp_extra` is deliberately not vendored: it wraps playback in the
`audio_player` / `file_iterator` abstraction, which this application does not
use because it drives `esp_codec_dev` directly.

The on-board **ESP32-C6 must be flashed with the `esp_hosted` slave firmware**
before WiFi works. The P4 has no radio of its own; `esp_wifi_remote` forwards
the `esp_wifi_*` calls over SDIO to the C6. Waveshare's `04_wifistation`
example is the reference for this, and the repo's `firmware/` directory carries
prebuilt images.

## Verified pin map

From `components/esp32_p4_wifi6_touch_lcd_5/include/bsp/esp32_p4_wifi6_touch_lcd_5.h`:

| Function | GPIO |
| --- | --- |
| I2C SCL / SDA (codec + touch) | 8 / 7 |
| I2S MCLK | 13 |
| I2S BCLK (SCLK) | 12 |
| I2S LRCK (WS) | 10 |
| I2S DOUT (to codec) | 9 |
| I2S DSIN (from codec) | 11 |
| Speaker amplifier enable | 53 |
| LCD backlight | 26 |
| LCD reset | 27 |
| SD CLK / CMD | 43 / 44 |
| SD D0–D3 | 39 / 40 / 41 / 42 |

The BSP owns all of these — the application never touches a pin directly.

## What actually differs from the S3 build

### Audio: same two-codec split as the BOX-3

The BSP header for this board claims a single ES8311 handles both playback and
capture. **That is wrong** — it is stale text copied from the esp-box BSP. The
boot log settles it:

```
I (3841) ES8311: Work in Slave mode
I (3848) ES7210: Work in Slave mode
I (3855) ES7210: Enable ES7210_INPUT_MIC1
```

So the layout matches the BOX-3: an **ES8311** for the speaker and a separate
**ES7210** for the microphone, sharing one I2S port.

The sample-rate reconciliation in `spk_open()` is still needed, because the
single shared I2S port is pinned by the continuously-open microphone.

The good news: the board's own codec example runs at **16 kHz / 16-bit**, which
is exactly what this application uses, so no resampling is involved.

#### The open-once-and-mute lifecycle does not work here

An earlier revision of this port replaced the BOX-3's open/close-per-broadcast
lifecycle with a single lazy open plus `esp_codec_dev_set_out_mute()` between
broadcasts, on the theory that fewer open/close cycles is safer. It silences
the loudspeaker completely. Two mechanisms compound:

* `esp_codec_dev` only reprograms the codec inside `open()`. It is `open()`
  that calls `es8311_set_fs()` and `es8311_enable()`, and `es8311_enable()` is
  what runs `es8311_start()` and powers the amplifier on GPIO 53. Held open
  forever, that sequence runs exactly once — during the boot self-test, before
  the microphone exists — and is never re-applied. `set_out_mute()` toggles one
  DAC register; it cannot re-arm a DAC path that has been reclocked underneath
  it.
* Holding it open changes what happens when `capture_task` opens the
  microphone. `audio_codec_data_i2s.c` reconfigures the peer TX clock alongside
  RX only while the output side is *not* enabled (*"TX is master, set to RX not
  take effect need reconfig TX also"*). With the speaker held open that branch
  is skipped, the microphone takes over the shared clock, and the DAC goes
  quiet while `esp_codec_dev_write()` still returns success at every step.

So the BOX-3 lifecycle is restored: `spk_open()` per broadcast, `spk_close()`
afterwards. The boot self-test must close the output path too, so that the
shared-I2S state when `capture_task` opens the microphone is identical to the
BOX-3's.

### UI updates must not block the audio tasks

Two fixes that only matter at this screen size:

* `bsp_display_lock()` in the vendored BSP returned `esp_lv_adapter_lock()`
  (an `esp_err_t`, `ESP_OK == 0`) directly as a `bool`, so it reported
  **`false` on success**. Fixed in the BSP to compare against `ESP_OK`.
* `display_set_rms()` runs from `capture_task` once per DMA frame (~15 Hz) and
  `display_set_status()` runs from `player_task` between broadcast chunks.
  Taking the LVGL mutex with "block forever" there was fine on the S3's
  320×240 panel; on 720×1280 the LVGL task holds the mutex long enough that
  `capture_task` (prio 18) starves `player_task` (prio 8), which then stops
  draining the play queue and drops the broadcast as "queue full". Every UI
  update reachable from an audio task now goes through `ui_lock()`, which
  gives up after 100 ms, and the RMS readout is throttled to ~4 Hz.

### Configuration: everything comes from `settings.txt`

No deployment detail is compiled in any more. `settings.example.txt` in this
directory is the annotated template — copy it to the SD card root as
`settings.txt`.

```
settings.txt  →  Config module  →  MQTTConfig  →  MQTT subsystem  →  Application
```

| File | Role |
| --- | --- |
| `main/config.c` / `.h` | The only code that reads `settings.txt`. Exposes `config_get_string/int/bool/float()`. Keys are case-insensitive, so SD cards written for older firmware keep working. |
| `main/mqtt_config.c` / `.h` | `mqtt_config_t` plus `mqtt_config_load()`. Documents every MQTT key and its default, validates and clamps values. |
| `main/audiosense_mqtt.c` | Receives an `mqtt_config_t` at `audiosense_mqtt_init()` and keeps a private copy. Contains no broker constant. |
| `main/main.c` | `load_settings()` pulls the non-MQTT firmware settings through the same Config module. |

Replacing the home server, moving the broker, renaming a topic root or turning
TLS off is now an SD-card edit, not a rebuild.

The wire protocol is unchanged. Only the *roots* `mqtt_topic_recordings`,
`mqtt_topic_devices` and `mqtt_topic_commands` are configurable; the suffixes
the firmware appends (`/<id>/stream/info`, `/chunk/<n>`, `/complete`, `/ip`,
`/status`), the JSON payload shapes and the QoS/retain flags are the contract
with the desktop application and stay in `audiosense_mqtt.h`. Every default
equals the value that used to be hardcoded, so a sentinel with **no**
`settings.txt` at all behaves exactly as it did before.

`mqtt_topic_visual` and `mqtt_topic_events` are reserved: parsed, validated and
logged, but no code path subscribes to or publishes on them yet.

Nothing about configuration is fatal. A missing file, a malformed line, an
out-of-range number or an absent key each resolve to a documented default with
a warning; the firmware never refuses to boot over one field.

### WiFi: remote radio

`esp_wifi_remote` + `esp_hosted` replace the built-in radio. The application's
WiFi code should compile unmodified, but the `CONFIG_ESP_WIFI_*` buffer tuning
from the S3 `sdkconfig.defaults` does not exist on this target and was dropped
rather than carried over — those knobs now belong to the C6 firmware.

### Display: 720×1280 MIPI-DSI, LVGL 9

* Panel controller is **HX8394** over MIPI-DSI, driven via `esp_lv_adapter`.
* **LVGL 9.4**, whereas the S3 build used LVGL 8 through `esp_lvgl_port`.
  `bsp_display_start()` now returns `lv_display_t *` rather than `lv_disp_t *`.
* The UI was laid out for the BOX-3's 320×240 screen with hard-coded pixel
  sizes (`lv_obj_set_size(top, 320, 36)`, a 280 px RMS bar, fixed y-offsets).
  It is being reworked to derive its geometry from the active display so it
  fills 720×1280 instead of sitting in a corner.
* **`bsp_display_lock(0)` must become `bsp_display_lock(-1)`.** Under
  `esp_lvgl_port` on the S3, a timeout of `0` meant "wait forever". This board
  locks through `esp_lv_adapter`, and Waveshare's own example passes `-1` for
  the block-forever case. Left at `0`, the lock can fail and the UI would then
  be mutated without holding the LVGL mutex.

`bsp_display_start_with_config()` additionally takes rotation, tear-avoidance
mode and touch mirroring flags — see the `bsp_display_cfg_t` usage in
Waveshare's `08_lvgl_demo_v9/main/main.c`.

### Build configuration

* Target `esp32p4`, QIO flash, PSRAM at 200 MHz with XIP, 256 KB L2 cache with
  128 B lines — all copied from Waveshare's validated defaults.
* `factory` partition grown 3 MiB → 4 MiB: LVGL 9, the MIPI-DSI driver and the
  hosted transport are all new payload, and the build is `-O2` not `-Os`.
* `CONFIG_SPIRAM_MODE_OCT` and the 80 MHz PSRAM speed were S3-only and are gone.

## Boot peripheral self-test

During boot the screen shows a three-line panel below the status text:

```
✓ Speaker  chain ok + beep
✓ Camera   OV5647 ok
✓ SD card  mounted
```

It is tinted by the worst result (green / amber / red) and stays on screen
afterwards. The same line goes to the log as `[selftest] ...`.

**Camera** detection is real. The OV5647's SCCB lines are on the same I2C bus
as the codec and touch controller — Waveshare's video example passes
`bsp_i2c_get_handle()` straight into its camera init — so the check probes
address `0x36` and reads the chip ID from registers `0x300A`/`0x300B`,
expecting `0x5647`. That distinguishes absent, present-but-not-answering, and
wrong-part. No `esp_video` dependency is needed for this.

**Speaker** detection is *not* real, and it is worth being explicit about why:
the loudspeaker is a passive driver on the far side of the amplifier, and
neither the ES8311 nor the P4 senses load current. Nothing in software can
distinguish "speaker attached" from "speaker unplugged". What the check does
verify is the entire chain up to the amplifier terminals — ES8311 answering at
I2C `0x18`, output path opening at the session format, PCM writes accepted —
and then plays a short 880 Hz tone (faded in and out so the amp never sees a
step edge). So `✓ Speaker` means everything up to the speaker terminals works;
if you *hear* the beep, the speaker is good too.

The existing BOX-3 UI is unchanged: RMS bar and readout, WiFi icon, clock,
queue count and all status messages carried over verbatim. Note that the WiFi
icon is a *state* indicator rather than a blinking one — it switches between a
green `LV_SYMBOL_WIFI` when associated and a red `LV_SYMBOL_WARNING` when not.
That is what the BOX-3 build did too; it never blinked.

## Open items — need on-hardware verification

These are settled in code but cannot be confirmed without the board:

1. **Microphone gain.** `MIC_IN_GAIN_DB` is 42.0, the same as the BOX-3, since
   the capture part turned out to be the same ES7210. Trim against the
   on-screen RMS readout if speech does not sit well above the noise floor.
2. **Speaker volume.** `SPK_OUT_VOLUME` (80) was tuned for the BOX-3's
   amplifier; this board has its own amp on GPIO 53.
3. **SD mount point.** `MQTT_PLAY_WAV_PATH` in `audiosense_mqtt.h` hard-codes
   `/sdcard`. The BSP's `BSP_SD_MOUNT_POINT` is `CONFIG_BSP_SD_MOUNT_POINT`,
   which defaults to `/sdcard` — confirm that once the BSP is vendored, since
   the two must agree.
3. **Touch orientation flags** (`swap_xy`, `mirror_x`, `mirror_y`) for the
   chosen rotation.
4. **RMS threshold.** A different microphone and enclosure means
   `DEFAULT_RMS_THRESHOLD` almost certainly needs re-tuning for
   voice-activity detection to trigger sensibly.
5. **Flash size.** Defaults assume 16 MiB. Switch to
   `CONFIG_ESPTOOLPY_FLASHSIZE_32MB` if your board has the 32 MiB part.
6. **Camera probe reliability.** The OV5647 module has its own oscillator, so
   it should answer on SCCB without the CSI stack running. If the self-test
   reports "not detected" on a camera you know is plugged in, the sensor is
   likely held in reset or power-down until `esp_video`'s sensor init releases
   it — in that case the probe has to be replaced by a real
   `esp_video_init()` + `open("/dev/video0")` check, which costs the
   `esp_video` dependency.
