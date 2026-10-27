
idf_component_register(
    SRCS "esp32_p4_wifi6_touch_lcd_5.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "priv_include"
    REQUIRES driver lvgl__lvgl espressif__esp_lvgl_adapter
    PRIV_REQUIRES esp_lcd usb spiffs fatfs
)
