/* Board layer for the merged image — an adapter, not an initialiser.
 *
 * In p4_webrtc_firmware this file brought up I2C, both audio codecs and
 * the MIPI-DSI panel. Here the sentinel has already done all three by the
 * time any of this is reachable (see sentinel_app.h), so board_init() only
 * checks that they are there and the getters hand out the sentinel's
 * handles. Calling bsp_audio_codec_speaker_init() or bsp_display_new() a
 * second time on one board is how you get a device that works right up
 * until the second subsystem starts.
 *
 * The status overlay is gone with it. The original wrote RGB565 text
 * straight into the DPI framebuffer, which is fine when nothing else owns
 * the panel; here LVGL does. The board_status_* calls that webrtc.c makes
 * are therefore forwarded to the sentinel's own status label, and are
 * dropped while a call has the UI frozen — which is what you want: those
 * messages exist to be read between calls.
 */

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "board.h"
#include "bsp/esp-bsp.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "sentinel_app.h"
#include "settings.h"

static const char *TAG = "BOARD";

/* The OV5647's SCCB lines hang off the same bus as the codecs and the
 * touch controller. Registers are 16-bit addressed, high byte first. */
#define OV5647_SCCB_ADDR 0x36
#define OV5647_REG_CHIP_ID_HI 0x300A
#define OV5647_CHIP_ID 0x5647

esp_codec_dev_handle_t board_get_playback_handle(void)
{
    return sentinel_app_speaker_handle();
}

esp_codec_dev_handle_t board_get_record_handle(void)
{
    return sentinel_app_microphone_handle();
}

esp_lcd_panel_handle_t board_get_lcd_handle(void)
{
    /* The BSP keeps the panel handle even when it was started under LVGL,
     * which is what lets av_render decode into the same framebuffer LVGL
     * draws the audio bar into — one at a time, arbitrated by mode. */
    return bsp_display_get_panel_handle();
}

i2c_master_bus_handle_t board_get_i2c_handle(void)
{
    return bsp_i2c_get_handle();
}

esp_err_t board_init(void)
{
    if (board_get_record_handle() == NULL || board_get_playback_handle() == NULL) {
        ESP_LOGE(TAG, "Codecs are not up; the sentinel must init first");
        return ESP_ERR_INVALID_STATE;
    }
    if (board_get_lcd_handle() == NULL) {
        ESP_LOGW(TAG, "No panel handle; inbound call video will not render");
    }
    ESP_LOGI(TAG, "Board adapter ready: %dx%d panel, codecs from the sentinel",
             BSP_LCD_H_RES, BSP_LCD_V_RES);
    return ESP_OK;
}

/* Fills `dst` with one fade-in/fade-out sine burst. The envelope matters:
 * a hard start would put a step edge into the class-D amplifier, which the
 * speaker reproduces as a click louder than the tone itself. */
static void fill_tone(int16_t *dst, int samples)
{
    const int fade = samples / 8;
    for (int i = 0; i < samples; i++) {
        float env = 1.0f;
        if (i < fade) {
            env = (float)i / (float)fade;
        } else if (i > samples - fade) {
            env = (float)(samples - i) / (float)fade;
        }
        float phase = 2.0f * (float)M_PI * (float)ANNOUNCE_TONE_HZ *
                      (float)i / (float)AUDIO_SAMPLE_RATE;
        dst[i] = (int16_t)((float)ANNOUNCE_AMPLITUDE * env * sinf(phase));
    }
}

