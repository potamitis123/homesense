/*
 * ESP32-S3-BOX-3 AudioSense  –  main.c
 *
 * Changes vs. previous version
 * ─────────────────────────────
 *  REFACTOR – audio_sad.c + audio_stream.c replaced by audio_pipeline.c.
 *
 *  The old architecture was:
 *    sad_monitor_task → [SAD trigger] → do_record_stream()
 *      → audio_stream_init/start/wait/deinit (blocking)
 *    Result: I2S DMA stalled while MQTT was publishing.
 *
 *  The new architecture is:
 *    capture_task  (Core 1, prio 18)  –  permanent DMA loop, SAD, ring push
 *    streamer_task (Core 0, prio  4)  –  ring drain, MQTT publish
 *    PSRAM ring buffer (~2 MiB NOSPLIT) decouples the two tasks.
 *
 *  API change in app_main:
 *    sad_ctx_t + xTaskCreate(sad_monitor_task) replaced by
 *    audio_pipeline_ctx_t + audio_pipeline_init() + audio_pipeline_start().
 *
 *  sad_is_busy() → audio_pipeline_is_busy() (ping_handler).
 *
 *  FIX 3, FIX 4 (player_task stack, speaker mutex) unchanged.
 */

#include <string.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <dirent.h>
#include <errno.h>
#include <time.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_http_server.h"
#include "nvs_flash.h"
#include "esp_vfs_fat.h"
#include "ff.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"

#include "bsp/esp-bsp.h"
#include "esp_codec_dev.h"
#include "lvgl.h"

#include "audiosense_mqtt.h"
#include "audio_pipeline.h"   /* replaces audio_sad.h + audio_stream.h */

static const char *TAG = "AUDIOSENSE";

/* ══════════════════════════ DEFAULTS ═══════════════════════════ */

/*
 * WiFi credentials are NOT hardcoded. They must be provided via
 * settings.txt on the SD card as WIFI_SSID=... / WIFI_PASS=...
 * (see load_settings()). If settings.txt is missing or does not
 * contain WIFI_SSID, wifi_init() logs an error and skips bringing
 * the radio up rather than attempting to associate with an empty
 * SSID.
 */
#define DEFAULT_WIFI_SSID             ""
#define DEFAULT_WIFI_PASS             ""

#define DEFAULT_RMS_THRESHOLD         1000
#define DEFAULT_SAMPLE_RATE           16000

/* Loudspeaker output volume (0-100), re-asserted on every open. */
#define SPK_OUT_VOLUME                80

#define DEFAULT_BITS_PER_SAMPLE       16
#define DEFAULT_RECORD_BUFFER_FRAMES  1024
#define DEFAULT_DISCARD_SAMPLES       3000

#define DEFAULT_PREROLL_MS            300
#define DEFAULT_SILENCE_TIMEOUT_MS    1500
#define DEFAULT_MIN_RECORD_MS         500
#define DEFAULT_MAX_RECORD_MS         30000

#define DEFAULT_COOLDOWN_DRAIN_MS     3000
#define DEFAULT_COOLDOWN_RMS_CHECKS   10
#define DEFAULT_POST_RECORD_DELAY_MS  2000
#define DEFAULT_TIMEZONE_OFFSET_SEC   (2 * 3600)

/* ══════════════════════════ RUNTIME SETTINGS ═══════════════════ */

static char g_wifi_ssid[64]          = DEFAULT_WIFI_SSID;
static char g_wifi_pass[64]          = DEFAULT_WIFI_PASS;
       char g_device_id[32]          = "box3-unknown";

       int  g_rms_threshold          = DEFAULT_RMS_THRESHOLD;
       int  g_sample_rate            = DEFAULT_SAMPLE_RATE;
       int  g_bits_per_sample        = DEFAULT_BITS_PER_SAMPLE;
       int  g_record_buffer_frames   = DEFAULT_RECORD_BUFFER_FRAMES;
       int  g_discard_samples        = DEFAULT_DISCARD_SAMPLES;

       int  g_preroll_ms             = DEFAULT_PREROLL_MS;
       int  g_silence_timeout_ms     = DEFAULT_SILENCE_TIMEOUT_MS;
       int  g_min_record_ms          = DEFAULT_MIN_RECORD_MS;
       int  g_max_record_ms          = DEFAULT_MAX_RECORD_MS;

       int  g_cooldown_drain_ms      = DEFAULT_COOLDOWN_DRAIN_MS;
       int  g_cooldown_rms_checks    = DEFAULT_COOLDOWN_RMS_CHECKS;
       int  g_post_record_delay_ms   = DEFAULT_POST_RECORD_DELAY_MS;
       int  g_timezone_offset_sec    = DEFAULT_TIMEZONE_OFFSET_SEC;

/* ══════════════════════════ SETTINGS LOADER ════════════════════ */

