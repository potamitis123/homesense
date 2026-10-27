/*
 * audiosense_mqtt.c
 *
 * Changes vs. previous version
 * ─────────────────────────────
 *  FIX 1 – mqtt_connect() called esp_mqtt_client_start() on an already-
 *           running client after the first recording kept the connection
 *           alive.  ... (unchanged)
 *
 *  FIX 2 – mqtt_connect() called esp_mqtt_client_stop() on a 15-second
 *           connect timeout.  ... (unchanged)
 *
 *  FIX 5 – audiosense_mqtt_init() now starts the MQTT client immediately
 *           instead of deferring start to the first publish.  Without
 *           this, the device never subscribes to commands/<id>/stream/#
 *           or commands/<id>/play/# until *after* it has spoken once,
 *           so any "play" message sent before the first SAD trigger is
 *           silently dropped at the broker (no subscriber, QoS 0).
 *
 *           This also makes loudspeaker playback survive the TLS
 *           disconnect storm: subscriptions are re-registered in the
 *           CONNECTED handler on every reconnect, with no dependency on
 *           the publish path.
 */

#include "audiosense_mqtt.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "esp_log.h"
#include "mqtt_client.h"

static const char *TAG = "MQTT";

/* ── TLS certificates ────────────────────────────────────────────── */
extern const uint8_t ca_crt_start[]    asm("_binary_ca_crt_start");
extern const uint8_t ca_crt_end[]      asm("_binary_ca_crt_end");
extern const uint8_t hlias_crt_start[] asm("_binary_hlias_crt_start");
extern const uint8_t hlias_crt_end[]   asm("_binary_hlias_crt_end");
extern const uint8_t hlias_key_start[] asm("_binary_hlias_key_start");
extern const uint8_t hlias_key_end[]   asm("_binary_hlias_key_end");

/* ── Core state ──────────────────────────────────────────────────── */
#define MQTT_CONNECTED_BIT  BIT1

static esp_mqtt_client_handle_t s_client             = NULL;
static EventGroupHandle_t       s_event_group        = NULL;
static bool                     s_initialized        = false;
static bool                     s_client_ever_started = false; /* FIX 1 */
static char                     s_client_id[32]      = "box3-unknown";
static char                     s_device_ip[32]      = "";
static char                     s_status_topic[96]   = "";  /* devices/<id>/status */

/* ── Legacy SD-play state ─────────────────────────────────────────── */
static audiosense_mqtt_play_cb_t  s_play_cb         = NULL;
static FILE                      *s_play_file        = NULL;
static bool                       s_play_in_progress = false;

/* ── Live streaming receive state ────────────────────────────────── */
static mqtt_play_stream_start_fn  s_stream_start_cb   = NULL;
static mqtt_play_stream_chunk_fn  s_stream_chunk_cb   = NULL;
static mqtt_play_stream_end_fn    s_stream_end_cb     = NULL;
static bool                       s_stream_play_active = false;

/* ── Fragmented-payload routing ──────────────────────────────────── *
 * esp-mqtt splits any message larger than buffer.size across several
 * MQTT_EVENT_DATA events. Only the FIRST carries ev->topic and has
 * current_data_offset == 0; every continuation has topic == NULL, so
 * the topic-matching below cannot classify it.
 *
 * This matters for the talk-back path specifically. The device's own
 * uploads aggregate 4 DMA frames (4 × 2048 = 8192 B) per chunk, and an
 * app that mirrors that chunk size sends 8192 B here — larger than
 * buffer.size, hence always fragmented. Discarding the continuations
 * silently truncated every incoming chunk, which is not audible as
 * "quieter" but as torn PCM.
 *
 * Remembering where the first fragment was routed lets the remainder
 * follow it.
 * ─────────────────────────────────────────────────────────────────── */
typedef enum {
    FRAG_NONE = 0,
    FRAG_PLAY_CHUNK,      /* → SD staging file  */
    FRAG_STREAM_CHUNK,    /* → loudspeaker queue */
} frag_route_t;

static frag_route_t s_frag_route = FRAG_NONE;

/* ── Live streaming transmit state ───────────────────────────────── */
static char s_tx_device_id[32] = "";

/* ── Live Audio Mode state ───────────────────────────────────────── *
 * s_live_topic is devices/<id>/live, built once at init alongside
 * s_status_topic so the publish paths never rebuild it.
 * ─────────────────────────────────────────────────────────────────── */
static audiosense_mqtt_live_cmd_cb_t s_live_cmd_cb  = NULL;
static char s_live_topic[96]     = "";
static char s_live_device_id[32] = "";

