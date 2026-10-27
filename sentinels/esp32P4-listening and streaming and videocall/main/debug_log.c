#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "debug_log.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "settings.h"

static const char *TAG = "DEBUG_LOG";

static struct {
    /* Ring of formatted log text. Bytes, not lines: a line that does not fit
     * is dropped whole, so the file never contains half a line. */
    uint8_t *ring;
    size_t size;
    size_t head;
    size_t tail;
    size_t used;
    SemaphoreHandle_t lock;

    vprintf_like_t prev;
    volatile bool capturing;
    volatile bool sync;
    volatile bool stop;

    TaskHandle_t writer;
    /* Guards the FILE* itself. The writer rotates the file underneath the
     * console's `log flush`, which would otherwise be writing through a
     * pointer that fclose() had already freed. Never held across a ring
     * operation, so it cannot interact with the log hook. */
    SemaphoreHandle_t file_lock;
    FILE *file;
    char path[64];
    size_t written;
    uint32_t lines;
    uint32_t dropped;
} s_log;

/* ── ring ───────────────────────────────────────────────────────────────*/

static void ring_push(const char *text, size_t len)
{
    /* Zero timeout: the hook runs on whatever task just logged, including
     * the ones this firmware keeps unblocked on purpose. A line lost to a
     * busy ring is cheaper than a millisecond of priority inversion in the
     * peer-connection thread. */
    if (xSemaphoreTake(s_log.lock, 0) != pdTRUE) {
        s_log.dropped++;
        return;
    }
    if (len > s_log.size - s_log.used) {
        s_log.dropped++;
        xSemaphoreGive(s_log.lock);
        return;
    }
    size_t first = s_log.size - s_log.head;
    if (first > len) {
        first = len;
    }
    memcpy(s_log.ring + s_log.head, text, first);
    if (len > first) {
        memcpy(s_log.ring, text + first, len - first);
    }
    s_log.head = (s_log.head + len) % s_log.size;
    s_log.used += len;
    s_log.lines++;
    xSemaphoreGive(s_log.lock);

    /* Wake the writer now rather than letting it find this on its next poll.
     * A crash loses whatever is still in the ring, so the ring should be
     * empty as often as possible — the writer is woken per line and syncs
     * what it drained before going back to sleep. */
    if (s_log.writer != NULL) {
        xTaskNotifyGive(s_log.writer);
    }
}

/* Copies out at most `max` bytes. The file write happens outside the lock,
 * so a slow card cannot stall the tasks that are trying to log. */
static size_t ring_pop(uint8_t *dst, size_t max)
{
    if (xSemaphoreTake(s_log.lock, portMAX_DELAY) != pdTRUE) {
        return 0;
    }
    size_t len = s_log.used < max ? s_log.used : max;
    if (len > 0) {
        size_t first = s_log.size - s_log.tail;
        if (first > len) {
            first = len;
        }
        memcpy(dst, s_log.ring + s_log.tail, first);
        if (len > first) {
            memcpy(dst + first, s_log.ring, len - first);
        }
        s_log.tail = (s_log.tail + len) % s_log.size;
        s_log.used -= len;
    }
    xSemaphoreGive(s_log.lock);
    return len;
}

/* ── hook ───────────────────────────────────────────────────────────────*/

static void rotate_and_open(void);

