cmake_minimum_required(VERSION 3.16)

# Vendored components (av_render, esp_peer, esp_webrtc, media_lib_utils,
# webrtc_utils and the Waveshare BSP) are not on the component registry.
# Copy them in before the first build:
#
#   Copy-Item ..\p4_webrtc_firmware\components_all\components .\components -Recurse
#
# See README.md.

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32p4_sentinel_intercom)
