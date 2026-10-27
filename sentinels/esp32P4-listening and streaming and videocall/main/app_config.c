#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "app_config.h"
#include "bsp/esp-bsp.h"
#include "esp_log.h"
#include "settings.h"

static const char *TAG = "CONFIG";

static app_config_t s_cfg;

#define COPY_DEFAULT(field, value) \
    snprintf(s_cfg.field, sizeof(s_cfg.field), "%s", value)

static void set_defaults(void)
{
    memset(&s_cfg, 0, sizeof(s_cfg));
    COPY_DEFAULT(device_id, DEVICE_ID_DEFAULT);
    COPY_DEFAULT(wifi_ssid, WIFI_SSID_DEFAULT);
    COPY_DEFAULT(wifi_pass, WIFI_PASSWORD_DEFAULT);
    COPY_DEFAULT(mqtt_uri, MQTT_BROKER_URI_DEFAULT);
    COPY_DEFAULT(mqtt_user, MQTT_USERNAME_DEFAULT);
    COPY_DEFAULT(mqtt_pass, MQTT_PASSWORD_DEFAULT);
    COPY_DEFAULT(stun_url, ICE_STUN_URL_DEFAULT);
    COPY_DEFAULT(turn_user, ICE_TURN_USER_DEFAULT);
    COPY_DEFAULT(turn_pass, ICE_TURN_PASSWORD_DEFAULT);
    COPY_DEFAULT(openai_key, OPENAI_API_KEY_DEFAULT);

    s_cfg.debug_log_enabled = DEBUG_LOG_ENABLED_DEFAULT;
    s_cfg.debug_log_sync = DEBUG_LOG_SYNC_DEFAULT;
    COPY_DEFAULT(debug_log_path, DEBUG_LOG_PATH_DEFAULT);

    COPY_DEFAULT(video_stream_url, VIDEO_STREAM_URL_DEFAULT);
    s_cfg.video_stream_enabled = VIDEO_STREAM_ENABLED_DEFAULT;
    s_cfg.video_stream_width = VIDEO_STREAM_WIDTH_DEFAULT;
    s_cfg.video_stream_height = VIDEO_STREAM_HEIGHT_DEFAULT;
    s_cfg.video_stream_fps = VIDEO_STREAM_FPS_DEFAULT;
}

/* Case-insensitive so the video keys can be written the way they are
 * documented (ov5647_video_enabled) next to the historical upper-case ones
 * without the file having to pick one convention. */
static bool assign(const char *key, const char *val, const char *name,
                   char *dst, size_t dst_size)
{
    if (strcasecmp(key, name) != 0) {
        return false;
    }
    snprintf(dst, dst_size, "%s", val);
    return true;
}

static bool assign_int(const char *key, const char *val, const char *name,
                       int *dst, int min, int max)
{
    if (strcasecmp(key, name) != 0) {
        return false;
    }
    char *end = NULL;
    long parsed = strtol(val, &end, 10);
    if (end == val || parsed < min || parsed > max) {
        ESP_LOGW(TAG, "%s=%s is out of range [%d..%d], keeping %d", name, val,
                 min, max, *dst);
        return true;
    }
    *dst = (int)parsed;
    return true;
}

/* Anything but an explicit affirmative is off: a typo must not silently
 * start a stream on a unit that was meant to stay quiet. */
static bool assign_bool(const char *key, const char *val, const char *name,
                        bool *dst)
{
    if (strcasecmp(key, name) != 0) {
        return false;
    }
    *dst = (strcasecmp(val, "1") == 0 || strcasecmp(val, "true") == 0 ||
            strcasecmp(val, "yes") == 0 || strcasecmp(val, "on") == 0);
    return true;
}

#define TRY(name, field) \
    assign(key, val, name, s_cfg.field, sizeof(s_cfg.field))
#define TRY_INT(name, field, min, max) \
    assign_int(key, val, name, &s_cfg.field, min, max)
