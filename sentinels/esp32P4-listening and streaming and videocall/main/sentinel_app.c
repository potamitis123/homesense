/*
 * Waveshare ESP32-P4-WIFI6-Touch-LCD-5 AudioSense  –  main.c
 *
 * Ported from the ESP32-S3-BOX-3 build. The board-support API is the
 * same (bsp_i2c_init / bsp_audio_codec_*_init / bsp_sdcard_mount /
 * bsp_display_*), so the differences are confined to:
 *
 *   - display_init(): LVGL 9 instead of 8, and geometry derived from the
 *     active display rather than hard-coded for 320x240.
 *   - bsp_display_lock(): -1 is "block forever" here. Under
 *     esp_lvgl_port on the S3 that was 0; esp_lv_adapter treats 0 as
 *     "don't wait", which would let the UI be touched without the mutex.
 *     Only display_init() blocks forever, though — every UI update
 *     reachable from an audio task goes through ui_lock(), which gives up
 *     after UI_LOCK_TIMEOUT_MS rather than stalling playback.
 *   - the speaker lifecycle is the BOX-3's: open per broadcast, close
 *     afterwards. See the SPEAKER OPEN / WRITE / CLOSE block for why the
 *     open-once-and-mute variant silences this board.
 *
 * Changes vs. previous version
 * ─────────────────────────────
 *  CONFIG   – settings.txt parsing moved out of load_settings() into the
 *             generic Config module (config.h); MQTT broker, credentials
 *             and topics now arrive as an mqtt_config_t (mqtt_config.h)
 *             instead of compile-time constants.
 *
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
#include <math.h>
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
#include "esp_lv_adapter.h"
#include "driver/i2c_master.h"   /* boot self-test probes the shared I2C bus */
#include "lvgl.h"

#include "audiosense_mqtt.h"
#include "audio_pipeline.h"   /* replaces audio_sad.h + audio_stream.h */
#include "config.h"           /* generic settings.txt key/value store */
#include "mqtt_config.h"      /* MQTTConfig, loaded via config.h getters */
#include "sentinel_app.h"     /* the control surface the mode arbiter drives */

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

/*
 * Microphone gain in dB. Kept at the BOX-3's 42.0 because this board
 * captures through an ES7210 as well — the BSP header's claim that a lone
 * ES8311 handles both directions is stale text from the esp-box BSP, and
 * the boot log shows the ES7210 being initialised. Same part, same gain.
 */
#define MIC_IN_GAIN_DB                42.0

/*
 * I2C addresses on the bus the BSP brings up (SDA = GPIO 7, SCL = GPIO 8).
 * The OV5647's SCCB lines hang off this same bus: Waveshare's video
 * example passes bsp_i2c_get_handle() straight into its camera init, so
 * the camera is probeable without dragging in the esp_video stack.
 */
#define ES8311_I2C_ADDR               0x18
#define OV5647_I2C_ADDR               0x36
#define OV5647_REG_CHIP_ID_HI         0x300A   /* 0x300A/0x300B = 0x56/0x47 */
#define OV5647_CHIP_ID                0x5647

/* Boot confirmation tone. Faded in/out so the amplifier never sees a step. */
#define BOOT_BEEP_HZ                  880
#define BOOT_BEEP_MS                  120
#define BOOT_BEEP_AMPLITUDE           8000

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

/*
 * Copies a configured string into one of the fixed-size globals above,
 * truncating rather than overflowing.
 *
 * Written as a clamped memcpy instead of strncpy because GCC 14 rejects
 * strncpy here under -Werror: it cannot prove the source is shorter than
 * the destination and so warns about a possibly unterminated result
 * (-Wstringop-truncation).
 */
