idf_component_register(
    SRCS
        # Boot and arbitration
        "main.c"
        "mode_arbiter.c"

        # Audio sentinel (from P4-audio-sentinel)
        "sentinel_app.c"
        "audio_pipeline.c"
        "audiosense_mqtt.c"
        "live_audio.c"
        "config.c"
        "mqtt_config.c"

        # Audiovisual intercom (from p4_webrtc_firmware)
        "board.c"
        "media_sys.c"
        "network.c"
        "webrtc.c"
        "mqtt_signaling.c"
        "broker_addr.c"
        "video_stream.c"
        "debug_log.c"
        "app_config.c"
        "ai_agent.c"
    INCLUDE_DIRS
        "."
    # Signaling and uplink credentials. EMBED_TXTFILES NUL-terminates each
    # blob, which is what esp-mqtt and esp-tls expect when the matching
    # *_len field is left at zero.
    EMBED_TXTFILES
        "certs/ca.crt"
        "certs/hlias.crt"
        "certs/hlias.key"
    REQUIRES
        console
        driver
        esp_event
        esp_http_client
        esp_http_server
        esp_lcd
        esp_netif
        esp_timer
        esp_wifi
        esp-tls
        fatfs
        json
        mqtt
        nvs_flash
        # Local / vendored stack
        av_render
        esp_peer
        esp_webrtc
        esp32_p4_wifi6_touch_lcd_5
        media_lib_utils
        webrtc_utils
        # Managed (espressif/* → espressif__*)
        espressif__cjson
        espressif__esp_audio_codec
        espressif__esp_capture
        espressif__esp_codec_dev
        espressif__esp_video
        espressif__esp_video_codec
        espressif__esp_websocket_client
        espressif__esp_wifi_remote
        espressif__media_lib_sal
        lvgl__lvgl
        espressif__esp_lvgl_adapter
)

# Panel UI is labels and a bar: it does not decode JPEG. The HW JPEG
# codec's DMA descriptors must live in internal RAM, and LVGL taking a
# decoder engine at boot is what left jpeg_new_encoder_engine() with no
# rxlink. Frame buffers already go to PSRAM; this only frees the internal
# descriptors for the camera encoder.
idf_build_set_property(COMPILE_OPTIONS "-DESP_LV_ENABLE_HW_JPEG=0" APPEND)
