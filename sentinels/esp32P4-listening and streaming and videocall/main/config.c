/*
 * config.c  –  Generic key/value configuration store
 *
 * See config.h for the file format and the failure policy.
 *
 * Implementation notes
 * ────────────────────
 * The store is a flat, growable array of {key, value} strdup'd pairs.
 * There are a few dozen keys and they are read a handful of times during
 * boot, so a linear scan is the right amount of machinery — a hash table
 * would cost more code than it saves.
 *
 * Entries live in PSRAM when it is available. The array is small, but it
 * is allocated during boot while WiFi and mbedTLS are still growing into
 * internal RAM, and internal RAM is the scarce resource on this board.
 */

#include "config.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <ctype.h>
#include <errno.h>

#include "esp_log.h"
#include "esp_heap_caps.h"

static const char *TAG = "config";

/* Longest line accepted from settings.txt. Long enough for a base64
 * token; lines beyond this are truncated with a warning rather than
 * silently split into a bogus extra pair. */
#define CONFIG_LINE_MAX  512

typedef struct {
    char *key;
    char *val;
} config_entry_t;

static config_entry_t *s_entries  = NULL;
static int             s_count    = 0;
static int             s_capacity = 0;

/* ══════════════════════════ ALLOCATION ═════════════════════════ */

static void *cfg_malloc(size_t n)
{
    void *p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return p ? p : malloc(n);
}

static char *cfg_strdup(const char *s)
{
    size_t n = strlen(s) + 1;
    char  *p = cfg_malloc(n);
    if (p) memcpy(p, s, n);
    return p;
}

/* ══════════════════════════ PARSING HELPERS ════════════════════ */

/* Strips leading whitespace and returns the new start. */
static char *ltrim(char *s)
{
    while (*s && isspace((unsigned char)*s)) s++;
    return s;
}

/* Strips trailing whitespace in place. */
static void rtrim(char *s)
{
    size_t n = strlen(s);
    while (n && isspace((unsigned char)s[n - 1])) s[--n] = '\0';
}

/*
 * Replaces the value of an existing key, or appends a new pair.
 * Last occurrence of a key wins, which matches how the previous
 * hand-rolled parser behaved.
 */
static bool store_put(const char *key, const char *val)
{
    for (int i = 0; i < s_count; i++) {
        if (strcasecmp(s_entries[i].key, key) == 0) {
            char *nv = cfg_strdup(val);
            if (!nv) return false;
            free(s_entries[i].val);
            s_entries[i].val = nv;
            return true;
        }
    }

    if (s_count == s_capacity) {
        int new_cap = s_capacity ? s_capacity * 2 : 32;
        config_entry_t *grown =
            cfg_malloc((size_t)new_cap * sizeof(config_entry_t));
        if (!grown) return false;
        if (s_entries) {
            memcpy(grown, s_entries,
                   (size_t)s_count * sizeof(config_entry_t));
            free(s_entries);
        }
        s_entries  = grown;
        s_capacity = new_cap;
    }

    char *k = cfg_strdup(key);
    char *v = cfg_strdup(val);
    if (!k || !v) {
        free(k);
        free(v);
        return false;
    }
    s_entries[s_count].key = k;
    s_entries[s_count].val = v;
    s_count++;
    return true;
}

static const char *store_find(const char *key)
{
    if (!key) return NULL;
    for (int i = 0; i < s_count; i++) {
        if (strcasecmp(s_entries[i].key, key) == 0) return s_entries[i].val;
    }
    return NULL;
}

/* ══════════════════════════ LOAD / UNLOAD ══════════════════════ */

void config_unload(void)
{
    for (int i = 0; i < s_count; i++) {
        free(s_entries[i].key);
        free(s_entries[i].val);
    }
    free(s_entries);
    s_entries  = NULL;
    s_count    = 0;
    s_capacity = 0;
}