static void load_settings(void)
{
    const char *path = BSP_SD_MOUNT_POINT "/settings.txt";
    FILE *f = fopen(path, "r");
    if (!f) {
        ESP_LOGI(TAG, "[settings] No settings.txt – using defaults");
        return;
    }

    char line[256];
    int  loaded = 0;

    while (fgets(line, sizeof(line), f)) {
        char *nl = strchr(line, '\n'); if (nl) *nl = '\0';
        char *cr = strchr(line, '\r'); if (cr) *cr = '\0';
        if (!line[0] || line[0] == '#') continue;

        char *eq = strchr(line, '=');
        if (!eq) {
            ESP_LOGW(TAG, "[settings] bad line: %s", line);
            continue;
        }
        *eq = '\0';
        const char *key = line, *val = eq + 1;

        if      (!strcmp(key,"WIFI_SSID"))
            { strncpy(g_wifi_ssid, val, sizeof(g_wifi_ssid)-1); loaded++; }
        else if (!strcmp(key,"WIFI_PASS"))
            { strncpy(g_wifi_pass, val, sizeof(g_wifi_pass)-1); loaded++; }
        else if (!strcmp(key,"DEVICE_ID"))
            { strncpy(g_device_id, val, sizeof(g_device_id)-1); loaded++; }
        else if (!strcmp(key,"RMS_THRESHOLD"))
            { g_rms_threshold        = atoi(val); loaded++; }
        else if (!strcmp(key,"SAMPLE_RATE"))
            { g_sample_rate          = atoi(val); loaded++; }
        else if (!strcmp(key,"BITS_PER_SAMPLE"))
            { g_bits_per_sample      = atoi(val); loaded++; }
        else if (!strcmp(key,"RECORD_BUFFER_FRAMES"))
            { g_record_buffer_frames = atoi(val); loaded++; }
        else if (!strcmp(key,"DISCARD_SAMPLES"))
            { g_discard_samples      = atoi(val); loaded++; }
        else if (!strcmp(key,"PREROLL_MS"))
            { g_preroll_ms           = atoi(val); loaded++; }
        else if (!strcmp(key,"SILENCE_TIMEOUT_MS"))
            { g_silence_timeout_ms   = atoi(val); loaded++; }
        else if (!strcmp(key,"MIN_RECORD_MS"))
            { g_min_record_ms        = atoi(val); loaded++; }
        else if (!strcmp(key,"MAX_RECORD_MS") ||
                 !strcmp(key,"RECORD_DURATION_MS"))
            { g_max_record_ms        = atoi(val); loaded++; }
        else if (!strcmp(key,"COOLDOWN_DRAIN_MS"))
            { g_cooldown_drain_ms    = atoi(val); loaded++; }
        else if (!strcmp(key,"COOLDOWN_RMS_CHECKS"))
            { g_cooldown_rms_checks  = atoi(val); loaded++; }
        else if (!strcmp(key,"POST_RECORD_DELAY_MS"))
            { g_post_record_delay_ms = atoi(val); loaded++; }
        else if (!strcmp(key,"TIMEZONE_OFFSET_SEC"))
            { g_timezone_offset_sec  = atoi(val); loaded++; }
        else
            { ESP_LOGW(TAG, "[settings] unknown key: %s", key); }
    }
    fclose(f);
    ESP_LOGI(TAG,
        "[settings] Loaded %d key(s)  DEVICE=%s SSID=%s RMS=%d",
        loaded, g_device_id, g_wifi_ssid, g_rms_threshold);

    if (!g_wifi_ssid[0]) {
        ESP_LOGW(TAG,
            "[settings] No WIFI_SSID in settings.txt – WiFi will not "
            "be started. Add WIFI_SSID=... / WIFI_PASS=... to "
            "%s", path);
    }
}

/* ══════════════════════════ GLOBALS ════════════════════════════ */

static EventGroupHandle_t wifi_event_group;
static const int WIFI_CONNECTED_BIT = BIT0;

static esp_codec_dev_handle_t mic_codec_dev = NULL;
static esp_codec_dev_handle_t spk_codec_dev = NULL;

/*
 * FIX 4: Speaker exclusion mutex.
 * Both users (play_wav_from_sd and player_task) take non-blocking;
 * if the take fails the caller returns immediately.
 */
static SemaphoreHandle_t s_spk_mutex = NULL;

/* ══════════════════════════ DISPLAY ════════════════════════════ */

static lv_obj_t *lbl_status;
static lv_obj_t *lbl_time;
static lv_obj_t *lbl_wifi;
static lv_obj_t *lbl_queue;
static lv_obj_t *lbl_rms;
static lv_obj_t *rms_bar;

static void display_init(void)
{
    bsp_display_start();
    bsp_display_backlight_on();
    bsp_display_lock(0);

    lv_obj_set_style_bg_color(lv_scr_act(), lv_color_hex(0x1a1a2e), 0);

    lv_obj_t *top = lv_obj_create(lv_scr_act());
    lv_obj_set_size(top, 320, 36);
    lv_obj_align(top, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_style_bg_color(top, lv_color_hex(0x16213e), 0);
    lv_obj_set_style_border_width(top, 0, 0);
    lv_obj_set_style_radius(top, 0, 0);
    lv_obj_set_style_pad_all(top, 8, 0);

    lbl_wifi = lv_label_create(top);
    lv_label_set_text(lbl_wifi, LV_SYMBOL_WARNING);
    lv_obj_set_style_text_color(lbl_wifi, lv_color_hex(0xff6b6b), 0);
    lv_obj_align(lbl_wifi, LV_ALIGN_LEFT_MID, 0, 0);

    lbl_time = lv_label_create(top);
    lv_label_set_text(lbl_time, "--:--");
    lv_obj_set_style_text_color(lbl_time, lv_color_hex(0xeaeaea), 0);
    lv_obj_align(lbl_time, LV_ALIGN_RIGHT_MID, 0, 0);

    lbl_rms = lv_label_create(lv_scr_act());
    lv_label_set_text(lbl_rms, "RMS: ----");
    lv_obj_set_style_text_color(lbl_rms, lv_color_hex(0x00ffff), 0);
    lv_obj_set_style_text_font(lbl_rms, &lv_font_montserrat_16, 0);
    lv_obj_align(lbl_rms, LV_ALIGN_TOP_MID, 0, 45);

    rms_bar = lv_bar_create(lv_scr_act());
    lv_obj_set_size(rms_bar, 280, 15);
    lv_bar_set_range(rms_bar, 0, 1000);
    lv_bar_set_value(rms_bar, 0, LV_ANIM_OFF);
    lv_obj_align(rms_bar, LV_ALIGN_TOP_MID, 0, 70);
    lv_obj_set_style_bg_color(rms_bar, lv_color_hex(0x333333), LV_PART_MAIN);
    lv_obj_set_style_bg_color(rms_bar, lv_color_hex(0x00ff00),
                              LV_PART_INDICATOR);

    lbl_status = lv_label_create(lv_scr_act());
    lv_label_set_text(lbl_status, "Starting...");
    lv_obj_set_style_text_color(lbl_status, lv_color_hex(0x4ecca3), 0);
    lv_obj_set_style_text_font(lbl_status, &lv_font_montserrat_24, 0);
    lv_obj_align(lbl_status, LV_ALIGN_CENTER, 0, 20);

    lbl_queue = lv_label_create(lv_scr_act());
    lv_label_set_text(lbl_queue, "");
    lv_obj_set_style_text_color(lbl_queue, lv_color_hex(0x888888), 0);
    lv_obj_align(lbl_queue, LV_ALIGN_BOTTOM_MID, 0, -10);

    bsp_display_unlock();
}

static void display_set_status(const char *text, uint32_t color)
{
    bsp_display_lock(0);
    lv_label_set_text(lbl_status, text);
    lv_obj_set_style_text_color(lbl_status, lv_color_hex(color), 0);
    bsp_display_unlock();
}

static void display_set_rms(int16_t rms)
{
    char buf[40];
    snprintf(buf, sizeof(buf), "RMS: %d (thr: %d)", rms, g_rms_threshold);
    bsp_display_lock(0);
    lv_label_set_text(lbl_rms, buf);
    int v = rms > 1000 ? 1000 : rms;
    lv_bar_set_value(rms_bar, v, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(rms_bar,
        lv_color_hex(rms > g_rms_threshold ? 0xff0000 : 0x00ff00),
        LV_PART_INDICATOR);
    bsp_display_unlock();
}

static void display_set_wifi(bool connected)
{
    bsp_display_lock(0);
    if (connected) {
        lv_label_set_text(lbl_wifi, LV_SYMBOL_WIFI);
        lv_obj_set_style_text_color(lbl_wifi, lv_color_hex(0x4ecca3), 0);
    } else {
        lv_label_set_text(lbl_wifi, LV_SYMBOL_WARNING);
        lv_obj_set_style_text_color(lbl_wifi, lv_color_hex(0xff6b6b), 0);
    }
    bsp_display_unlock();
}

static void display_update_time(void)
{
    time_t now; struct tm ti;
    time(&now);
    now += g_timezone_offset_sec;
    localtime_r(&now, &ti);
    char s[16];
    strftime(s, sizeof(s), "%H:%M", &ti);
    bsp_display_lock(0);
    lv_label_set_text(lbl_time, s);
    bsp_display_unlock();
}

static void display_set_queue_count(int count)
{
    bsp_display_lock(0);
    if (count > 0) {
        char buf[32];
        snprintf(buf, sizeof(buf), LV_SYMBOL_UPLOAD " %d pending", count);
        lv_label_set_text(lbl_queue, buf);
    } else {
        lv_label_set_text(lbl_queue, "");
    }
    bsp_display_unlock();
}

/* ══════════════════════════ WIFI ═══════════════════════════════ */

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi disconnected, retrying...");
        xEventGroupClearBits(wifi_event_group, WIFI_CONNECTED_BIT);
        display_set_wifi(false);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
        char ip_str[32];
        snprintf(ip_str, sizeof(ip_str), IPSTR, IP2STR(&evt->ip_info.ip));

        ESP_LOGI(TAG, "WiFi connected, IP=%s", ip_str);
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
        display_set_wifi(true);

        /*
         * Re-announce on every GOT_IP, not just the first one at
         * boot. Covers (a) DHCP handing out a different lease on a
         * later reconnect, and (b) a mass reconnect after a shared
         * power outage where this device's own IP takes longer to
         * arrive than app_main()'s boot-time wait window – the old
         * one-shot call there would simply miss it and never
         * announce anything for the whole session. See
         * audiosense_mqtt_set_device_ip() for the publish side.
         */
        audiosense_mqtt_set_device_ip(ip_str);
    }
}

