/*
 * mqtt_config.c  –  MQTT deployment configuration
 *
 * Reads through the generic Config module only; it never touches
 * settings.txt itself. See mqtt_config.h for the key list and defaults.
 */

#include "mqtt_config.h"
#include "config.h"

#include <string.h>

#include "esp_log.h"

static const char *TAG = "mqtt_cfg";

/*
 * Copies a configured string into a fixed field, warning if it had to be
 * truncated instead of letting the value silently change meaning — a
 * clipped hostname or topic root would fail in a way that is very hard to
 * read back from the broker side.
 */
static void copy_field(char *dst, size_t dst_size,
                       const char *key, const char *def_val)
{
    const char *src = config_get_string(key, def_val);
    size_t n = strlen(src);

    if (n > dst_size - 1) {
        ESP_LOGW(TAG, "%s is %u B, longer than the %u B field – "
                 "truncating", key, (unsigned)n, (unsigned)(dst_size - 1));
        n = dst_size - 1;
    }
    memcpy(dst, src, n);
    dst[n] = '\0';
}

/*
 * Reads an int and clamps it into [lo, hi] rather than rejecting it, so a
 * typo degrades to a working value instead of an unreachable broker.
 */
static int load_clamped(const char *key, int def_val, int lo, int hi)
{
    int v = config_get_int(key, def_val);
    if (v < lo || v > hi) {
        ESP_LOGW(TAG, "%s=%d is outside %d..%d – clamping", key, v, lo, hi);
        if (v < lo) v = lo;
        if (v > hi) v = hi;
    }
    return v;
}

/*
 * A topic root must not be empty (it would produce "/<id>/stream/info",
 * a different topic than the server subscribes to) and must not carry its
 * own separators or wildcards, since the publish paths append "/<id>/…".
 */
static void sanitise_topic(char *topic, size_t size,
                           const char *key, const char *def_val)
{
    if (!topic[0]) {
        ESP_LOGW(TAG, "%s is empty – using default \"%s\"", key, def_val);
        strncpy(topic, def_val, size - 1);
        topic[size - 1] = '\0';
        return;
    }

    size_t n = strlen(topic);
    while (n > 1 && topic[n - 1] == '/') topic[--n] = '\0';

    if (strchr(topic, '#') || strchr(topic, '+')) {
        ESP_LOGW(TAG, "%s=\"%s\" contains an MQTT wildcard, which cannot "
                 "appear in a published topic – using default \"%s\"",
                 key, topic, def_val);
        strncpy(topic, def_val, size - 1);
        topic[size - 1] = '\0';
    }
}

void mqtt_config_set_defaults(mqtt_config_t *cfg)
{
    if (!cfg) return;
    memset(cfg, 0, sizeof(*cfg));

    strncpy(cfg->host,     MQTT_CFG_DEFAULT_HOST,     sizeof(cfg->host) - 1);
    cfg->port = MQTT_CFG_DEFAULT_PORT;
    strncpy(cfg->username, MQTT_CFG_DEFAULT_USER,     sizeof(cfg->username) - 1);
    strncpy(cfg->password, MQTT_CFG_DEFAULT_PASSWORD, sizeof(cfg->password) - 1);
    cfg->use_tls = MQTT_CFG_DEFAULT_TLS;

    strncpy(cfg->topic_recordings, MQTT_CFG_DEFAULT_TOPIC_RECORDINGS,
            sizeof(cfg->topic_recordings) - 1);
    strncpy(cfg->topic_devices,    MQTT_CFG_DEFAULT_TOPIC_DEVICES,
            sizeof(cfg->topic_devices) - 1);
    strncpy(cfg->topic_commands,   MQTT_CFG_DEFAULT_TOPIC_COMMANDS,
            sizeof(cfg->topic_commands) - 1);
    strncpy(cfg->topic_visual,     MQTT_CFG_DEFAULT_TOPIC_VISUAL,
            sizeof(cfg->topic_visual) - 1);
    strncpy(cfg->topic_events,     MQTT_CFG_DEFAULT_TOPIC_EVENTS,
            sizeof(cfg->topic_events) - 1);

    cfg->chunk_size         = MQTT_CFG_DEFAULT_CHUNK_SIZE;
    cfg->buffer_size        = MQTT_CFG_DEFAULT_BUFFER_SIZE;
    cfg->network_timeout_ms = MQTT_CFG_DEFAULT_NETWORK_TIMEOUT;
    cfg->connect_timeout_ms = MQTT_CFG_DEFAULT_CONNECT_TIMEOUT;
}

