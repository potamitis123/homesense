/* Sentinel call logic.
 *
 * The upstream videocall_demo is a two-way handset: it opens with the peer
 * connection disabled (no_auto_reconnect), rings, and waits for a button
 * press on each end before enabling it. This unit has no accept button, so
 * that whole negotiation step is removed rather than automated around —
 * the peer connection is left on auto, and esp_webrtc answers an incoming
 * offer the moment it arrives.
 *
 * That leaves RING doing one job only: announcing. It is decoupled from
 * connection setup, which is why a caller that does not speak the command
 * protocol at all — a plain browser posting an offer to the MQTT topic —
 * still gets through. The announcement then falls back to the CONNECTING
 * event so something audible happens either way.
 *
 * Media directions: audio is duplex. Video is inbound only on this build
 * (see VIDEO_SEND_ENABLED). The caller is seen and heard; this unit is heard.
 */

#include <stdio.h>
#include <string.h>

#include "common.h"
#include "esp_log.h"
#include "esp_peer_default.h"
#include "esp_timer.h"
#include "esp_webrtc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "media_lib_os.h"
#include "mode_arbiter.h"
#include "mqtt_signaling.h"
#include "video_stream.h"

#define TAG "CALL"

/* Wire-compatible with the upstream demo's command vocabulary. */
#define CMD_RING "RING"
#define CMD_ACCEPTED "ACCEPT_CALL"
#define CMD_DENIED "DENY_CALL"

#define SAME_CMD(data, size, cmd) \
    ((size) >= (int)(sizeof(cmd) - 1) && strncmp((const char *)(data), (cmd), sizeof(cmd) - 1) == 0)

/* Signaling credentials, linked in from certs/ by EMBED_TXTFILES rather than
 * read from the SD card: the card is optional on this board, and a unit that
 * silently falls back to an unauthenticated broker is worse than one that
 * refuses to signal at all. */
extern const char ca_crt_start[] asm("_binary_ca_crt_start");
extern const char hlias_crt_start[] asm("_binary_hlias_crt_start");
extern const char hlias_key_start[] asm("_binary_hlias_key_start");

static esp_webrtc_handle_t s_webrtc;
static call_state_t s_state;
static mqtt_signaling_cfg_t s_signaling_cfg;
/* Referenced by pointer from esp_webrtc_cfg_t.peer_cfg.extra_cfg, which
 * esp_peer reads from pc_task rather than only during esp_webrtc_open()'s
 * own stack frame. A local here would be a dangling pointer by the time
 * that thread gets to it, same reasoning as s_signaling_cfg above. */
static esp_peer_default_cfg_t s_peer_default_cfg;
static char s_room[64];
static bool s_announced;
static volatile bool s_tone_busy;

/* Set when a call ends, acted on from call_service_poll().
 *
 * Re-enabling the peer connection is not enough to take a second call: the
 * unit answers exactly one offer per boot, sending ACCEPT_CALL for every
 * later one but never an SDP answer, until it is reset. Whatever state
 * esp_peer keeps from the finished call, esp_webrtc_enable_peer_connection()
 * does not clear it -- but a reboot does, and closing and reopening the whole
 * session is the closest thing to a reboot that does not drop the MQTT link
 * for longer than a poll interval.
 *
 * Deferred rather than done in the event handler because that handler runs on
 * esp_webrtc's own thread, and closing the handle from inside its callback is
 * how you deadlock it. call_service_poll() runs on the main task. */
static volatile bool s_restart_pending;
static int64_t s_connecting_us;

call_state_t call_get_state(void)
{
    return s_state;
}

static void set_state(call_state_t state)
{
    if (s_state == state) {
        return;
    }
    s_state = state;
    if (state == CALL_STATE_IDLE) {
        s_announced = false;
        s_connecting_us = 0;
    }
    if (state == CALL_STATE_CONNECTING) {
        s_connecting_us = esp_timer_get_time();
    }
}

/* Runs on its own thread: board_play_announce_tone() blocks for the length
 * of the tones, and this is reached from signaling and event callbacks that
 * must not stall.
 *
 * The tone is written to the ES8311 directly, which is only safe while
 * av_render has not yet opened that codec for call audio. Skipping once
 * connected is what keeps the two off each other. */
