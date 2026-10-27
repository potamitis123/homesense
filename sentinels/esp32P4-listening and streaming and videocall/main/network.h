/* Wi-Fi station bring-up.
 *
 * The P4 has no radio. esp_wifi_remote forwards the esp_wifi_* API over
 * SDIO to the on-board ESP32-C6, so this is ordinary station code, but it
 * only works once the C6 has been flashed with the esp_hosted slave
 * firmware. If association never completes and the log shows no AP scan at
 * all, suspect the C6 image before suspecting the credentials.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Called on every association change
 *
 * @param[in] connected true once an IP is bound, false on disconnect
 */
typedef void (*network_event_cb_t)(bool connected);

/**
 * @brief Initialise NVS, the network stack and the station interface, then
 *        start associating
 *
 * @note Returns as soon as the connection attempt is under way. Use the
 *       callback, or poll network_is_connected(), to know when there is an
 *       IP address.
 *
 * @param[in] ssid Network name; an empty string leaves the radio down
 * @param[in] password Passphrase, may be NULL or empty for an open network
 * @param[in] cb Association change callback, may be NULL
 *
 * @return ESP_OK on success, otherwise the failing esp_wifi call's error
 */
esp_err_t network_init(const char *ssid, const char *password,
                       network_event_cb_t cb);

/**
 * @brief Re-associate with a different network
 *
 * @param[in] ssid Network name
 * @param[in] password Passphrase, may be NULL
 *
 * @return ESP_OK on success
 */
esp_err_t network_connect_wifi(const char *ssid, const char *password);

/**
 * @brief Notice a change in the sentinel's WiFi state
 *
 * @note Merged-image only. WiFi belongs to the sentinel, whose event
 *       handler cannot reach the callback registered here, so the
 *       transition is polled from the main loop instead. Call every couple
 *       of seconds.
 */
void network_poll_events(void);

/**
 * @brief Whether the station currently holds an IP address
 */
bool network_is_connected(void);

#ifdef __cplusplus
}
#endif
