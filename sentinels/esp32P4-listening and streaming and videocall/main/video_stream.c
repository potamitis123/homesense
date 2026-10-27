#include <stdio.h>
#include <string.h>

#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "media_sys.h"
#include "network.h"
#include "settings.h"
#include "video_stream.h"

static const char *TAG = "VIDEO_TX";

/* Enough for the 204 the server answers with, and for the short JSON it
 * sends when it rejects a frame. Anything longer is left on the socket and
 * the connection is dropped rather than reused. */
#define RESPONSE_DRAIN_LEN 192

static struct {
    char url[256];
    const char *ca_cert;
    int width;
    int height;
    int fps;

    TaskHandle_t task;
    volatile bool stop;
    volatile bool running;

    /* Call handshake. Written from the signaling and event callbacks, read by
     * the uplink task; paused_ack is the task confirming it has let go of the
     * capture graph, which is the thing the caller actually has to wait for. */
    volatile bool paused;
    volatile bool paused_ack;
    volatile bool call_active;
    volatile uint32_t pause_deadline;
    volatile uint32_t hold_http_until;

    esp_http_client_handle_t client;
    video_stream_stats_t stats;
} s_tx;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* ── HTTP ───────────────────────────────────────────────────────────────
 *
 * One client handle is kept for the life of the uplink and reused frame to
 * frame; with keep_alive_enable the socket, and on https:// the TLS session,
 * survive between POSTs. Any failure drops the handle instead of trying to
 * reason about which half of it is still good — a fresh handle after a Wi-Fi
 * drop is a few hundred bytes and one handshake, and it is the difference
 * between recovering and wedging.
 */

static void client_drop(void)
{
    if (s_tx.client != NULL) {
        esp_http_client_cleanup(s_tx.client);
        s_tx.client = NULL;
    }
    s_tx.stats.online = false;
}

static esp_err_t client_ensure(void)
{
    if (s_tx.client != NULL) {
        return ESP_OK;
    }
    esp_http_client_config_t cfg = {
        .url = s_tx.url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = VIDEO_STREAM_POST_TIMEOUT_MS,
        .keep_alive_enable = true,
        .buffer_size_tx = 1024,
        /* cert_len stays 0: EMBED_TXTFILES NUL-terminates the PEM, which is
         * what esp-tls expects from a string certificate. NULL here for a
         * plain http:// url, which is the bring-up case. */
        .cert_pem = s_tx.ca_cert,
    };
    s_tx.client = esp_http_client_init(&cfg);
    if (s_tx.client == NULL) {
        ESP_LOGE(TAG, "Cannot create the HTTP client");
        return ESP_FAIL;
    }
    esp_http_client_set_header(s_tx.client, "Content-Type", "image/jpeg");
    return ESP_OK;
}

/* Sends one frame straight out of the capture buffer: nothing is copied and
 * nothing is allocated per frame, which is what keeps a 3 fps uplink off the
 * heap entirely. */
static esp_err_t post_frame(const uint8_t *jpeg, size_t len)
{
    if (client_ensure() != ESP_OK) {
        return ESP_FAIL;
    }

    esp_err_t ret = esp_http_client_open(s_tx.client, (int)len);
    if (ret != ESP_OK) {
        return ret;
    }

    ret = ESP_FAIL;
    do {
        int written = esp_http_client_write(s_tx.client, (const char *)jpeg,
                                            (int)len);
        if (written < 0 || (size_t)written != len) {
            ESP_LOGW(TAG, "Short write: %d of %u bytes", written,
                     (unsigned)len);
            break;
        }
        if (esp_http_client_fetch_headers(s_tx.client) < 0) {
            break;
        }
        /* Read the body even when there is none: leaving bytes unread would
         * desynchronise the next request on a kept-alive socket. */
        char drain[RESPONSE_DRAIN_LEN];
        int read = esp_http_client_read_response(s_tx.client, drain,
                                                 sizeof(drain) - 1);
        int status = esp_http_client_get_status_code(s_tx.client);
        s_tx.stats.last_status = status;
        if (status < 200 || status >= 300) {
            if (read > 0) {
                drain[read] = '\0';
                ESP_LOGW(TAG, "Server refused the frame: HTTP %d %s", status,
                         drain);
            } else {
                ESP_LOGW(TAG, "Server refused the frame: HTTP %d", status);
            }
            break;
        }
        ret = ESP_OK;
    } while (0);

    esp_http_client_close(s_tx.client);
    return ret;
}

/* ── Uplink task ────────────────────────────────────────────────────────*/