static void announce_thread(void *arg)
{
    if (s_state != CALL_STATE_CONNECTED) {
        esp_err_t ret = board_play_announce_tone();
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Announcement tone failed: %s", esp_err_to_name(ret));
        }
    }
    s_tone_busy = false;
    media_lib_thread_destroy(NULL);
}

/* s_announced makes the announcement once per call, whichever of RING or
 * CONNECTING arrives first; s_tone_busy stops a second thread being spawned
 * while the first is still writing to the codec. */
static void announce_incoming_call(void)
{
    if (s_announced || s_tone_busy) {
        return;
    }
    /* Earliest point at which a call is known to be coming — RING, or a
     * caller that went straight to an offer. The situational-awareness
     * uplink has the capture graph running from boot, and esp_capture will
     * not let esp_webrtc set its own sink up on a started graph: the call
     * would come up with no audio from this unit. So the camera is handed
     * back here, before the peer connection is enabled.
     *
     * Normally the offer has already triggered this from the signaling
     * layer, which is earlier and therefore the one that matters; this
     * covers a caller that rings before offering. Idempotent. */
    mode_arbiter_enter_call();

    s_announced = true;
    s_tone_busy = true;
    if (s_state < CALL_STATE_CONNECTING) {
        set_state(CALL_STATE_ANNOUNCING);
    }
    ESP_LOGI(TAG, "Incoming call, announcing");

    media_lib_thread_handle_t thread = NULL;
    if (media_lib_thread_create_from_scheduler(&thread, "announce",
                                               announce_thread, NULL) != 0) {
        ESP_LOGW(TAG, "Could not start the announcement thread");
        s_tone_busy = false;
    }
}

static int on_custom_data(esp_webrtc_custom_data_via_t via, uint8_t *data,
                          int size, void *ctx)
{
    if (size == 0 || s_webrtc == NULL) {
        return 0;
    }
    ESP_LOGI(TAG, "Command from peer: %.*s", size, (char *)data);

    if (SAME_CMD(data, size, CMD_RING)) {
        announce_incoming_call();
        /* Answer immediately. There is no accept step and no ringing
         * timeout to expire, so the caller is told the call is taken as
         * soon as it is announced. */
        esp_webrtc_send_custom_data(s_webrtc, ESP_WEBRTC_CUSTOM_DATA_VIA_SIGNALING,
                                    (uint8_t *)CMD_ACCEPTED,
                                    strlen(CMD_ACCEPTED));
    } else if (SAME_CMD(data, size, CMD_DENIED)) {
        ESP_LOGI(TAG, "Caller hung up");
        esp_webrtc_enable_peer_connection(s_webrtc, false);
        set_state(CALL_STATE_IDLE);
        /* Normally DISCONNECTED follows and sets this too; flagged here as
         * well for the case where disabling the peer connection is the end
         * of it and no event arrives. Acting on it twice is a no-op. */
        s_restart_pending = true;
    }
    return 0;
}

static int on_webrtc_event(esp_webrtc_event_t *event, void *ctx)
{
    switch (event->type) {
    case ESP_WEBRTC_EVENT_CONNECTING:
        /* Covers callers that post an offer without sending RING first. */
        announce_incoming_call();
        set_state(CALL_STATE_CONNECTING);
        board_status_print("Call: connecting");
        break;
    case ESP_WEBRTC_EVENT_CONNECTED:
        ESP_LOGI(TAG, "Call connected");
        set_state(CALL_STATE_CONNECTED);
        board_status_print("Call: connected");
        /* Hold the uplink's pause for the length of the call; without this
         * it would decide the call had stalled and take the camera back. */
        video_stream_call_connected();
        break;
    case ESP_WEBRTC_EVENT_DATA_CHANNEL_OPENED:
        /* First MJPEG frames follow on this channel — release the status
         * overlay so av_render can own the panel. */
        board_status_print("Video: starting");
        board_status_stop();
        break;
    case ESP_WEBRTC_EVENT_CONNECT_FAILED:
    case ESP_WEBRTC_EVENT_DISCONNECTED:
        ESP_LOGI(TAG, "Call ended");
        set_state(CALL_STATE_IDLE);
        /* Hands the codecs, the panel and the camera back to the sentinel.
         * esp_webrtc has closed its own codecs by the time this event is
         * delivered, which is what lets the microphone be reopened at the
         * sentinel's 16 kHz rather than the call's 8 kHz. */
        mode_arbiter_end_call();
        /* call_hangup() and a peer's DENY_CALL both disable the peer
         * connection to tear the call down; nothing else turns it back on.
         * Left disabled, the unit would announce and auto-ACCEPT the next
         * RING (on_custom_data doesn't check pc state) but never actually
         * negotiate it. Re-enable here so it is ready for the next offer.
         *
         * Necessary but, on its own, not sufficient — see s_restart_pending.
         * Kept because the rebuild is a poll interval away and this is what
         * the unit runs on until then. */
        esp_webrtc_enable_peer_connection(s_webrtc, true);
        s_restart_pending = true;
        if (!board_status_active()) {
            board_status_begin();
            board_status_print("Ready: waiting call");
        } else {
            board_status_print("Call: ended");
        }
        break;
    default:
        break;
    }
    return 0;
}

