/* Mode arbiter — the single owner of every shared resource on the board.
 *
 * This unit is two applications in one image, and they cannot run together:
 *
 *   SENTINEL  the audio sentinel. LVGL audio-bar UI, ES7210 held open at
 *             16 kHz, RMS-triggered utterances published over MQTT, live
 *             audio (PTT) over the same MQTT connection, broadcasts played
 *             out of the ES8311, and the OV5647 uplink posting JPEGs.
 *
 *   CALL      the audiovisual intercom. esp_capture owns the ES7210 at
 *             8 kHz for G.711, av_render owns the ES8311 and the panel,
 *             the caller's video is decoded straight into the framebuffer.
 *
 * Exactly one of them is live at any moment. That is not a simplification
 * to be relaxed later — it is what the hardware allows:
 *
 *   - Both codecs share one I2S port, and whichever direction opens first
 *     pins the clock for the other. The sentinel runs the port at 16 kHz,
 *     the call at 8 kHz, so a switch means fully closing both codecs and
 *     reopening them, not muting. (P4-audio-sentinel's README documents at
 *     length what happens when you try to be clever about this: the
 *     loudspeaker goes silent while every write still returns success.)
 *
 *   - esp_capture's audio source and the sentinel's capture task would
 *     otherwise both hold esp_codec_dev open on the same ES7210.
 *
 *   - LVGL and av_render both draw into the DPI framebuffer.
 *
 * ── Why the switch has to happen before the offer is parsed ──────────
 *
 * esp_webrtc creates its capture sink inside pc_start(), which runs when
 * the SDP offer arrives — not at esp_webrtc_start(). Two consequences
 * decide the shape of this module:
 *
 *   1. esp_capture refuses a sink setup on a started graph, so sink 0 is
 *      reserved at boot by media_sys.c (see reserve_call_sink() there).
 *      Without that, a call that arrives while the uplink is running comes
 *      up with no microphone at all — silently, because inbound audio and
 *      video never touch esp_capture and keep working.
 *
 *   2. pc_start() then *enables* that sink, which opens the ES7210. If the
 *      sentinel still holds the microphone at that moment, the two collide.
 *      So mode_arbiter_enter_call() is called from the signaling layer the
 *      instant an offer is seen — before esp_webrtc is handed it — and it
 *      blocks until the sentinel has actually let go.
 *
 * Waiting in the signaling path delays the answer by a few hundred
 * milliseconds. That is the right trade: an answer sent while the codecs
 * are still owned by the sentinel produces a call with no audio, which is
 * far worse than one that connects slightly later.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    P4_MODE_BOOT = 0,  /*!< Before the first mode is entered */
    P4_MODE_SENTINEL,  /*!< Listening; the audio bar is on screen */
    P4_MODE_TO_CALL,   /*!< Handing the hardware over to the intercom */
    P4_MODE_CALL,      /*!< A call owns the codecs and the panel */
    P4_MODE_TO_SENTINEL, /*!< Handing it back */
} p4_mode_t;

/**
 * @brief Prepare the arbiter
 *
 * @note Call once, after both subsystems have been built but before either
 *       is running. Does not enter a mode; mode_arbiter_enter_sentinel()
 *       does that.
 */
esp_err_t mode_arbiter_init(void);

/**
 * @brief Enter sentinel mode
 *
 * @note Reopens the microphone at the sentinel's sample rate, thaws the
 *       LVGL UI and restarts the camera uplink. Safe to call when already
 *       in sentinel mode.
 */
esp_err_t mode_arbiter_enter_sentinel(void);

/**
 * @brief Hand the hardware to an incoming call
 *
 * @note Blocks until the sentinel has released the codecs, or until
 *       MODE_SWITCH_TIMEOUT_MS. Must be called before the offer reaches
 *       esp_webrtc — see the header comment. Safe to call repeatedly for
 *       the same call; only the first does work.
 *
 * @return ESP_OK when the hardware is free, ESP_ERR_TIMEOUT if the
 *         sentinel did not release it (the call is then allowed to proceed
 *         anyway, on the grounds that a degraded call beats no call)
 */
esp_err_t mode_arbiter_enter_call(void);

/**
 * @brief The call has ended; return to listening
 *
 * @note Safe to call when no call is in progress, which matters because
 *       esp_webrtc can report a call ending more than once.
 */
esp_err_t mode_arbiter_end_call(void);

/**
 * @brief Current mode
 */
p4_mode_t mode_arbiter_get(void);

/**
 * @brief Whether a call currently owns the hardware
 *
 * @note Includes the two transitional states: from the sentinel's point of
 *       view the hardware is gone as soon as the handover starts.
 */
bool mode_arbiter_call_owns_hardware(void);

/**
 * @brief Human-readable mode name, for logs and the status line
 */
const char *mode_arbiter_name(p4_mode_t mode);

#ifdef __cplusplus
}
#endif