/*
 * Last live state we wanted the world to see. Republished from the
 * CONNECTED handler so that a stop (or start) which happened while the
 * broker was unreachable is reconciled on reconnect, rather than
 * leaving a stale retained {"live":true} on a device that is no longer
 * streaming.
 */
static bool s_live_state = false;

/* Declared early (definition further down) so set_device_ip() can
 * use it to decide whether to publish immediately. */
static bool audiosense_mqtt_is_connected_impl(void)
{
    return s_event_group &&
           (xEventGroupGetBits(s_event_group) & MQTT_CONNECTED_BIT);
}

/* ══════════════════════════ API SETTERS ════════════════════════════ */

void audiosense_mqtt_set_client_id(const char *id)
{
    strncpy(s_client_id, id, sizeof(s_client_id) - 1);
    s_client_id[sizeof(s_client_id) - 1] = '\0';
}

/*
 * Called from wifi_event_handler() on EVERY IP_EVENT_STA_GOT_IP —
 * not just once at boot. This matters for two real scenarios:
 *
 *  1. A mid-session WiFi bounce that hands out a *different* DHCP
 *     lease than the device had before. The old retained
 *     devices/<id>/ip value would otherwise sit on the broker
 *     forever (retain=true), sending any HTTP-push control-plane to
 *     a dead address.
 *
 *  2. A mass reconnect (e.g. every sentinel + the router all coming
 *     back from the same power outage at once) where DHCP/WiFi
 *     association for a given device takes longer than main.c's
 *     boot-time wait window. Since this is event-driven rather than
 *     a one-shot check inside that wait loop, it fires correctly no
 *     matter how late the IP actually arrives.
 *
 * If MQTT is already connected (i.e. this is a live IP change, not
 * the very first connect of the session), publish the retained
 * topic immediately instead of waiting for the next MQTT-level
 * reconnect, which may not happen again for a long time.
 */
void audiosense_mqtt_set_device_ip(const char *ip)
{
    strncpy(s_device_ip, ip, sizeof(s_device_ip) - 1);
    s_device_ip[sizeof(s_device_ip) - 1] = '\0';

    if (s_client && s_device_ip[0] && audiosense_mqtt_is_connected_impl()) {
        char t[96];
        snprintf(t, sizeof(t), "%s/%s/ip",
                 MQTT_DEVICE_IP_TOPIC_BASE, s_client_id);
        esp_mqtt_client_publish(s_client, t,
                                s_device_ip, strlen(s_device_ip),
                                1, 1);
        ESP_LOGI(TAG, "IP (live update): %s → %s", t, s_device_ip);
    }
}

void audiosense_mqtt_set_play_callback(audiosense_mqtt_play_cb_t cb)
{
    s_play_cb = cb;
}

void audiosense_mqtt_set_play_stream_callbacks(
         mqtt_play_stream_start_fn on_start,
         mqtt_play_stream_chunk_fn on_chunk,
         mqtt_play_stream_end_fn   on_end)
{
    s_stream_start_cb = on_start;
    s_stream_chunk_cb = on_chunk;
    s_stream_end_cb   = on_end;
}

void audiosense_mqtt_set_live_cmd_callback(audiosense_mqtt_live_cmd_cb_t cb)
{
    s_live_cmd_cb = cb;
}

/* ══════════════════════════ JSON HELPER ════════════════════════════ */

static int json_get_int(const char *data, int data_len,
                         const char *key, int def_val)
{
    char tmp[256];
    int  n = (data_len < (int)sizeof(tmp) - 1)
             ? data_len : (int)sizeof(tmp) - 1;
    memcpy(tmp, data, n);
    tmp[n] = '\0';

    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\":", key);
    char *p = strstr(tmp, pat);
    if (!p) {
        snprintf(pat, sizeof(pat), "\"%s\" :", key);
        p = strstr(tmp, pat);
        if (!p) return def_val;
    }

    p = strchr(p, ':');
    if (!p) return def_val;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    return atoi(p);
}

/*
 * Open an incoming talk-back session. Called from the info message
 * (preferred) or inferred from the first chunk when the app omits info
 * – which is what the current push-to-talk client does.
 */
static void stream_rx_arm(int sr, int bps, int ch, bool from_info)
{
    if (s_stream_play_active) return;

    s_stream_play_active = true;
    ESP_LOGI(TAG, "[stream_rx] start %dHz %dbit ch%d%s",
             sr, bps, ch,
             from_info ? "" : " (inferred – app sent no info)");
    if (s_stream_start_cb) s_stream_start_cb(sr, bps, ch);
}

