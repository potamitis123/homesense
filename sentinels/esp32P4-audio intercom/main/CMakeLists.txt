idf_component_register(SRCS "audio_pipeline.c" "main.c"
                            "audiosense_mqtt.c"
                            "live_audio.c"
                            "config.c"
                            "mqtt_config.c"
                       INCLUDE_DIRS "."
                       EMBED_TXTFILES "certs/ca.crt"
                                      "certs/hlias.crt"
                                      "certs/hlias.key")
