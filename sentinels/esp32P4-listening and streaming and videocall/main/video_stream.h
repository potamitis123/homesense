/* Situational-awareness video uplink: OV5647 -> home server, as MJPEG.
 *
 * One dedicated task posts whole JPEG frames to the server, one HTTP POST
 * per frame, at a rate the server is not expected to keep pace with. It is
 * independent of the call path on purpose:
 *
 *   - it runs whether or not a call is up, because the point of it is to be
 *     able to look before ringing;
 *   - it uses no WebRTC machinery, so a future higher-rate server->P4 call
 *     stream and the duplex Opus audio can be negotiated without this stream
 *     having to be renegotiated, paused or torn down;
 *   - it leases sink 1 of the existing capture graph (see media_sys.h) and
 *     never initialises the camera itself.
 *
 * Enabled by ov5647_video_enabled=1 in /sdcard/settings.txt, off otherwise.
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
    const char *url;       /*!< Base URL; the device id is appended as a path */
    const char *device_id; /*!< Identifies this unit to the server */
    const char *ca_cert;   /*!< CA PEM for an https:// url, NULL for http:// */
    int width;             /*!< Frame width, e.g. 640 */
    int height;            /*!< Frame height, e.g. 480 */
    int fps;               /*!< Target frame rate, 1..15 */
} video_stream_cfg_t;

typedef struct {
    bool running;           /*!< The uplink task is alive */
    bool online;            /*!< The last POST reached the server */
    uint32_t frames_sent;   /*!< Frames the server accepted */
    uint32_t frames_failed; /*!< POSTs that failed, since boot */
    uint64_t bytes_sent;    /*!< JPEG payload accepted by the server */
    int last_status;        /*!< Last HTTP status code, 0 if never connected */
} video_stream_stats_t;

/**
 * @brief Start the uplink task
 *
 * @note Returns as soon as the task exists; it brings the camera sink up
 *       itself and waits for the network, so this may be called before
 *       Wi-Fi has associated. A camera that will not start is logged and
 *       retried, never fatal.
 *
 * @param[in] cfg Destination and frame geometry; copied, not retained
 *
 * @return ESP_OK on success, ESP_ERR_INVALID_ARG on a bad config,
 *         ESP_ERR_INVALID_STATE if already running, ESP_FAIL if the task
 *         could not be created
 */
esp_err_t video_stream_start(const video_stream_cfg_t *cfg);

/**
 * @brief Stop the uplink and release the camera sink
 *
 * @note Blocks until the task has released any frame it holds. Safe to call
 *       when not running.
 */
void video_stream_stop(void);

/**
 * @brief Whether the uplink task is running
 */
bool video_stream_is_running(void);

/**
 * @brief A call is arriving: give the capture graph back
 *
 * @note Blocks briefly (up to a second) until the uplink has actually let go,
 *       because esp_webrtc is about to set its own sink up and esp_capture
 *       will not accept that on a running graph. Call before the peer
 *       connection is enabled. No-op when VIDEO_STREAM_YIELD_DURING_CALL is
 *       false, or when the uplink is not running.
 */
void video_stream_call_started(void);

/**
 * @brief The call is up, so hold the pause for as long as it lasts
 *
 * @note Without this the uplink would take the camera back mid-call, on the
 *       assumption that a RING which never became a call had stalled it.
 */
void video_stream_call_connected(void);

/**
 * @brief The call has ended: take the camera back
 */
void video_stream_call_ended(void);

/**
 * @brief Snapshot of the uplink's counters, for the console
 */
void video_stream_get_stats(video_stream_stats_t *out);

#ifdef __cplusplus
}
#endif