esp_err_t board_play_announce_tone(void)
{
    esp_codec_dev_handle_t playback = board_get_playback_handle();
    if (playback == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    const int tone_samples = AUDIO_SAMPLE_RATE * ANNOUNCE_TONE_MS / 1000;
    const int gap_samples = AUDIO_SAMPLE_RATE * ANNOUNCE_GAP_MS / 1000;
    const int total = tone_samples + gap_samples;

    /* Internal DMA-capable RAM: this buffer is handed straight to I2S. */
    int16_t *buf = heap_caps_calloc(total, sizeof(int16_t),
                                    MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    if (buf == NULL) {
        return ESP_ERR_NO_MEM;
    }
    fill_tone(buf, tone_samples);

    esp_codec_dev_sample_info_t fs = {
        .sample_rate = AUDIO_SAMPLE_RATE,
        .channel = AUDIO_CHANNELS,
        .bits_per_sample = AUDIO_BITS_PER_SAMPLE,
    };
    esp_err_t ret = ESP_OK;
    /* Only reachable once the arbiter has taken the loudspeaker off the
     * sentinel, so the codec is free and this open is what sets the rate. */
    int err = esp_codec_dev_open(playback, &fs);
    if (err != ESP_CODEC_DEV_OK) {
        ESP_LOGW(TAG, "announce tone skipped, codec busy (%d)", err);
        heap_caps_free(buf);
        return ESP_ERR_INVALID_STATE;
    }
    esp_codec_dev_set_out_vol(playback, SPEAKER_VOLUME);
    esp_codec_dev_set_out_mute(playback, false);

    for (int i = 0; i < ANNOUNCE_REPEAT; i++) {
        /* Last burst carries no trailing gap, so the codec closes on
         * silence rather than mid-decay. */
        int len = (i == ANNOUNCE_REPEAT - 1) ? tone_samples : total;
        err = esp_codec_dev_write(playback, buf, len * (int)sizeof(int16_t));
        if (err != ESP_CODEC_DEV_OK) {
            ESP_LOGE(TAG, "announce tone write failed (%d)", err);
            ret = ESP_FAIL;
            break;
        }
    }

    esp_codec_dev_close(playback);
    heap_caps_free(buf);
    return ret;
}

static esp_err_t ov5647_read_reg(uint16_t reg, uint8_t *out)
{
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    if (bus == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = OV5647_SCCB_ADDR,
        .scl_speed_hz = 100000,
    };
    i2c_master_dev_handle_t dev = NULL;
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(bus, &dev_cfg, &dev), TAG,
                        "SCCB device add failed");

    uint8_t addr_be[2] = { (uint8_t)(reg >> 8), (uint8_t)(reg & 0xff) };
    esp_err_t ret = i2c_master_transmit_receive(dev, addr_be, sizeof(addr_be),
                                                out, 1, 200);
    i2c_master_bus_rm_device(dev);
    return ret;
}

esp_err_t board_probe_camera(void)
{
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    if (bus == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (i2c_master_probe(bus, OV5647_SCCB_ADDR, 200) != ESP_OK) {
        ESP_LOGE(TAG, "OV5647 not detected on SCCB");
        return ESP_ERR_NOT_FOUND;
    }

    uint8_t hi = 0, lo = 0;
    if (ov5647_read_reg(OV5647_REG_CHIP_ID_HI, &hi) != ESP_OK ||
        ov5647_read_reg(OV5647_REG_CHIP_ID_HI + 1, &lo) != ESP_OK) {
        ESP_LOGW(TAG, "OV5647 acknowledges but will not report its ID");
        return ESP_ERR_INVALID_RESPONSE;
    }

    uint16_t id = ((uint16_t)hi << 8) | lo;
    if (id != OV5647_CHIP_ID) {
        ESP_LOGW(TAG, "Camera reports ID 0x%04X, expected 0x%04X",
                 (unsigned)id, OV5647_CHIP_ID);
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "OV5647 detected");
    return ESP_OK;
}

/* ── Status overlay → the sentinel's status label ───────────────────
 *
 * webrtc.c reports call progress through these. They now land on the
 * LVGL label the sentinel already owns, and are silently dropped while a
 * call has the UI frozen.
 */

void board_status_begin(void)
{
    /* Nothing to set up: the sentinel's UI exists from boot. */
}

void board_status_print(const char *fmt, ...)
{
    if (fmt == NULL) {
        return;
    }
    char line[64];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);

    ESP_LOGI(TAG, "status: %s", line);
    sentinel_app_set_status(line, 0x4ecca3);
}

void board_status_stop(void)
{
    /* The arbiter freezes the UI when a call takes the panel; there is no
     * separate overlay to tear down. */
}

bool board_status_active(void)
{
    return !sentinel_app_is_suspended();
}