bool config_load(const char *path)
{
    if (!path) return false;

    FILE *f = fopen(path, "r");
    if (!f) {
        ESP_LOGW(TAG, "cannot open %s (errno %d) – %s", path, errno,
                 s_count ? "keeping the previously loaded keys"
                         : "every key will fall back to its built-in default");
        return s_count > 0;
    }

    config_unload();

    char line[CONFIG_LINE_MAX];
    int  bad = 0;

    while (fgets(line, sizeof(line), f)) {
        /* A line longer than the buffer arrives in pieces; the tail
         * pieces would parse as garbage pairs, so drop them. */
        if (!strchr(line, '\n') && !feof(f)) {
            ESP_LOGW(TAG, "line longer than %d B – ignoring it",
                     CONFIG_LINE_MAX - 1);
            int c;
            while ((c = fgetc(f)) != '\n' && c != EOF) { }
            bad++;
            continue;
        }

        char *p = ltrim(line);
        rtrim(p);
        if (!*p || *p == '#' || *p == ';') continue;

        char *eq = strchr(p, '=');
        if (!eq) {
            ESP_LOGW(TAG, "malformed line (no '='): %s", p);
            bad++;
            continue;
        }

        *eq = '\0';
        char *key = p;
        char *val = eq + 1;

        rtrim(key);
        key = ltrim(key);
        val = ltrim(val);
        rtrim(val);

        if (!*key) {
            ESP_LOGW(TAG, "malformed line (empty key)");
            bad++;
            continue;
        }

        if (!store_put(key, val)) {
            ESP_LOGE(TAG, "out of memory storing '%s' – remaining keys "
                     "will use their defaults", key);
            break;
        }
    }

    fclose(f);
    ESP_LOGI(TAG, "loaded %d key(s) from %s (%d line(s) ignored)",
             s_count, path, bad);
    return true;
}

int config_count(void)
{
    return s_count;
}

bool config_has(const char *key)
{
    return store_find(key) != NULL;
}

/* ══════════════════════════ TYPED GETTERS ══════════════════════ */

const char *config_get_string(const char *key, const char *def_val)
{
    const char *v = store_find(key);
    if (v) return v;
    ESP_LOGW(TAG, "%s not set – using default \"%s\"",
             key ? key : "(null)", def_val ? def_val : "");
    return def_val;
}

int config_get_int(const char *key, int def_val)
{
    const char *v = store_find(key);
    if (!v) {
        ESP_LOGW(TAG, "%s not set – using default %d",
                 key ? key : "(null)", def_val);
        return def_val;
    }

    char *end = NULL;
    errno = 0;
    long n = strtol(v, &end, 0);   /* base 0: accepts 0x… and decimal */
    if (end == v || errno == ERANGE) {
        ESP_LOGW(TAG, "%s=\"%s\" is not an integer – using default %d",
                 key, v, def_val);
        return def_val;
    }
    return (int)n;
}

float config_get_float(const char *key, float def_val)
{
    const char *v = store_find(key);
    if (!v) {
        ESP_LOGW(TAG, "%s not set – using default %.3f",
                 key ? key : "(null)", (double)def_val);
        return def_val;
    }

    char *end = NULL;
    errno = 0;
    float n = strtof(v, &end);
    if (end == v || errno == ERANGE) {
        ESP_LOGW(TAG, "%s=\"%s\" is not a number – using default %.3f",
                 key, v, (double)def_val);
        return def_val;
    }
    return n;
}

bool config_get_bool(const char *key, bool def_val)
{
    const char *v = store_find(key);
    if (!v) {
        ESP_LOGW(TAG, "%s not set – using default %s",
                 key ? key : "(null)", def_val ? "true" : "false");
        return def_val;
    }

    static const char *yes[] = { "1", "true",  "yes", "on",  "enabled"  };
    static const char *no[]  = { "0", "false", "no",  "off", "disabled" };

    for (size_t i = 0; i < sizeof(yes) / sizeof(yes[0]); i++) {
        if (strcasecmp(v, yes[i]) == 0) return true;
        if (strcasecmp(v, no[i])  == 0) return false;
    }

    ESP_LOGW(TAG, "%s=\"%s\" is not a boolean – using default %s",
             key, v, def_val ? "true" : "false");
    return def_val;
}
