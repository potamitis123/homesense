#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "media_sys.h"
#include "mode_arbiter.h"
#include "sentinel_app.h"
#include "settings.h"
#include "video_stream.h"

static const char *TAG = "MODE";

/* How long the sentinel is given to let go of the codecs. Generous: it may
 * be finishing an utterance, and cutting that short to answer a call the
 * caller will wait for anyway is the wrong trade. Past this the call
 * proceeds regardless — a call with poor audio beats a missed one. */
#define MODE_SWITCH_TIMEOUT_MS 3000

static struct {
    p4_mode_t mode;
    SemaphoreHandle_t lock;
} s_arb;

const char *mode_arbiter_name(p4_mode_t mode)
{
    switch (mode) {
    case P4_MODE_SENTINEL:    return "sentinel";
    case P4_MODE_TO_CALL:     return "sentinel->call";
    case P4_MODE_CALL:        return "call";
    case P4_MODE_TO_SENTINEL: return "call->sentinel";
    default:                  return "boot";
    }
}

esp_err_t mode_arbiter_init(void)
{
    if (s_arb.lock != NULL) {
        return ESP_OK;
    }
    s_arb.lock = xSemaphoreCreateMutex();
    if (s_arb.lock == NULL) {
        ESP_LOGE(TAG, "Cannot create the mode lock");
        return ESP_ERR_NO_MEM;
    }
    s_arb.mode = P4_MODE_BOOT;
    return ESP_OK;
}

p4_mode_t mode_arbiter_get(void)
{
    return s_arb.mode;
}

bool mode_arbiter_call_owns_hardware(void)
{
    /* The transitional states count as the call's: the sentinel has already
     * been told to let go, and anything that asks "may I touch the codecs?"
     * during the handover must be told no. */
    return s_arb.mode == P4_MODE_CALL || s_arb.mode == P4_MODE_TO_CALL;
}

esp_err_t mode_arbiter_enter_sentinel(void)
{
    if (s_arb.lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_arb.lock, portMAX_DELAY);
    if (s_arb.mode == P4_MODE_SENTINEL) {
        xSemaphoreGive(s_arb.lock);
        return ESP_OK;
    }
    s_arb.mode = P4_MODE_TO_SENTINEL;
    ESP_LOGI(TAG, "Entering sentinel mode");

    /* Order matters and is the reverse of the handover. The call's codecs
     * must already be closed by esp_webrtc before the microphone is
     * reopened, or the shared I2S port is pinned at the call's rate and the
     * sentinel records at the wrong speed. esp_webrtc closes them during
     * its own teardown, which has completed by the time the DISCONNECTED
     * event that brought us here was delivered. */
    esp_err_t ret = sentinel_app_resume();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Sentinel did not resume: %s", esp_err_to_name(ret));
    }

    /* The panel has been written over by av_render for the length of the
     * call; LVGL still believes its framebuffer is intact. */
    sentinel_app_redraw();

    /* Last, because it is the least important of the three and the only one
     * that can be retried for free: the uplink task re-arms itself. */
    video_stream_call_ended();

    s_arb.mode = P4_MODE_SENTINEL;
    sentinel_app_set_status("Listening", 0x4ecca3);
    ESP_LOGI(TAG, "Sentinel mode active");
    xSemaphoreGive(s_arb.lock);
    return ret;
}

esp_err_t mode_arbiter_enter_call(void)
{
    if (s_arb.lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    /* Re-entrant by design: an offer and a RING for the same call both land
     * here, and so does a second offer from a caller that retries. */
    if (mode_arbiter_call_owns_hardware()) {
        return ESP_OK;
    }
    xSemaphoreTake(s_arb.lock, portMAX_DELAY);
    if (mode_arbiter_call_owns_hardware()) {
        xSemaphoreGive(s_arb.lock);
        return ESP_OK;
    }
    s_arb.mode = P4_MODE_TO_CALL;
    ESP_LOGI(TAG, "Call incoming; taking the hardware from the sentinel");

    /* Camera first: it is the only one of the three that can be released
     * without waiting for anything, and doing it first frees encoder time
     * for the handover. */
    video_stream_call_started();

    /* Then the codecs. This is the one that must complete before the offer
     * is parsed: pc_start() enables esp_webrtc's capture sink, which opens
     * the same ES7210 the sentinel is holding. */
    esp_err_t ret = sentinel_app_suspend(MODE_SWITCH_TIMEOUT_MS);
    if (ret != ESP_OK) {
        /* Proceed anyway. The call may come up without a microphone, which
         * is bad, but refusing the call outright is worse — and the log
         * line below is what tells us which happened. */
        ESP_LOGE(TAG, "Sentinel did not release the codecs in %d ms (%s); "
                      "answering anyway, this call may have no microphone",
                 MODE_SWITCH_TIMEOUT_MS, esp_err_to_name(ret));
    }

    s_arb.mode = P4_MODE_CALL;
    ESP_LOGI(TAG, "Call mode active");
    xSemaphoreGive(s_arb.lock);
    return ret;
}

esp_err_t mode_arbiter_end_call(void)
{
    if (s_arb.lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    /* esp_webrtc reports a call ending through more than one path —
     * DISCONNECTED, CONNECT_FAILED, a peer's DENY_CALL, and call_hangup()
     * — so this is expected to be called when there is nothing to end. */
    if (s_arb.mode != P4_MODE_CALL && s_arb.mode != P4_MODE_TO_CALL) {
        return ESP_OK;
    }
    ESP_LOGI(TAG, "Call over");
    return mode_arbiter_enter_sentinel();
}
