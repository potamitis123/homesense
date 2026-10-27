/* Compile-time configuration for the Waveshare P4 video-call sentinel.
 *
 * This is the only file that should need editing to bring the board up on a
 * new network / broker. Everything below it is board- or protocol-generic.
 */

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

/* ── Identity ───────────────────────────────────────────────────────────
 *
 * DEVICE_ID names this unit on the signaling bus; the caller addresses it
 * by this string. It is overridable at runtime from /sdcard/settings.txt
 * (key DEVICE_ID), which is how units are told apart without a rebuild.
 */
#define DEVICE_ID_DEFAULT "p4-door-1"

/* ── Wi-Fi ──────────────────────────────────────────────────────────────
 *
 * The P4 has no radio of its own. esp_wifi_remote forwards the esp_wifi_*
 * API over SDIO to the on-board ESP32-C6, which must already be running the
 * esp_hosted slave firmware. Credentials here are only the fallback:
 * /sdcard/settings.txt (WIFI_SSID / WIFI_PASS) wins when present.
 */
#define WIFI_SSID_DEFAULT ""
#define WIFI_PASSWORD_DEFAULT ""

/* ── MQTT signaling ─────────────────────────────────────────────────────
 *
 * Replaces the apprtc HTTPS+websocket signaling the upstream demo uses.
 * Offer/answer/candidate/bye and the call-control commands all ride MQTT
 * topics under MQTT_SIGNAL_TOPIC_PREFIX; see mqtt_signaling.h for the
 * topic layout and message schema.
 *
 * An mqtts:// URI switches mqtt_signaling.c to mutual TLS using the PEMs
 * embedded from certs/. That is not optional against the home-server broker:
 * its LAN listener sets require_certificate and ignores usernames entirely,
 * and its plaintext listener is bound to 127.0.0.1. It is also the right
 * default regardless — the SDP carries ICE candidates and the DTLS
 * fingerprint, so anyone able to rewrite signaling can redirect the call.
 *
 * MQTT_USERNAME/PASSWORD are therefore dead weight on this broker and are
 * only kept for a plain mqtt:// bring-up broker.
 */
#define MQTT_BROKER_URI_DEFAULT "mqtts://your-broker.example.com:8883"
#define MQTT_USERNAME_DEFAULT ""
#define MQTT_PASSWORD_DEFAULT ""
#define MQTT_SIGNAL_TOPIC_PREFIX "videocall"

/* ── ICE ────────────────────────────────────────────────────────────────
 *
 * apprtc handed the device a STUN/TURN server as part of the room join.
 * MQTT signaling has no such handshake, so the servers are configured here
 * and published to esp_peer through the signaling layer's on_ice_info
 * callback. A STUN server is enough on a flat LAN; add TURN credentials
 * when caller and callee can be on different networks.
 */
/* esp_peer expects "stun:host:port" (no "//"). stun:// fails DNS resolve. */
#define ICE_STUN_URL_DEFAULT "stun:stun.l.google.com:19302"
#define ICE_TURN_USER_DEFAULT ""
#define ICE_TURN_PASSWORD_DEFAULT ""

/* ── Call behaviour ─────────────────────────────────────────────────────
 *
 * This unit is a sentinel: it never rings anyone and it has no accept
 * button. An incoming RING is answered automatically after the two-tone
 * announcement finishes playing.
 *
 * AUTO_ANSWER_DELAY_MS is deliberately not zero. The beep is played
 * through the ES8311 directly while the codec is still idle; the peer
 * connection is only enabled afterwards, so that av_render can take the
 * codec over without fighting the tone for the shared I2S port.
 */
#define CALL_AUTO_ANSWER (true)
#define AUTO_ANSWER_DELAY_MS (150)
#define CALL_SETUP_TIMEOUT_MS (30000)

/* Two-tone incoming-call announcement. Each burst is faded in and out so
 * the class-D amplifier never sees a step edge. */
#define ANNOUNCE_TONE_HZ (880)
#define ANNOUNCE_TONE_MS (110)
#define ANNOUNCE_GAP_MS (70)
#define ANNOUNCE_AMPLITUDE (9000)
#define ANNOUNCE_REPEAT (2)

/* ── Audio ──────────────────────────────────────────────────────────────
 *
 * G.711 A-law at 8 kHz, duplex. The ES8311 (playback) and ES7210 (capture)
 * share one I2S port on this board, so both directions are pinned to the
 * same rate and width — this is the rate the whole media graph runs at.
 */
