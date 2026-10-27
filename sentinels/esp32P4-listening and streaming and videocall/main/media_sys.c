#include <stdlib.h>
#include <string.h>

#include "av_render.h"
#include "av_render_default.h"
#include "board.h"
#include "bsp/esp-bsp.h"
#include "esp_audio_dec_default.h"
#include "esp_audio_enc_default.h"
#include "esp_capture_defaults.h"
#include "esp_capture_sink.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_video_dec_default.h"
#include "decoder/impl/esp_video_dec_mjpeg.h"
#include "esp_video_enc_default.h"
#include "encoder/impl/esp_video_enc_mjpeg.h"
#include "esp_video_init.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "media_lib_os.h"
#include "media_sys.h"
#include "settings.h"

static const char *TAG = "MEDIA_SYS";

/* esp_webrtc claims sink 0. Stills for the AI path go through a second sink
 * so the two never reconfigure each other's encoder.
 *
 * The situational-awareness uplink shares that same second sink rather than
 * opening a third: the SoC has one JPEG encoder, and a third sink would only
 * add another geometry for it to switch between. The two users of sink 1 are
 * serialised by s_capture.video_lock, held for as long as a frame is checked
 * out — which is why acquire and release must be paired from one task. */
#define VIDEO_SINK_INDEX 1
#define STILL_TIMEOUT_MS 2000

/* Sink 0 belongs to esp_webrtc, but it is reserved here at boot rather than
 * left to esp_webrtc to create. See reserve_call_sink(). */
#define CALL_SINK_INDEX 0

static struct {
    esp_capture_handle_t capture;
    esp_capture_video_src_if_t *vid_src;
    esp_capture_audio_src_if_t *aud_src;
    esp_capture_sink_handle_t video_sink;
    SemaphoreHandle_t video_lock;
    bool stream_running;
    int sink_width;
    int sink_height;
    int sink_fps;
} s_capture;

static struct {
    audio_render_handle_t audio_render;
    video_render_handle_t video_render;
    av_render_handle_t player;
} s_player;

static esp_capture_video_src_if_t *create_video_source(void)
{
    /* The OV5647 shares the board's I2C bus with both audio codecs and the
     * touch controller, so the bus is handed in already initialised rather
     * than letting esp_video create its own. Reset and power-down are
     * strapped on this board, not wired to GPIOs — hence -1 for both. */
    esp_video_init_csi_config_t csi_config[] = {
        {
            .sccb_config = {
                .init_sccb = false,
                .i2c_handle = board_get_i2c_handle(),
                .freq = CONFIG_BSP_I2C_CLK_SPEED_HZ,
            },
            .reset_pin = -1,
            .pwdn_pin = -1,
        },
    };
    esp_video_init_config_t cam_config = {
        .csi = csi_config,
    };

    /* CSI + ISP first. JPEG encode/decode are separate VFS devices; if they
     * fail, esp_video_init(ALL) used to tear the camera down with them. */
    esp_err_t ret = esp_video_init_with_flags(&cam_config,
        ESP_VIDEO_INIT_FLAGS_MIPI_CSI | ESP_VIDEO_INIT_FLAGS_ISP);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "esp_video_init CSI/ISP failed: %s", esp_err_to_name(ret));
        return NULL;
    }
    ret = esp_video_init_with_flags(&cam_config,
        ESP_VIDEO_INIT_FLAGS_JPEG_ENC | ESP_VIDEO_INIT_FLAGS_JPEG_DEC);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "JPEG VFS devices failed (%s); camera still available",
                 esp_err_to_name(ret));
    }

    esp_capture_video_v4l2_src_cfg_t v4l2_cfg = { .buf_count = 2 };
    strncpy(v4l2_cfg.dev_name, "/dev/video0", sizeof(v4l2_cfg.dev_name) - 1);
    return esp_capture_new_video_v4l2_src(&v4l2_cfg);
}