static int log_hook(const char *fmt, va_list ap)
{
    /* The console keeps working exactly as before, whether or not the card
     * is there. Its copy goes first so a monitor still sees the line even if
     * formatting for the ring fails. */
    va_list console_ap;
    va_copy(console_ap, ap);
    int ret = s_log.prev ? s_log.prev(fmt, console_ap)
                         : vprintf(fmt, console_ap);
    va_end(console_ap);

    if (!s_log.capturing || s_log.ring == NULL) {
        return ret;
    }
    /* The writer task's own output -- and FATFS's, logged from underneath
     * it -- must not come back round into the ring it is draining. */
    if (s_log.writer != NULL && xTaskGetCurrentTaskHandle() == s_log.writer) {
        return ret;
    }

    char line[DEBUG_LOG_LINE_MAX];
    int n = vsnprintf(line, sizeof(line), fmt, ap);
    if (n <= 0) {
        return ret;
    }
    if (n >= (int)sizeof(line)) {
        n = (int)sizeof(line) - 1; /* truncated; the console copy is intact */
    }

    /* DEBUG_SYNC: the line is on the card before this call returns, so even
     * the one immediately before a panic survives. The cost is an SD write
     * inside whatever task just logged — which is why it is opt-in and not
     * the default. */
    if (s_log.sync && s_log.file != NULL &&
        xSemaphoreTake(s_log.file_lock,
                       pdMS_TO_TICKS(DEBUG_LOG_SYNC_WAIT_MS)) == pdTRUE) {
        if (s_log.file != NULL) {
            fwrite(line, 1, (size_t)n, s_log.file);
            s_log.written += (size_t)n;
            s_log.lines++;
            fflush(s_log.file);
            fsync(fileno(s_log.file));
            if (s_log.written >= DEBUG_LOG_MAX_BYTES) {
                rotate_and_open();
            }
            xSemaphoreGive(s_log.file_lock);
            return ret;
        }
        xSemaphoreGive(s_log.file_lock);
    }
    /* Card busy, or async mode: the ring keeps the line rather than losing
     * it, and the writer syncs it within a tick. */
    ring_push(line, (size_t)n);
    return ret;
}

/* ── writer ─────────────────────────────────────────────────────────────*/

static void rotate_and_open(void)
{
    if (s_log.file != NULL) {
        fclose(s_log.file);
        s_log.file = NULL;
    }
    /* One boot, one file. Appending across boots is how a card fills up
     * unnoticed, and the previous run is usually the one being asked about,
     * so exactly one generation is kept. */
    char old[sizeof(s_log.path) + 2];
    snprintf(old, sizeof(old), "%s.1", s_log.path);
    remove(old);
    rename(s_log.path, old);

    s_log.file = fopen(s_log.path, "w");
    s_log.written = 0;
}

static void writer_task(void *arg)
{
    uint8_t *chunk = heap_caps_malloc(DEBUG_LOG_CHUNK,
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (chunk == NULL) {
        chunk = malloc(DEBUG_LOG_CHUNK);
    }
    if (chunk == NULL) {
        s_log.writer = NULL;
        vTaskDelete(NULL);
        return;
    }

    uint32_t reported_drops = 0;
    while (!s_log.stop) {
        /* Woken per line by ring_push(); the timeout is only so that stop
         * and the drop counter are still noticed on a silent system. */
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(DEBUG_LOG_IDLE_WAIT_MS));

        bool wrote = false;
        size_t len;
        while ((len = ring_pop(chunk, DEBUG_LOG_CHUNK)) > 0) {
            xSemaphoreTake(s_log.file_lock, portMAX_DELAY);
            if (s_log.file != NULL) {
                fwrite(chunk, 1, len, s_log.file);
                s_log.written += len;
                wrote = true;
                if (s_log.written >= DEBUG_LOG_MAX_BYTES) {
                    rotate_and_open();
                }
            }
            xSemaphoreGive(s_log.file_lock);
        }

        xSemaphoreTake(s_log.file_lock, portMAX_DELAY);
        if (s_log.file != NULL && s_log.dropped != reported_drops) {
            /* Written straight to the file: logging it would recurse. */
            char note[96];
            int n = snprintf(note, sizeof(note),
                             "--- debug_log: %u lines dropped, ring full ---\n",
                             (unsigned)s_log.dropped);
            fwrite(note, 1, (size_t)n, s_log.file);
            reported_drops = s_log.dropped;
            wrote = true;
        }
        if (wrote && s_log.file != NULL) {
            /* Every drain, not on a timer: what is on the card after this
             * survives a panic, a watchdog or the power going. The cost is
             * bounded because a busy system fills whole chunks, so this is
             * one sync per 4 kB rather than one per line. */
            fflush(s_log.file);
            fsync(fileno(s_log.file));
        }
        xSemaphoreGive(s_log.file_lock);
    }

    xSemaphoreTake(s_log.file_lock, portMAX_DELAY);
    if (s_log.file != NULL) {
        fflush(s_log.file);
        fsync(fileno(s_log.file));
        fclose(s_log.file);
        s_log.file = NULL;
    }
    xSemaphoreGive(s_log.file_lock);
    free(chunk);
    s_log.writer = NULL;
    vTaskDelete(NULL);
}

/* ── API ────────────────────────────────────────────────────────────────*/