#define AUDIO_SAMPLE_RATE (8000)
#define AUDIO_CHANNELS (1)
#define AUDIO_BITS_PER_SAMPLE (16)
#define SPEAKER_VOLUME (80)
#define MIC_GAIN_DB (42.0)

/* ── Video ──────────────────────────────────────────────────────────────
 *
 * Video inbound is decoded onto the MIPI panel. OV5647 stills to the
 * dashboard during a call are off (VIDEO_SEND_ENABLED). HTTP ingest stays
 * paused for POSTs during the call (DNS vs MQTT).
 *
 * VIDEO_WIDTH / VIDEO_HEIGHT are the geometry the *inbound* stream is
 * decoded against when JPEG SOF probe fails: because video rides the data
 * channel there is no video m-line and no negotiation, so esp_webrtc
 * declares the decoder from these constants (esp_webrtc.c, pc_on_data).
 * The dashboard reads the same two numbers from parameters.json and must
 * be changed alongside them.
 *
 * Set inbound to the panel's own native geometry (720x1280 portrait, per
 * board.c/BOARD's "720x1280 panel" boot line), not the camera's. The
 * downlink renderer (media_sys.c, av_render_alloc_lcd_render with
 * use_frame_buffer=true) decodes straight into the DPI framebuffer with
 * no intermediate blit/scale step -- that only works when the decoded
 * image's stride matches the framebuffer's native width. It didn't: at
 * 640x480 against a 720-wide framebuffer, every scanline landed 80px
 * short of where the next one started, which is what a "crushed /
 * pixelated" image straight out of a garbled call actually is (a stride
 * mismatch, not a compression-quality problem). Portrait video from a
 * landscape webcam will look stretched/cropped on arrival, a real but
 * far more acceptable tradeoff than corrupted video.
 *
 * Video inbound is the 720x1280 panel stream. OV5647 send on the same
 * call data channel is off: reserving MJPEG on sink 0 at boot makes HTTP
 * ingest build a second encoder path (call size + 640x480). That dual
 * pipeline lost V4L2 frames, hung GMF stop, and blocked pc_task inside
 * start_stream — ICE looked up, the dashboard said Peer-unreachable, and
 * the panel stayed yielded with no CONNECTED/DISCONNECTED. Audio unmute
 * on I2S open does not need that encoder. Turn VIDEO_SEND_ENABLED on only
 * after send shares the HTTP sink geometry and does not add a second path.
 */
#define VIDEO_SEND_ENABLED (false)
#define VIDEO_WIDTH (720)
#define VIDEO_HEIGHT (1280)
#define VIDEO_FPS (10)
#define VIDEO_SEND_WIDTH (320)
#define VIDEO_SEND_HEIGHT (240)
#define VIDEO_SEND_FPS (1)

/* ── Situational-awareness uplink (OV5647 → server) ─────────────────────
 *
 * A second, call-independent video path: the OV5647's frames are posted to
 * the home server as whole JPEGs, one HTTP POST per frame, so the operator
 * can see the door without ringing it. It is deliberately *not* the WebRTC
 * data channel above — that channel only exists while a browser has a call
 * up, and this stream has to run continuously.
 *
 * One POST per frame rather than one long chunked upload: at 2-5 fps the
 * header overhead is noise next to a 30 kB JPEG, the socket is reused by
 * keep-alive, and every frame is self-delimiting. A dropped connection
 * costs one frame and the next POST reconnects, which is the whole of the
 * recovery story (see video_stream.c).
 *
 * Port 8060 rather than the dashboard's 8050 on purpose: 8050 is served by
 * Werkzeug, which closes the connection after every response, so posting
 * there costs a TLS handshake per frame. 8060 is a small keep-alive listener
 * in the same server process (ast_src/video_stream/ingest_server.py) that
 * holds one connection open for the life of the link. Both accept frames --
 * https://your-broker.example.com:8050/video/api/ingest is the fallback if only
 * the dashboard's port is reachable.
 *
 * These are only the fallbacks. /sdcard/settings.txt wins:
 *
 *     ov5647_video_enabled=1
 *     VIDEO_STREAM_URL=https://your-broker.example.com:8050/video/api/ingest
 *     VIDEO_STREAM_WIDTH=640
 *     VIDEO_STREAM_HEIGHT=480
 *     VIDEO_STREAM_FPS=3
 *
 * The default is off, so a unit whose settings.txt predates this feature
 * behaves exactly as it did before.
 *
 * DEVICE_ID is appended to VIDEO_STREAM_URL as a path element, so one line
 * in settings.txt serves every unit and the server can tell them apart.
 *
 * An https:// URL is verified against the same embedded certs/ca.crt the
 * broker connection uses; the Dash server presents certs/hlias.crt, whose
 * CN is your-broker.example.com, so the URL's host has to be that name (the LAN
 * resolves it through dns_redirector.py) rather than a bare IP.
 */
