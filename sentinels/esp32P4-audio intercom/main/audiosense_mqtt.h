/*
 * audiosense_mqtt.h
 */
#ifndef AUDIOSENSE_MQTT_H
#define AUDIOSENSE_MQTT_H

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "mqtt_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ── Broker and topic roots ──────────────────────────────────────── *
 * Not defined here any more. The broker address, credentials, TLS
 * setting and topic roots arrive as an mqtt_config_t, loaded from
 * settings.txt by mqtt_config_load() and handed to
 * audiosense_mqtt_init(). Everything below is protocol shape — the
 * contract with the desktop application — and is intentionally still
 * compile-time.
 * ─────────────────────────────────────────────────────────────────── */

/* ── Presence (LWT / birth) ──────────────────────────────────────── *
 * Topic:  <mqtt_topic_devices>/<id>/status                           *
 * Both the LWT (registered at connect time, delivered by the broker  *
 * if the client drops without a clean DISCONNECT) and the birth      *
 * message (published by us right after MQTT_EVENT_CONNECTED) are     *
 * sent with retain=true, qos=1:                                      *
 *                                                                     *
 *   LWT (broker-delivered on ungraceful loss): {"status":"offline"}  *
 *   Birth (us, on every connect/reconnect):    {"status":"online"}   *
 *                                                                     *
 * Because both are retained, a server that subscribes late still     *
 * gets the current state immediately from the broker's retained      *
 * store rather than waiting for a live event.                        *
 * ─────────────────────────────────────────────────────────────────── */
#define MQTT_STATUS_PAYLOAD_ONLINE  "{\"status\":\"online\"}"
#define MQTT_STATUS_PAYLOAD_OFFLINE "{\"status\":\"offline\"}"

/*
 * Staging file for an incoming broadcast, written chunk by chunk as
 * the MQTT payload arrives and renamed into the playback queue only
 * once "complete" is received.
 *
 * The name MUST NOT match the play-queue pattern that main.c's
 * check_and_play_queued() scans for (PLAY_QUEUE_PREFIX + "*.wav").
 * It used to be "play_mqtt.wav", which did match: the scanner picked
 * the file up while it was still being written, played the fraction
 * of a second that had arrived so far, and unlinked it out from under
 * the still-open handle - so the broadcast was inaudible and the
 * rename below failed with ENOENT.
 */
#define MQTT_PLAY_WAV_PATH        "/sdcard/rx_stage.wav"

/* ── Live Audio Mode control ─────────────────────────────────────── *
 * Topic:  <mqtt_topic_commands>/<id>/control     QoS 1
 *
 * Payload carries one of two commands, accepted either bare or wrapped
 * in JSON so the desktop application can use whichever it already
 * emits:
 *
 *   start_live_audio            {"command":"start_live_audio"}
 *   stop_live_audio             {"command":"stop_live_audio"}
 *
 * The device answers on <mqtt_topic_devices>/<id>/live, retained and
 * QoS 1, with {"live":true} once the Opus stream is actually running
 * and {"live":false} when it has stopped. Because it is retained, a
 * dashboard that subscribes late still learns the current mode without
 * polling — the same pattern the presence topic uses.
 *
 * Note that the reply reflects the *stream*, not the command: if the
 * command is accepted but the encoder cannot open, no {"live":true} is
 * ever published.
 * ─────────────────────────────────────────────────────────────────── */
#define MQTT_LIVE_PAYLOAD_ON   "{\"live\":true}"
#define MQTT_LIVE_PAYLOAD_OFF  "{\"live\":false}"

#define MQTT_CMD_START_LIVE    "start_live_audio"
#define MQTT_CMD_STOP_LIVE     "stop_live_audio"

/**
 * Called from the MQTT task when a live-audio command arrives.
 * @p start is true for start_live_audio, false for stop_live_audio.
 *
 * Runs in MQTT task context and must not block: the implementation in
 * main.c only sets a flag that capture_task polls.
 */
typedef void (*audiosense_mqtt_live_cmd_cb_t)(bool start);

/* ── Legacy SD-play callback ─────────────────────────────────────── */
typedef void (*audiosense_mqtt_play_cb_t)(void);

/* ── Live streaming receive callbacks ────────────────────────────── *
 *                                                                    *
 *  Registered via audiosense_mqtt_set_play_stream_callbacks().       *
 *  All three are called from the MQTT task context – they must not   *
 *  block.  main.c's on_play_stream_* wrappers push data into a       *
 *  FreeRTOS queue consumed by player_task.                           *
 * ─────────────────────────────────────────────────────────────────── */
typedef void (*mqtt_play_stream_start_fn)(int sample_rate,
                                          int bits_per_sample,
                                          int channels);