/*
 * Returns false (and leaves the radio untouched) if settings.txt did
 * not supply a WIFI_SSID. wifi_event_group is still created so that
 * wifi_is_connected() remains safe to call even in this case.
 */
static bool wifi_init(void)
{
    wifi_event_group = xEventGroupCreate();

    if (!g_wifi_ssid[0]) {
        ESP_LOGE(TAG,
            "[wifi] No WIFI_SSID configured (settings.txt) – "
            "skipping WiFi init");
        return false;
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                               &wifi_event_handler, NULL);
    esp_event_handler_register(IP_EVENT,   IP_EVENT_STA_GOT_IP,
                               &wifi_event_handler, NULL);

    wifi_config_t wc = {0};
    strncpy((char *)wc.sta.ssid,     g_wifi_ssid,
            sizeof(wc.sta.ssid)     - 1);
    strncpy((char *)wc.sta.password, g_wifi_pass,
            sizeof(wc.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    return true;
}

static bool wifi_is_connected(void)
{
    return wifi_event_group &&
           (xEventGroupGetBits(wifi_event_group) & WIFI_CONNECTED_BIT) != 0;
}

/* ══════════════════════════ NTP ════════════════════════════════ */

static void ntp_init(void)
{
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, "pool.ntp.org");
    esp_sntp_init();
    int r = 0;
    while (sntp_get_sync_status() == SNTP_SYNC_STATUS_RESET && ++r < 20)
        vTaskDelay(pdMS_TO_TICKS(500));
    ESP_LOGI(TAG, "NTP %s", r < 20 ? "synced" : "timeout");
}

/* ══════════════════════════ AUDIO INIT ═════════════════════════ */

static void audio_init(void)
{
    bsp_i2c_init();
    spk_codec_dev = bsp_audio_codec_speaker_init();
    mic_codec_dev = bsp_audio_codec_microphone_init();

    if (mic_codec_dev) esp_codec_dev_set_in_gain(mic_codec_dev, 42.0);
    if (spk_codec_dev) esp_codec_dev_set_out_vol(spk_codec_dev, SPK_OUT_VOLUME);

    if (!mic_codec_dev) ESP_LOGE(TAG, "Mic init failed");
    if (!spk_codec_dev) ESP_LOGE(TAG, "Speaker init failed");
}

/* ══════════════════════════ OUTGOING STREAM CALLBACKS ══════════
 *
 * Called from streamer_task context (audio_pipeline.c).
 * on_stream_start may block ≤15 s inside mqtt_connect(); the ring
 * buffer absorbs mic frames that arrive during that window.
 * Timestamp is generated here (at publish time, per Q4 decision).
 * ════════════════════════════════════════════════════════════ */

static bool on_stream_start(int sample_rate, int bits_per_sample,
                             int channels)
{
    time_t   now;
    struct   tm ti;
    time(&now);
    now += g_timezone_offset_sec;
    localtime_r(&now, &ti);

    char ts[32];
    strftime(ts, sizeof(ts), "%Y%m%d_%H%M%S", &ti);

    display_set_status("Streaming...", 0xffa500);
    return audiosense_mqtt_stream_start(g_device_id, ts,
                                        sample_rate, bits_per_sample,
                                        channels);
}

static bool on_stream_chunk(const void *data, int len, uint32_t chunk_idx)
{
    return audiosense_mqtt_stream_chunk(data, len, chunk_idx);
}

static bool on_stream_end(uint32_t total_chunks, uint32_t total_bytes)
{
    return audiosense_mqtt_stream_end(total_chunks, total_bytes);
}

/* ══════════════════════ LIVE AUDIO MODE CONTROL ════════════════════ *
 *
 * Called from the MQTT task when start_live_audio / stop_live_audio
 * arrives on commands/<id>/control.
 *
 * Deliberately trivial: the MQTT event handler must not block, and the
 * mode change has to happen at an audio frame boundary rather than
 * whenever the network happens to deliver a message. Recording the
 * request and letting capture_task pick it up is what guarantees an
 * in-flight Event Mode utterance is never truncated by a mode switch.
 * ─────────────────────────────────────────────────────────────────── */
static void on_live_cmd(bool start)
{
    audio_pipeline_set_live(start);
}

/* ══════════════════════════ HTTP SERVER ════════════════════════ */

#define PLAY_CHUNK_BYTES  4096
#define PLAY_QUEUE_PREFIX "play_"

static volatile uint32_t play_seq = 0;

static esp_err_t ping_handler(httpd_req_t *req)
{
    display_set_status("Pinged!", 0xa8e6cf);
    char resp[64];
    snprintf(resp, sizeof(resp),
             "{\"status\":\"ok\",\"device\":\"%s\"}", g_device_id);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_send(req, resp, strlen(resp));
    vTaskDelay(pdMS_TO_TICKS(2000));
    /* Use audio_pipeline_is_busy() – replaces sad_is_busy() */
    if (!audio_pipeline_is_busy()) display_set_status("Idle", 0x4ecca3);
    return ESP_OK;
}

static esp_err_t play_handler(httpd_req_t *req)
{
    if (!spk_codec_dev) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR,
                            "No speaker");
        return ESP_FAIL;
    }
    if (req->content_len < 44) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "WAV too small");
        return ESP_FAIL;
    }

    char path[128];
    snprintf(path, sizeof(path), "%s/%s%04u.wav",
             BSP_SD_MOUNT_POINT, PLAY_QUEUE_PREFIX,
             (unsigned)(play_seq++ % 10000));

    FILE *f = fopen(path, "wb");
    if (!f) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR,
                            "SD error");
        return ESP_FAIL;
    }

    char *buf = malloc(PLAY_CHUNK_BYTES);
    if (!buf) {
        fclose(f);
        unlink(path);
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "OOM");
        return ESP_FAIL;
    }

    int  rem = req->content_len;
    bool ok  = true;
    while (rem > 0) {
        int n = rem > PLAY_CHUNK_BYTES ? PLAY_CHUNK_BYTES : rem;
        int r = httpd_req_recv(req, buf, n);
        if (r <= 0) {
            if (r == HTTPD_SOCK_ERR_TIMEOUT) continue;
            ok = false;
            break;
        }
        fwrite(buf, 1, r, f);
        rem -= r;
    }
    free(buf);
    fclose(f);

    if (!ok) {
        unlink(path);
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR,
                            "Recv error");
        return ESP_FAIL;
    }

    const char *resp = "{\"status\":\"queued\"}";
    httpd_resp_set_type(req, "application/json");
    httpd_resp_send(req, resp, strlen(resp));
    return ESP_OK;
}