#define VIDEO_STREAM_ENABLED_DEFAULT (false)
#define VIDEO_STREAM_URL_DEFAULT \
    "https://your-broker.example.com:8060/ingest"
#define VIDEO_STREAM_WIDTH_DEFAULT (640)
#define VIDEO_STREAM_HEIGHT_DEFAULT (480)
#define VIDEO_STREAM_FPS_DEFAULT (3)

/* The uplink hands the capture graph back for the duration of a call.
 *
 * esp_capture wants its sinks configured before the graph is started, and
 * esp_webrtc sets sink 0 up when a call arrives. With the uplink already
 * running the graph from boot, that setup lands on a started capture and the
 * sentinel's own microphone never reaches the caller -- inbound audio and
 * video are unaffected, which is what makes it look like "the P4 went mute".
 * So the uplink stops its sink and the graph on the first sign of a call and
 * takes them back when the call ends.
 *
 * Setting this false attempts to keep the uplink running through a call,
 * which is what the eventual "low-rate uplink + full-rate call" design wants.
 * Leave it true until the capture stack is proven to allow a sink to be set
 * up underneath a running graph; the symptom of getting it wrong is silent
 * one-way audio, not an error.
 */
/* Now false. The condition above -- "leave it true until the capture stack is
 * proven to allow a sink to be set up underneath a running graph" -- is met by
 * esp_webrtc_set_no_auto_capture(), which webrtc.c now calls:
 *
 *     "In default capture will start and stop internally by WebRTC. In some
 *      scenario capture is controlled by other module, capture is only one
 *      sink path of it."
 *
 * That is this arrangement exactly: the uplink owns the graph from boot and
 * the call is one sink path within it. esp_capture supports parallel sinks;
 * what it refuses is a sink being configured on an already-started graph,
 * and with auto capture off esp_webrtc no longer takes the graph over.
 *
 * Why it matters here: this unit watches a room and the operator talks into
 * that room, so going blind for the duration of every call defeats the point.
 *
 * If this is wrong the symptom is the documented one -- the sentinel's
 * microphone never reaches the caller, while inbound audio and video are
 * fine. Set it back to true and the old behaviour returns.
 */
#define VIDEO_STREAM_YIELD_DURING_CALL (false)

/* A RING that never becomes a call must not leave the uplink parked forever.
 * Longer than CALL_SETUP_TIMEOUT_MS, so a call that is merely slow to
 * negotiate is not interrupted. */
#define VIDEO_STREAM_CALL_PAUSE_MAX_MS (45000)

/* No frame for this long, with the sink supposedly running, means the graph
 * was stopped underneath us (esp_webrtc does exactly that when a call ends).
 * The uplink re-arms rather than going quiet until the next reboot. */
#define VIDEO_STREAM_STARVE_MS (15000)

/* After hangup, MQTT signaling is torn down and rebuilt. HTTP ingest must
 * not call getaddrinfo in that window: lwIP only has one NETDB slot. */
#define VIDEO_STREAM_HTTP_HOLD_AFTER_CALL_MS (15000)

/* One POST must not outlive several frame intervals, or a wedged server
 * would stall the uplink task instead of failing it into the retry path. */
#define VIDEO_STREAM_POST_TIMEOUT_MS (8000)
#define VIDEO_STREAM_RETRY_MIN_MS (1000)
#define VIDEO_STREAM_RETRY_MAX_MS (10000)
#define VIDEO_STREAM_STATS_PERIOD_MS (60000)

/* Internal RAM: the mbedtls handshake unwinds on this stack. Priority is
 * below AUD_SRC (15) and venc_0 (10) on purpose — a late frame here is
 * invisible, a late audio buffer is audible — and core 0 keeps it off the
 * core pc_task is pinned to. */