typedef void (*mqtt_play_stream_chunk_fn)(const void *data, int len);
typedef void (*mqtt_play_stream_end_fn)  (void);

/* ── Public API ──────────────────────────────────────────────────── */

/* Must be called BEFORE audiosense_mqtt_init() */
void  audiosense_mqtt_set_client_id(const char *id);

/* Optional – call once after WiFi connects; published retained on connect */
void  audiosense_mqtt_set_device_ip(const char *ip);

/**
 * Start the MQTT client against @p cfg.
 *
 * @p cfg is copied, so the caller may let it go out of scope. Passing
 * NULL is treated as "use the documented defaults" rather than an error,
 * so a caller that failed to load configuration still gets a working
 * client instead of no MQTT at all.
 *
 * audiosense_mqtt_set_client_id() must have been called first: the
 * client id is part of the LWT topic, which is registered here.
 */
bool  audiosense_mqtt_init(const mqtt_config_t *cfg);
bool  audiosense_mqtt_is_connected(void);

/* Legacy: upload a complete .mp3 file from the SD card */
bool  audiosense_mqtt_publish_file(const char *filepath,
                                    const char *device_id,
                                    int sample_rate,
                                    int bits_per_sample,
                                    int duration_sec);

/* Legacy: SD-play WAV assembled from <commands>/<id>/play/# */
void  audiosense_mqtt_set_play_callback(audiosense_mqtt_play_cb_t cb);

/* Live streaming transmit – called frame-by-frame from streamer_task.
 *
 *   Topics used (parallel to the legacy MP3 protocol), where <rec> is
 *   mqtt_topic_recordings, "audio/recordings" by default:
 *     <rec>/<id>/stream/info        QoS 1  JSON metadata
 *     <rec>/<id>/stream/chunk/<n>   QoS 0  raw PCM binary
 *     <rec>/<id>/stream/complete    QoS 1  JSON totals
 */
bool  audiosense_mqtt_stream_start(const char *device_id,
                                    const char *timestamp,
                                    int sample_rate,
                                    int bits_per_sample,
                                    int channels);
bool  audiosense_mqtt_stream_chunk(const void *data, int len,
                                    uint32_t chunk_idx);
bool  audiosense_mqtt_stream_end  (uint32_t total_chunks,
                                    uint32_t total_bytes);

/* Live streaming receive – register before audiosense_mqtt_init().
 *
 *   Incoming topics handled, under <cmd> = mqtt_topic_commands
 *   ("commands" by default):
 *     <cmd>/<id>/stream/info        → on_start()
 *     <cmd>/<id>/stream/chunk/<n>   → on_chunk()
 *     <cmd>/<id>/stream/complete    → on_end()
 */
void  audiosense_mqtt_set_play_stream_callbacks(
          mqtt_play_stream_start_fn on_start,
          mqtt_play_stream_chunk_fn on_chunk,
          mqtt_play_stream_end_fn   on_end);

/* ── Live Audio Mode: control receive ────────────────────────────── *
 * Register before audiosense_mqtt_init() so the subscription made in
 * the CONNECTED handler has somewhere to deliver.
 * ─────────────────────────────────────────────────────────────────── */
void  audiosense_mqtt_set_live_cmd_callback(audiosense_mqtt_live_cmd_cb_t cb);

/* ── Live Audio Mode: Opus transmit ──────────────────────────────── *
 *
 *   <rec>/<id>/live/info       QoS 1  JSON codec + stream metadata
 *   <rec>/<id>/live/opus       QoS 0  bundled Opus frames, binary
 *   <rec>/<id>/live/complete   QoS 1  JSON totals
 *
 * Unlike the Event Mode stream, the chunk index is NOT part of the
 * topic. A continuous stream would otherwise create a new topic every
 * 100 ms, which bloats broker-side per-topic bookkeeping for no
 * benefit; the sequence number lives in the payload header instead
 * (see live_audio.h for the byte layout).
 *
 * live_packet() is deliberately QoS 0. At QoS 1 each bundle would wait
 * for a PUBACK, serialising the stream on round-trip latency, and a
 * late retransmission of 100 ms of speech has no value anyway.
 * ─────────────────────────────────────────────────────────────────── */
bool  audiosense_mqtt_live_start(const char *device_id,
                                  const char *timestamp,
                                  int sample_rate,
                                  int channels,
                                  int bitrate,
                                  int frame_ms,
                                  int frames_per_packet);
bool  audiosense_mqtt_live_packet(const void *data, int len);
bool  audiosense_mqtt_live_end   (uint32_t total_packets,
                                  uint32_t total_bytes);

#ifdef __cplusplus
}
#endif

#endif /* AUDIOSENSE_MQTT_H */