static void start_http_server(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size         = 8192;
    cfg.max_uri_handlers   = 4;
    cfg.recv_wait_timeout  = 30;
    cfg.send_wait_timeout  = 10;

    httpd_handle_t srv = NULL;
    if (httpd_start(&srv, &cfg) != ESP_OK) return;

    httpd_uri_t u_ping = { .uri="/ping", .method=HTTP_GET,
                           .handler=ping_handler };
    httpd_uri_t u_play = { .uri="/play", .method=HTTP_POST,
                           .handler=play_handler };
    httpd_register_uri_handler(srv, &u_ping);
    httpd_register_uri_handler(srv, &u_play);
    ESP_LOGI(TAG, "HTTP server: /ping /play");
}

/* ══════════════════════════ SPEAKER OPEN / WRITE ═══════════════ *
 *
 * Both playback paths (SD queue and live MQTT stream) go through
 * these two helpers so the two traps below are handled in one place.
 *
 * Trap 1 – half-open speaker.
 *   esp_codec_dev_open() sets its internal "opened" flag BEFORE the
 *   codec-level set_fs / enable calls that can fail. If it returns an
 *   error and the caller does not close the handle, every later open
 *   returns ESP_CODEC_DEV_OK immediately without touching I2S,
 *   without enabling the ES8311 and without powering the amplifier.
 *   esp_codec_dev_write() still succeeds, because the shared-I2S
 *   layer keeps the TX channel enabled while the microphone RX
 *   channel is running. The result is a permanently silent speaker
 *   that reports success at every step. Always close on failure.
 *
 * Trap 2 – format conflict with the microphone.
 *   The ES8311 (speaker) and ES7210 (microphone) share one I2S port,
 *   and capture_task holds the microphone open for the whole session,
 *   which pins the port to g_sample_rate / g_bits_per_sample. The
 *   shared-I2S layer rejects any playback format that disagrees, but
 *   esp_codec_dev_open() discards that error: the I2S clock stays at
 *   the microphone rate while the ES8311 is programmed for the
 *   requested rate. Resolve it here, loudly, instead.
 * ════════════════════════════════════════════════════════════ */

