/*
 * mqtt_config.h  –  MQTT deployment configuration
 *
 * Everything the MQTT subsystem needs to know about *where* it is
 * deployed lives in this struct. audiosense_mqtt.c receives one at
 * init() and never refers to a compile-time broker constant, so
 * replacing the home server, moving the broker, renaming a topic root or
 * turning TLS off is a settings.txt edit rather than a rebuild.
 *
 * What is NOT here, on purpose
 * ────────────────────────────
 * The wire protocol. Topic *suffixes* (/stream/info, /chunk/<n>,
 * /complete, /ip, /status), the JSON payload shapes and the QoS/retain
 * flags are the contract with the desktop application, not deployment
 * settings, so they stay in audiosense_mqtt.[ch]. Only the topic roots
 * are configurable. An existing sentinel pointed at the existing server
 * with an empty settings.txt behaves byte-for-byte as it did before.
 *
 * settings.txt keys
 * ─────────────────
 * Keys are matched case-insensitively (see config.h), so the lowercase
 * spelling below and the historical SHOUTING_CASE spelling both work.
 *
 *   Broker
 *     mqtt_host                 hostname or IP of the broker
 *                               default: your-broker.example.com
 *     mqtt_port                 TCP port,  1..65535
 *                               default: 8883
 *     mqtt_user                 username, may be empty
 *                               default: your_mqtt_user
 *     mqtt_password             password, may be empty
 *                               default: your_mqtt_password
 *     mqtt_tls                  true  → mqtts:// with the embedded CA and
 *                                       client certificate/key
 *                               false → plain mqtt://, certificates unused
 *                               default: true
 *
 *   Topic roots
 *     mqtt_topic_recordings     outgoing audio from this device
 *                               default: audio/recordings
 *     mqtt_topic_devices        retained presence + IP announcements
 *                               default: devices
 *     mqtt_topic_commands       incoming playback commands
 *                               default: commands
 *     mqtt_topic_visual         RESERVED – parsed and validated, not yet
 *                               subscribed or published by any code path
 *                               default: visual
 *     mqtt_topic_events         RESERVED – as above
 *                               default: events
 *
 *   Transport tuning (defaults reproduce the previous hardcoded values;
 *   changing them does not change the protocol or the reconnect logic)
 *     mqtt_chunk_size           bytes per audio chunk publish
 *                               default: 4096
 *     mqtt_buffer_size          esp-mqtt rx/tx buffer, bytes
 *                               default: 6144
 *     mqtt_network_timeout_ms   esp-mqtt socket timeout
 *                               default: 30000
 *     mqtt_connect_timeout_ms   how long a publish waits for CONNECTED
 *                               default: 15000
 */
#ifndef MQTT_CONFIG_H
#define MQTT_CONFIG_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ── Documented defaults ─────────────────────────────────────────
 * These are the values the firmware used as compile-time constants
 * before configuration moved to settings.txt. They remain the defaults
 * so that an existing device with no MQTT keys in settings.txt keeps
 * talking to the existing server unchanged. */
#define MQTT_CFG_DEFAULT_HOST              "your-broker.example.com"
#define MQTT_CFG_DEFAULT_PORT              8883
#define MQTT_CFG_DEFAULT_USER              "your_mqtt_user"
#define MQTT_CFG_DEFAULT_PASSWORD          "your_mqtt_password"
#define MQTT_CFG_DEFAULT_TLS               true

#define MQTT_CFG_DEFAULT_TOPIC_RECORDINGS  "audio/recordings"
#define MQTT_CFG_DEFAULT_TOPIC_DEVICES     "devices"
#define MQTT_CFG_DEFAULT_TOPIC_COMMANDS    "commands"
#define MQTT_CFG_DEFAULT_TOPIC_VISUAL      "visual"
#define MQTT_CFG_DEFAULT_TOPIC_EVENTS      "events"

#define MQTT_CFG_DEFAULT_CHUNK_SIZE        4096
#define MQTT_CFG_DEFAULT_BUFFER_SIZE       6144
#define MQTT_CFG_DEFAULT_NETWORK_TIMEOUT   30000
#define MQTT_CFG_DEFAULT_CONNECT_TIMEOUT   15000

/* Field sizes. Topic roots are short by nature; the publish paths build
 * "<root>/<device_id>/stream/chunk/<n>" into a 128-byte buffer, so a root
 * longer than this could not be used anyway. */
#define MQTT_CFG_HOST_LEN   96
#define MQTT_CFG_CRED_LEN   64
#define MQTT_CFG_TOPIC_LEN  48

typedef struct {
    /* Broker */
    char     host[MQTT_CFG_HOST_LEN];
    uint16_t port;
    char     username[MQTT_CFG_CRED_LEN];
    char     password[MQTT_CFG_CRED_LEN];
    bool     use_tls;

    /* Topic roots */
    char topic_recordings[MQTT_CFG_TOPIC_LEN];
    char topic_devices[MQTT_CFG_TOPIC_LEN];
    char topic_commands[MQTT_CFG_TOPIC_LEN];
    char topic_visual[MQTT_CFG_TOPIC_LEN];   /* reserved, unused */
    char topic_events[MQTT_CFG_TOPIC_LEN];   /* reserved, unused */

    /* Transport tuning */
    int chunk_size;
    int buffer_size;
    int network_timeout_ms;
    int connect_timeout_ms;
} mqtt_config_t;

/**
 * Fill @p cfg from the Config module (config.h), which must already have
 * been populated by config_load().
 *
 * Every field falls back to its MQTT_CFG_DEFAULT_* value when the key is
 * absent or unusable, with a warning. Out-of-range numbers are clamped
 * rather than rejected. This never fails in a way that should stop the
 * boot: the returned bool reports whether the resulting configuration is
 * one the caller can expect to connect with (a usable host and port), so
 * it is worth logging, but the struct is always fully populated.
 *
 * Safe to call before config_load() too — you simply get all defaults.
 */
bool mqtt_config_load(mqtt_config_t *cfg);

/** Populate @p cfg with the documented defaults, ignoring settings.txt. */
void mqtt_config_set_defaults(mqtt_config_t *cfg);

/** One-line summary for the boot log. Never logs the password. */
void mqtt_config_log(const mqtt_config_t *cfg);

#ifdef __cplusplus
}
#endif
#endif /* MQTT_CONFIG_H */
