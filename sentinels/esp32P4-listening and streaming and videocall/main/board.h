/* Waveshare ESP32-P4-WIFI6-Touch-LCD-5 board layer.
 *
 * Everything hardware-specific lives behind this header. The media graph in
 * media_sys.c and the call logic in webrtc.c consume only the handles below,
 * which is what keeps Espressif's esp_capture / av_render / esp_peer stack
 * unmodified: upstream's board.c reaches into the `codec_board` component,
 * ours reaches into the Waveshare BSP, and neither is visible further up.
 */

#pragma once

#include <stdbool.h>
#include <stdarg.h>

#include "driver/i2c_master.h"
#include "esp_codec_dev.h"
#include "esp_err.h"
#include "esp_lcd_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Bring up I2C, both audio codecs and the MIPI-DSI panel
 *
 * @note No LVGL. av_render draws decoded frames straight into the panel's
 *       DPI framebuffer, so the graphics library would only contend for the
 *       same memory bandwidth. The BSP's bsp_display_new() gives us the raw
 *       esp_lcd_panel_handle_t without starting esp_lv_adapter.
 *
 * @return ESP_OK on success, otherwise the first failing BSP call's error
 */
esp_err_t board_init(void);

/**
 * @brief ES8311 playback handle, for av_render's I2S renderer
 */
esp_codec_dev_handle_t board_get_playback_handle(void);

/**
 * @brief ES7210 capture handle, for esp_capture's audio device source
 */
esp_codec_dev_handle_t board_get_record_handle(void);

/**
 * @brief MIPI-DSI panel handle, for av_render's LCD renderer
 */
esp_lcd_panel_handle_t board_get_lcd_handle(void);

/**
 * @brief The shared I2C bus (codecs, touch controller and the OV5647's SCCB)
 */
i2c_master_bus_handle_t board_get_i2c_handle(void);

/**
 * @brief Play the incoming-call announcement: ANNOUNCE_REPEAT short tones
 *
 * @note Blocks until the last tone has been written. Callers must invoke
 *       this before enabling the peer connection, because it drives the
 *       ES8311 directly and av_render takes exclusive ownership of that
 *       codec once call audio starts.
 *
 * @return ESP_OK if the tone reached the codec, otherwise an error
 */
esp_err_t board_play_announce_tone(void);

/**
 * @brief Probe the OV5647 over SCCB and read back its chip ID
 *
 * @note Runs before esp_video_init(), purely as a boot diagnostic. The
 *       sensor has its own oscillator and is not held in reset on this
 *       board (reset and power-down are both strapped, not GPIO-driven),
 *       so it answers on I2C without the CSI stack running.
 *
 * @return ESP_OK when 0x5647 reads back, ESP_ERR_NOT_FOUND when the sensor
 *         does not acknowledge, ESP_ERR_INVALID_RESPONSE on a wrong ID
 */
esp_err_t board_probe_camera(void);

/**
 * @brief Clear the panel and start the boot/status monitor overlay
 *
 * @note Draws plain RGB565 text with no LVGL. Call after board_init().
 *       board_status_stop() must be called before av_render owns the panel.
 */
void board_status_begin(void);

/**
 * @brief Append one status line and refresh the overlay
 *
 * @note No-op after board_status_stop(). Safe from the main task.
 */
void board_status_print(const char *fmt, ...);

/**
 * @brief Hand the panel to video; further status prints are ignored
 */
void board_status_stop(void);

/**
 * @brief Whether the status overlay is still active
 */
bool board_status_active(void);

#ifdef __cplusplus
}
#endif
