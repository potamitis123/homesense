/* ESP32-P4 sentinel + intercom.
 *
 * One image with two applications in it. The device listens: RMS-triggered
 * utterances go up over MQTT, broadcasts come back out of the loudspeaker,
 * push-to-talk rides the same MQTT connection, and the OV5647 posts a
 * low-rate JPEG stream to the dashboard. When a video call arrives it stops
 * listening, takes the call, and afterwards goes back to listening.
 *
 * Only one of the two ever owns the codecs, the panel and the camera. That
 * is a hardware constraint, not a policy — see mode_arbiter.h.
 *
 * This file is only the boot order, and the order is the interesting part:
 *
 *   1. debug log first, so board and media bring-up are captured
 *   2. SD card and settings, because everything below is configured from it
 *   3. the sentinel: display, codecs, self-test, WiFi, MQTT, pipeline. It
 *      owns the hardware other subsystems then borrow (sentinel_app.h)
 *   4. the intercom's media graphs, which reserve esp_webrtc's capture sink
 *      before anything can start the graph (media_sys.c) — get this after
 *      the uplink and calls come up with no microphone
 *   5. the camera uplink, which is what starts the graph
 *   6. signaling, once the network is up
 */

#include <stdio.h>
#include <string.h>

#include "argtable3/argtable3.h"
#include "bsp/esp-bsp.h"
#include "common.h"
#include "config.h"
#include "debug_log.h"
#include "esp_capture.h"
#include "esp_console.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "media_lib_adapter.h"
#include "media_lib_os.h"
#include "mode_arbiter.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "sentinel_app.h"
#include "video_stream.h"

static const char *TAG = "P4APP";

/* How much internal DMA RAM is actually free.
 *
 * Hosted's mempool was pushed into PSRAM because internal RAM "was full"
 * after the merge — that was never measured. If int-dma largest is big
 * enough for the mempool (~60 kB at RX_Q/TX_Q = 20) plus the streaming
 * double buffer, MEMPOOL_PREFER_SPIRAM can go back to n.
 *
 * Printed every other poll, so ~4 s. */
