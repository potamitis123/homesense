idf_component_register(
    SRCS 
    
    "wifi_manager.c"
    "my_mqtt_client.c"
    "i2s_audio.c"
    "usb_audio.c"
    "confing_file.c" 
    "main.c"
    INCLUDE_DIRS 
        "."
    EMBED_TXTFILES
        "certs/ca.crt"
        "certs/hlias.crt"
        "certs/hlias.key"
)