static void stream_rx_end(void)
{
    if (!s_stream_play_active) return;

    s_stream_play_active = false;
    ESP_LOGI(TAG, "[stream_rx] complete");
    if (s_stream_end_cb) s_stream_end_cb();
}

/* ══════════════════════════ EVENT HANDLER ══════════════════════════ */

static void mqtt_event_handler(void *arg, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t ev = event_data;

    switch ((esp_mqtt_event_id_t)event_id) {

    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT_EVENT_CONNECTED");
        xEventGroupSetBits(s_event_group, MQTT_CONNECTED_BIT);

        {
            char t[128];
            snprintf(t, sizeof(t), "%s/%s/play/#",
                     MQTT_PLAY_TOPIC_BASE, s_client_id);
            esp_mqtt_client_subscribe(ev->client, t, 1);
            ESP_LOGI(TAG, "sub: %s", t);
        }

        {
            char t[128];
            snprintf(t, sizeof(t), "%s/%s/stream/#",
                     MQTT_PLAY_TOPIC_BASE, s_client_id);
            esp_mqtt_client_subscribe(ev->client, t, 0);
            ESP_LOGI(TAG, "sub: %s", t);
        }

        /*
         * Live Audio Mode control. QoS 1: unlike an audio chunk, a lost
         * start/stop command leaves the device in the wrong mode with no
         * self-correction, so it must not be fire-and-forget.
         *
         * Re-subscribed on every reconnect along with the others, so a
         * command issued while the device was briefly offline is picked
         * up as soon as the broker redelivers it.
         */
        {
            char t[128];
            snprintf(t, sizeof(t), "%s/%s/control",
                     MQTT_PLAY_TOPIC_BASE, s_client_id);
            esp_mqtt_client_subscribe(ev->client, t, 1);
            ESP_LOGI(TAG, "sub: %s", t);
        }

        if (s_device_ip[0]) {
            char t[96];
            snprintf(t, sizeof(t), "%s/%s/ip",
                     MQTT_DEVICE_IP_TOPIC_BASE, s_client_id);
            esp_mqtt_client_publish(ev->client, t,
                                    s_device_ip, strlen(s_device_ip),
                                    1, 1);
            ESP_LOGI(TAG, "IP: %s → %s", t, s_device_ip);
        }

        /* ── Birth message ─────────────────────────────────────
         * Retained, QoS 1, mirrors the LWT topic/payload shape.
         * Published on every connect *and* every reconnect, so a
         * server watching the retained value always converges on
         * "online" shortly after we come back up, and on "offline"
         * (via the broker-delivered LWT) the moment we drop without
         * a clean disconnect.
         * ──────────────────────────────────────────────────────── */
        if (s_status_topic[0]) {
            esp_mqtt_client_publish(ev->client, s_status_topic,
                                    MQTT_STATUS_PAYLOAD_ONLINE,
                                    strlen(MQTT_STATUS_PAYLOAD_ONLINE),
                                    1, 1);
            ESP_LOGI(TAG, "birth: %s → online", s_status_topic);
        }

        /* Reconcile the retained live flag with reality after any
         * reconnect – see s_live_state. */
        if (s_live_topic[0]) {
            const char *lp = s_live_state ? MQTT_LIVE_PAYLOAD_ON
                                          : MQTT_LIVE_PAYLOAD_OFF;
            esp_mqtt_client_publish(ev->client, s_live_topic,
                                    lp, strlen(lp), 1, 1);
            ESP_LOGI(TAG, "live state: %s → %s", s_live_topic, lp);
        }
        break;

    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT_EVENT_DISCONNECTED");
        xEventGroupClearBits(s_event_group, MQTT_CONNECTED_BIT);
        if (s_play_file) { fclose(s_play_file); s_play_file = NULL; }
        s_play_in_progress   = false;
        s_stream_play_active = false;

        if (s_client_ever_started) {
            ESP_LOGW(TAG, "MQTT_EVENT_DISCONNECTED – reconnecting...");
            esp_mqtt_client_reconnect(ev->client);
        }
        break;

    case MQTT_EVENT_DATA:
    {
        /* Continuation of a fragmented payload: no topic to match, so
         * hand it to whatever the first fragment was routed to. */
        if (ev->current_data_offset != 0) {
            switch (s_frag_route) {
            case FRAG_PLAY_CHUNK:
                if (s_play_file && ev->data_len > 0)
                    fwrite(ev->data, 1, ev->data_len, s_play_file);
                break;
            case FRAG_STREAM_CHUNK:
                if (ev->data_len > 0 && s_stream_chunk_cb)
                    s_stream_chunk_cb(ev->data, ev->data_len);
                break;
            default:
                ESP_LOGW(TAG,
                    "[rx] orphan fragment  offset=%d len=%d – dropped",
                    ev->current_data_offset, ev->data_len);
                break;
            }
            break;
        }

        s_frag_route = FRAG_NONE;

        if (!ev->topic) break;

        char topic[128];
        {
            int tl = (ev->topic_len < (int)sizeof(topic) - 1)
                     ? ev->topic_len : (int)sizeof(topic) - 1;
            memcpy(topic, ev->topic, tl);
            topic[tl] = '\0';
        }

        /* ── Live Audio Mode control  commands/<id>/control  ──
         *
         * Checked first: it is an exact topic match (cheap), and both
         * handlers below consume the event unconditionally once their
         * prefix test runs, so anything placed after them is
         * unreachable.
         *
         * The command is matched as a substring so that a bare
         * "start_live_audio" payload and a JSON-wrapped
         * {"command":"start_live_audio"} both work without a JSON
         * parser. stop is tested first because neither command string
         * is a substring of the other, but a hypothetical payload
         * naming both should be read as "stop" — the safe direction for
         * a device that streams audio out of someone's home.
         */
        {
            char ctl[96];
            snprintf(ctl, sizeof(ctl), "%s/%s/control",
                     MQTT_PLAY_TOPIC_BASE, s_client_id);
            if (strcmp(topic, ctl) == 0) {
                char body[128];
                int  bl = (ev->data_len < (int)sizeof(body) - 1)
                          ? ev->data_len : (int)sizeof(body) - 1;
                if (bl > 0) memcpy(body, ev->data, bl);
                body[bl > 0 ? bl : 0] = '\0';

                if (strstr(body, MQTT_CMD_STOP_LIVE)) {
                    ESP_LOGI(TAG, "[control] %s", MQTT_CMD_STOP_LIVE);
                    /*
                     * The app often sends stop_live_audio without a
                     * matching stream/complete, so tear down talk-back
                     * here as well as releasing live TX.
                     */
                    stream_rx_end();
                    if (s_live_cmd_cb) s_live_cmd_cb(false);
                } else if (strstr(body, MQTT_CMD_START_LIVE)) {
                    ESP_LOGI(TAG, "[control] %s", MQTT_CMD_START_LIVE);
                    if (s_live_cmd_cb) s_live_cmd_cb(true);
                } else {
                    ESP_LOGW(TAG, "[control] unrecognised command: \"%s\"",
                             body);
                }
                break;
            }
        }

        /* ── Legacy  commands/<id>/play/#  ── */
        {
            char pfx[96];
            snprintf(pfx, sizeof(pfx), "%s/%s/play/",
                     MQTT_PLAY_TOPIC_BASE, s_client_id);
            if (strncmp(topic, pfx, strlen(pfx)) == 0) {
                const char *sub = topic + strlen(pfx);

                if (!strcmp(sub, "info")) {
                    if (s_play_file) fclose(s_play_file);
                    s_play_file = fopen(MQTT_PLAY_WAV_PATH, "wb");
                    if (!s_play_file) {
                        ESP_LOGE(TAG, "[play] cannot open %s",
                                 MQTT_PLAY_WAV_PATH);
                        break;
                    }
                    s_play_in_progress = true;
                    ESP_LOGI(TAG, "[play] receiving WAV → %s",
                             MQTT_PLAY_WAV_PATH);

                } else if (!strncmp(sub, "chunk/", 6) && s_play_in_progress) {
                    s_frag_route = FRAG_PLAY_CHUNK;
                    if (s_play_file && ev->data_len > 0)
                        fwrite(ev->data, 1, ev->data_len, s_play_file);

                } else if (!strcmp(sub, "complete") && s_play_in_progress) {
                    if (s_play_file) {
                        fclose(s_play_file);
                        s_play_file = NULL;
                    }
                    s_play_in_progress = false;
                    ESP_LOGI(TAG, "[play] WAV complete – triggering playback");
                    if (s_play_cb) s_play_cb();
                }
                break;
            }
        }

        /* ── Live stream  commands/<id>/stream/#  ── */
        {
            char pfx[96];
            snprintf(pfx, sizeof(pfx), "%s/%s/stream/",
                     MQTT_PLAY_TOPIC_BASE, s_client_id);
            if (strncmp(topic, pfx, strlen(pfx)) != 0) {
                /*
                 * A message arrived on something we subscribed to but
                 * cannot classify. Silence here made a mis-addressed
                 * publish (wrong device id, wrong topic root) look
                 * exactly like no publish at all, so say so.
                 */
                ESP_LOGW(TAG, "[rx] unhandled topic \"%s\" (%d B)",
                         topic, ev->total_data_len);
                break;
            }
            const char *sub = topic + strlen(pfx);

            if (!strcmp(sub, "info")) {
                int sr  = json_get_int(ev->data, ev->data_len,
                                       "sample_rate",    16000);
                int bps = json_get_int(ev->data, ev->data_len,
                                       "bits_per_sample",   16);
                int ch  = json_get_int(ev->data, ev->data_len,
                                       "channels",            1);
                stream_rx_arm(sr, bps, ch, true);

            } else if (!strncmp(sub, "chunk/", 6)) {
                if (!s_stream_play_active) {
                    /*
                     * Push-to-talk currently publishes 960-byte PCM
                     * chunks with no preceding info message. Opening a
                     * session on the first chunk keeps the loudspeaker
                     * path alive and lets main.c start live TX.
                     */
                    stream_rx_arm(16000, 16, 1, false);
                }
                if (!s_stream_play_active) break;

                s_frag_route = FRAG_STREAM_CHUNK;
                if (ev->data_len < ev->total_data_len)
                    ESP_LOGD(TAG,
                        "[stream_rx] fragmented chunk %d/%d B",
                        ev->data_len, ev->total_data_len);
                if (ev->data_len > 0 && s_stream_chunk_cb)
                    s_stream_chunk_cb(ev->data, ev->data_len);

            } else if (!strcmp(sub, "complete")) {
                stream_rx_end();
            }
        }
        break;
    }

    case MQTT_EVENT_PUBLISHED:
        ESP_LOGD(TAG, "PUBLISHED msg_id=%d", ev->msg_id);
        break;

    case MQTT_EVENT_ERROR:
        ESP_LOGE(TAG, "MQTT_EVENT_ERROR");
        if (ev->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT)
            ESP_LOGE(TAG, "  tls 0x%x  stack 0x%x  errno %d",
                     ev->error_handle->esp_tls_last_esp_err,
                     ev->error_handle->esp_tls_stack_err,
                     ev->error_handle->esp_transport_sock_errno);
        break;

    default: break;
    }
}

