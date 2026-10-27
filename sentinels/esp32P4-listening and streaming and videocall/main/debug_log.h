/* Mirror the serial monitor to a file on the SD card.
 *
 * Enabled with DEBUG=1 in /sdcard/settings.txt. The unit is normally
 * deployed with no console attached, so when it misbehaves the log that
 * would have explained it is the one thing that cannot be recovered
 * afterwards. This captures the same stream the monitor shows.
 *
 * Everything ESP_LOG* prints goes through one vprintf hook, so nothing in
 * the rest of the firmware has to know this exists. The hook itself only
 * formats and copies into a RAM ring; a dedicated low-priority task does the
 * SD writes. That split is the point: an SDMMC write can block for tens of
 * milliseconds, and doing it inline would put that stall inside whichever
 * task happened to log — including the audio and peer-connection threads
 * this firmware is careful to keep unblocked.
 *
 * What it cannot capture: bootloader output, anything logged before
 * debug_log_begin(), and panic handler output (which bypasses ESP_LOG and
 * writes straight to the console).
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool capturing;         /*!< The hook is installed */
    bool writing;           /*!< A file is open and the writer task is up */
    bool sync;              /*!< Lines are written inline, not via the task */
    uint32_t lines;         /*!< Lines captured */
    uint32_t dropped;       /*!< Lines dropped because the ring was full */
    size_t buffered;        /*!< Bytes waiting to be written */
    size_t written;         /*!< Bytes written to the current file */
    char path[64];          /*!< The file being written */
} debug_log_stats_t;

/**
 * @brief Install the log hook and start buffering in RAM
 *
 * @note Call as early in app_main() as possible, before the SD card is
 *       mounted: settings.txt cannot be read until then, so the alternative
 *       is losing every line logged during board and media bring-up — which
 *       is exactly the part worth having. Lines are held in the ring until
 *       debug_log_start_file() drains them, or discarded by
 *       debug_log_abort() if DEBUG is not set.
 *
 * @return ESP_OK, or ESP_ERR_NO_MEM if the ring could not be allocated (in
 *         which case logging continues to the console as normal)
 */
esp_err_t debug_log_begin(void);

/**
 * @brief Open the log file and start the writer task
 *
 * @note Everything buffered by debug_log_begin() is written first, so the
 *       file starts at the first line of app_main(). Rotates an existing log
 *       to <path>.1 rather than appending, so one boot is one file.
 *
 *       Durability: the writer is woken per line and fsyncs each drain, so a
 *       crash loses at most the last tick's worth of lines. `sync` closes
 *       that gap by writing from the logging task itself — the line before a
 *       panic is then guaranteed, at the cost of an SD write inside whatever
 *       task logged it.
 *
 * @param[in] path Absolute path on the mounted card, NULL for the default
 * @param[in] sync Write and fsync inline instead of through the writer task
 *
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE if begin() was not called
 *         or the file is already open, ESP_FAIL if the file cannot be opened
 */
esp_err_t debug_log_start_file(const char *path, bool sync);

/**
 * @brief Stop capturing and release the ring
 *
 * @note For the DEBUG=0 path: the hook is removed and the buffered lines
 *       dropped, leaving console logging exactly as it was.
 */
void debug_log_abort(void);

/**
 * @brief Flush buffered lines to the card now
 *
 * @note The writer flushes on its own every DEBUG_LOG_FLUSH_MS; this is for
 *       the console command, before pulling the card out.
 */
void debug_log_flush(void);

/**
 * @brief Whether lines are being written to the card
 */
bool debug_log_is_writing(void);

/**
 * @brief Snapshot of the capture counters, for the console
 */
void debug_log_get_stats(debug_log_stats_t *out);

#ifdef __cplusplus
}
#endif