static esp_err_t build_capture_system(void)
{
    s_capture.vid_src = create_video_source();
    if (s_capture.vid_src == NULL) {
        /* A missing camera must not take the intercom down: audio is the
         * half of the call this unit actually contributes. */
        ESP_LOGW(TAG, "No camera; continuing audio-only");
    }

    esp_capture_audio_dev_src_cfg_t codec_cfg = {
        .record_handle = board_get_record_handle(),
        .mic_gain_db = MIC_GAIN_DB,
    };
    s_capture.aud_src = esp_capture_new_audio_dev_src(&codec_cfg);
    if (s_capture.aud_src == NULL) {
        ESP_LOGE(TAG, "Failed to create the audio capture source");
        return ESP_FAIL;
    }

    esp_capture_cfg_t cfg = {
        .sync_mode = ESP_CAPTURE_SYNC_MODE_AUDIO,
        .audio_src = s_capture.aud_src,
        .video_src = s_capture.vid_src,
    };
    if (esp_capture_open(&cfg, &s_capture.capture) != ESP_CAPTURE_ERR_OK) {
        ESP_LOGE(TAG, "esp_capture_open failed");
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* Reserve esp_webrtc's capture sink before anything starts the graph.
 *
 * esp_webrtc creates sink 0 itself, inside pc_start() — that is, when the
 * *offer* arrives, not at esp_webrtc_start(). By then the uplink has had the
 * capture graph running since boot, and esp_capture refuses a sink setup on a
 * started graph:
 *
 *     if (capture->started) { ... "Not support setup sink after started" }
 *                                      -- esp_capture.c, esp_capture_sink_setup
 *
 * so sink 0 never existed, the call came up with no capture path, and this
 * unit sent no audio at all ("Send A:0" in the webrtc stats) while inbound
 * audio and video — which are decode paths and never touch esp_capture —
 * worked perfectly. That asymmetry is what made it look like a codec fault.
 *
 * The same function has an escape hatch immediately above that error: a setup
 * whose configuration matches an existing sink returns the existing handle,
 * started or not. So the fix is to create sink 0 here, at boot, with exactly
 * the configuration esp_webrtc will ask for later. It then finds its own sink
 * waiting for it and succeeds whatever the graph is doing.
 *
 * Which means this must mirror pc_start()'s sink_cfg exactly, and stay in step
 * with webrtc.c's peer_cfg:
 *
 *   - audio format follows the peer codec (G711A), and esp_webrtc hardcodes
 *     16 bits per sample, so that is hardcoded here too rather than taken from
 *     AUDIO_BITS_PER_SAMPLE;
 *   - a RECV_ONLY direction makes esp_webrtc set the format to NONE, and
 *     esp_capture only compares the geometry fields when the format is set.
 *
 * Reserved, not enabled: only enabled paths are started, so the ES7210 stays
 * shut until esp_webrtc enables this sink for a call.
 */
static esp_err_t reserve_call_sink(void)
{
    esp_capture_sink_cfg_t sink_cfg = {
        .audio_info = {
            .format_id = ESP_CAPTURE_FMT_ID_G711A,
            .sample_rate = AUDIO_SAMPLE_RATE,
            .channel = AUDIO_CHANNELS,
            .bits_per_sample = 16,
        },
        .video_info = {
            .format_id = VIDEO_SEND_ENABLED ? ESP_CAPTURE_FMT_ID_MJPEG
                                            : ESP_CAPTURE_FMT_ID_NONE,
            .width = VIDEO_SEND_ENABLED ? VIDEO_SEND_WIDTH : VIDEO_WIDTH,
            .height = VIDEO_SEND_ENABLED ? VIDEO_SEND_HEIGHT : VIDEO_HEIGHT,
            .fps = VIDEO_SEND_ENABLED ? VIDEO_SEND_FPS : VIDEO_FPS,
        },
    };

    esp_capture_sink_handle_t sink = NULL;
    if (esp_capture_sink_setup(s_capture.capture, CALL_SINK_INDEX, &sink_cfg,
                               &sink) != ESP_CAPTURE_ERR_OK) {
        /* Not fatal on its own: with the uplink disabled the graph is never
         * started early, and esp_webrtc's own setup still works. */
        ESP_LOGE(TAG, "Could not reserve the call's capture sink; a call "
                      "started while the uplink is running will have no "
                      "microphone");
        return ESP_ERR_INVALID_STATE;
    }
    ESP_LOGI(TAG, "Call sink reserved: G711A %d Hz %d ch, video %s",
             AUDIO_SAMPLE_RATE, AUDIO_CHANNELS,
             VIDEO_SEND_ENABLED ? "MJPEG send" : "recv-only");
    return ESP_OK;
}

static esp_err_t build_player_system(void)
{
    /* fixed_clock keeps the renderer from reprogramming the I2S clock. The
     * ES8311 and ES7210 share one port here, so a rate change on playback
     * would drag capture with it and desynchronise the uplink. */
    i2s_render_cfg_t i2s_cfg = {
        .play_handle = board_get_playback_handle(),
        .fixed_clock = true,
        .volume = SPEAKER_VOLUME,
    };
    s_player.audio_render = av_render_alloc_i2s_render(&i2s_cfg);
    if (s_player.audio_render == NULL) {
        ESP_LOGE(TAG, "Failed to create the audio renderer");
        return ESP_FAIL;
    }

    /* dsi_panel and use_frame_buffer together let the renderer decode
     * straight into the DPI framebuffers the BSP already allocated, instead
     * of decoding to a scratch buffer and blitting. */
    lcd_render_cfg_t lcd_cfg = {
        .lcd_handle = board_get_lcd_handle(),
        .dsi_panel = true,
        .use_frame_buffer = true,
    };
    s_player.video_render = av_render_alloc_lcd_render(&lcd_cfg);
    if (s_player.video_render == NULL) {
        ESP_LOGE(TAG, "Failed to create the video renderer");
        return ESP_FAIL;
    }

    av_render_cfg_t render_cfg = {
        .audio_render = s_player.audio_render,
        .video_render = s_player.video_render,
        /* No A/V sync, because there is nothing to synchronise against.
         *
         * Inbound video is whole JPEGs on the data channel with no frame
         * header and no timestamp, so every frame is presented with pts 0.
         * FOLLOW_AUDIO compares that against a running audio clock in
         * video_sync_control_before_render(), decides the video is impossibly
         * far behind, logs
         *
         *     AV_RENDER: Video lost sync now:0 audio:3262546 ref:649
         *
         * and resets sent_frame_num to resynchronise -- repeatedly, once the
         * audio clock has advanced a couple of seconds. On a static scene the
         * reset is invisible, which is why a webcam call looks perfect; on
         * moving footage it is the "crushed" picture.
         *
         * SYNC_NONE renders each frame as it arrives, which is the only
         * meaningful policy for a stream that carries no presentation times.
         * Audio is unaffected: it has its own timestamps and its own render
         * path.
         */
        .sync_mode = AV_RENDER_SYNC_NONE,
        .audio_raw_fifo_size = 4096,
        .audio_render_fifo_size = 6 * 1024,
        .video_raw_fifo_size = 500 * 1024,
        .allow_drop_data = true,
    };
    s_player.player = av_render_open(&render_cfg);
    if (s_player.player == NULL) {
        ESP_LOGE(TAG, "av_render_open failed");
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t media_sys_buildup(void)
{
    if (s_capture.video_lock == NULL) {
        s_capture.video_lock = xSemaphoreCreateMutex();
        if (s_capture.video_lock == NULL) {
            ESP_LOGE(TAG, "Cannot create the video sink lock");
            return ESP_ERR_NO_MEM;
        }
    }

    esp_audio_enc_register_default();
    esp_audio_dec_register_default();
    esp_video_enc_register_default();
    esp_video_dec_register_default();
    /* HW JPEG needs 64-byte DMA descriptors in internal RAM. That heap is
     * already used by DSI/CSI/I2S/SDIO, so jpeg_new_*_engine fails and the
     * capture graph then panics if we re-arm it. Software MJPEG runs from
     * PSRAM and is what lets inbound call video actually play. */
#if CONFIG_VIDEO_ENCODER_HW_MJPEG_SUPPORT
    esp_video_enc_unregister_mjpeg();
#endif
#if CONFIG_VIDEO_DECODER_HW_MJPEG_SUPPORT
    esp_video_dec_unregister_mjpeg();
#endif
    ESP_LOGI(TAG, "MJPEG codec is software (PSRAM); HW JPEG DMA is not used");

    ESP_RETURN_ON_ERROR(build_capture_system(), TAG, "capture buildup failed");
    /* Before the uplink can start the graph, and before any call. Failure is
     * logged inside rather than returned: it costs the microphone during a
     * call that overlaps the uplink, not the rest of the sentinel. */
    (void)reserve_call_sink();
    ESP_RETURN_ON_ERROR(build_player_system(), TAG, "player buildup failed");

    ESP_LOGI(TAG, "Media graphs ready (video send %s)",
             VIDEO_SEND_ENABLED ? "on" : "off");
    return ESP_OK;
}

esp_err_t media_sys_get_provider(esp_webrtc_media_provider_t *provider)
{
    if (provider == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_capture.capture == NULL || s_player.player == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    provider->capture = s_capture.capture;
    provider->player = s_player.player;
    return ESP_OK;
}

/* Sink 1 is configured once, by whichever of the two users gets there first
 * — in practice the uplink, which starts at boot. esp_capture has no way to
 * re-cut a sink's geometry underneath a running encoder, so a later caller
 * asking for a different size is told what it is actually getting rather
 * than being handed a silently wrong frame. */
static esp_err_t video_sink_ensure(int width, int height, int fps)
{
    if (s_capture.capture == NULL || s_capture.vid_src == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_capture.video_sink != NULL) {
        if (width != s_capture.sink_width || height != s_capture.sink_height) {
            ESP_LOGW(TAG, "Video sink is already %dx%d; serving %dx%d from it",
                     s_capture.sink_width, s_capture.sink_height, width, height);
        }
        return ESP_OK;
    }

    esp_capture_sink_cfg_t sink_cfg = {
        .video_info = {
            .format_id = ESP_CAPTURE_FMT_ID_MJPEG,
            .width = width,
            .height = height,
            .fps = fps,
        },
    };
    if (esp_capture_sink_setup(s_capture.capture, VIDEO_SINK_INDEX, &sink_cfg,
                               &s_capture.video_sink) != ESP_CAPTURE_ERR_OK) {
        ESP_LOGE(TAG, "Failed to set up the video sink");
        s_capture.video_sink = NULL;
        return ESP_ERR_INVALID_STATE;
    }
    s_capture.sink_width = width;
    s_capture.sink_height = height;
    s_capture.sink_fps = fps;
    return ESP_OK;
}

esp_err_t media_sys_video_stream_start(int width, int height, int fps)
{
    if (s_capture.video_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_capture.capture == NULL || s_capture.vid_src == NULL) {
        /* No camera: build_capture_system() already said so at boot. */
        return ESP_ERR_NOT_FOUND;
    }

    xSemaphoreTake(s_capture.video_lock, portMAX_DELAY);
    esp_err_t ret = video_sink_ensure(width, height, fps);
    if (ret == ESP_OK && !s_capture.stream_running) {
        if (esp_capture_sink_enable(s_capture.video_sink,
                                    ESP_CAPTURE_RUN_MODE_ALWAYS)
            != ESP_CAPTURE_ERR_OK) {
            ESP_LOGE(TAG, "Failed to enable the video sink");
            xSemaphoreGive(s_capture.video_lock);
            return ESP_FAIL;
        }
        /* Deliberately never paired with esp_capture_stop(): during a call
         * esp_webrtc owns the capture's run state, and stopping it here to
         * end the uplink would take the call's audio down with it. */
        esp_capture_start(s_capture.capture);
        s_capture.stream_running = true;
        ESP_LOGI(TAG, "Video uplink sink running at %dx%d @ %d fps",
                 s_capture.sink_width, s_capture.sink_height,
                 s_capture.sink_fps);
    }
    xSemaphoreGive(s_capture.video_lock);
    return ret;
}

void media_sys_video_stream_stop(void)
{
    if (s_capture.video_lock == NULL) {
        return;
    }
    xSemaphoreTake(s_capture.video_lock, portMAX_DELAY);
    if (s_capture.stream_running) {
        esp_capture_sink_enable(s_capture.video_sink,
                                ESP_CAPTURE_RUN_MODE_DISABLE);
        s_capture.stream_running = false;
        ESP_LOGI(TAG, "Video uplink sink stopped");
    }
    xSemaphoreGive(s_capture.video_lock);
}

bool media_sys_video_stream_running(void)
{
    return s_capture.stream_running;
}

esp_err_t media_sys_video_stream_acquire(esp_capture_stream_frame_t *frame)
{
    if (frame == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_capture.video_lock == NULL || !s_capture.stream_running) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_capture.video_lock, portMAX_DELAY);
    /* Re-checked under the lock: a concurrent stop() would otherwise leave
     * us acquiring from a sink that is no longer enabled. */
    if (!s_capture.stream_running) {
        xSemaphoreGive(s_capture.video_lock);
        return ESP_ERR_INVALID_STATE;
    }
    memset(frame, 0, sizeof(*frame));
    frame->stream_type = ESP_CAPTURE_STREAM_TYPE_VIDEO;
    if (esp_capture_sink_acquire_frame(s_capture.video_sink, frame, true) !=
        ESP_CAPTURE_ERR_OK) {
        xSemaphoreGive(s_capture.video_lock);
        return ESP_ERR_NOT_FOUND;
    }
    /* Lock stays held until release: the frame points into the sink's own
     * buffer, so nothing else may touch the sink while it is out. */
    return ESP_OK;
}

void media_sys_video_stream_release(esp_capture_stream_frame_t *frame)
{
    if (frame == NULL || s_capture.video_sink == NULL) {
        return;
    }
    esp_capture_sink_release_frame(s_capture.video_sink, frame);
    xSemaphoreGive(s_capture.video_lock);
}

esp_err_t media_sys_grab_still(uint8_t **jpeg, size_t *len)
{
    if (jpeg == NULL || len == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *jpeg = NULL;
    *len = 0;

    if (s_capture.capture == NULL || s_capture.vid_src == NULL ||
        s_capture.video_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (xSemaphoreTake(s_capture.video_lock,
                       pdMS_TO_TICKS(STILL_TIMEOUT_MS)) != pdTRUE) {
        ESP_LOGW(TAG, "Stills: the video sink is busy");
        return ESP_ERR_TIMEOUT;
    }

    /* When the uplink already owns the sink, borrow its stream instead of
     * enabling and disabling underneath it — a still is just the next frame
     * that was going to be sent anyway. */
    const bool borrowed = s_capture.stream_running;
    esp_err_t ret = video_sink_ensure(VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS);
    if (ret != ESP_OK) {
        xSemaphoreGive(s_capture.video_lock);
        return ret;
    }
    if (!borrowed) {
        esp_capture_sink_enable(s_capture.video_sink,
                                ESP_CAPTURE_RUN_MODE_ALWAYS);
        esp_capture_start(s_capture.capture);
    }

    ret = ESP_ERR_TIMEOUT;
    uint32_t deadline = (uint32_t)(esp_timer_get_time() / 1000) +
                        STILL_TIMEOUT_MS;
    while ((uint32_t)(esp_timer_get_time() / 1000) < deadline) {
        esp_capture_stream_frame_t frame = {
            .stream_type = ESP_CAPTURE_STREAM_TYPE_VIDEO,
        };
        if (esp_capture_sink_acquire_frame(s_capture.video_sink, &frame, true) ==
            ESP_CAPTURE_ERR_OK) {
            uint8_t *copy = malloc(frame.size);
            if (copy != NULL) {
                memcpy(copy, frame.data, frame.size);
                *jpeg = copy;
                *len = frame.size;
                ret = ESP_OK;
            } else {
                ret = ESP_ERR_NO_MEM;
            }
            esp_capture_sink_release_frame(s_capture.video_sink, &frame);
            break;
        }
        media_lib_thread_sleep(30);
    }

    if (!borrowed) {
        esp_capture_sink_enable(s_capture.video_sink,
                                ESP_CAPTURE_RUN_MODE_DISABLE);
    }
    xSemaphoreGive(s_capture.video_lock);
    if (ret == ESP_ERR_TIMEOUT) {
        ESP_LOGW(TAG, "No JPEG frame within %d ms", STILL_TIMEOUT_MS);
    }
    return ret;
}