/* ══════════════════════════ INIT ════════════════════════════════════ */

bool audiosense_mqtt_init(void)
{
    s_event_group = xEventGroupCreate();

    ESP_LOGI(TAG, "CA:%dB  cert:%dB  key:%dB",
             (int)(ca_crt_end    - ca_crt_start),
             (int)(hlias_crt_end - hlias_crt_start),
             (int)(hlias_key_end - hlias_key_start));

    /* Built once, before client init, so cfg.session.last_will.topic
     * can point at it. s_client_id must already be set – callers are
     * required to call audiosense_mqtt_set_client_id() first. */
    snprintf(s_status_topic, sizeof(s_status_topic), "%s/%s/status",
             MQTT_DEVICE_IP_TOPIC_BASE, s_client_id);

    /* Live Audio Mode state topic, built here for the same reason: the
     * publish paths run per-session and should not re-derive it. */
    snprintf(s_live_topic, sizeof(s_live_topic), "%s/%s/live",
             MQTT_DEVICE_IP_TOPIC_BASE, s_client_id);

    esp_mqtt_client_config_t cfg = {
        .broker.address.hostname  = MQTT_BROKER_HOST,
        .broker.address.port      = MQTT_BROKER_PORT,
        .broker.address.transport = MQTT_TRANSPORT_OVER_SSL,
        .broker.verification.certificate = (const char *)ca_crt_start,

        .credentials.client_id = s_client_id,
        .credentials.username  = MQTT_USER,
        .credentials.authentication.password    = MQTT_PASS,
        .credentials.authentication.certificate = (const char *)hlias_crt_start,
        .credentials.authentication.key         = (const char *)hlias_key_start,

        /* ── LWT: broker publishes this, retained, if we vanish
         * without a clean DISCONNECT (crash, WiFi loss, power cut).
         * Paired with the "online" birth message published in
         * MQTT_EVENT_CONNECTED above. ──────────────────────────── */
        .session.last_will.topic   = s_status_topic,
        .session.last_will.msg     = MQTT_STATUS_PAYLOAD_OFFLINE,
        .session.last_will.msg_len = 0, /* 0 = use strlen(msg) */
        .session.last_will.qos     = 1,
        .session.last_will.retain  = true,

        .network.disable_auto_reconnect = true,
        .network.timeout_ms             = 30000,
        .buffer.size     = 6144,
        .buffer.out_size = 6144,
    };

    s_client = esp_mqtt_client_init(&cfg);
    if (!s_client) { ESP_LOGE(TAG, "init failed"); return false; }

    ESP_ERROR_CHECK(esp_mqtt_client_register_event(
        s_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL));

    s_initialized = true;

    /* ────────────────────────── FIX 5 ──────────────────────────
     * Start the client immediately so the device subscribes to
     * commands/<id>/play/# and commands/<id>/stream/# at boot.
     * Without this, RX (loudspeaker playback from MQTT) does not
     * work until *after* the first SAD trigger, because the client
     * task is otherwise never started and no CONNECTED event ever
     * fires to register the subscriptions.
     * ──────────────────────────────────────────────────────────── */
    if (esp_mqtt_client_start(s_client) != ESP_OK) {
        ESP_LOGE(TAG, "client start failed at init");
        return false;
    }
    s_client_ever_started = true;

    ESP_LOGI(TAG, "MQTT client started (subscriptions register on connect)");
    return true;
}

