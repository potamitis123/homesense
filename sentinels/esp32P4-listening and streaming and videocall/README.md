# ESP32-P4 Sentinel + Intercom

One image, two applications, never both at once:

| | **SENTINEL** (default) | **CALL** |
|---|---|---|
| Screen | LVGL audio-bar UI | caller's video, decoded into the framebuffer |
| Microphone | ES7210 open at 16 kHz, RMS-triggered utterances → MQTT | `esp_capture` sink 0, G.711A at 8 kHz |
| Loudspeaker | broadcasts from `commands/<id>/stream/#` | `av_render` call audio |
| Camera | OV5647 → JPEG uplink to the Visuals tab | idle |
| PTT | live-audio mode over MQTT (Opus on `live/opus`) | n/a |

The device listens. When a video call arrives it stops listening, takes the
call, and when the call ends it goes back to listening.

Sources: [`../P4-audio-sentinel`](../P4-audio-sentinel) and
[`../p4_webrtc_firmware`](../p4_webrtc_firmware). Neither is modified by this
project — files are copied in and adapted here.

## Why exclusivity is a hardware fact, not a simplification

* **One I2S port, two codecs.** The ES8311 (speaker) and ES7210 (microphone)
  share it, and whichever direction opens first pins the clock for the other.
  The sentinel runs it at 16 kHz, a call at 8 kHz. Switching means fully
  closing and reopening both codecs — not muting, not reconfiguring.
  `P4-audio-sentinel/README.md` documents what happens if you try to be
  clever here: the loudspeaker goes silent while every write still returns
  success.
* **One microphone, two owners.** The sentinel's capture task holds
  `esp_codec_dev` open on the ES7210; `esp_capture`'s audio source opens the
  same device when a call starts.
* **One framebuffer, two painters.** LVGL and `av_render` both draw into the
  DPI framebuffer.

## The handover has to happen before the offer is parsed

`esp_webrtc` creates its capture sink inside `pc_start()` — which runs when
the **SDP offer arrives**, not at `esp_webrtc_start()`. Two consequences
shape this design, both verified against the component sources:

1. `esp_capture_sink_setup()` refuses on a started graph
   (`esp_capture.c`: *"Not support setup sink after started"*). Since the
   camera uplink starts the graph at boot, sink 0 would never be created and
   the call would come up with **no microphone** — silently, because inbound
   audio and video never touch `esp_capture` and keep working. `media_sys.c`
   therefore reserves sink 0 at boot, with exactly the configuration
   `esp_webrtc` will later ask for, so its setup matches an existing sink and
   succeeds whatever the graph is doing.
2. `pc_start()` then *enables* that sink, which opens the ES7210. So the
   sentinel must have let go **before** the offer reaches `esp_webrtc`. The
   switch is triggered from the signaling layer the instant an offer is seen,
   and blocks until the codecs are actually free.

Blocking there delays the answer by up to `MODE_SWITCH_TIMEOUT_MS`. That is
deliberate: an answer sent while the sentinel still owns the codecs produces a
call with no audio, which is worse than one that connects a little later.

## State machine

```
        boot
          │
          ▼
    ┌───────────┐   offer seen in signaling    ┌───────────┐
    │ SENTINEL  │ ──────────────────────────▶  │   CALL    │
    │ listening │   1. release the camera      │ intercom  │
    │           │   2. finish any utterance    │           │
    │           │   3. close mic + speaker     │           │
    │           │   4. freeze the LVGL UI      │           │
    └───────────┘ ◀──────────────────────────  └───────────┘
                    call ends (DISCONNECTED /
                    CONNECT_FAILED / DENY_CALL /
                    hangup): reopen mic at 16 kHz,
                    repaint the UI, re-arm the uplink
```

An utterance in flight is finished and streamed rather than truncated — the
same courtesy live-audio mode already gets. MQTT presence stays connected
throughout both modes.

## Layout

| File | From | Change |
|---|---|---|
| `mode_arbiter.c/.h` | new | The single owner of the codecs, panel and camera |
| `sentinel_app.c` | sentinel `main.c` | `app_main()` split into init + suspend/resume |
| `sentinel_app.h` | new | The control surface the arbiter drives |
| `audio_pipeline.c/.h` | sentinel | + suspend at a frame boundary |
| `audiosense_mqtt.*`, `live_audio.*`, `config.*`, `mqtt_config.*` | sentinel | unchanged |
| `media_sys.c/.h` | intercom | sink 0 reserved at boot |
| `webrtc.c`, `mqtt_signaling.c/.h` | intercom | + arbiter hooks on offer / call end |
| `board.*`, `network.*`, `video_stream.*`, `debug_log.*`, `app_config.*`, `ai_agent.*`, `settings.h` | intercom | unchanged |
| `main.c` | new | Boot order, then hands over to the arbiter |

## Build

The WebRTC stack and the Waveshare BSP are not on the component registry:

```powershell
Copy-Item ..\p4_webrtc_firmware\components_all\components .\components -Recurse
idf.py set-target esp32p4
idf.py build
```

The on-board ESP32-C6 must already be running the `esp_hosted` slave
firmware; the P4 has no radio of its own.

## Status

Done: project skeleton, both source trees imported, the mode arbiter and its
interfaces, the merged `settings.txt`, and the sink-0 reservation that makes
the camera uplink and the call coexist.

Not yet done — see the arbiter's header for the contract each must satisfy:

1. `sentinel_app.c` — rename `app_main()`, implement `sentinel_app_suspend()`
   / `_resume()` / `_redraw()`, and gate `ui_lock()` on the mode.
2. `audio_pipeline.c` — a suspend request honoured at a frame boundary,
   mirroring the existing `audio_pipeline_set_live()`.
3. `mqtt_signaling.c` — call `mode_arbiter_enter_call()` when an offer
   arrives, before handing it to `esp_webrtc`.
4. `webrtc.c` — call `mode_arbiter_end_call()` on DISCONNECTED /
   CONNECT_FAILED / DENY_CALL / hangup.
5. `main.c` — boot order: config → display → codecs → self-test →
   `media_sys_buildup()` (reserves sink 0) → WiFi → both MQTT clients →
   uplink → `mode_arbiter_enter_sentinel()`.
6. Unify the two `settings.txt` parsers. `config.c` (sentinel, generic and
   case-insensitive) and `app_config.c` (intercom) currently both read the
   file; the intercom parser warns about every key it does not recognise,
   which is now most of the file.