static void settings_copy(char *dst, size_t dst_size,
                          const char *key, const char *def_val)
{
    const char *src = config_get_string(key, def_val);
    size_t n = strlen(src);
    if (n > dst_size - 1) n = dst_size - 1;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

/*
 * load_settings() no longer parses settings.txt itself. It:
 *   1. hands the file to the generic Config module (config_load()),
 *   2. pulls every non-MQTT firmware setting out via config_get_*(),
 *      falling back to the DEFAULT_* constants above for anything
 *      settings.txt doesn't define.
 *
 * MQTT configuration is intentionally NOT read here – it's loaded
 * separately in app_main() via mqtt_config_load() (mqtt_config.h), which
 * reads through the same Config module. That keeps the MQTT subsystem's
 * configuration self-contained and documented next to the struct it
 * populates, rather than mixed into this function.
 *
 * A missing settings.txt, or any single missing/invalid key, is never
 * fatal: config_load()/config_get_*() always hand back a usable default
 * and log a warning.
 */
static void load_settings(void)
{
    const char *path = BSP_SD_MOUNT_POINT "/settings.txt";

    /* main.c already parsed this file once. Reload only if that did
     * not happen (no SD at boot). A failed remount must not wipe the
     * keys we already have. */
    if (config_count() == 0 && !config_load(path)) {
        ESP_LOGI(TAG, "[settings] using defaults for every key");
    }

    settings_copy(g_wifi_ssid, sizeof(g_wifi_ssid),
                  "WIFI_SSID", DEFAULT_WIFI_SSID);
    settings_copy(g_wifi_pass, sizeof(g_wifi_pass),
                  "WIFI_PASS", DEFAULT_WIFI_PASS);
    settings_copy(g_device_id, sizeof(g_device_id),
                  "DEVICE_ID", g_device_id);

    g_rms_threshold        = config_get_int("RMS_THRESHOLD",
                                            DEFAULT_RMS_THRESHOLD);
    g_sample_rate          = config_get_int("SAMPLE_RATE",
                                            DEFAULT_SAMPLE_RATE);
    g_bits_per_sample      = config_get_int("BITS_PER_SAMPLE",
                                            DEFAULT_BITS_PER_SAMPLE);
    g_record_buffer_frames = config_get_int("RECORD_BUFFER_FRAMES",
                                            DEFAULT_RECORD_BUFFER_FRAMES);
    g_discard_samples      = config_get_int("DISCARD_SAMPLES",
                                            DEFAULT_DISCARD_SAMPLES);

    g_preroll_ms           = config_get_int("PREROLL_MS",
                                            DEFAULT_PREROLL_MS);
    g_silence_timeout_ms   = config_get_int("SILENCE_TIMEOUT_MS",
                                            DEFAULT_SILENCE_TIMEOUT_MS);
    g_min_record_ms        = config_get_int("MIN_RECORD_MS",
                                            DEFAULT_MIN_RECORD_MS);

    /* RECORD_DURATION_MS is the historical key name for this field;
     * MAX_RECORD_MS is accepted as an alias. If both are present,
     * MAX_RECORD_MS wins — the previous parser checked it second, so
     * that is the behaviour existing SD cards were written against. */
    g_max_record_ms = config_get_int("RECORD_DURATION_MS",
                                     DEFAULT_MAX_RECORD_MS);
    g_max_record_ms = config_get_int("MAX_RECORD_MS", g_max_record_ms);

    g_cooldown_drain_ms    = config_get_int("COOLDOWN_DRAIN_MS",
                                            DEFAULT_COOLDOWN_DRAIN_MS);
    g_cooldown_rms_checks  = config_get_int("COOLDOWN_RMS_CHECKS",
                                            DEFAULT_COOLDOWN_RMS_CHECKS);
    g_post_record_delay_ms = config_get_int("POST_RECORD_DELAY_MS",
                                            DEFAULT_POST_RECORD_DELAY_MS);
    g_timezone_offset_sec  = config_get_int("TIMEZONE_OFFSET_SEC",
                                            DEFAULT_TIMEZONE_OFFSET_SEC);

    ESP_LOGI(TAG, "[settings] DEVICE=%s SSID=%s RMS=%d",
             g_device_id, g_wifi_ssid, g_rms_threshold);

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

/* ── Suspended for a video call ─────────────────────────────────────
 *
 * Set by sentinel_app_suspend(), cleared by sentinel_app_resume().
 *
 * s_ui_frozen is checked in ui_lock(), which every UI update in this file
 * goes through, so one flag stops the status text, the RMS bar, the clock,
 * the WiFi icon and the queue count from invalidating widgets. That is not
 * enough on its own: the LVGL adapter task still refreshes every 15 ms
 * into the same DPI framebuffers av_render decodes into. Dummy-draw plus
 * pausing the adapter is what actually leaves the panel to the call.
 *
 * s_audio_yielded stops the loudspeaker path. Broadcast chunks that arrive
 * over MQTT during a call are dropped rather than queued: playing them
 * afterwards would put minutes-old audio into the room, and holding them
 * would run the queue out of PSRAM on a long call.
 * ──────────────────────────────────────────────────────────────────── */
static volatile bool s_ui_frozen     = false;
static volatile bool s_audio_yielded = false;
static lv_display_t *s_disp;

/* ══════════════════════════ DISPLAY ════════════════════════════ */

static lv_obj_t *lbl_status;
static lv_obj_t *lbl_time;
static lv_obj_t *lbl_wifi;
static lv_obj_t *lbl_queue;
static lv_obj_t *lbl_rms;
static lv_obj_t *rms_bar;
static lv_obj_t *lbl_periph;   /* boot peripheral self-test panel */

/*
 * Scales a metric that was authored against the BOX-3's 320 px-wide panel
 * to whatever this display actually is (720 px here). Keeping the original
 * numbers and scaling them preserves the proportions of the old layout and
 * means the UI does not have to be touched again if the panel changes.
 */
static int32_t ui_scale(int32_t v)
{
    return v * lv_display_get_horizontal_resolution(NULL) / 320;
}

/*
 * Every UI update below is cosmetic, and several of them are issued from
 * the audio tasks: display_set_rms() from capture_task once per DMA frame,
 * display_set_status() from player_task between broadcast chunks. Taking
 * the LVGL mutex with "block forever" there is what the BOX-3 did, but it
 * is not safe on this board: redrawing a label on a 720x1280 MIPI panel is
 * ~40x the work it was on the 320x240 S3 panel, so the LVGL task holds the
 * mutex far longer and capture_task (prio 18) contends for it ~15x a
 * second. player_task (prio 8) then loses the race, stops draining
 * s_play_queue, and the broadcast is dropped as "queue full".
 *
 * A cosmetic update is never worth stalling audio for: take the mutex with
 * a bounded timeout and skip the update if the UI is busy.
 */
#define UI_LOCK_TIMEOUT_MS   100u
#define UI_STATUS_LOCK_MS    800u

static bool ui_lock_timeout(uint32_t timeout_ms)
{
    /* While a call owns the panel, av_render is decoding straight into the
     * framebuffer. Refusing the lock here is what keeps every UI update in
     * this file — status, RMS, clock, WiFi icon, queue count — from
     * repainting over the video, without having to gate each one. */
    if (s_ui_frozen) {
        return false;
    }
    return bsp_display_lock(timeout_ms);
}

/* Hand the DPI framebuffers to av_render for the length of a call.
 * Dummy-draw skips LVGL's flush; pause stops lv_timer_handler so it
 * cannot keep writing the listening UI into those same buffers. */
static void ui_yield_panel_to_call(bool yield)
{
    lv_display_t *disp = s_disp ? s_disp : lv_display_get_default();
    if (disp == NULL) {
        ESP_LOGW(TAG, "No LVGL display; cannot %s the panel",
                 yield ? "yield" : "return");
        return;
    }

    if (yield) {
        esp_err_t err = esp_lv_adapter_set_dummy_draw(disp, true);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "dummy draw on failed: %s", esp_err_to_name(err));
        }
        err = esp_lv_adapter_pause(-1);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "LVGL pause failed: %s", esp_err_to_name(err));
        } else {
            ESP_LOGI(TAG, "Panel yielded to av_render");
        }
    } else {
        esp_err_t err = esp_lv_adapter_resume();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "LVGL resume failed: %s", esp_err_to_name(err));
        }
        err = esp_lv_adapter_set_dummy_draw(disp, false);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "dummy draw off failed: %s", esp_err_to_name(err));
        } else {
            ESP_LOGI(TAG, "Panel returned to LVGL");
        }
    }
}

