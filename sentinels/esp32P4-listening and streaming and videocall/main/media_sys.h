/* Capture and render graphs.
 *
 * Wires the Waveshare hardware into Espressif's media components and hands
 * the resulting handles to esp_webrtc:
 *
 *     OV5647 --CSI--> esp_video --v4l2--> esp_capture --MJPEG--.
 *     ES7210 --I2S--> esp_codec_dev ----> esp_capture --G711A--+--> esp_webrtc
 *                                                    |         |
 *                                                    `--MJPEG--+--> sink 1:
 *                                                       stills, and the
 *                                                       uplink in
 *                                                       video_stream.c
 *
 *     esp_webrtc --G711A--> av_render --> esp_codec_dev --I2S--> ES8311
 *     esp_webrtc --MJPEG--> av_render --> esp_lcd_panel --DSI--> HX8394
 *
 * Both graphs are built at boot and outlive individual calls. esp_webrtc
 * starts and stops the capture side itself as calls come and go.
 *
 * There is exactly one camera initialisation, in build_capture_system(). The
 * situational-awareness uplink is a second *consumer* of it, not a second
 * camera: it leases sink 1 through the media_sys_video_stream_* calls below,
 * which is what lets it keep running while a call is up.
 */

#pragma once

#include <stdbool.h>

#include "esp_capture_sink.h"
#include "esp_err.h"
#include "esp_webrtc.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Register the codecs and build both graphs
 *
 * @note Calls esp_video_init(), so board_init() must have brought the I2C
 *       bus up first — the OV5647's SCCB shares it with the audio codecs.
 *
 * @return ESP_OK on success, otherwise the first failing stage's error
 */
esp_err_t media_sys_buildup(void);

/**
 * @brief Fill in the capture and player handles for esp_webrtc
 *
 * @param[out] provider Receives the capture and render handles
 *
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE before buildup
 */
esp_err_t media_sys_get_provider(esp_webrtc_media_provider_t *provider);

/**
 * @brief Start delivering JPEG frames on sink 1 for the uplink
 *
 * @note Enables the sink and starts the capture graph, but never stops it:
 *       during a call esp_webrtc owns the graph's run state. Idempotent.
 *       The first caller's geometry configures the sink; a later, different
 *       request is logged and served at the geometry already in force.
 *
 * @param[in] width  Frame width in pixels
 * @param[in] height Frame height in pixels
 * @param[in] fps    Encoder frame rate
 *
 * @return ESP_OK on success, ESP_ERR_NOT_FOUND with no camera,
 *         ESP_ERR_INVALID_STATE before buildup or if the sink cannot be set up
 */
esp_err_t media_sys_video_stream_start(int width, int height, int fps);

/**
 * @brief Stop delivering uplink frames
 *
 * @note Disables sink 1 only, and deliberately never stops the capture
 *       graph. Stopping it is neither needed — sink 0 is reserved at boot,
 *       so esp_webrtc can always set its sink up (see media_sys.c,
 *       reserve_call_sink()) — nor safe, because this is reached from the
 *       call path after esp_webrtc has already enabled its own sink, and
 *       stopping the graph there would cut the call's microphone.
 *       Call audio, the panel and the AI stills path are untouched.
 */
void media_sys_video_stream_stop(void);

/**
 * @brief Whether sink 1 is currently feeding the uplink
 */
bool media_sys_video_stream_running(void);

/**
 * @brief Check out the next uplink frame, without copying it
 *
 * @note On success the sink lock is held until media_sys_video_stream_release(),
 *       so the two must be paired from the same task and nothing slow that
 *       can be deferred should sit between them. `frame->data` points into
 *       the sink's own buffer and is only valid until the release.
 *
 * @param[out] frame Receives the JPEG buffer, size and timestamp
 *
 * @return ESP_OK on success, ESP_ERR_NOT_FOUND when no frame is ready yet,
 *         ESP_ERR_INVALID_STATE when the uplink is not running
 */
esp_err_t media_sys_video_stream_acquire(esp_capture_stream_frame_t *frame);

/**
 * @brief Give an acquired frame back to the sink
 *
 * @param[in] frame The frame returned by media_sys_video_stream_acquire()
 */
void media_sys_video_stream_release(esp_capture_stream_frame_t *frame);

/**
 * @brief Capture one JPEG still, for the AI vision path
 *
 * @note Allocates the returned buffer; the caller frees it. When the uplink
 *       is running the still is borrowed from its stream rather than the
 *       sink being cycled underneath it, so the two coexist; the call waits
 *       up to the still timeout for the sink and reports ESP_ERR_TIMEOUT if
 *       the uplink does not hand it back in time.
 *
 * @param[out] jpeg Receives a malloc'd JPEG buffer
 * @param[out] len  Receives its length
 *
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE if the capture graph is
 *         busy or absent, ESP_ERR_TIMEOUT if no frame arrived
 */
esp_err_t media_sys_grab_still(uint8_t **jpeg, size_t *len);

#ifdef __cplusplus
}
#endif