bool audiosense_mqtt_is_connected(void)
{
    return audiosense_mqtt_is_connected_impl();
}

static bool mqtt_connect(void)
{
    if (audiosense_mqtt_is_connected()) return true;

    xEventGroupClearBits(s_event_group, MQTT_CONNECTED_BIT);

    if (!s_client_ever_started) {
        if (esp_mqtt_client_start(s_client) != ESP_OK) {
            ESP_LOGE(TAG, "[mqtt] client start failed");
            return false;
        }
        s_client_ever_started = true;
        ESP_LOGI(TAG, "[mqtt] client started");
    } else {
        esp_mqtt_client_reconnect(s_client);
        ESP_LOGI(TAG, "[mqtt] reconnect triggered");
    }

    EventBits_t b = xEventGroupWaitBits(s_event_group, MQTT_CONNECTED_BIT,
                        pdFALSE, pdFALSE, pdMS_TO_TICKS(15000));
    if (b & MQTT_CONNECTED_BIT) {
        ESP_LOGI(TAG, "[mqtt] connected");
        return true;
    }

    ESP_LOGW(TAG, "[mqtt] connect timeout – will retry on next trigger");
    return false;
}

static void mqtt_disconnect(void)
{
    esp_mqtt_client_stop(s_client);
    s_client_ever_started = false;
    xEventGroupClearBits(s_event_group, MQTT_CONNECTED_BIT);
    vTaskDelay(pdMS_TO_TICKS(200));
}

