/*
 * config.h  –  Generic key/value configuration store
 *
 * Purpose
 * ───────
 * This is the *only* place in the firmware that reads settings.txt. Every
 * other module asks for values by key through config_get_*() and has no
 * idea whether they came from the SD card, NVS, or a compiled-in default.
 * Adding a new tunable therefore never means touching a parser again.
 *
 * File format
 * ───────────
 *   KEY=VALUE          one pair per line
 *   # comment          full-line comment ('#' or ';' as the first
 *   ; comment          non-blank character)
 *   <blank>            ignored
 *
 *   - Whitespace around KEY and VALUE is stripped, so "a = b" is the
 *     same as "a=b". Whitespace *inside* a value is preserved.
 *   - Keys are matched case-insensitively, so MQTT_HOST, mqtt_host and
 *     Mqtt_Host are the same key. This is what keeps settings.txt files
 *     written for older firmware (which used SHOUTING_CASE throughout)
 *     working unchanged.
 *   - There is deliberately no inline-comment syntax: a '#' after the
 *     '=' is part of the value, because passwords contain '#'.
 *   - A repeated key wins on its last occurrence.
 *   - An empty value ("WIFI_PASS=") is a real, present, empty string —
 *     not a missing key. That distinction matters for open networks.
 *
 * Failure policy
 * ──────────────
 * Nothing here is ever fatal. A missing file, an unreadable file, a
 * malformed line or a missing key all resolve to the caller's default
 * with a log line. The firmware must keep running with defaults rather
 * than refuse to boot because one field is absent.
 */
#ifndef CONFIG_H
#define CONFIG_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Parse a KEY=VALUE file into the store, replacing anything loaded
 * before.
 *
 * Safe to call when the file does not exist: a previous successful load
 * is kept, otherwise the store stays empty and getters return defaults.
 *
 * @param path  absolute path, e.g. BSP_SD_MOUNT_POINT "/settings.txt"
 * @return true if the file was opened and parsed (even if it contained
 *         no usable pairs); false if it could not be opened.
 */
bool config_load(const char *path);

/** Release the store. Getters keep working and return their defaults. */
void config_unload(void);

/** Number of key/value pairs currently held. */
int config_count(void);

/** True if @p key is present, regardless of its value. */
bool config_has(const char *key);

/**
 * @return the stored value, or @p def_val if the key is absent.
 *
 * The returned pointer stays valid until the next config_load() or
 * config_unload(). Callers that keep the value past that point (or past
 * a reload) must copy it; the firmware's modules all copy into their own
 * structs at boot.
 */
const char *config_get_string(const char *key, const char *def_val);

/**
 * Decimal, or 0x-prefixed hex. Returns @p def_val if the key is absent
 * or the value is not a number at all. A trailing suffix is ignored, so
 * "8883" and "8883 " both parse.
 */
int config_get_int(const char *key, int def_val);

/**
 * Accepts 1/0, true/false, yes/no, on/off, enabled/disabled, in any
 * case. Anything else logs a warning and yields @p def_val.
 */
bool config_get_bool(const char *key, bool def_val);

/** Returns @p def_val if the key is absent or is not a number. */
float config_get_float(const char *key, float def_val);

#ifdef __cplusplus
}
#endif
#endif /* CONFIG_H */