static void report_memory(void)
{
    static int divider;

    if (++divider < 2) {
        return;
    }
    divider = 0;
    ESP_LOGI(TAG,
             "heap: int-dma free=%u largest=%u | internal free=%u | psram free=%u",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

/* Same CA the broker connection is verified against; the uplink posts to
 * the home server over TLS with it. */
extern const char ca_crt_start[] asm("_binary_ca_crt_start");

/* ── Thread placement ───────────────────────────────────────────────
 *
 * The media components ask the application where their threads should
 * live. The defaults assume more internal RAM than this board has left
 * after the DSI framebuffers, so stacks go to PSRAM.
 *
 * Core 1 already carries the sentinel's capture_task at priority 18, which
 * must not be starved: a late DMA read is a lost frame of audio. pc_task
 * is the intercom's equivalent and is pinned there too — the two never run
 * at the same time, because the modes are exclusive.
 */
static void thread_scheduler(const char *name, media_lib_thread_cfg_t *cfg)
{
    if (strcmp(name, "pc_task") == 0) {
        /* ICE, DTLS and SRTP all unwind on this stack. */
        cfg->stack_size = 25 * 1024;
        cfg->priority = 18;
        cfg->core_id = 1;
    } else if (strcmp(name, "venc_0") == 0) {
        cfg->priority = 10;
    } else if (strcmp(name, "AUD_SRC") == 0) {
        /* Starves easily; a late capture buffer is an audible gap. */
        cfg->priority = 15;
    } else if (strcmp(name, "start") == 0) {
        cfg->stack_size = 6 * 1024;
    }
}

static void capture_scheduler(const char *name,
                              esp_capture_thread_schedule_cfg_t *cfg)
{
    media_lib_thread_cfg_t tmp = {
        .stack_size = cfg->stack_size,
        .priority = cfg->priority,
        .core_id = cfg->core_id,
    };
    thread_scheduler(name, &tmp);
    cfg->stack_size = tmp.stack_size;
    cfg->priority = tmp.priority;
    cfg->core_id = tmp.core_id;
    cfg->stack_in_ext = true;
}

/* ── Console ────────────────────────────────────────────────────────
 *
 * Diagnostics only. The unit needs no operator input to listen or to take
 * a call.
 */

static int cmd_status(int argc, char **argv)
{
    static const char *call_names[] = { "idle", "announcing", "connecting",
                                        "connected" };
    const app_config_t *cfg = app_config_get();
    printf("mode    : %s\n", mode_arbiter_name(mode_arbiter_get()));
    printf("sentinel: %s\n",
           sentinel_app_is_suspended() ? "suspended" : "listening");
    printf("network : %s\n", network_is_connected() ? "up" : "down");
    printf("call    : %s\n", call_names[call_get_state()]);
    printf("device  : %s\n", cfg->device_id);
    printf("broker  : %s\n", cfg->mqtt_uri);
    return 0;
}

static int cmd_hangup(int argc, char **argv)
{
    call_hangup();
    return 0;
}

static int cmd_video(int argc, char **argv)
{
    const char *action = argc > 1 ? argv[1] : "status";
    if (strcmp(action, "off") == 0) {
        video_stream_stop();
        return 0;
    }
    if (strcmp(action, "status") != 0) {
        printf("usage: video [status|off]\n");
        return 1;
    }
    const app_config_t *cfg = app_config_get();
    video_stream_stats_t stats;
    video_stream_get_stats(&stats);
    printf("uplink  : %s\n", stats.running ? "running" : "stopped");
    printf("server  : %s (last HTTP %d)\n", stats.online ? "up" : "down",
           stats.last_status);
    printf("url     : %s/%s\n", cfg->video_stream_url, cfg->device_id);
    printf("frames  : %u sent, %u failed\n", (unsigned)stats.frames_sent,
           (unsigned)stats.frames_failed);
    return 0;
}

static int cmd_log(int argc, char **argv)
{
    const char *action = argc > 1 ? argv[1] : "status";
    if (strcmp(action, "flush") == 0) {
        debug_log_flush();
        printf("flushed\n");
        return 0;
    }
    debug_log_stats_t stats;
    debug_log_get_stats(&stats);
    printf("capture : %s%s\n", stats.capturing ? "on" : "off",
           stats.sync ? " (sync per line)" : "");
    printf("file    : %s\n", stats.writing ? stats.path : "(not writing)");
    printf("lines   : %u captured, %u dropped\n", (unsigned)stats.lines,
           (unsigned)stats.dropped);
    return 0;
}

static void console_init(void)
{
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    repl_config.prompt = "p4>";
    repl_config.task_stack_size = 8 * 1024;
    repl_config.max_cmdline_length = 512;

#if CONFIG_ESP_CONSOLE_UART
    esp_console_dev_uart_config_t uart_config =
        ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    if (esp_console_new_repl_uart(&uart_config, &repl_config, &repl) != ESP_OK) {
        return;
    }
#elif CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    esp_console_dev_usb_serial_jtag_config_t jtag_config =
        ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    if (esp_console_new_repl_usb_serial_jtag(&jtag_config, &repl_config,
                                             &repl) != ESP_OK) {
        return;
    }
#else
    ESP_LOGW(TAG, "No console device configured");
    return;
#endif

    const esp_console_cmd_t cmds[] = {
        { .command = "status", .help = "Mode, network and call state\n",
          .func = cmd_status },
        { .command = "hangup", .help = "End the current call\n",
          .func = cmd_hangup },
        { .command = "video", .help = "Camera uplink: video [status|off]\n",
          .func = cmd_video },
        { .command = "log", .help = "SD card debug log: log [status|flush]\n",
          .func = cmd_log },
    };
    for (int i = 0; i < (int)(sizeof(cmds) / sizeof(cmds[0])); i++) {
        esp_console_cmd_register(&cmds[i]);
    }
    esp_console_start_repl(repl);
}

/* ── Startup ────────────────────────────────────────────────────────*/

static bool s_call_service_up;

static void on_network_change(bool connected)
{
    if (!connected) {
        ESP_LOGW(TAG, "Network down, dropping signaling");
        call_service_stop();
        s_call_service_up = false;
    }
}

static void start_video_uplink(const app_config_t *cfg)
{
    if (!cfg->video_stream_enabled) {
        ESP_LOGI(TAG, "Camera uplink off (ov5647_video_enabled=0)");
        return;
    }
    video_stream_cfg_t vs_cfg = {
        .url = cfg->video_stream_url,
        .device_id = cfg->device_id,
        .ca_cert = ca_crt_start,
        .width = cfg->video_stream_width,
        .height = cfg->video_stream_height,
        .fps = cfg->video_stream_fps,
    };
    if (video_stream_start(&vs_cfg) != ESP_OK) {
        ESP_LOGE(TAG, "Camera uplink did not start");
    }
}

void app_main(void)
{
    /* Before anything logs: the ring buffers into RAM until settings.txt
     * has been read and says whether to keep it. */
    debug_log_begin();

    esp_log_level_set("*", ESP_LOG_INFO);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* The media components allocate through this adapter; it has to be in
     * place before anything else touches them. */
    media_lib_add_default_adapter();
    media_lib_thread_set_schedule_cb(thread_scheduler);
    esp_capture_set_thread_scheduler(capture_scheduler);

    /* ── Settings ────────────────────────────────────────────────────
     *
     * Two parsers over one file. config.c is the sentinel's generic
     * key/value store; app_config.c is the intercom's typed struct. Both
     * tolerate keys they do not recognise, so each simply ignores the
     * other's half of settings.txt. */
    if (bsp_sdcard_mount() != ESP_OK) {
        ESP_LOGW(TAG, "No SD card; running on compiled-in defaults");
    } else {
        config_load(BSP_SD_MOUNT_POINT "/settings.txt");
    }
    app_config_load();
    const app_config_t *cfg = app_config_get();

    if (cfg->debug_log_enabled) {
        debug_log_start_file(cfg->debug_log_path, cfg->debug_log_sync);
    } else {
        debug_log_abort();
    }

    /* ── The sentinel ────────────────────────────────────────────────
     *
     * Brings up the display, both codecs, WiFi and its MQTT client, and
     * starts listening. Everything below borrows that hardware. */
    if (sentinel_app_init() != ESP_OK) {
        /* Not fatal: a unit that cannot listen should still answer the
         * door. The intercom needs the codecs, which sentinel_app_init()
         * creates before anything that can fail. */
        ESP_LOGE(TAG, "Sentinel failed to start; continuing as intercom only");
    }

    /* ── The intercom ────────────────────────────────────────────────*/
    if (board_init() != ESP_OK) {
        ESP_LOGE(TAG, "Board adapter unavailable; calls will not work");
    }
    if (board_probe_camera() != ESP_OK) {
        ESP_LOGW(TAG, "Camera self-test failed; calls will be audio-only");
    }
    /* Reserves esp_webrtc's capture sink. Must happen before the uplink
     * starts the graph — see media_sys.c, reserve_call_sink(). */
    if (media_sys_buildup() != ESP_OK) {
        ESP_LOGE(TAG, "Media graphs failed; calls will not work");
    }

    if (mode_arbiter_init() != ESP_OK) {
        ESP_LOGE(TAG, "Mode arbiter unavailable; halting is safer than "
                      "two subsystems fighting over the codecs");
        return;
    }
    mode_arbiter_enter_sentinel();

    /* Starts the capture graph, so it follows the sink reservation. */
    start_video_uplink(cfg);

    network_init(cfg->wifi_ssid, cfg->wifi_pass, on_network_change);
    console_init();

    /* Signaling is brought up from here rather than a network callback so
     * that it retries: a broker not answering at boot should not leave the
     * unit permanently unable to take a call. */
    while (1) {
        network_poll_events();

        if (network_is_connected() && !s_call_service_up) {
            if (call_service_start(cfg->device_id) == ESP_OK) {
                s_call_service_up = true;
                ESP_LOGI(TAG, "Ready: listening, and waiting for calls in "
                              "room %s", cfg->device_id);
            }
        }
        if (s_call_service_up) {
            call_service_poll();
        }
        report_memory();
        media_lib_thread_sleep(2000);
    }
}