static void log_stats(void)
{
    ESP_LOGI(TAG, "Uplink: %u frames, %llu kB sent, %u failed",
             (unsigned)s_tx.stats.frames_sent,
             (unsigned long long)(s_tx.stats.bytes_sent / 1024),
             (unsigned)s_tx.stats.frames_failed);
}

static void stream_task(void *arg)
{
    const uint32_t interval_ms = 1000 / (s_tx.fps > 0 ? s_tx.fps : 1);
    uint32_t next_due = now_ms();
    uint32_t next_stats = now_ms() + VIDEO_STREAM_STATS_PERIOD_MS;
    uint32_t backoff_ms = 0;
    uint32_t camera_retry_at = 0;
    uint32_t last_frame_at = now_ms();
    bool camera_up = false;
    bool was_online = false;
    bool got_frame = false;
    bool encoder_dead = false;

    ESP_LOGI(TAG, "Uplink task started: %dx%d @ %d fps to %s", s_tx.width,
             s_tx.height, s_tx.fps, s_tx.url);

    while (!s_tx.stop) {
        /* A call owns the camera. The suspend is done here rather than in
         * video_stream_call_started() so that it always happens on this task,
         * with no frame checked out and no chance of a callback thread
         * blocking behind an in-flight POST. */
        if (s_tx.paused) {
            if (camera_up) {
                ESP_LOGI(TAG, "Call starting; releasing the camera");
                /* Sink 1 only. The graph itself stays up: esp_webrtc has
                 * its sink reserved from boot and may already be using the
                 * graph for this call by the time we get here. */
                media_sys_video_stream_stop();
                camera_up = false;
            }
            client_drop();
            was_online = false;
            s_tx.paused_ack = true;
            /* A RING that never became a call must not park the uplink for
             * good; a call that did connect holds it for as long as it runs. */
            if (!s_tx.call_active &&
                (int32_t)(now_ms() - s_tx.pause_deadline) >= 0) {
                ESP_LOGW(TAG, "No call after %d ms; taking the camera back",
                         VIDEO_STREAM_CALL_PAUSE_MAX_MS);
                s_tx.paused = false;
            }
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        s_tx.paused_ack = false;

        /* Wi-Fi down. The socket is gone with it, and there is no point
         * burning the encoder on frames nobody can receive: the sink is
         * released and taken again when the network comes back. */
        if (!network_is_connected()) {
            if (camera_up) {
                ESP_LOGW(TAG, "Network down, pausing the uplink");
                media_sys_video_stream_stop();
                camera_up = false;
            }
            client_drop();
            was_online = false;
            backoff_ms = 0;
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }

        if (encoder_dead) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        /* A live call must not POST to the ingest URL. That TLS handshake
         * calls getaddrinfo on the same host MQTT uses, and lwIP only
         * allows one of those at a time. Hold after hangup too, so the
         * session rebuild can reconnect MQTT by IP without a DNS race.
         * The JPEG sink is stopped for the call so it cannot share the
         * encoder with inbound decode. */
        if (s_tx.call_active ||
            (int32_t)(now_ms() - s_tx.hold_http_until) < 0) {
            last_frame_at = now_ms();
            if (s_tx.call_active && camera_up) {
                media_sys_video_stream_stop();
                camera_up = false;
            }
            if (s_tx.client != NULL) {
                client_drop();
                was_online = false;
            }
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }

        if (!camera_up) {
            /* Signed difference, like the frame clock below: the millisecond
             * counter wraps every 49 days and this unit is meant to stay up
             * longer than that. */
            if ((int32_t)(now_ms() - camera_retry_at) < 0) {
                vTaskDelay(pdMS_TO_TICKS(200));
                continue;
            }
            esp_err_t ret = media_sys_video_stream_start(s_tx.width,
                                                         s_tx.height, s_tx.fps);
            if (ret != ESP_OK) {
                /* A camera that will not start is this feature's problem
                 * alone: the sentinel keeps taking calls without it. */
                ESP_LOGE(TAG, "Camera unavailable (%s); retrying in 10 s",
                         esp_err_to_name(ret));
                camera_retry_at = now_ms() + 10000;
                continue;
            }
            camera_up = true;
            got_frame = false;
            last_frame_at = now_ms();
            next_due = now_ms();
        }

        /* esp_webrtc stops the capture graph when a call ends. If that lands
         * while the uplink thinks it owns the sink -- a call we never saw the
         * end of, or one that ended without an event -- frames simply stop
         * arriving. Re-arm rather than going quiet until the next reboot.
         *
         * If the encoder never produced a frame (JPEG DMA descriptors failed
         * to allocate), stop/start crashes in prepare_pipeline on a NULL
         * path. Leave the graph down instead of looping that. */
        if (!s_tx.call_active &&
            (int32_t)(now_ms() - last_frame_at) > VIDEO_STREAM_STARVE_MS) {
            if (!got_frame) {
                ESP_LOGE(TAG, "JPEG encoder produced no frames; leaving the camera graph down");
                media_sys_video_stream_stop();
                camera_up = false;
                encoder_dead = true;
                continue;
            }
            /* Do not stop/start the graph. That hang is what blocked the
             * WebRTC task on the last camera-send attempt. Keep acquiring. */
            ESP_LOGW(TAG, "No frames for %d ms; leaving the camera graph up",
                     VIDEO_STREAM_STARVE_MS);
            last_frame_at = now_ms();
        }

        uint32_t now = now_ms();
        if ((int32_t)(next_due - now) > 0) {
            uint32_t wait = next_due - now;
            vTaskDelay(pdMS_TO_TICKS(wait > 50 ? 50 : wait));
            continue;
        }
        next_due = now + interval_ms;

        esp_capture_stream_frame_t frame;
        if (media_sys_video_stream_acquire(&frame) != ESP_OK) {
            /* The encoder has not produced one yet. Not an error at these
             * rates; the next tick will find it. */
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        last_frame_at = now_ms();
        got_frame = true;

        /* Only post a complete JPEG.
         *
         * A truncated one still decodes: the viewer gets clean MCU rows down
         * to where the data stopped, a partial row at the right edge and
         * noise below it -- the L-shaped corruption along the bottom and
         * right. Dropping the frame instead costs one frame interval and
         * leaves the picture clean.
         *
         * Every JPEG ends with EOI (FF D9). Checking the tail is enough to
         * tell a finished frame from a cut-off one, and it is two byte
         * comparisons per frame. The reject counter is the diagnostic: if it
         * climbs with scene complexity, the encoder's output is being
         * truncated rather than the network losing anything. */
        const bool complete = (frame.size >= 4) &&
                              (frame.data[frame.size - 2] == 0xFF) &&
                              (frame.data[frame.size - 1] == 0xD9);
        if (!complete) {
            s_tx.stats.frames_failed++;
            if ((s_tx.stats.frames_failed % 10) == 1) {
                ESP_LOGW(TAG, "Dropped a truncated frame (%u bytes, no EOI); "
                         "%u so far", (unsigned)frame.size,
                         (unsigned)s_tx.stats.frames_failed);
            }
            media_sys_video_stream_release(&frame);
            continue;
        }

        esp_err_t sent = post_frame(frame.data, frame.size);
        size_t size = frame.size;
        media_sys_video_stream_release(&frame);

        if (sent == ESP_OK) {
            s_tx.stats.frames_sent++;
            s_tx.stats.bytes_sent += size;
            s_tx.stats.online = true;
            backoff_ms = 0;
            if (!was_online) {
                ESP_LOGI(TAG, "Uplink online: server accepting frames");
                was_online = true;
            }
        } else {
            s_tx.stats.frames_failed++;
            if (was_online) {
                ESP_LOGW(TAG, "Uplink offline: %s", esp_err_to_name(sent));
                was_online = false;
            }
            client_drop();
            backoff_ms = backoff_ms == 0
                             ? VIDEO_STREAM_RETRY_MIN_MS
                             : (backoff_ms * 2 > VIDEO_STREAM_RETRY_MAX_MS
                                    ? VIDEO_STREAM_RETRY_MAX_MS
                                    : backoff_ms * 2);
            vTaskDelay(pdMS_TO_TICKS(backoff_ms));
            next_due = now_ms();
        }

        if ((int32_t)(now_ms() - next_stats) >= 0) {
            log_stats();
            next_stats = now_ms() + VIDEO_STREAM_STATS_PERIOD_MS;
        }
    }

    if (camera_up) {
        media_sys_video_stream_stop();
    }
    client_drop();
    log_stats();
    ESP_LOGI(TAG, "Uplink task stopped");

    s_tx.stats.running = false;
    s_tx.running = false;
    s_tx.task = NULL;
    vTaskDelete(NULL);
}

/* ── API ────────────────────────────────────────────────────────────────*/

esp_err_t video_stream_start(const video_stream_cfg_t *cfg)
{
    if (cfg == NULL || cfg->url == NULL || cfg->url[0] == '\0' ||
        cfg->device_id == NULL || cfg->device_id[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_tx.running) {
        return ESP_ERR_INVALID_STATE;
    }

    memset(&s_tx.stats, 0, sizeof(s_tx.stats));
    /* The device id is a path element rather than a header so that one URL
     * in settings.txt serves a fleet and the server can route on it without
     * parsing anything. */
    snprintf(s_tx.url, sizeof(s_tx.url), "%s/%s", cfg->url, cfg->device_id);
    s_tx.ca_cert = strncmp(s_tx.url, "https://", 8) == 0 ? cfg->ca_cert : NULL;
    if (strncmp(s_tx.url, "https://", 8) == 0 && s_tx.ca_cert == NULL) {
        ESP_LOGW(TAG, "https:// with no CA certificate; the POST will fail");
    }
    s_tx.width = cfg->width > 0 ? cfg->width : VIDEO_STREAM_WIDTH_DEFAULT;
    s_tx.height = cfg->height > 0 ? cfg->height : VIDEO_STREAM_HEIGHT_DEFAULT;
    s_tx.fps = cfg->fps > 0 ? cfg->fps : VIDEO_STREAM_FPS_DEFAULT;
    s_tx.stop = false;
    s_tx.running = true;
    s_tx.stats.running = true;

    if (xTaskCreatePinnedToCore(stream_task, "video_tx",
                                VIDEO_STREAM_TASK_STACK, NULL,
                                VIDEO_STREAM_TASK_PRIO, &s_tx.task,
                                VIDEO_STREAM_TASK_CORE) != pdPASS) {
        ESP_LOGE(TAG, "Cannot create the uplink task");
        s_tx.running = false;
        s_tx.stats.running = false;
        return ESP_FAIL;
    }
    return ESP_OK;
}

void video_stream_stop(void)
{
    if (!s_tx.running) {
        return;
    }
    s_tx.stop = true;
    /* The task can be inside a POST with a frame checked out; the wait is
     * bounded by the POST timeout, and leaving before it returns would strand
     * that frame in the sink. */
    for (int i = 0; i < 200 && s_tx.task != NULL; i++) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    if (s_tx.task != NULL) {
        ESP_LOGE(TAG, "Uplink task did not stop; leaving it running");
        return;
    }
    ESP_LOGI(TAG, "Uplink stopped");
}

bool video_stream_is_running(void)
{
    return s_tx.running;
}

/* ── Call handshake ─────────────────────────────────────────────────────
 *
 * Called from the signaling and webrtc event callbacks. Those must not
 * stall, so the only blocking one is call_started(), and it is bounded: it
 * waits for the uplink task to confirm it has let go, because esp_webrtc is
 * about to configure its own sink and esp_capture will not take that on a
 * running graph.
 */

void video_stream_call_started(void)
{
    /* Hold HTTP POSTs for the whole call. The ingest URL is the same host
     * as MQTT; concurrent getaddrinfo during ICE is what returns 202 and
     * leaves the panel on "No MQTT!". The camera graph can stay up. */
    s_tx.call_active = true;
    if (!VIDEO_STREAM_YIELD_DURING_CALL || !s_tx.running || s_tx.paused) {
        return;
    }
    s_tx.pause_deadline = now_ms() + VIDEO_STREAM_CALL_PAUSE_MAX_MS;
    s_tx.paused_ack = false;
    s_tx.paused = true;

    /* Normally the task is idle between frames and acks within a tick; the
     * worst case is an in-flight POST, bounded by its own timeout. Waiting
     * the whole POST timeout here would stall signaling, so after a second
     * the call proceeds anyway and takes the audio risk rather than the
     * certainty of a missed answer. */
    for (int i = 0; i < 100 && !s_tx.paused_ack; i++) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (!s_tx.paused_ack) {
        ESP_LOGW(TAG, "Uplink did not release the camera in time");
    }
}

void video_stream_call_connected(void)
{
    s_tx.call_active = true;
}

void video_stream_call_ended(void)
{
    s_tx.call_active = false;
    s_tx.hold_http_until = now_ms() + VIDEO_STREAM_HTTP_HOLD_AFTER_CALL_MS;
    if (s_tx.paused) {
        ESP_LOGI(TAG, "Call over; taking the camera back");
        s_tx.paused = false;
    }
}

void video_stream_get_stats(video_stream_stats_t *out)
{
    if (out == NULL) {
        return;
    }
    *out = s_tx.stats;
    out->running = s_tx.running;
}
