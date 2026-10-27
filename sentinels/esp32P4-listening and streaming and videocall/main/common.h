/* Shared declarations for the video-call sentinel. */

#pragma once

#include "app_config.h"
#include "board.h"
#include "media_sys.h"
#include "network.h"
#include "settings.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Call state, as reported by call_get_state()
 */
typedef enum {
    CALL_STATE_IDLE,       /*!< Registered with the broker, nothing incoming */
    CALL_STATE_ANNOUNCING, /*!< RING received, playing the two-tone alert */
    CALL_STATE_CONNECTING, /*!< Answered; peer connection being negotiated */
    CALL_STATE_CONNECTED,  /*!< Media flowing */
} call_state_t;

/**
 * @brief Open the signaling channel and wait for a caller
 *
 * @note Returns once signaling is up. Incoming calls are answered by the
 *       sentinel logic without further application involvement.
 *
 * @param[in] room Topic namespace on the broker, normally the device id
 *
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE with no network
 */
esp_err_t call_service_start(const char *room);

/**
 * @brief Tear down the signaling channel and any call in progress
 */
esp_err_t call_service_stop(void);

/**
 * @brief Drive esp_webrtc's periodic bookkeeping
 *
 * @note Must be called every couple of seconds from a task context; this is
 *       where connection timeouts are noticed, and where the signaling
 *       session is rebuilt after a call ends — the unit answers only one
 *       offer per session, so the rebuild is what makes a second call
 *       possible without a reset. Never call it from an esp_webrtc callback.
 */
void call_service_poll(void);

/**
 * @brief Current call state
 */
call_state_t call_get_state(void);

/**
 * @brief Hang up an active call
 *
 * @note Only meaningful from the console. There is no hang-up control on
 *       the device itself: the caller ends the call.
 */
void call_hangup(void);

#ifdef __cplusplus
}
#endif