/* ══════════════════════════ LEGACY FILE PUBLISH ════════════════════ */

bool audiosense_mqtt_publish_file(const char *filepath,
                                   const char *device_id,
                                   int sample_rate,
                                   int bits_per_sample,
                                   int duration_sec)
{
    if (!s_initialized) { ESP_LOGW(TAG, "Not initialized"); return false; }
    if (!mqtt_connect()) return false;

    FILE *f = fopen(filepath, "rb");
    if (!f) {
        ESP_LOGE(TAG, "open %s", filepath);
        mqtt_disconnect();
        return false;
    }

    fseek(f, 0, SEEK_END);
    long total_size   = ftell(f);
    fseek(f, 0, SEEK_SET);
    int  total_chunks = (int)((total_size + MQTT_CHUNK_SIZE - 1) / MQTT_CHUNK_SIZE);

    char topic[128], json[512];

    snprintf(topic, sizeof(topic), "%s/%s/info", MQTT_TOPIC_BASE, device_id);
    snprintf(json,  sizeof(json),
        "{\"total_size\":%ld,\"duration\":%d,\"format\":\"wav\","
        "\"chunks\":%d,\"samplerate\":%d,\"bits\":%d,"
        "\"username\":\"%s\",\"rms_threshold\":0.0,"
        "\"esp_audio_duration\":%d}",
        total_size, duration_sec, total_chunks,
        sample_rate, bits_per_sample, device_id, duration_sec);

    if (esp_mqtt_client_publish(s_client, topic,
                                json, strlen(json), 0, 0) < 0) {
        ESP_LOGE(TAG, "info publish failed");
        fclose(f);
        mqtt_disconnect();
        return false;
    }
    ESP_LOGI(TAG, "info: %ld B  %d chunks  %ds",
             total_size, total_chunks, duration_sec);
    vTaskDelay(pdMS_TO_TICKS(200));

    uint8_t *cbuf = malloc(MQTT_CHUNK_SIZE);
    if (!cbuf) { fclose(f); mqtt_disconnect(); return false; }

    bool ok = true;
    for (int i = 0; i < total_chunks; i++) {
        size_t want = (i == total_chunks - 1)
                      ? (size_t)(total_size - (long)i * MQTT_CHUNK_SIZE)
                      : MQTT_CHUNK_SIZE;
        size_t got = fread(cbuf, 1, want, f);
        if (!got) { ok = false; break; }

        snprintf(topic, sizeof(topic),
                 "%s/%s/chunk/%d", MQTT_TOPIC_BASE, device_id, i);
        if (esp_mqtt_client_publish(s_client, topic,
                                    (const char *)cbuf, got, 0, 0) < 0) {
            ESP_LOGE(TAG, "chunk %d failed", i);
            ok = false;
            break;
        }
        if (i % 10 == 0 || i == total_chunks - 1)
            ESP_LOGI(TAG, "chunk %d/%d", i + 1, total_chunks);
        vTaskDelay(pdMS_TO_TICKS(60));
    }
    free(cbuf);
    fclose(f);

    if (ok) {
        vTaskDelay(pdMS_TO_TICKS(500));
        snprintf(topic, sizeof(topic),
                 "%s/%s/complete", MQTT_TOPIC_BASE, device_id);
        snprintf(json, sizeof(json),
                 "{\"successful_chunks\":%d,\"total_chunks\":%d}",
                 total_chunks, total_chunks);
        esp_mqtt_client_publish(s_client, topic,
                                json, strlen(json), 0, 0);
        ESP_LOGI(TAG, "complete");
        vTaskDelay(pdMS_TO_TICKS(1000));
    } else {
        mqtt_disconnect();
    }
    return ok;
}

