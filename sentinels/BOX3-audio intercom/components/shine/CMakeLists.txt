# Shine MP3 Encoder - ESP-IDF Component
# Source: https://github.com/toots/shine (savonet/shine - canonical upstream)
#
# SETUP: Clone or download shine, then copy ALL files from shine/src/lib/
#        into this component's src/ directory:
#
#   git clone https://github.com/toots/shine.git /tmp/shine
#   cp /tmp/shine/src/lib/*.c /tmp/shine/src/lib/*.h components/shine/src/

idf_component_register(
    SRCS "src/bitstream.c"
         "src/huffman.c"
         "src/l3bitstream.c"
         "src/l3loop.c"
         "src/l3mdct.c"
         "src/l3subband.c"
         "src/layer3.c"
         "src/reservoir.c"
         "src/tables.c"
    INCLUDE_DIRS "src"
    REQUIRES "esp_common"
)

# Critical: -O3 and -ffast-math for acceptable encode speed on ESP32-S3
target_compile_options(${COMPONENT_LIB} PRIVATE -O3 -ffast-math)
