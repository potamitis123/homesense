/* Runtime configuration, overridable from the SD card.
 *
 * The compile-time values in settings.h are the fallback. Anything present
 * in /sdcard/settings.txt wins, which is what lets a unit be renamed or
 * pointed at a different broker without a rebuild — the field case this
 * board is actually used in.
 *
 * File format is one KEY=VALUE per line; blank lines and lines starting
 * with '#' are ignored. Recognised keys:
 *
 *     DEVICE_ID       WIFI_SSID       WIFI_PASS
 *     MQTT_URI        MQTT_USER       MQTT_PASS
 *     STUN_URL        TURN_USER       TURN_PASS
 *     OPENAI_API_KEY
 *     DEBUG           DEBUG_LOG_PATH
 *     ov5647_video_enabled
 *     VIDEO_STREAM_URL  VIDEO_STREAM_WIDTH  VIDEO_STREAM_HEIGHT
 *     VIDEO_STREAM_FPS
 *
 * Keys are matched case-insensitively, and any key the file omits keeps its
 * compile-time default — an older settings.txt with none of the video keys
 * in it is a supported file, not a broken one.
 */

#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    char device_id[32];
    char wifi_ssid[64];
    char wifi_pass[64];
    char mqtt_uri[128];
    char mqtt_user[64];
    char mqtt_pass[64];
    char stun_url[128];
    char turn_user[64];
    char turn_pass[64];
    char openai_key[192];

    /* Mirror the serial monitor to the SD card. Off unless DEBUG=1.
     * debug_log_sync trades throughput for losing nothing to a crash. */
    bool debug_log_enabled;
    bool debug_log_sync;
    char debug_log_path[64];

    /* Situational-awareness uplink. Off unless settings.txt turns it on. */
    bool video_stream_enabled;
    char video_stream_url[192];
    int video_stream_width;
    int video_stream_height;
    int video_stream_fps;
} app_config_t;

/**
 * @brief Populate from settings.h, then overlay /sdcard/settings.txt
 *
 * @note Safe to call with no SD card inserted: a missing or unreadable file
 *       leaves the compile-time defaults in place and is not an error.
 */
void app_config_load(void);

/**
 * @brief The active configuration
 */
const app_config_t *app_config_get(void);

#ifdef __cplusplus
}
#endif