/* ══════════════════════════ STREAMING TRANSMIT ═════════════════════ */

bool audiosense_mqtt_stream_start(const char *device_id,
                                   const char *timestamp,
                                   int sample_rate,
                                   int bits_per_sample,
                                   int channels)
{
    if (!s_initialized) {
        ESP_LOGW(TAG, "[stream_tx] not initialized");
        return false;
    }
    if (!mqtt_connect()) {
        ESP_LOGW(TAG, "[stream_tx] no MQTT connection");
        return false;
    }

    strncpy(s_tx_device_id, device_id, sizeof(s_tx_device_id) - 1);
    s_tx_device_id[sizeof(s_tx_device_id) - 1] = '\0';

    char topic[128], json[256];
    snprintf(topic, sizeof(topic),
             "%s/%s/stream/info", MQTT_TOPIC_BASE, device_id);
    snprintf(json, sizeof(json),
             "{\"device_id\":\"%s\",\"timestamp\":\"%s\","
             "\"sample_rate\":%d,\"bits_per_sample\":%d,"
             "\"channels\":%d,\"format\":\"pcm\"}",
             device_id, timestamp,
             sample_rate, bits_per_sample, channels);

    int ret = esp_mqtt_client_publish(s_client, topic,
                                      json, strlen(json), 1, 0);
    if (ret < 0) {
        ESP_LOGE(TAG, "[stream_tx] info publish failed");
        return false;
    }
    ESP_LOGI(TAG, "[stream_tx] started  ts=%s  %dHz %dbit ch%d",
             timestamp, sample_rate, bits_per_sample, channels);
    return true;
}

bool audiosense_mqtt_stream_chunk(const void *data, int len,
                                   uint32_t chunk_idx)
{
    if (!audiosense_mqtt_is_connected()) return false;
    if (!s_tx_device_id[0])             return false;

    char topic[128];
    snprintf(topic, sizeof(topic),
             "%s/%s/stream/chunk/%u",
             MQTT_TOPIC_BASE, s_tx_device_id, (unsigned)chunk_idx);

    return esp_mqtt_client_publish(s_client, topic,
                                   (const char *)data, len, 0, 0) >= 0;
}

bool audiosense_mqtt_stream_end(uint32_t total_chunks, uint32_t total_bytes)
{
    if (!audiosense_mqtt_is_connected()) {
        ESP_LOGW(TAG, "[stream_tx] end – not connected");
        return false;
    }

    char topic[128], json[128];
    snprintf(topic, sizeof(topic),
             "%s/%s/stream/complete", MQTT_TOPIC_BASE, s_tx_device_id);
    snprintf(json, sizeof(json),
             "{\"total_chunks\":%u,\"total_bytes\":%u}",
             (unsigned)total_chunks, (unsigned)total_bytes);

    int ret = esp_mqtt_client_publish(s_client, topic,
                                      json, strlen(json), 1, 0);
    ESP_LOGI(TAG, "[stream_tx] end  chunks=%u  bytes=%u",
             (unsigned)total_chunks, (unsigned)total_bytes);
    return ret >= 0;
}