bool mqtt_config_load(mqtt_config_t *cfg)
{
    if (!cfg) return false;

    memset(cfg, 0, sizeof(*cfg));

    /* ── Broker ──────────────────────────────────────────────── */
    copy_field(cfg->host,     sizeof(cfg->host),
               "mqtt_host",     MQTT_CFG_DEFAULT_HOST);
    copy_field(cfg->username, sizeof(cfg->username),
               "mqtt_user",     MQTT_CFG_DEFAULT_USER);
    copy_field(cfg->password, sizeof(cfg->password),
               "mqtt_password", MQTT_CFG_DEFAULT_PASSWORD);

    cfg->port    = (uint16_t)load_clamped("mqtt_port",
                                          MQTT_CFG_DEFAULT_PORT, 1, 65535);
    cfg->use_tls = config_get_bool("mqtt_tls", MQTT_CFG_DEFAULT_TLS);

    /* ── Topic roots ─────────────────────────────────────────── */
    copy_field(cfg->topic_recordings, sizeof(cfg->topic_recordings),
               "mqtt_topic_recordings", MQTT_CFG_DEFAULT_TOPIC_RECORDINGS);
    copy_field(cfg->topic_devices,    sizeof(cfg->topic_devices),
               "mqtt_topic_devices",    MQTT_CFG_DEFAULT_TOPIC_DEVICES);
    copy_field(cfg->topic_commands,   sizeof(cfg->topic_commands),
               "mqtt_topic_commands",   MQTT_CFG_DEFAULT_TOPIC_COMMANDS);
    copy_field(cfg->topic_visual,     sizeof(cfg->topic_visual),
               "mqtt_topic_visual",     MQTT_CFG_DEFAULT_TOPIC_VISUAL);
    copy_field(cfg->topic_events,     sizeof(cfg->topic_events),
               "mqtt_topic_events",     MQTT_CFG_DEFAULT_TOPIC_EVENTS);

    sanitise_topic(cfg->topic_recordings, sizeof(cfg->topic_recordings),
                   "mqtt_topic_recordings", MQTT_CFG_DEFAULT_TOPIC_RECORDINGS);
    sanitise_topic(cfg->topic_devices,    sizeof(cfg->topic_devices),
                   "mqtt_topic_devices",    MQTT_CFG_DEFAULT_TOPIC_DEVICES);
    sanitise_topic(cfg->topic_commands,   sizeof(cfg->topic_commands),
                   "mqtt_topic_commands",   MQTT_CFG_DEFAULT_TOPIC_COMMANDS);
    sanitise_topic(cfg->topic_visual,     sizeof(cfg->topic_visual),
                   "mqtt_topic_visual",     MQTT_CFG_DEFAULT_TOPIC_VISUAL);
    sanitise_topic(cfg->topic_events,     sizeof(cfg->topic_events),
                   "mqtt_topic_events",     MQTT_CFG_DEFAULT_TOPIC_EVENTS);

    /* ── Transport tuning ────────────────────────────────────── */
    cfg->chunk_size = load_clamped("mqtt_chunk_size",
                                   MQTT_CFG_DEFAULT_CHUNK_SIZE, 256, 32768);
    cfg->buffer_size = load_clamped("mqtt_buffer_size",
                                    MQTT_CFG_DEFAULT_BUFFER_SIZE, 1024, 65536);
    cfg->network_timeout_ms = load_clamped("mqtt_network_timeout_ms",
                                    MQTT_CFG_DEFAULT_NETWORK_TIMEOUT,
                                    1000, 120000);
    cfg->connect_timeout_ms = load_clamped("mqtt_connect_timeout_ms",
                                    MQTT_CFG_DEFAULT_CONNECT_TIMEOUT,
                                    1000, 120000);

    /*
     * A chunk has to fit in the publish buffer alongside its topic and
     * MQTT header, or every audio publish fails. Rather than let that
     * happen at runtime, grow the buffer to match the chunk size here.
     */
    if (cfg->chunk_size + 512 > cfg->buffer_size) {
        int grown = cfg->chunk_size + 1024;
        ESP_LOGW(TAG, "mqtt_buffer_size=%d cannot hold a %d B chunk – "
                 "raising it to %d", cfg->buffer_size, cfg->chunk_size,
                 grown);
        cfg->buffer_size = grown;
    }

    mqtt_config_log(cfg);

    if (!cfg->host[0]) {
        ESP_LOGE(TAG, "mqtt_host resolved to an empty string – MQTT "
                 "cannot connect. Set mqtt_host in settings.txt.");
        return false;
    }
    return true;
}

void mqtt_config_log(const mqtt_config_t *cfg)
{
    if (!cfg) return;
    ESP_LOGI(TAG,
        "broker=%s%s:%u user=%s pass=%s | topics rec=%s dev=%s cmd=%s "
        "(reserved: visual=%s events=%s) | chunk=%dB buf=%dB "
        "net_timeout=%dms connect_timeout=%dms",
        cfg->use_tls ? "mqtts://" : "mqtt://",
        cfg->host, (unsigned)cfg->port,
        cfg->username[0] ? cfg->username : "(none)",
        cfg->password[0] ? "(set)" : "(none)",
        cfg->topic_recordings, cfg->topic_devices, cfg->topic_commands,
        cfg->topic_visual, cfg->topic_events,
        cfg->chunk_size, cfg->buffer_size,
        cfg->network_timeout_ms, cfg->connect_timeout_ms);
}
