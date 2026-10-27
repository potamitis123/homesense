/* The audio sentinel, as a controllable subsystem.
 *
 * This is P4-audio-sentinel's main.c with its app_main() split into an
 * init call and a suspend/resume pair, so that the mode arbiter can take
 * the audio hardware away from it for the duration of a video call and
 * give it back afterwards. Nothing about how it listens, triggers,
 * uploads or plays has changed.
 *
 * Everything here is called from the arbiter, never directly.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_codec_dev.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ── Hardware the sentinel brings up and the intercom borrows ───────
 *
 * The sentinel initialises I2C, both codecs, the display and WiFi during
 * sentinel_app_init(), because it is what boots first. The intercom's
 * board.c and network.c are adapters over these rather than a second set
 * of initialisers — two bsp_audio_codec_*_init() calls or two
 * esp_wifi_init() calls on one board is how you get a device that works
 * until the second subsystem starts.
 * ──────────────────────────────────────────────────────────────────── */

/** ES8311 playback handle, or NULL if it failed to initialise. */
esp_codec_dev_handle_t sentinel_app_speaker_handle(void);

/** ES7210 capture handle, or NULL if it failed to initialise. */
esp_codec_dev_handle_t sentinel_app_microphone_handle(void);

/** Whether WiFi is associated. */
bool sentinel_app_wifi_connected(void);

/**
 * @brief Bring the sentinel up: display, codecs, self-test, WiFi, MQTT, tasks
 *
 * @note This is the old app_main() in everything but name. It leaves the
 *       sentinel running — the arbiter suspends it when a call arrives.
 */
esp_err_t sentinel_app_init(void);

/**
 * @brief Release the audio hardware and freeze the UI
 *
 * @note Blocks until the capture task has actually stopped reading and the
 *       microphone is closed, because the caller's next step is to let
 *       esp_capture open the same codec. Specifically it:
 *
 *         - tells the capture task to stop at a frame boundary, so an
 *           utterance in flight is finished and streamed rather than
 *           truncated (the same courtesy live-audio mode already gets);
 *         - leaves live audio mode if it is running, and publishes
 *           {"live":false} so the dashboard is not left believing the
 *           sentinel is still streaming;
 *         - drains and closes the loudspeaker path, so a broadcast in
 *           progress cannot fight av_render for the ES8311;
 *         - stops the LVGL adapter (dummy draw + pause) so it cannot
 *           keep flushing the listening UI into the same DPI
 *           framebuffers av_render decodes into.
 *
 *       MQTT stays connected throughout: presence, and the fact that this
 *       device is busy on a call, are still worth reporting.
 *
 * @param[in] timeout_ms How long to wait for the capture task to stop
 *
 * @return ESP_OK when the codecs are free, ESP_ERR_TIMEOUT otherwise
 */
esp_err_t sentinel_app_suspend(uint32_t timeout_ms);

/**
 * @brief Take the audio hardware back and resume listening
 *
 * @note Reopens the microphone at the sentinel's own sample rate, which is
 *       why the call must have fully closed its codecs first: the shared
 *       I2S port is pinned by whoever opens it.
 */
esp_err_t sentinel_app_resume(void);

/**
 * @brief Whether the sentinel is currently suspended
 */
bool sentinel_app_is_suspended(void);

/**
 * @brief Post a line to the sentinel's status area
 *
 * @note No-op while suspended, so call-time messages do not repaint over
 *       the video.
 */
void sentinel_app_set_status(const char *text, uint32_t colour);

/**
 * @brief Repaint the whole UI
 *
 * @note Called by the arbiter after a call, because av_render has been
 *       writing over the framebuffer LVGL believes it still owns.
 */
void sentinel_app_redraw(void);

#ifdef __cplusplus
}
#endif