esp_err_t call_service_start(const char *room)
{
    if (!network_is_connected()) {
        ESP_LOGE(TAG, "No network yet");
        return ESP_ERR_INVALID_STATE;
    }
    if (s_webrtc) {
        call_service_stop();
    }

    const app_config_t *app = app_config_get();
    snprintf(s_room, sizeof(s_room), "%s", room);
    s_signaling_cfg = (mqtt_signaling_cfg_t){
        .broker_uri = app->mqtt_uri,
        .username = app->mqtt_user,
        .password = app->mqtt_pass,
        .room = s_room,
        .stun_url = app->stun_url,
        .turn_user = app->turn_user,
        .turn_password = app->turn_pass,
        .ca_cert = ca_crt_start,
        .client_cert = hlias_crt_start,
        .client_key = hlias_key_start,
    };

    s_peer_default_cfg = (esp_peer_default_cfg_t){
        .data_ch_cfg = {
            .send_cache_size = DATA_CH_SEND_CACHE_SIZE,
            .recv_cache_size = DATA_CH_RECV_CACHE_SIZE,
        },
        /* Stay ICE-controlled after hangup; otherwise a retained MQTT bye
         * flips the agent to Controlling and it emits a fake "answer". */
        .keep_role = true,
        /* Browser callers trickle many host/srflx/relay (+IPv6) candidates;
         * the default remote limit (~10) drops the usable LAN ones. */
        .max_candidates = 40,
        .agent_recv_timeout = 500,
    };

    esp_webrtc_cfg_t cfg = {
        .peer_cfg = {
            .audio_info = {
                .codec = ESP_PEER_AUDIO_CODEC_G711A,
                .sample_rate = AUDIO_SAMPLE_RATE,
                .channel = AUDIO_CHANNELS,
            },
            .video_info = {
                .codec = ESP_PEER_VIDEO_CODEC_MJPEG,
                .width = VIDEO_WIDTH,
                .height = VIDEO_HEIGHT,
                .fps = VIDEO_FPS,
            },
            /* Duplex audio; video inbound only unless VIDEO_SEND_ENABLED. */
            .audio_dir = ESP_PEER_MEDIA_DIR_SEND_RECV,
            .video_dir = VIDEO_SEND_ENABLED ? ESP_PEER_MEDIA_DIR_SEND_RECV
                                            : ESP_PEER_MEDIA_DIR_RECV_ONLY,
            .capture_width = VIDEO_SEND_ENABLED ? VIDEO_SEND_WIDTH : 0,
            .capture_height = VIDEO_SEND_ENABLED ? VIDEO_SEND_HEIGHT : 0,
            .capture_fps = VIDEO_SEND_ENABLED ? VIDEO_SEND_FPS : 0,
            .on_custom_data = on_custom_data,
            .enable_data_channel = DATA_CHANNEL_ENABLED,
            .video_over_data_channel = VIDEO_OVER_DATA_CHANNEL,
            /* Left false, unlike upstream: this is what makes the call
             * answer itself instead of waiting for a button. */
            .no_auto_reconnect = false,
            .extra_cfg = &s_peer_default_cfg,
            .extra_size = sizeof(s_peer_default_cfg),
        },
        .signaling_cfg = {
            .signal_url = s_room,
            .extra_cfg = &s_signaling_cfg,
            .extra_size = sizeof(s_signaling_cfg),
        },
        .peer_impl = esp_peer_get_default_impl(),
        .signaling_impl = esp_signaling_get_mqtt_impl(),
    };

    int ret = esp_webrtc_open(&cfg, &s_webrtc);
    if (ret != 0) {
        ESP_LOGE(TAG, "esp_webrtc_open failed (%d)", ret);
        return ESP_FAIL;
    }

    esp_webrtc_media_provider_t provider = { 0 };
    if (media_sys_get_provider(&provider) != ESP_OK) {
        ESP_LOGE(TAG, "Media graphs are not built");
        call_service_stop();
        return ESP_ERR_INVALID_STATE;
    }
    esp_webrtc_set_media_provider(s_webrtc, &provider);

    /* The capture graph belongs to the situational-awareness uplink, which
     * has it running from boot; this call is only one sink path within it.
     * Left on its default, esp_webrtc starts and stops the whole graph around
     * each call, which is why the OV5647 feed went blank for the duration of
     * every call and came back on hangup -- precisely the wrong behaviour for
     * a unit whose job is watching a room you are talking into.
     *
     * esp_capture supports parallel sinks; what it refuses is configuring a
     * new sink on an already-started graph, which is what forced the old
     * release-the-camera dance. With auto capture off, esp_webrtc stops
     * taking the graph over and both sinks simply keep running. */
    esp_webrtc_set_no_auto_capture(s_webrtc, true);

    esp_webrtc_set_event_handler(s_webrtc, on_webrtc_event, NULL);

    ret = esp_webrtc_start(s_webrtc);
    if (ret != 0) {
        ESP_LOGE(TAG, "esp_webrtc_start failed (%d)", ret);
        call_service_stop();
        return ESP_FAIL;
    }
    set_state(CALL_STATE_IDLE);
    ESP_LOGI(TAG, "Waiting for calls in room %s", s_room);
    return ESP_OK;
}

