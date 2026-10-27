# Audio Transport Layer

Modular audio I/O for the Sentinel Home Server.  Push-to-Talk is one
implementation of an `AudioTransport`; continuous live streaming will plug in
beside it without changing the Dash UI.

## Architecture

```
UI (Dash audio_details)
        │
        ▼
PushToTalkController          ← press() / release() / set_targets()
        │
        ▼
AudioTransport                ← start_session / stop_session / send_audio_frame
        │
        ├── MQTTAudioTransport       (current PTT — chunked WAV over MQTT)
        └── StreamingAudioTransport  (Live Audio Mode placeholder)
                │
                ▼
AudioSessionManager           ← owns sessions, routes inbound frames to sinks
        │
        ├── sink: speakers
        ├── sink: recorder
        ├── sink: ai
        └── sink: android / …
```

The UI must never import MQTT clients, topic helpers, or codec details.

## Package layout

| Module | Role |
|--------|------|
| `base.py` | `AudioTransport` ABC, `AudioFrame`, `AudioSessionConfig`, codecs / modes |
| `constants.py` | Sample rate, MQTT play topics, live-audio command topics |
| `pcm_utils.py` | Normalise / WAV encode / `PttSegmenter` |
| `mqtt_transport.py` | **Production PTT** — `commands/{id}/play/{info,chunk,complete}` |
| `streaming_transport.py` | Live Audio Mode commands + UDP/WebRTC/Opus stubs |
| `mic_source.py` | Continuous `sounddevice` capture |
| `ptt_controller.py` | Hold-to-talk orchestration |
| `session_manager.py` | Singleton session owner + sink router |

## Hold-to-talk behaviour (default)

| Action | Streaming mode (default) | MQTT play fallback |
|--------|--------------------------|--------------------|
| **press** | `commands/{id}/start_live_audio` + open media plane + forward mic frames | Arm utterance buffer |
| **release** | `commands/{id}/stop_live_audio` + close media | WAV → `play/info\|chunk\|complete` |

Configure in `common_files/parameters.json`::

```json
"audio_transport": {
    "ptt_mode": "streaming",
    "fallback_to_mqtt_play": true
}
```

Set `"ptt_mode": "mqtt_play"` to force the legacy one-shot play protocol.
With `"fallback_to_mqtt_play": true`, a failed live-audio start automatically
falls back to MQTT play for that hold.

## Current Push-to-Talk MQTT play protocol (fallback)

Behaviour is intentionally identical to the former inline `BoxStreamer`:

1. Mic runs continuously; Spacebar arms `PttSegmenter`.
2. On release, PCM is normalised, padded (250 ms), encoded as WAV.
3. Published to each selected Sentinel:
   - `commands/{device_id}/play/info`
   - `commands/{device_id}/play/chunk/N`
   - `commands/{device_id}/play/complete`
4. Firmware reassembles the file and plays it on the loudspeaker.

**No firmware change is required** for PTT after this refactor.

## Live Audio Mode (StreamingAudioTransport)

Sentinels support a second operating state:

| MQTT topic | Effect |
|------------|--------|
| `commands/{device_id}/start_live_audio` | Enter Live Audio Mode (continuous TX toward the laptop) |
| `commands/{device_id}/stop_live_audio` | Exit and restore Event Mode |

`AudioSessionManager.start_live_audio(device_ids)` / `stop_live_audio(session_id)`
publish those commands today.  The media plane is not implemented yet.

## Adding a real-time transport (UDP / Opus / WebRTC)

Implement `AudioTransport` (or extend `StreamingAudioTransport`):

1. **`negotiate(preferred)`** — agree codec, bitrate, sample rate, direction.
2. **`_open_media(config, session_id)`** — bind UDP or create a WebRTC `PeerConnection`;
   start a receive thread that builds `AudioFrame`s and calls `_emit_received`.
3. **`send_audio_frame(frame)`** — encode (e.g. Opus) and write to the media plane.
4. **`_close_media(session_id)`** — tear down sockets / PC; join threads.
5. Keep MQTT (or signaling) for `start_live_audio` / `stop_live_audio` only.

Register the new class in `AudioSessionManager._transports` (or add a factory).
Wire duplex frames through `register_sink(...)` — the UI stays unchanged.

### Suggested future codecs

| Codec | Use |
|-------|-----|
| `AudioCodec.OPUS` | Primary continuous / duplex path |
| `AudioCodec.G711_ULAW` | Interop with existing WebRTC intercom |
| `AudioCodec.PCM_RAW` | Debugging / low-latency LAN |

`AudioSessionConfig.bitrate` and `TransportCapabilities.supports_bitrate_selection`
are already reserved for negotiation.

## Duplex and multi-listener routing

```python
mgr = AudioSessionManager.get_instance()

def to_speakers(session_id, frame, sink_name):
    ...  # play PCM on desktop

def to_ai(session_id, frame, sink_name):
    ...  # feed AST / ASR pipeline

mgr.register_sink("speakers", to_speakers)
mgr.register_sink("ai", to_ai)

sid = mgr.start_live_audio(["box3-2"])
# ... later ...
mgr.stop_live_audio(sid)
```

When `StreamingAudioTransport` begins emitting inbound frames, the manager
fans them out to every registered sink.  The same pattern scales to video
tracks or multiple simultaneous listeners.

## UI integration

```python
from ast_src.audio_transport import AudioSessionManager

ptt = AudioSessionManager.get_instance().get_ptt_controller()
ptt.press(["box3-2"])
# ... user speaks ...
ptt.release()
```

Switching the default PTT transport later is a one-line change inside
`AudioSessionManager` (construct `PushToTalkController` with a different
`AudioTransport`).  Dash callbacks do not need to change.