esp_err_t debug_log_begin(void)
{
    if (s_log.ring != NULL) {
        return ESP_OK;
    }
    s_log.lock = xSemaphoreCreateMutex();
    if (s_log.lock == NULL) {
        return ESP_ERR_NO_MEM;
    }
    /* PSRAM: internal RAM is the scarce one on this board after the DSI
     * framebuffers, and nothing here is touched from an ISR. */
    s_log.ring = heap_caps_malloc(DEBUG_LOG_RING_BYTES,
                                  MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_log.ring == NULL) {
        s_log.ring = malloc(DEBUG_LOG_RING_BYTES);
    }
    if (s_log.ring == NULL) {
        vSemaphoreDelete(s_log.lock);
        s_log.lock = NULL;
        return ESP_ERR_NO_MEM;
    }
    s_log.size = DEBUG_LOG_RING_BYTES;
    s_log.head = s_log.tail = s_log.used = 0;
    s_log.capturing = true;
    s_log.prev = esp_log_set_vprintf(log_hook);
    return ESP_OK;
}

esp_err_t debug_log_start_file(const char *path, bool sync)
{
    if (s_log.ring == NULL || !s_log.capturing) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_log.file != NULL || s_log.writer != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_log.file_lock == NULL) {
        s_log.file_lock = xSemaphoreCreateMutex();
        if (s_log.file_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    snprintf(s_log.path, sizeof(s_log.path), "%s",
             path != NULL ? path : DEBUG_LOG_PATH_DEFAULT);

    rotate_and_open();
    if (s_log.file == NULL) {
        ESP_LOGE(TAG, "Cannot open %s; debug logging stays off", s_log.path);
        debug_log_abort();
        return ESP_FAIL;
    }

    s_log.stop = false;
    if (xTaskCreatePinnedToCore(writer_task, "dbg_log", DEBUG_LOG_TASK_STACK,
                                NULL, DEBUG_LOG_TASK_PRIO, &s_log.writer,
                                DEBUG_LOG_TASK_CORE) != pdPASS) {
        ESP_LOGE(TAG, "Cannot create the log writer task");
        fclose(s_log.file);
        s_log.file = NULL;
        debug_log_abort();
        return ESP_FAIL;
    }
    /* Set last: the hook must not take the sync path before the writer
     * exists, or a failure above would leave it writing to a closed file. */
    s_log.sync = sync;
    ESP_LOGI(TAG, "Debug log writing to %s (%s, max %d kB, then rotate to .1)",
             s_log.path, sync ? "sync per line" : "synced per drain",
             DEBUG_LOG_MAX_BYTES / 1024);
    return ESP_OK;
}

void debug_log_abort(void)
{
    s_log.capturing = false;
    s_log.sync = false;
    if (s_log.prev != NULL) {
        esp_log_set_vprintf(s_log.prev);
        s_log.prev = NULL;
    }
    s_log.stop = true;
    for (int i = 0; i < 40 && s_log.writer != NULL; i++) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    if (s_log.ring != NULL && s_log.writer == NULL) {
        heap_caps_free(s_log.ring);
        s_log.ring = NULL;
        s_log.size = s_log.used = s_log.head = s_log.tail = 0;
    }
}

void debug_log_flush(void)
{
    if (s_log.file == NULL || s_log.file_lock == NULL) {
        return;
    }
    /* Under the lock: the writer may be rotating the file, and this would
     * otherwise flush through a pointer fclose() has already freed. */
    if (xSemaphoreTake(s_log.file_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return;
    }
    if (s_log.file != NULL) {
        fflush(s_log.file);
        fsync(fileno(s_log.file));
    }
    xSemaphoreGive(s_log.file_lock);
}

bool debug_log_is_writing(void)
{
    return s_log.file != NULL && s_log.writer != NULL;
}

void debug_log_get_stats(debug_log_stats_t *out)
{
    if (out == NULL) {
        return;
    }
    memset(out, 0, sizeof(*out));
    out->capturing = s_log.capturing;
    out->writing = debug_log_is_writing();
    out->sync = s_log.sync;
    out->lines = s_log.lines;
    out->dropped = s_log.dropped;
    out->buffered = s_log.used;
    out->written = s_log.written;
    snprintf(out->path, sizeof(out->path), "%s", s_log.path);
}