static bool ui_lock(void)
{
    return ui_lock_timeout(UI_LOCK_TIMEOUT_MS);
}

static void display_init(void)
{
    s_disp = bsp_display_start();
    bsp_display_backlight_on();
    bsp_display_lock(-1);

    lv_obj_t *scr = lv_screen_active();
    const int32_t disp_w = lv_display_get_horizontal_resolution(NULL);

    /* Montserrat only exists in discrete sizes, so the fonts step up
     * rather than scale continuously: the 16/24 pair used on 320 px
     * becomes 32/48 at 720 px, with 24 for the status bar. */
    const lv_font_t *font_bar = &lv_font_montserrat_24;
    const lv_font_t *font_mid = &lv_font_montserrat_32;
    const lv_font_t *font_big = &lv_font_montserrat_48;

    lv_obj_set_style_bg_color(scr, lv_color_hex(0x1a1a2e), 0);

    lv_obj_t *top = lv_obj_create(scr);
    lv_obj_set_size(top, disp_w, ui_scale(36));
    lv_obj_align(top, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_style_bg_color(top, lv_color_hex(0x16213e), 0);
    lv_obj_set_style_border_width(top, 0, 0);
    lv_obj_set_style_radius(top, 0, 0);
    lv_obj_set_style_pad_all(top, ui_scale(8), 0);
    lv_obj_remove_flag(top, LV_OBJ_FLAG_SCROLLABLE);

    lbl_wifi = lv_label_create(top);
    lv_label_set_text(lbl_wifi, LV_SYMBOL_WARNING);
    lv_obj_set_style_text_color(lbl_wifi, lv_color_hex(0xff6b6b), 0);
    lv_obj_set_style_text_font(lbl_wifi, font_bar, 0);
    lv_obj_align(lbl_wifi, LV_ALIGN_LEFT_MID, 0, 0);

    lbl_time = lv_label_create(top);
    lv_label_set_text(lbl_time, "--:--");
    lv_obj_set_style_text_color(lbl_time, lv_color_hex(0xeaeaea), 0);
    lv_obj_set_style_text_font(lbl_time, font_bar, 0);
    lv_obj_align(lbl_time, LV_ALIGN_RIGHT_MID, 0, 0);

    lbl_rms = lv_label_create(scr);
    lv_label_set_text(lbl_rms, "RMS: ----");
    lv_obj_set_style_text_color(lbl_rms, lv_color_hex(0x00ffff), 0);
    lv_obj_set_style_text_font(lbl_rms, font_mid, 0);
    lv_obj_align(lbl_rms, LV_ALIGN_TOP_MID, 0, ui_scale(45));

    rms_bar = lv_bar_create(scr);
    lv_obj_set_size(rms_bar, ui_scale(280), ui_scale(28));
    lv_bar_set_range(rms_bar, 0, 1000);
    lv_bar_set_value(rms_bar, 0, LV_ANIM_OFF);
    lv_obj_align(rms_bar, LV_ALIGN_TOP_MID, 0, ui_scale(70));
    lv_obj_set_style_bg_color(rms_bar, lv_color_hex(0x333333), LV_PART_MAIN);
    lv_obj_set_style_bg_color(rms_bar, lv_color_hex(0x00ff00),
                              LV_PART_INDICATOR);

    lbl_status = lv_label_create(scr);
    lv_label_set_text(lbl_status, "Starting...");
    lv_obj_set_style_text_color(lbl_status, lv_color_hex(0x4ecca3), 0);
    lv_obj_set_style_text_font(lbl_status, font_big, 0);
    lv_obj_align(lbl_status, LV_ALIGN_CENTER, 0, ui_scale(20));

    /* Peripheral self-test results, filled in during boot and then left
     * on screen — there is plenty of room on a 1280 px-tall panel and it
     * is useful at a glance. */
    lbl_periph = lv_label_create(scr);
    lv_label_set_text(lbl_periph, "");
    lv_obj_set_style_text_color(lbl_periph, lv_color_hex(0x888888), 0);
    lv_obj_set_style_text_font(lbl_periph, font_bar, 0);
    lv_obj_set_style_text_align(lbl_periph, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_align(lbl_periph, LV_ALIGN_CENTER, 0, ui_scale(80));

    lbl_queue = lv_label_create(scr);
    lv_label_set_text(lbl_queue, "");
    lv_obj_set_style_text_color(lbl_queue, lv_color_hex(0x888888), 0);
    lv_obj_set_style_text_font(lbl_queue, font_bar, 0);
    lv_obj_align(lbl_queue, LV_ALIGN_BOTTOM_MID, 0, -ui_scale(10));

    bsp_display_unlock();
}

static void display_set_status(const char *text, uint32_t color)
{
    if (!ui_lock_timeout(UI_STATUS_LOCK_MS)) return;
    lv_label_set_text(lbl_status, text);
    lv_obj_set_style_text_color(lbl_status, lv_color_hex(color), 0);
    bsp_display_unlock();
}

static void display_set_peripherals(const char *text, uint32_t color)
{
    if (!ui_lock_timeout(UI_STATUS_LOCK_MS)) return;
    lv_label_set_text(lbl_periph, text);
    lv_obj_set_style_text_color(lbl_periph, lv_color_hex(color), 0);
    bsp_display_unlock();
}

/*
 * Called from capture_task for every DMA frame (~15 Hz at the defaults).
 * Redrawing the readout and bar that often on a 720x1280 panel is pure
 * mutex pressure for no visible benefit, so it is throttled to ~4 Hz.
 */
#define RMS_UI_INTERVAL_MS  250u

static void display_set_rms(int16_t rms)
{
    static uint32_t last_ms = 0;
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (now_ms - last_ms < RMS_UI_INTERVAL_MS) return;
    last_ms = now_ms;

    int range = g_rms_threshold > 200 ? g_rms_threshold : 200;
    char buf[40];
    snprintf(buf, sizeof(buf), "RMS: %d (thr: %d)", rms, g_rms_threshold);
    if (!ui_lock_timeout(UI_STATUS_LOCK_MS)) return;
    lv_label_set_text(lbl_rms, buf);
    lv_bar_set_range(rms_bar, 0, range);
    int v = rms < 0 ? 0 : (int)rms;
    if (v > range) v = range;
    lv_bar_set_value(rms_bar, v, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(rms_bar,
        lv_color_hex(rms > g_rms_threshold ? 0xff0000 : 0x00ff00),
        LV_PART_INDICATOR);
    bsp_display_unlock();
}

static void display_set_wifi(bool connected)
{
    if (!ui_lock_timeout(UI_STATUS_LOCK_MS)) return;
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
    if (!ui_lock_timeout(UI_STATUS_LOCK_MS)) return;
    lv_label_set_text(lbl_time, s);
    bsp_display_unlock();
}

static void display_set_queue_count(int count)
{
    if (!ui_lock()) return;
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
/*
 * Copies a settings string into one of esp_wifi's fixed-size fields,
 * truncating if necessary and always leaving it NUL-terminated.
 *
 * Written with an explicitly clamped memcpy because GCC 14 rejects both
 * of the obvious alternatives under -Werror: strncpy trips
 * -Wstringop-truncation (it would not terminate a maximally long source)
 * and snprintf trips -Wformat-truncation (the 64-byte settings buffers
 * provably exceed the 32-byte SSID field). Clamping the length at runtime
 * leaves the compiler nothing to prove.
 */
static void wifi_copy_field(uint8_t *dst, size_t dst_size, const char *src)
{
    size_t n = strlen(src);
    if (n > dst_size - 1) n = dst_size - 1;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

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
    wifi_copy_field(wc.sta.ssid,     sizeof(wc.sta.ssid),     g_wifi_ssid);
    wifi_copy_field(wc.sta.password, sizeof(wc.sta.password), g_wifi_pass);

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
    /* esp_sntp_init() posts to the lwIP tcpip mbox. That mbox does not
     * exist until wifi_init() has called esp_netif_init(). */
    if (!g_wifi_ssid[0]) {
        ESP_LOGW(TAG, "NTP skipped (WiFi not started)");
        return;
    }
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

    if (mic_codec_dev) esp_codec_dev_set_in_gain(mic_codec_dev,
                                                 MIC_IN_GAIN_DB);
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

    bool ok = audiosense_mqtt_stream_start(g_device_id, ts,
                                           sample_rate, bits_per_sample,
                                           channels);
    display_set_status(ok ? "Streaming..." : "No MQTT!",
                       ok ? 0xffa500 : 0xff6b6b);
    return ok;
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
 * arrives on <commands>/<id>/control.
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

/* ══════════════════════ SPEAKER OPEN / WRITE / CLOSE ═══════════ *
 *
 * Both playback paths (SD queue and live MQTT stream) go through these
 * helpers so the traps below are handled in one place.
 *
 * The lifecycle here is deliberately the BOX-3's: open the output path
 * for each broadcast and close it again afterwards. An earlier version of
 * this port opened it once at boot and then only muted/unmuted between
 * broadcasts, on the theory that fewer open/close cycles is safer. On
 * this board that silences the loudspeaker completely, for two reasons
 * that compound:
 *
 *   - esp_codec_dev only reprograms the codec inside open(). It is
 *     open() that calls es8311_set_fs() and es8311_enable(), and
 *     es8311_enable() is what runs es8311_start() and powers the
 *     amplifier on GPIO 53. Held open forever, that sequence runs exactly
 *     once — during the boot self-test, before the microphone exists —
 *     and is never re-applied. esp_codec_dev_set_out_mute() only toggles
 *     one DAC register; it cannot re-arm a DAC path the shared I2S layer
 *     has since reclocked underneath it.
 *
 *   - Holding it open changes what happens when capture_task opens the
 *     microphone. audio_codec_data_i2s.c reconfigures the peer TX clock
 *     alongside RX only when the output side is *not* enabled ("TX is
 *     master, set to RX not take effect need reconfig TX also"). With the
 *     speaker held open that branch is skipped, so the microphone takes
 *     over the shared clock and the DAC — never reprogrammed again — goes
 *     quiet while every write still reports success.
 *
 * Closing after each broadcast means the next one re-runs set_fs +
 * enable + PA power from scratch, which is why it works on the BOX-3 and
 * why it is restored here.
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
 *   capture_task holds the microphone open for the whole session, which
 *   pins the shared I2S port to g_sample_rate / g_bits_per_sample. The
 *   shared-I2S layer rejects any playback format that disagrees, but
 *   esp_codec_dev_open() discards that error: the I2S clock stays at
 *   the microphone rate while the DAC is programmed for the requested
 *   rate. Resolve it here, loudly, instead.
 * ════════════════════════════════════════════════════════════ */

/* The output path is mono: the shared I2S port is pinned by the
 * continuously-open microphone, which capture_task opens with channel=1. */
#define SPK_CHANNELS 1

static bool spk_open(int sample_rate, int bits_per_sample, int channels)
{
    if (!spk_codec_dev) return false;

    if (sample_rate     <= 0) sample_rate     = g_sample_rate;
    if (bits_per_sample <= 0) bits_per_sample = g_bits_per_sample;
    if (channels        <= 0) channels        = SPK_CHANNELS;

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

    if (channels != SPK_CHANNELS) {
        ESP_LOGW(TAG,
            "[spk] requested %d channels but the shared I2S port is "
            "pinned to %d by the microphone – playing as mono, stereo "
            "material will be pitched up", channels, SPK_CHANNELS);
        channels = SPK_CHANNELS;
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
     * volume and unmute rather than trusting the values cached at init
     * to survive the power-down cycle. */
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

/* End of a broadcast: power the amplifier down and release the output
 * side of the shared I2S port so the next open reprograms the codec. */
static void spk_close(void)
{
    if (spk_codec_dev) esp_codec_dev_close(spk_codec_dev);
}

/* ══════════════════════ BOOT PERIPHERAL SELF-TEST ══════════════ *
 *
 * Reports on screen what is actually attached, during boot.
 *
 * The camera is genuinely detectable. The OV5647's SCCB lines sit on the
 * same I2C bus as the codec and the touch controller, so probing its
 * address and reading its chip ID establishes both that it is plugged in
 * and that it is answering correctly.
 *
 * The loudspeaker is not, and it is worth being clear about why: it is a
 * passive driver on the far side of the amplifier, and neither the
 * ES8311 nor the P4 senses load current. No register can tell "speaker
 * attached" from "speaker unplugged". What *is* verifiable is the whole
 * chain up to the amplifier pin — codec answering on I2C, output path
 * opening at the session format, PCM writes accepted — and the boot beep
 * then closes the last link through your ears. So a PASS here means
 * "everything up to the speaker terminals is working"; if you hear the
 * beep, the speaker is fine too.
 * ════════════════════════════════════════════════════════════ */

typedef enum { CHK_PASS = 0, CHK_WARN = 1, CHK_FAIL = 2 } chk_t;

static bool i2c_present(uint16_t addr)
{
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    if (!bus) return false;
    return i2c_master_probe(bus, addr, 200) == ESP_OK;
}

/* OV5647 registers are 16-bit addressed, high byte first. */
static esp_err_t ov5647_read_reg(uint16_t reg, uint8_t *out)
{
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    if (!bus) return ESP_ERR_INVALID_STATE;

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = OV5647_I2C_ADDR,
        .scl_speed_hz    = 100000,
    };
    i2c_master_dev_handle_t dev = NULL;
    esp_err_t err = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
    if (err != ESP_OK) return err;

    uint8_t addr_be[2] = { (uint8_t)(reg >> 8), (uint8_t)(reg & 0xff) };
    err = i2c_master_transmit_receive(dev, addr_be, sizeof(addr_be),
                                      out, 1, 200);
    i2c_master_bus_rm_device(dev);
    return err;
}

static chk_t camera_selftest(char *detail, size_t n)
{
    if (!i2c_present(OV5647_I2C_ADDR)) {
        snprintf(detail, n, "not detected");
        return CHK_FAIL;
    }

    uint8_t hi = 0, lo = 0;
    if (ov5647_read_reg(OV5647_REG_CHIP_ID_HI,     &hi) != ESP_OK ||
        ov5647_read_reg(OV5647_REG_CHIP_ID_HI + 1, &lo) != ESP_OK) {
        snprintf(detail, n, "answers, no ID");
        return CHK_WARN;
    }

    uint16_t id = ((uint16_t)hi << 8) | lo;
    if (id != OV5647_CHIP_ID) {
        snprintf(detail, n, "wrong ID 0x%04X", (unsigned)id);
        return CHK_WARN;
    }

    snprintf(detail, n, "OV5647 ok");
    return CHK_PASS;
}

/*
 * No mutex is taken around the speaker here: this runs from app_main
 * before player_task and the capture pipeline are created, so nothing
 * else can be touching the codec yet.
 */
static chk_t speaker_selftest(char *detail, size_t n)
{
    if (!i2c_present(ES8311_I2C_ADDR)) {
        snprintf(detail, n, "codec absent");
        return CHK_FAIL;
    }
    if (!spk_codec_dev) {
        snprintf(detail, n, "codec init failed");
        return CHK_FAIL;
    }
    if (!spk_open(g_sample_rate, g_bits_per_sample, SPK_CHANNELS)) {
        snprintf(detail, n, "output open failed");
        return CHK_FAIL;
    }

    const int samples = g_sample_rate * BOOT_BEEP_MS / 1000;

    /* Internal, DMA-capable RAM: this buffer goes straight to I2S. */
    int16_t *tone = heap_caps_malloc((size_t)samples * sizeof(int16_t),
                                     MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    if (!tone) {
        spk_close();
        snprintf(detail, n, "chain ok, beep OOM");
        return CHK_WARN;
    }

    const int fade = samples / 8;
    for (int i = 0; i < samples; i++) {
        float env = 1.0f;
        if (i < fade)                    env = (float)i / (float)fade;
        else if (i > samples - fade)     env = (float)(samples - i) / (float)fade;

        float ph = 2.0f * (float)M_PI * (float)BOOT_BEEP_HZ
                 * (float)i / (float)g_sample_rate;
        tone[i] = (int16_t)(BOOT_BEEP_AMPLITUDE * env * sinf(ph));
    }

    bool ok = spk_write(tone, samples * (int)sizeof(int16_t));
    heap_caps_free(tone);
    /* Must close, not just mute: capture_task opens the microphone next,
     * and the shared-I2S layer only reclocks TX alongside RX while the
     * output side is closed. Leaving it open here is exactly what made
     * every later broadcast silent. */
    spk_close();

    if (!ok) {
        snprintf(detail, n, "write rejected");
        return CHK_FAIL;
    }
    snprintf(detail, n, "chain ok + beep");
    return CHK_PASS;
}

static void run_peripheral_selftest(void)
{
    char spk_detail[32] = "", cam_detail[32] = "";

    chk_t spk = speaker_selftest(spk_detail, sizeof(spk_detail));
    chk_t cam = camera_selftest(cam_detail, sizeof(cam_detail));
    chk_t sd  = bsp_sdcard ? CHK_PASS : CHK_FAIL;

    static const char *mark[] = {
        [CHK_PASS] = LV_SYMBOL_OK,
        [CHK_WARN] = LV_SYMBOL_WARNING,
        [CHK_FAIL] = LV_SYMBOL_CLOSE,
    };

    /* One label, so it takes the colour of the worst result. */
    chk_t worst = spk;
    if (cam > worst) worst = cam;
    if (sd  > worst) worst = sd;

    static const uint32_t tint[] = {
        [CHK_PASS] = 0x4ecca3,
        [CHK_WARN] = 0xffaa00,
        [CHK_FAIL] = 0xff6b6b,
    };

    char panel[192];
    snprintf(panel, sizeof(panel),
             "%s Speaker  %s\n"
             "%s Camera   %s\n"
             "%s SD card  %s",
             mark[spk], spk_detail,
             mark[cam], cam_detail,
             mark[sd],  sd == CHK_PASS ? "mounted" : "not mounted");

    display_set_peripherals(panel, tint[worst]);

    ESP_LOGI(TAG, "[selftest] speaker=%s | camera=%s | sd=%s",
             spk_detail, cam_detail,
             sd == CHK_PASS ? "mounted" : "not mounted");
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
        spk_close();
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
    spk_close();
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
     * port; entering CAP_LIVE during talk-back contended with playback
     * and silenced the loudspeaker. Live TX is only started by an
     * explicit start_live_audio on commands/<id>/control.
     */
}

static void on_play_stream_chunk(const void *data, int len)
{
    if (!s_play_queue) return;

    /* A call owns the loudspeaker. Dropped rather than queued: playing a
     * broadcast after the call would put minutes-old audio into the room,
     * and holding one would exhaust the queue's PSRAM on a long call. */
    if (s_audio_yielded) {
        return;
    }

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
                spk_close();
                codec_open = false;
                xSemaphoreGive(s_spk_mutex);
                display_set_status("Idle", 0x4ecca3);
                ESP_LOGW(TAG, "[player] stream timeout – codec closed");
            }
            continue;
        }

        if (pc.is_end) {
            if (codec_open) {
                spk_close();
                codec_open = false;
                xSemaphoreGive(s_spk_mutex);
            }
            display_set_status("Idle", 0x4ecca3);
            ESP_LOGI(TAG, "[player] stream complete");
            continue;
        }

        /* Yielded mid-broadcast: close the output path now rather than at
         * the next queue timeout, because the caller is about to be given
         * this codec. */
        if (s_audio_yielded) {
            if (codec_open) {
                spk_close();
                codec_open = false;
                xSemaphoreGive(s_spk_mutex);
                ESP_LOGI(TAG, "[player] speaker released for a call");
            }
            heap_caps_free(pc.data);
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
            spk_close();
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

/* Was app_main(). Split out so the mode arbiter can start the sentinel and
 * later take the hardware away from it; nothing about how it listens,
 * triggers, uploads or plays has changed.
 *
 * The three "halt forever" paths are now returns: in this image a sentinel
 * that cannot start must still leave the intercom able to take a call, so
 * failures are reported upward instead of parking the CPU. NVS and the
 * display are brought up by main.c before this is called, because the
 * intercom needs them too. */
esp_err_t sentinel_app_init(void)
{
    /* LVGL and the panel. The intercom's board.c is an adapter over this
     * rather than a second initialiser, and takes the panel handle from
     * the BSP when a call needs to render into the same framebuffer. */
    display_init();
    display_set_status("Booting...", 0x4ecca3);

    /* ── Speaker mutex (FIX 4) ───────────────────────────────── */
    s_spk_mutex = xSemaphoreCreateMutex();
    if (!s_spk_mutex) {
        ESP_LOGE(TAG, "Speaker mutex alloc failed");
        return ESP_ERR_NO_MEM;
    }

    /* ── SD Card ───────────────────────────────────────────────
     *
     * The card is already mounted from app_main(). A second
     * bsp_sdcard_mount() used to fail because the SD LDO is already
     * held, and that failure used to skip load_settings() — leaving
     * WIFI_SSID empty even though settings.txt had been read. */
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
    }
    load_settings();
    display_set_status(g_wifi_ssid[0] ? "Settings OK" : "No WiFi in settings",
                       g_wifi_ssid[0] ? 0x00ff00 : 0xff6b6b);
    vTaskDelay(pdMS_TO_TICKS(400));

    /* ── Audio ───────────────────────────────────────────────── */
    display_set_status("Audio init...", 0x4ecca3);
    audio_init();
    display_set_status(
        (mic_codec_dev && spk_codec_dev) ? "Audio OK" : "Audio ERROR!",
        (mic_codec_dev && spk_codec_dev) ? 0x00ff00  : 0xff0000);
    vTaskDelay(pdMS_TO_TICKS(500));

    /* ── Peripherals (external speaker chain + OV5647 camera) ──
     *
     * Runs before the pipeline and player_task exist, so it has the
     * codec to itself and the boot beep cannot collide with playback. */
    display_set_status("Self-test...", 0x4ecca3);
    run_peripheral_selftest();
    vTaskDelay(pdMS_TO_TICKS(1500));

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
                    display_set_wifi(true);
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
    if (!g_wifi_ssid[0]) {
        ESP_LOGW(TAG, "MQTT/HTTP skipped (WiFi not started)");
    } else {
    display_set_status("MQTT init...", 0x4ecca3);

    /* Broker, credentials, TLS setting and topic roots come from
     * settings.txt (falling back to documented defaults for anything not
     * present there) through the same Config module load_settings() used
     * above – see mqtt_config.h for the key list. */
    static mqtt_config_t s_mqtt_cfg;
    if (!mqtt_config_load(&s_mqtt_cfg)) {
        ESP_LOGW(TAG, "MQTT configuration is incomplete – init will be "
                 "attempted anyway so the rest of the device stays up");
    }

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
    if (audiosense_mqtt_init(&s_mqtt_cfg)) {
        audiosense_mqtt_set_play_callback(on_mqtt_play_received);
        audiosense_mqtt_set_play_stream_callbacks(
            on_play_stream_start,
            on_play_stream_chunk,
            on_play_stream_end);
        /* init() only starts the client. The panel's "MQTT OK" is the
         * CONNECTED event, which needs the SDIO path to stay up through
         * TLS. */
        bool mqtt_up = false;
        for (int i = 0; i < 30; i++) {
            if (audiosense_mqtt_is_connected()) {
                mqtt_up = true;
                break;
            }
            char prog[24];
            snprintf(prog, sizeof(prog), "MQTT... %ds", i / 2);
            display_set_status(prog, 0xffaa00);
            vTaskDelay(pdMS_TO_TICKS(500));
        }
        display_set_status(mqtt_up ? "MQTT OK" : "MQTT wait",
                           mqtt_up ? 0x00ff00 : 0xffaa00);
        vTaskDelay(pdMS_TO_TICKS(mqtt_up ? 500 : 1500));
    } else {
        display_set_status("MQTT init fail", 0xff6b6b);
        vTaskDelay(pdMS_TO_TICKS(1500));
    }

    start_http_server();
    }

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
        ESP_LOGE(TAG, "audio_pipeline_init failed");
        return ESP_FAIL;
    }

    if (audio_pipeline_start() != ESP_OK) {
        display_set_status("Pipeline start!", 0xff0000);
        ESP_LOGE(TAG, "audio_pipeline_start failed");
        return ESP_FAIL;
    }

    /*
     * Task stack summary:
     *   capture_task  8192 B PSRAM  (prio 18, Core 1)
     *   streamer_task 8192 B PSRAM  (prio  4, Core 0)
     *   player_task   8192 B        (prio  8, Core ?)  FIX 3
     *   time_task     8192 B        (prio  3)  LVGL clock + heap diag
     */
    xTaskCreate(player_task,      "player",  8192, NULL, 8, NULL);
    xTaskCreate(time_update_task, "time",    8192, NULL, 3, NULL);

    display_set_status("Idle", 0x4ecca3);
    ESP_LOGI(TAG,
        "=== AUDIOSENSE READY  device=%s thr=%d  "
        "pipeline: cap(core1,p18) str(core0,p4)  ring~2MiB PSRAM ===",
        g_device_id, g_rms_threshold);
    return ESP_OK;
}

/* ══════════════════ MODE ARBITER CONTROL SURFACE ═══════════════════
 *
 * The hardware handover, from the sentinel's side. See mode_arbiter.h for
 * why it has to be synchronous and why it happens before the offer is
 * parsed rather than when the call connects.
 * ════════════════════════════════════════════════════════════════════ */

esp_err_t sentinel_app_suspend(uint32_t timeout_ms)
{
    if (s_audio_yielded) {
        return ESP_OK;
    }
    ESP_LOGI(TAG, "Suspending: handing the codecs to a call");

    /* Order matters. The loudspeaker first, because it is released
     * asynchronously by player_task and giving it a head start costs
     * nothing; the microphone second, because that is the one the caller
     * has to wait for. */
    s_audio_yielded = true;

    /* Leaving live audio is part of the pipeline's own suspend path, but
     * the dashboard is told here: a retained {"live":false} stops it
     * believing this sentinel is still streaming for the length of the
     * call. */
    if (audio_pipeline_is_live()) {
        audio_pipeline_set_live(false);
    }

    audio_pipeline_suspend(true);

    /* An utterance in flight finishes first, so this can legitimately take
     * a second or two. */
    const uint32_t step_ms = 20;
    uint32_t waited = 0;
    while (!audio_pipeline_is_suspended() && waited < timeout_ms) {
        vTaskDelay(pdMS_TO_TICKS(step_ms));
        waited += step_ms;
    }

    /* Whatever player_task was doing, make sure the output side is shut:
     * it releases the codec on its next loop, but a broadcast that ended
     * moments ago may have left it open on the timeout path. */
    if (xSemaphoreTake(s_spk_mutex, pdMS_TO_TICKS(200)) == pdTRUE) {
        spk_close();
        xSemaphoreGive(s_spk_mutex);
    }

    /* Last: freeze the UI and stop LVGL from flushing the listening
     * screen over the call's video. */
    s_ui_frozen = true;
    ui_yield_panel_to_call(true);

    if (!audio_pipeline_is_suspended()) {
        ESP_LOGE(TAG, "Microphone not released after %" PRIu32 " ms",
                 timeout_ms);
        return ESP_ERR_TIMEOUT;
    }
    ESP_LOGI(TAG, "Suspended in %" PRIu32 " ms; codecs are free", waited);
    return ESP_OK;
}

esp_err_t sentinel_app_resume(void)
{
    if (!s_audio_yielded) {
        return ESP_OK;
    }
    ESP_LOGI(TAG, "Resuming: taking the codecs back");

    s_ui_frozen     = false;
    s_audio_yielded = false;
    ui_yield_panel_to_call(false);
    audio_pipeline_suspend(false);

    /* Bounded wait so the caller learns about a microphone that did not
     * reopen; the pipeline logs the detail. Generous because reopening
     * includes the codec warm-up discard. */
    const uint32_t step_ms = 20;
    uint32_t waited = 0;
    while (audio_pipeline_is_suspended() && waited < 5000) {
        vTaskDelay(pdMS_TO_TICKS(step_ms));
        waited += step_ms;
    }
    if (audio_pipeline_is_suspended()) {
        ESP_LOGE(TAG, "Microphone did not reopen after the call");
        return ESP_ERR_TIMEOUT;
    }
    ESP_LOGI(TAG, "Resumed in %" PRIu32 " ms", waited);
    return ESP_OK;
}

bool sentinel_app_is_suspended(void)
{
    return s_audio_yielded;
}

esp_codec_dev_handle_t sentinel_app_speaker_handle(void)
{
    return spk_codec_dev;
}

esp_codec_dev_handle_t sentinel_app_microphone_handle(void)
{
    return mic_codec_dev;
}

bool sentinel_app_wifi_connected(void)
{
    return wifi_is_connected();
}

void sentinel_app_set_status(const char *text, uint32_t colour)
{
    display_set_status(text, colour);
}

void sentinel_app_redraw(void)
{
    /* av_render has been writing over the framebuffer for the length of
     * the call, but LVGL's own invalidation bookkeeping says the screen is
     * clean, so nothing would be repainted. Invalidate the lot. */
    if (!bsp_display_lock(UI_STATUS_LOCK_MS)) {
        ESP_LOGW(TAG, "Could not take the UI lock to repaint after the call");
        return;
    }
    lv_obj_invalidate(lv_screen_active());
    bsp_display_unlock();
}