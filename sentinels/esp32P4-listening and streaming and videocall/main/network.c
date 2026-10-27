/* Network layer for the merged image — a shim over the sentinel's WiFi.
 *
 * In p4_webrtc_firmware this file owned the radio: nvs_flash_init,
 * esp_netif_init, the default event loop, esp_wifi_init and the STA event
 * handler. The sentinel does all of that in sentinel_app_init(), and one
 * board cannot have two esp_wifi_init() calls, so here the intercom simply
 * reads the state the sentinel maintains.
 *
 * network.h is unchanged, so webrtc.c and main.c compile against the same
 * API they always did.
 */

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "network.h"
#include "sentinel_app.h"

static const char *TAG = "NETWORK";

static network_event_cb_t s_cb;
static bool s_last_reported;

bool network_is_connected(void)
{
    return sentinel_app_wifi_connected();
}

esp_err_t network_init(const char *ssid, const char *password,
                       network_event_cb_t cb)
{
    /* Credentials are ignored: the sentinel joined with the ones from
     * settings.txt before this is reached. Keeping the signature means
     * the intercom's call sites did not have to change. */
    (void)ssid;
    (void)password;
    s_cb = cb;
    s_last_reported = network_is_connected();
    ESP_LOGI(TAG, "Using the sentinel's WiFi connection (currently %s)",
             s_last_reported ? "up" : "down");
    return ESP_OK;
}

esp_err_t network_connect_wifi(const char *ssid, const char *password)
{
    (void)ssid;
    (void)password;
    ESP_LOGW(TAG, "WiFi is owned by the sentinel; edit settings.txt instead");
    return ESP_ERR_NOT_SUPPORTED;
}

void network_poll_events(void)
{
    /* The sentinel has its own event handler but no way to reach the
     * intercom's callback, so the transition is detected by polling from
     * main's loop. A couple of seconds of latency on "the network went
     * away" is immaterial: it only decides when signaling is torn down
     * and retried. */
    bool now = network_is_connected();
    if (now == s_last_reported) {
        return;
    }
    s_last_reported = now;
    ESP_LOGI(TAG, "Network went %s", now ? "up" : "down");
    if (s_cb) {
        s_cb(now);
    }
}