void call_service_poll(void)
{
    /* Only ever between calls: rebuilding under a live call would drop it,
     * and s_state is back to IDLE before the event that set the flag
     * returns. */
    if (s_restart_pending && s_state == CALL_STATE_IDLE) {
        s_restart_pending = false;
        ESP_LOGI(TAG, "Rebuilding the call session so the next offer answers");

        /* call_service_start() writes through to s_room, so it cannot be
         * handed s_room itself — that would be an overlapping copy. */
        char room[sizeof(s_room)];
        snprintf(room, sizeof(room), "%s", s_room);

        call_service_stop();
        /* ICE UDP sockets, HTTP ingest, and the old signaling client all
         * call getaddrinfo. Rebuilding immediately was starving DNS so
         * snippets printed "No MQTT!" after the call. */
        vTaskDelay(pdMS_TO_TICKS(2000));
        if (call_service_start(room) != ESP_OK) {
            /* Most likely the network went down with the call. Retried on the
             * next poll; if the link is really gone, main.c's network
             * callback takes the service down and owns the restart. */
            ESP_LOGW(TAG, "Session rebuild failed, retrying");
            s_restart_pending = true;
        }
        return;
    }
    if (s_webrtc) {
        if (s_state == CALL_STATE_CONNECTING && s_connecting_us != 0 &&
            (esp_timer_get_time() - s_connecting_us) > 25LL * 1000 * 1000) {
            ESP_LOGW(TAG, "Call did not connect; returning the panel");
            call_hangup();
            s_restart_pending = true;
            return;
        }
        esp_webrtc_query(s_webrtc);
    }
}

void call_hangup(void)
{
    if (s_webrtc == NULL || s_state == CALL_STATE_IDLE) {
        return;
    }
    ESP_LOGI(TAG, "Hanging up");
    esp_webrtc_send_custom_data(s_webrtc, ESP_WEBRTC_CUSTOM_DATA_VIA_SIGNALING,
                                (uint8_t *)CMD_DENIED, strlen(CMD_DENIED));
    esp_webrtc_enable_peer_connection(s_webrtc, false);
    set_state(CALL_STATE_IDLE);
    /* Disabling the peer connection does not always produce a DISCONNECTED
     * event, so the hardware is handed back from here as well; both paths
     * are idempotent. */
    mode_arbiter_end_call();
}

esp_err_t call_service_stop(void)
{
    if (s_webrtc == NULL) {
        return ESP_OK;
    }
    esp_webrtc_handle_t handle = s_webrtc;
    s_webrtc = NULL;
    set_state(CALL_STATE_IDLE);
    esp_webrtc_close(handle);
    mode_arbiter_end_call();
    return ESP_OK;
}
