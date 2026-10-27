idf_component_register(SRCS "audio_pipeline.c" "main.c"
                            "audiosense_mqtt.c"
                            "live_audio.c"
                       INCLUDE_DIRS "."
                       EMBED_TXTFILES "certs/ca.crt"
                                      "certs/hlias.crt"
                                      "certs/hlias.key")

# Retired files – safe to delete from the source tree:
#   audio_sad.c   audio_sad.h
#   audio_stream.c  audio_stream.h