static bool spk_open(int sample_rate, int bits_per_sample, int channels)
{
    if (!spk_codec_dev) return false;

    if (sample_rate     <= 0) sample_rate     = g_sample_rate;
    if (bits_per_sample <= 0) bits_per_sample = g_bits_per_sample;
    if (channels        <= 0) channels        = 1;

    if (sample_rate != g_sample_rate ||
        bits_per_sample != g_bits_per_sample) {
        ESP_LOGW(TAG,
            "[spk] requested %dHz/%dbit but the microphone pins the "
            "shared I2S port to %dHz/%dbit – playing at the microphone "
            "format, pitch will be wrong",
            sample_rate, bits_per_sample,
            g_sample_rate, g_bits_per_sample);
        sample_rate     = g_sample_rate;
        bits_per_sample = g_bits_per_sample;
    }

    esp_codec_dev_sample_info_t fs = {
        .sample_rate     = (uint32_t)sample_rate,
        .channel         = (uint32_t)channels,
        .bits_per_sample = (uint32_t)bits_per_sample,
    };

    int err = esp_codec_dev_open(spk_codec_dev, &fs);
    if (err != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG,
            "[spk] open failed (%d) – closing handle so the next "
            "attempt is not silently accepted", err);
        esp_codec_dev_close(spk_codec_dev);
        return false;
    }

    /* The ES8311 is muted and powered down by every close; re-assert
     * volume and unmute rather than trusting the values cached at
     * init to survive the power-down cycle. */
    esp_codec_dev_set_out_vol(spk_codec_dev, SPK_OUT_VOLUME);
    esp_codec_dev_set_out_mute(spk_codec_dev, false);

    ESP_LOGI(TAG, "[spk] open %dHz %dbit ch%d vol%d",
             sample_rate, bits_per_sample, channels, SPK_OUT_VOLUME);
    return true;
}

static bool spk_write(const void *data, int len)
{
    int err = esp_codec_dev_write(spk_codec_dev, (void *)data, len);
    if (err != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG, "[spk] write of %d B failed (%d)", len, err);
        return false;
    }
    return true;
}

/* ══════════════════════════ WAV HEADER ═════════════════════════ *
 *
 * The queued files come from whatever the sender produced, so the
 * PCM does not necessarily start at byte 44: a "LIST"/"INFO" or
 * "fact" chunk ahead of "data" shifts it, and the format is not
 * necessarily the microphone's. Walk the RIFF chunks instead of
 * assuming a fixed 44-byte header.
 * ════════════════════════════════════════════════════════════ */

typedef struct {
    long data_offset;
    long data_bytes;
    int  sample_rate;
    int  bits_per_sample;
    int  channels;
} wav_info_t;