#define VIDEO_STREAM_TASK_STACK (8 * 1024)
#define VIDEO_STREAM_TASK_PRIO (5)
#define VIDEO_STREAM_TASK_CORE (0)

/* MJPEG rides the SCTP data channel rather than RTP. That is upstream's
 * choice for this demo and it is kept: MJPEG has no RTP payload format that
 * esp_peer implements, and the data channel gives it reliable delivery.
 *
 * It also happens to be what makes a browser peer possible at all. esp_peer
 * decodes H.264 baseline over RTP, but the P4 has no hardware H.264 decoder,
 * whereas one whole JPEG per SCTP message is something a canvas and
 * toBlob('image/jpeg') produce directly. */
#define DATA_CHANNEL_ENABLED (true)
#define VIDEO_OVER_DATA_CHANNEL (true)
#define DATA_CH_SEND_CACHE_SIZE (400 * 1024)
#define DATA_CH_RECV_CACHE_SIZE (400 * 1024)

/* ── Debug log ──────────────────────────────────────────────────────────
 *
 * DEBUG=1 in settings.txt mirrors everything the serial monitor shows to a
 * file on the SD card. Off by default: it costs a task, a PSRAM ring and a
 * steady trickle of card writes, none of which a deployed unit needs.
 *
 * The ring exists because SD writes block. The log hook runs on whichever
 * task just logged — including the ones pinned and prioritised to stay
 * unblocked — so it only formats and copies, and a low-priority writer task
 * does the file I/O. DEBUG_LOG_RING_BYTES is the burst it can absorb while
 * the card is busy; overflow drops whole lines and is reported in the file.
 *
 * One boot, one file: the previous log is rotated to <path>.1, so pulling
 * the card after a failure gives you that run and the one before it.
 */
/* Durability. The writer is woken per line and fsyncs whatever it drained
 * before sleeping again, so what is on the card survives a panic, a watchdog
 * or a power cut — the ring is empty most of the time, and a crash loses at
 * most the handful of lines logged in the last tick or two.
 *
 * DEBUG_SYNC=1 closes even that gap by writing and syncing from the logging
 * task itself, so the line before a panic is guaranteed. It also puts an SD
 * write inside whichever task logged, including the audio and peer threads,
 * which is why it is opt-in: use it to catch a hard crash, not to run the
 * unit day to day.
 */
#define DEBUG_LOG_ENABLED_DEFAULT (false)
#define DEBUG_LOG_SYNC_DEFAULT (false)
#define DEBUG_LOG_PATH_DEFAULT "/sdcard/sentinel_log.txt"
#define DEBUG_LOG_RING_BYTES (48 * 1024)
#define DEBUG_LOG_CHUNK (4 * 1024)
#define DEBUG_LOG_LINE_MAX (256)
#define DEBUG_LOG_MAX_BYTES (4 * 1024 * 1024)
/* Only how long the writer sleeps when nothing is logging; lines wake it. */
#define DEBUG_LOG_IDLE_WAIT_MS (200)
/* How long a logging task waits for the card in DEBUG_SYNC mode before
 * falling back to the ring rather than stalling any further. */
#define DEBUG_LOG_SYNC_WAIT_MS (100)
#define DEBUG_LOG_TASK_STACK (4 * 1024)
#define DEBUG_LOG_TASK_PRIO (3)
#define DEBUG_LOG_TASK_CORE (0)

/* ── OpenAI ─────────────────────────────────────────────────────────────
 *
 * Scaffolding for the case where the caller is an AI rather than a person.
 * Disabled by default and not wired into the call path — see ai_agent.h for
 * what is stubbed and what would still have to be built.
 */
#define AI_AGENT_ENABLED (false)
#define OPENAI_API_KEY_DEFAULT ""
#define OPENAI_BASE_URL "https://api.openai.com/v1"
#define OPENAI_REALTIME_URL "wss://api.openai.com/v1/realtime"
#define OPENAI_REALTIME_MODEL "gpt-4o-realtime-preview"
#define OPENAI_VISION_MODEL "gpt-4o-mini"
#define OPENAI_SYSTEM_PROMPT \
    "You are the voice of a door intercom. Keep replies to one or two " \
    "short sentences."

#ifdef __cplusplus
}
#endif
