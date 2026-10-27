/* OpenAI integration for the case where the caller is an AI rather than a
 * person.
 *
 * Nothing here is wired into the call path yet — app_main only initialises
 * it when AI_AGENT_ENABLED is set, and it is off by default. The two halves
 * are at different stages and it is worth being explicit about which:
 *
 *   Vision (ai_agent_describe_frame)
 *       Complete and self-contained. Takes a JPEG straight off the capture
 *       sink, base64s it into a chat-completions request and returns the
 *       model's text. Useful on its own for a sentinel that should say who
 *       is at the door.
 *
 *   Realtime voice (ai_agent_realtime_*)
 *       The transport, session setup and audio framing are written, and the
 *       codec choice is deliberate: the Realtime API speaks G.711 A-law at
 *       8 kHz, which is exactly what this board's call audio already is, so
 *       frames pass through without transcoding or resampling. It has never
 *       been run against the live service. Treat the message names and the
 *       event schema as the parts most likely to have drifted.
 *
 * To make the AI the caller rather than a passenger on an existing call,
 * the missing piece is a bridge that feeds ai_agent_realtime_send_audio()
 * from the capture sink and pushes ai_agent_realtime_audio_cb_t output into
 * av_render, in place of the peer connection. The peer connection and the
 * agent are alternative sources for the same two pipes.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Called for each chunk of G.711 A-law audio the model produces
 *
 * @param[in] data  A-law samples, 8 kHz mono
 * @param[in] len   Byte count
 * @param[in] ctx   User context from ai_agent_realtime_start()
 */
typedef void (*ai_agent_realtime_audio_cb_t)(const uint8_t *data, int len,
                                             void *ctx);

/**
 * @brief Called when the model reports the caller stopped speaking
 */
typedef void (*ai_agent_realtime_done_cb_t)(void *ctx);

/**
 * @brief Runtime configuration
 */
typedef struct {
    const char *api_key;       /*!< OpenAI API key; required */
    const char *system_prompt; /*!< Instructions, NULL for the default */
} ai_agent_cfg_t;

/**
 * @brief Store credentials and instructions
 *
 * @note Copies both strings. Does not open any connection.
 *
 * @return ESP_OK on success, ESP_ERR_INVALID_ARG when no API key is given
 */
esp_err_t ai_agent_init(const ai_agent_cfg_t *cfg);

/**
 * @brief Release credentials and stop any running realtime session
 */
void ai_agent_deinit(void);

/**
 * @brief Whether ai_agent_init() has been called with a usable key
 */
bool ai_agent_is_ready(void);

/**
 * @brief Ask the vision model to describe one captured JPEG frame
 *
 * @note Blocks for the duration of the HTTPS round trip, which is seconds,
 *       not milliseconds. Never call it from the capture or render threads.
 *
 * @param[in]  jpeg     JPEG frame, as produced by the capture sink
 * @param[in]  jpeg_len Frame length in bytes
 * @param[out] out      Buffer receiving the NUL-terminated reply
 * @param[in]  out_size Size of @p out
 *
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE when not initialised,
 *         ESP_ERR_NO_MEM when the request could not be assembled, or the
 *         underlying esp_http_client error
 */
esp_err_t ai_agent_describe_frame(const uint8_t *jpeg, size_t jpeg_len,
                                  char *out, size_t out_size);

/**
 * @brief Open a Realtime API session
 *
 * @param[in] on_audio Invoked for each chunk of model audio
 * @param[in] on_done  Invoked when the model finishes a response, may be NULL
 * @param[in] ctx      Passed back to both callbacks
 *
 * @return ESP_OK on success, otherwise a websocket or configuration error
 */
esp_err_t ai_agent_realtime_start(ai_agent_realtime_audio_cb_t on_audio,
                                  ai_agent_realtime_done_cb_t on_done,
                                  void *ctx);

/**
 * @brief Append G.711 A-law audio to the model's input buffer
 *
 * @param[in] alaw 8 kHz mono A-law samples
 * @param[in] len  Byte count
 *
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE when no session is open
 */
esp_err_t ai_agent_realtime_send_audio(const uint8_t *alaw, int len);

/**
 * @brief Close the Realtime session
 */
void ai_agent_realtime_stop(void);

#ifdef __cplusplus
}
#endif