static uint32_t rd_le32(const uint8_t *p)
{
    return (uint32_t)p[0]        | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint16_t rd_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static bool wav_parse(FILE *f, wav_info_t *w)
{
    uint8_t riff[12];
    if (fread(riff, 1, sizeof(riff), f) != sizeof(riff))       return false;
    if (memcmp(riff, "RIFF", 4) || memcmp(riff + 8, "WAVE", 4)) return false;

    fseek(f, 0, SEEK_END);
    long file_end = ftell(f);
    fseek(f, 12, SEEK_SET);

    bool    have_fmt = false;
    uint8_t hdr[8];

    while (fread(hdr, 1, sizeof(hdr), f) == sizeof(hdr)) {
        long     body = ftell(f);
        uint32_t size = rd_le32(hdr + 4);

        if (!memcmp(hdr, "fmt ", 4) && size >= 16) {
            uint8_t fmt[16];
            if (fread(fmt, 1, sizeof(fmt), f) != sizeof(fmt)) return false;
            w->channels        = rd_le16(fmt +  2);
            w->sample_rate     = (int)rd_le32(fmt + 4);
            w->bits_per_sample = rd_le16(fmt + 14);
            have_fmt = true;
        } else if (!memcmp(hdr, "data", 4)) {
            w->data_offset = body;
            /* A stream written on the fly may carry size 0 or a size
             * that overruns the file; trust the file length then. */
            w->data_bytes  = (size && body + (long)size <= file_end)
                             ? (long)size
                             : file_end - body;
            return have_fmt && w->data_bytes > 0;
        }

        /* RIFF chunk bodies are word-aligned. */
        if (fseek(f, body + (long)size + ((long)size & 1), SEEK_SET) != 0)
            break;
    }
    return false;
}

/* ══════════════════════════ SD PLAYBACK ════════════════════════ */

typedef enum {
    SD_PLAY_OK,         /* played fully, file deleted                */
    SD_PLAY_BUSY,       /* speaker in use elsewhere – retry later    */
    SD_PLAY_BAD_FILE,   /* file itself is unplayable – must be purged */
} sd_play_result_t;

static sd_play_result_t play_wav_from_sd(const char *filepath)
{
    /* FIX 4: non-blocking mutex take */
    if (xSemaphoreTake(s_spk_mutex, 0) != pdTRUE) {
        ESP_LOGW(TAG, "[play_sd] speaker busy, skipping %s", filepath);
        return SD_PLAY_BUSY;
    }

    ESP_LOGI(TAG, "[play_sd] %s", filepath);
    display_set_status("Playing...", 0x4ecdc4);

    FILE *f = fopen(filepath, "rb");
    if (!f) {
        ESP_LOGE(TAG, "[play_sd] open failed");
        xSemaphoreGive(s_spk_mutex);
        return SD_PLAY_BAD_FILE;
    }

    wav_info_t w = {0};
    if (!wav_parse(f, &w)) {
        ESP_LOGW(TAG, "[play_sd] not a playable WAV – purging %s",
                 filepath);
        fclose(f);
        xSemaphoreGive(s_spk_mutex);
        return SD_PLAY_BAD_FILE;
    }

    long pcm_bytes = w.data_bytes;
    fseek(f, w.data_offset, SEEK_SET);

    ESP_LOGI(TAG, "[play_sd] %ld B PCM at offset %ld  %dHz %dbit ch%d",
             pcm_bytes, w.data_offset,
             w.sample_rate, w.bits_per_sample, w.channels);

    if (!spk_open(w.sample_rate, w.bits_per_sample, w.channels)) {
        fclose(f);
        xSemaphoreGive(s_spk_mutex);
        /* Codec-open failure is a device/driver problem, not this
         * particular file's fault – leave the file queued and let
         * the caller keep the speaker-busy-style retry behaviour so
         * we don't lose audio just because the codec hiccuped once. */
        return SD_PLAY_BUSY;
    }

    char *buf = malloc(PLAY_CHUNK_BYTES);
    if (!buf) {
        esp_codec_dev_close(spk_codec_dev);
        fclose(f);
        xSemaphoreGive(s_spk_mutex);
        return SD_PLAY_BUSY; /* transient OOM – retry later, don't purge */
    }

    long played = 0;
    bool wrote_ok = true;
    while (played < pcm_bytes) {
        size_t want = PLAY_CHUNK_BYTES;
        if (pcm_bytes - played < (long)want)
            want = (size_t)(pcm_bytes - played);
        size_t got = fread(buf, 1, want, f);
        if (!got) break;
        if (!spk_write(buf, (int)got)) {
            wrote_ok = false;
            break;
        }
        played += (long)got;
    }
    free(buf);
    esp_codec_dev_close(spk_codec_dev);
    fclose(f);

    if (!wrote_ok) {
        /* The speaker, not the file, is at fault – keep the file and
         * retry it once the codec is healthy again. */
        ESP_LOGE(TAG, "[play_sd] aborted after %ld/%ld B – keeping %s",
                 played, pcm_bytes, filepath);
        xSemaphoreGive(s_spk_mutex);
        return SD_PLAY_BUSY;
    }

    unlink(filepath);

    int bytes_per_sec = g_sample_rate * (g_bits_per_sample / 8);
    ESP_LOGI(TAG, "[play_sd] done %ld B (%.1fs)", played,
             bytes_per_sec ? (float)played / bytes_per_sec : 0.0f);
    display_set_status("Idle", 0x4ecca3);
    xSemaphoreGive(s_spk_mutex);   /* FIX 4 */
    return SD_PLAY_OK;
}

/*
 * Scans BSP_SD_MOUNT_POINT for queued play_*.wav files.
 *
 * IMPORTANT: a bad/corrupt file must never be able to permanently
 * jam this queue. Previously, the first matching filename returned
 * by readdir() was played (or silently skipped) and the function
 * always returned true if *any* match was found – even when nothing
 * was actually played. Because unlink() only happens on full
 * success, one stuck file sitting early in the FAT directory table
 * would be retried forever and would prevent every file behind it
 * from ever being reached, permanently jamming the SD playback path
 * without leaving any obvious error.
 *
 * Now: BAD_FILE results are purged immediately and the scan moves on
 * to the next candidate in the same pass; BUSY results stop the scan
 * for this cycle (retry the same file next time) without touching
 * the file.
 */
static bool check_and_play_queued(void)
{
    DIR *dir = opendir(BSP_SD_MOUNT_POINT);
    if (!dir) return false;

    /* Basename of the file audiosense_mqtt.c streams an incoming
     * broadcast into. It is only ready to play once renamed into the
     * queue by on_mqtt_play_received(); picking it up any earlier
     * truncates the audio and unlinks it under the open handle. */
    const char *stage = strrchr(MQTT_PLAY_WAV_PATH, '/');
    stage = stage ? stage + 1 : MQTT_PLAY_WAV_PATH;

    char path[512];
    struct dirent *e;
    while ((e = readdir(dir)) != NULL) {
        if (strncmp(e->d_name, PLAY_QUEUE_PREFIX,
                    strlen(PLAY_QUEUE_PREFIX)) != 0
            || !strstr(e->d_name, ".wav")
            || strcmp(e->d_name, stage) == 0) {
            continue;
        }

        snprintf(path, sizeof(path), "%s/%s",
                 BSP_SD_MOUNT_POINT, e->d_name);

        sd_play_result_t r = play_wav_from_sd(path);
        if (r == SD_PLAY_OK) {
            closedir(dir);
            return true;
        }
        if (r == SD_PLAY_BUSY) {
            /* Speaker unavailable right now – stop scanning, try
             * again on the next ~2s poll. Don't touch the file. */
            closedir(dir);
            return false;
        }
        /* SD_PLAY_BAD_FILE: already purged inside play_wav_from_sd()
         * where useful, or purge it here for the fopen-failure case. */
        unlink(path);
        ESP_LOGW(TAG, "[play_sd] purged unplayable file %s", path);
        /* keep scanning – don't let one bad file block the rest */
    }
    closedir(dir);
    return false;
}

/* ══════════════════════════ MQTT LEGACY PLAY CALLBACK ══════════ */

static void on_mqtt_play_received(void)
{
    char dst[128];
    snprintf(dst, sizeof(dst), "%s/%s%04u.wav",
             BSP_SD_MOUNT_POINT, PLAY_QUEUE_PREFIX,
             (unsigned)(play_seq++ % 10000));

    /* FATFS f_rename() will not overwrite. play_seq restarts at 0 on
     * every boot, so a file left behind by an earlier session would
     * otherwise fail the rename and lose the broadcast. */
    unlink(dst);

    if (rename(MQTT_PLAY_WAV_PATH, dst) == 0) {
        ESP_LOGI(TAG, "[mqtt_play] queued: %s", dst);
        return;
    }

    ESP_LOGE(TAG, "[mqtt_play] rename %s -> %s failed (errno %d)",
             MQTT_PLAY_WAV_PATH, dst, errno);
}

/* ══════════════════════════ INCOMING STREAM PLAY ═══════════════ */

#define PLAY_STREAM_QUEUE_DEPTH  24

typedef struct {
    uint8_t *data;
    int      len;
    bool     is_end;
} play_chunk_t;

static QueueHandle_t     s_play_queue       = NULL;
static volatile int      s_play_sample_rate = 0;
static volatile int      s_play_bits        = 0;
static volatile int      s_play_channels    = 1;

/*
 * Diagnostics – incremented on every drop so the *cause* of a dead
 * loudspeaker (OOM vs. queue-full vs. speaker-busy) is visible in
 * the logs instead of only "it stopped working". Logged periodically
 * by time_update_task().
 */
static volatile uint32_t s_play_oom_count        = 0;
static volatile uint32_t s_play_queue_full_count  = 0;
static volatile uint32_t s_play_busy_drop_count   = 0;
static volatile uint32_t s_play_end_lost_count    = 0;
static volatile uint32_t s_play_write_err_count   = 0;

static void on_play_stream_start(int sample_rate, int bits_per_sample,
                                  int channels)
{
    s_play_sample_rate = sample_rate;
    s_play_bits        = bits_per_sample;
    s_play_channels    = channels;
    display_set_status("Receiving...", 0x4ecdc4);
    ESP_LOGI(TAG, "[player] stream start %dHz %dbit ch%d",
             sample_rate, bits_per_sample, channels);
    /*
     * Do NOT start Live Audio Mode here. Mic + speaker share one I2S
     * port on the Box-3; entering CAP_LIVE during talk-back contended
     * with playback and silenced the loudspeaker. Live TX is only
     * started by an explicit start_live_audio on commands/<id>/control.
     */
}

static void on_play_stream_chunk(const void *data, int len)
{
    if (!s_play_queue) return;

    play_chunk_t pc;
    /*
     * IMPORTANT: allocate from PSRAM, not the default (internal)
     * heap. Internal RAM on the S3 is a few hundred KB shared with
     * WiFi/mbedTLS/LVGL/FreeRTOS and is the resource that actually
     * fragments over days of uptime. The outgoing (recording) path
     * never mallocs at all – it publishes straight out of the
     * PSRAM ring buffer – which is why it kept working while this
     * path, which mallocs a fresh internal-heap block per chunk,
     * degraded. Mirrors the MALLOC_CAP_SPIRAM policy already used
     * for the ring buffer / task stacks in audio_pipeline.c.
     */
    pc.data   = heap_caps_malloc((size_t)len,
                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    pc.is_end = false;
    pc.len    = len;

    if (!pc.data) {
        s_play_oom_count++;
        ESP_LOGW(TAG, "[player] OOM (PSRAM) – dropping chunk (total=%"
                 PRIu32 ")", s_play_oom_count);
        return;
    }
    memcpy(pc.data, data, len);

    if (xQueueSend(s_play_queue, &pc, 0) != pdTRUE) {
        heap_caps_free(pc.data);
        s_play_queue_full_count++;
        ESP_LOGW(TAG, "[player] queue full – dropping chunk (total=%"
                 PRIu32 ")", s_play_queue_full_count);
    }
}

static void on_play_stream_end(void)
{
    if (!s_play_queue) return;
    play_chunk_t sentinel = { .data = NULL, .len = 0, .is_end = true };
    if (xQueueSend(s_play_queue, &sentinel, pdMS_TO_TICKS(200)) != pdTRUE) {
        /*
         * Queue was still full 200ms after stream end. player_task's
         * own 200ms receive-timeout will still close the codec and
         * release s_spk_mutex, so this is not a permanent stall –
         * but it means the queue was saturated at exactly the wrong
         * moment, which is worth knowing about.
         */
        s_play_end_lost_count++;
        ESP_LOGW(TAG, "[player] end-of-stream marker dropped, queue "
                 "still full (total=%" PRIu32 ")", s_play_end_lost_count);
    }
}

/* ══════════════════════════ PLAYER TASK ════════════════════════
 *
 *  FIX 3: Stack 8192 bytes (LVGL call chain in display_set_status).
 *  FIX 4: s_spk_mutex replaces volatile bool.
 * ════════════════════════════════════════════════════════════ */

static void player_task(void *pv)
{
    bool         codec_open = false;
    play_chunk_t pc;

    while (1) {
        if (xQueueReceive(s_play_queue, &pc,
                          pdMS_TO_TICKS(200)) != pdTRUE) {
            if (codec_open) {
                esp_codec_dev_close(spk_codec_dev);
                codec_open = false;
                xSemaphoreGive(s_spk_mutex);
                display_set_status("Idle", 0x4ecca3);
                ESP_LOGW(TAG, "[player] stream timeout – codec closed");
            }
            continue;
        }

        if (pc.is_end) {
            if (codec_open) {
                esp_codec_dev_close(spk_codec_dev);
                codec_open = false;
                xSemaphoreGive(s_spk_mutex);
            }
            display_set_status("Idle", 0x4ecca3);
            ESP_LOGI(TAG, "[player] stream complete");
            continue;
        }

        if (!codec_open) {
            if (xSemaphoreTake(s_spk_mutex, 0) != pdTRUE) {
                heap_caps_free(pc.data);
                s_play_busy_drop_count++;
                ESP_LOGW(TAG, "[player] speaker busy, discarding chunk "
                         "(total=%" PRIu32 ")", s_play_busy_drop_count);
                continue;
            }
            if (!spk_open(s_play_sample_rate, s_play_bits,
                          s_play_channels)) {
                heap_caps_free(pc.data);
                xSemaphoreGive(s_spk_mutex);
                display_set_status("Spk error!", 0xff6b6b);
                continue;
            }
            codec_open = true;
            display_set_status("Playing...", 0x4ecdc4);
        }

        if (!spk_write(pc.data, pc.len)) {
            s_play_write_err_count++;
            esp_codec_dev_close(spk_codec_dev);
            codec_open = false;
            xSemaphoreGive(s_spk_mutex);
            display_set_status("Spk error!", 0xff6b6b);
        }
        heap_caps_free(pc.data);
    }
}

/* ══════════════════════════ TIME TASK ══════════════════════════ */

static void time_update_task(void *pv)
{
    int diag_div = 0;
    while (1) {
        display_update_time();

        /*
         * Every ~5 min: log internal-heap headroom plus the
         * loudspeaker drop counters. This is the trail to follow if
         * playback silently degrades again – internal free heap
         * trending down over days while s_play_oom_count climbs
         * would confirm internal-RAM fragmentation/exhaustion as the
         * cause; a climbing s_play_busy_drop_count instead would
         * point at s_spk_mutex contention with SD playback.
         */
        if (++diag_div >= 10) {
            diag_div = 0;
            ESP_LOGI(TAG,
                "[diag] heap: internal free=%u min_ever=%u  "
                "psram free=%u  |  play drops: oom=%" PRIu32
                " queue_full=%" PRIu32 " busy=%" PRIu32
                " end_lost=%" PRIu32 " write_err=%" PRIu32,
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                s_play_oom_count, s_play_queue_full_count,
                s_play_busy_drop_count, s_play_end_lost_count,
                s_play_write_err_count);
        }
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}

/* ══════════════════════════ APP_MAIN ═══════════════════════════ */

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    display_init();
    display_set_status("Booting...", 0x4ecca3);

    /* ── Speaker mutex (FIX 4) ───────────────────────────────── */
    s_spk_mutex = xSemaphoreCreateMutex();
    if (!s_spk_mutex) {
        ESP_LOGE(TAG, "Speaker mutex alloc failed – halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }

    /* ── SD Card ─────────────────────────────────────────────── */
    display_set_status("SD Card...", 0x4ecca3);
    if (bsp_sdcard_mount() != ESP_OK) {
        display_set_status("SD FAILED!", 0xff0000);
        vTaskDelay(pdMS_TO_TICKS(3000));
    } else {
        ESP_LOGI(TAG, "SD mounted");
        FATFS *fs; DWORD fc;
        if (f_getfree("0:", &fc, &fs) == FR_OK) {
            uint64_t tot = (uint64_t)(fs->n_fatent - 2) * fs->csize * 512;
            uint64_t avl = (uint64_t)fc * fs->csize * 512;
            char info[64];
            snprintf(info, sizeof(info), "SD: %.1f/%.1fGB",
                     avl / (1024.0 * 1024 * 1024),
                     tot / (1024.0 * 1024 * 1024));
            display_set_status(info, 0x00ff00);
            vTaskDelay(pdMS_TO_TICKS(1500));
        }
        load_settings();
        display_set_status("Settings OK", 0x00ff00);
        vTaskDelay(pdMS_TO_TICKS(400));
    }

    /* ── Audio ───────────────────────────────────────────────── */
    display_set_status("Audio init...", 0x4ecca3);
    audio_init();
    display_set_status(
        (mic_codec_dev && spk_codec_dev) ? "Audio OK" : "Audio ERROR!",
        (mic_codec_dev && spk_codec_dev) ? 0x00ff00  : 0xff0000);
    vTaskDelay(pdMS_TO_TICKS(500));

    /* ── Play queue (must exist before MQTT init) ────────────── */
    s_play_queue = xQueueCreate(PLAY_STREAM_QUEUE_DEPTH,
                                sizeof(play_chunk_t));
    if (!s_play_queue) ESP_LOGE(TAG, "Play queue alloc failed!");

    /* ── WiFi ────────────────────────────────────────────────── */
    display_set_status("WiFi...", 0xffaa00);
    char ip_str[32] = "";

    if (!wifi_init()) {
        display_set_status("No WiFi config!", 0xff6b6b);
        ESP_LOGE(TAG,
            "No WIFI_SSID in settings.txt – continuing without WiFi. "
            "MQTT/streaming will be unavailable.");
        vTaskDelay(pdMS_TO_TICKS(2000));
    } else {
        for (int i = 0; i < 60; i++) {
            if (xEventGroupGetBits(wifi_event_group) & WIFI_CONNECTED_BIT) {
                esp_netif_ip_info_t ip_info;
                esp_netif_t *netif =
                    esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
                if (netif &&
                    esp_netif_get_ip_info(netif, &ip_info) == ESP_OK) {
                    snprintf(ip_str, sizeof(ip_str), IPSTR,
                             IP2STR(&ip_info.ip));
                    char disp[40];
                    snprintf(disp, sizeof(disp), "IP: %s", ip_str);
                    display_set_status(disp, 0x00ff00);
                    vTaskDelay(pdMS_TO_TICKS(1500));
                }
                break;
            }
            char prog[32];
            snprintf(prog, sizeof(prog), "WiFi... %ds", i / 2);
            display_set_status(prog, 0xffaa00);
            vTaskDelay(pdMS_TO_TICKS(500));
        }
    }

    /* ── NTP ─────────────────────────────────────────────────── */
    display_set_status("Sync time...", 0x4ecca3);
    ntp_init();
    display_update_time();

    /* ── MQTT ────────────────────────────────────────────────── */
    display_set_status("MQTT init...", 0x4ecca3);
    audiosense_mqtt_set_client_id(g_device_id);

    /* Registered before init(), because the control-topic subscription is
     * made in the CONNECTED handler and init() starts the client. */
    audiosense_mqtt_set_live_cmd_callback(on_live_cmd);

    /* Redundant-but-harmless: wifi_event_handler()'s GOT_IP case
     * already called audiosense_mqtt_set_device_ip() with the same
     * value the moment WiFi connected (that's what set
     * WIFI_CONNECTED_BIT in the first place). Left in as a
     * belt-and-suspenders no-op for the boot path specifically. */
    if (ip_str[0]) audiosense_mqtt_set_device_ip(ip_str);
    if (audiosense_mqtt_init()) {
        audiosense_mqtt_set_play_callback(on_mqtt_play_received);
        audiosense_mqtt_set_play_stream_callbacks(
            on_play_stream_start,
            on_play_stream_chunk,
            on_play_stream_end);
        display_set_status("MQTT ready", 0x00ff00);
        vTaskDelay(pdMS_TO_TICKS(500));
    } else {
        display_set_status("MQTT init fail", 0xff6b6b);
        vTaskDelay(pdMS_TO_TICKS(1500));
    }

    start_http_server();

    /* ── Display queue label (no longer updated by pipeline) ─── */
    display_set_queue_count(0);

    /* ── Wire up audio pipeline ──────────────────────────────── */
    static audio_pipeline_ctx_t pipeline_ctx;
    pipeline_ctx.mic_dev       = mic_codec_dev;
    pipeline_ctx.stream_start  = on_stream_start;
    pipeline_ctx.stream_chunk  = on_stream_chunk;
    pipeline_ctx.stream_end    = on_stream_end;
    pipeline_ctx.on_status     = display_set_status;
    pipeline_ctx.on_rms        = display_set_rms;
    pipeline_ctx.wifi_connected = wifi_is_connected;
    pipeline_ctx.check_play    = check_and_play_queued;

    if (audio_pipeline_init(&pipeline_ctx) != ESP_OK) {
        display_set_status("Pipeline fail!", 0xff0000);
        ESP_LOGE(TAG, "audio_pipeline_init failed – halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }

    if (audio_pipeline_start() != ESP_OK) {
        display_set_status("Pipeline start!", 0xff0000);
        ESP_LOGE(TAG, "audio_pipeline_start failed – halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }

    /*
     * Task stack summary:
     *   capture_task  8192 B PSRAM  (prio 18, Core 1)
     *   streamer_task 8192 B PSRAM  (prio  4, Core 0)
     *   player_task   8192 B        (prio  8, Core ?)  FIX 3
     *   time_task     2048 B        (prio  3, Core ?)
     */
    xTaskCreate(player_task,      "player",  8192, NULL, 8, NULL);
    xTaskCreate(time_update_task, "time",    2048, NULL, 3, NULL);

    display_set_status("Idle", 0x4ecca3);
    ESP_LOGI(TAG,
        "=== AUDIOSENSE READY  device=%s thr=%d  "
        "pipeline: cap(core1,p18) str(core0,p4)  ring~2MiB PSRAM ===",
        g_device_id, g_rms_threshold);
}