#define TRY_BOOL(name, field) assign_bool(key, val, name, &s_cfg.field)

void app_config_load(void)
{
    set_defaults();

    const char *path = BSP_SD_MOUNT_POINT "/settings.txt";
    FILE *f = fopen(path, "r");
    if (f == NULL) {
        ESP_LOGI(TAG, "No %s, using compiled-in defaults", path);
        return;
    }

    char line[320];
    int loaded = 0;
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }
        char *eq = strchr(line, '=');
        if (eq == NULL) {
            ESP_LOGW(TAG, "Ignoring malformed line: %s", line);
            continue;
        }
        *eq = '\0';
        const char *key = line;
        const char *val = eq + 1;

        if (TRY("DEVICE_ID", device_id) || TRY("WIFI_SSID", wifi_ssid) ||
            TRY("WIFI_PASS", wifi_pass) || TRY("MQTT_URI", mqtt_uri) ||
            TRY("MQTT_USER", mqtt_user) || TRY("MQTT_PASS", mqtt_pass) ||
            TRY("STUN_URL", stun_url) || TRY("TURN_USER", turn_user) ||
            TRY("TURN_PASS", turn_pass) || TRY("OPENAI_API_KEY", openai_key) ||
            TRY_BOOL("DEBUG", debug_log_enabled) ||
            TRY_BOOL("DEBUG_SYNC", debug_log_sync) ||
            TRY("DEBUG_LOG_PATH", debug_log_path) ||
            TRY_BOOL("OV5647_VIDEO_ENABLED", video_stream_enabled) ||
            TRY("VIDEO_STREAM_URL", video_stream_url) ||
            TRY_INT("VIDEO_STREAM_WIDTH", video_stream_width, 160, 1920) ||
            TRY_INT("VIDEO_STREAM_HEIGHT", video_stream_height, 120, 1920) ||
            TRY_INT("VIDEO_STREAM_FPS", video_stream_fps, 1, 15)) {
            loaded++;
        } else {
            /* Silent, unlike the standalone intercom build: in the merged
             * image most of settings.txt belongs to the sentinel's own
             * parser (config.c), and warning about every one of its keys
             * would bury the lines that matter. Each parser ignores the
             * other's half. */
            ESP_LOGD(TAG, "Not an intercom key, leaving to config.c: %s", key);
        }
    }
    fclose(f);

    /* esp_peer parses "stun:host:port"; "stun://" fails with
     * "Fail to resolve server address". Normalize SD overrides. */
    if (strncmp(s_cfg.stun_url, "stun://", 7) == 0) {
        memmove(s_cfg.stun_url + 5, s_cfg.stun_url + 7,
                strlen(s_cfg.stun_url + 7) + 1);
    } else if (strncmp(s_cfg.stun_url, "turn://", 7) == 0) {
        memmove(s_cfg.stun_url + 5, s_cfg.stun_url + 7,
                strlen(s_cfg.stun_url + 7) + 1);
    }

    ESP_LOGI(TAG, "Loaded %d override(s): device=%s ssid=%s broker=%s", loaded,
             s_cfg.device_id, s_cfg.wifi_ssid, s_cfg.mqtt_uri);
    if (s_cfg.wifi_ssid[0] == '\0') {
        ESP_LOGW(TAG, "No WIFI_SSID configured, the radio will stay down");
    }
    if (s_cfg.video_stream_enabled) {
        ESP_LOGI(TAG, "OV5647 uplink enabled: %dx%d @ %d fps to %s",
                 s_cfg.video_stream_width, s_cfg.video_stream_height,
                 s_cfg.video_stream_fps, s_cfg.video_stream_url);
    } else {
        ESP_LOGI(TAG, "OV5647 uplink disabled (ov5647_video_enabled=0)");
    }
}

const app_config_t *app_config_get(void)
{
    return &s_cfg;
}