/* ══════════════════════════ LIVE AUDIO TRANSMIT ════════════════════ */

/*
 * Publish the retained live-state flag. Best effort: if this fails the
 * stream itself is unaffected, only a dashboard's view of it.
 */
static void live_publish_state(bool live)
{
    s_live_state = live;

    if (!s_live_topic[0] || !audiosense_mqtt_is_connected()) return;

    const char *payload = live ? MQTT_LIVE_PAYLOAD_ON
                               : MQTT_LIVE_PAYLOAD_OFF;
    esp_mqtt_client_publish(s_client, s_live_topic,
                            payload, strlen(payload), 1, 1);
    ESP_LOGI(TAG, "[live_tx] state: %s → %s", s_live_topic, payload);
}

bool audiosense_mqtt_live_start(const char *device_id,
                                 const char *timestamp,
                                 int sample_rate,
                                 int channels,
                                 int bitrate,
                                 int frame_ms,
                                 int frames_per_packet)
{
    if (!s_initialized) {
        ESP_LOGW(TAG, "[live_tx] not initialized");
        return false;
    }
    if (!mqtt_connect()) {
        ESP_LOGW(TAG, "[live_tx] no MQTT connection");
        return false;
    }

    strncpy(s_live_device_id, device_id, sizeof(s_live_device_id) - 1);
    s_live_device_id[sizeof(s_live_device_id) - 1] = '\0';

    char topic[128], json[320];
    snprintf(topic, sizeof(topic),
             "%s/%s/live/info", MQTT_TOPIC_BASE, device_id);
    /*
     * frames_per_packet and frame_ms together tell the receiver how much
     * audio one message carries, and "container":"len16" names the
     * uint16-length-prefixed layout documented in live_audio.h, so the
     * desktop side can validate rather than assume.
     */
    snprintf(json, sizeof(json),
             "{\"device_id\":\"%s\",\"timestamp\":\"%s\","
             "\"format\":\"opus\",\"sample_rate\":%d,\"channels\":%d,"
             "\"bitrate\":%d,\"frame_ms\":%d,\"frames_per_packet\":%d,"
             "\"container\":\"len16\"}",
             device_id, timestamp,
             sample_rate, channels, bitrate, frame_ms,
             frames_per_packet);

    int ret = esp_mqtt_client_publish(s_client, topic,
                                      json, strlen(json), 1, 0);
    if (ret < 0) {
        ESP_LOGE(TAG, "[live_tx] info publish failed");
        return false;
    }

    ESP_LOGI(TAG, "[live_tx] started  ts=%s  opus %dHz ch%d %dbps %dms×%d",
             timestamp, sample_rate, channels, bitrate,
             frame_ms, frames_per_packet);

    live_publish_state(true);
    return true;
}

bool audiosense_mqtt_live_packet(const void *data, int len)
{
    if (!audiosense_mqtt_is_connected()) return false;
    if (!s_live_device_id[0] || !data || len <= 0) return false;

    char topic[128];
    snprintf(topic, sizeof(topic),
             "%s/%s/live/opus", MQTT_TOPIC_BASE, s_live_device_id);

    return esp_mqtt_client_publish(s_client, topic,
                                   (const char *)data, len, 0, 0) >= 0;
}

bool audiosense_mqtt_live_end(uint32_t total_packets, uint32_t total_bytes)
{
    /*
     * State is published before the early return so that a stop which
     * lands while the broker is unreachable still clears the retained
     * flag once the connection is back — otherwise a dashboard could
     * show a device as live indefinitely after it stopped.
     */
    live_publish_state(false);

    if (!audiosense_mqtt_is_connected()) {
        ESP_LOGW(TAG, "[live_tx] end – not connected");
        return false;
    }

    char topic[128], json[128];
    snprintf(topic, sizeof(topic),
             "%s/%s/live/complete", MQTT_TOPIC_BASE, s_live_device_id);
    snprintf(json, sizeof(json),
             "{\"total_packets\":%u,\"total_bytes\":%u}",
             (unsigned)total_packets, (unsigned)total_bytes);

    int ret = esp_mqtt_client_publish(s_client, topic,
                                      json, strlen(json), 1, 0);
    ESP_LOGI(TAG, "[live_tx] end  packets=%u  bytes=%u",
             (unsigned)total_packets, (unsigned)total_bytes);
    return ret >= 0;
}