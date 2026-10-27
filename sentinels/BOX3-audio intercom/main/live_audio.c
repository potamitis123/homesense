/*
 * live_audio.c  –  Opus encode + packet bundling for Live Audio Mode
 *
 * See live_audio.h for the wire format and the call-context rules.
 *
 * The Opus encoder comes from espressif/esp_audio_codec, which is the
 * esp-adf-libs audio codec library packaged as a managed component. It
 * is used through the direct esp_opus_enc_* API rather than the
 * registration table, so no Kconfig option has to be enabled: the
 * implementation lives in the component's prebuilt libesp_audio_codec.a
 * and is always linked.
 */

#include "live_audio.h"

#include <string.h>
#include <stdlib.h>
#include <time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"
#include "esp_heap_caps.h"

#include "esp_opus_enc.h"

#include "audio_pipeline.h"    /* g_device_id, g_timezone_offset_sec */
#include "audiosense_mqtt.h"

static const char *TAG = "live";

/* Throughput log cadence. */
#define STATS_LOG_INTERVAL_MS  5000u

/* ════════════════════════════════════════════════════════════════
 * Module state
 *
 * Single-instance and single-threaded by contract: every function
 * here runs on streamer_task, so no locking is required.
 * ════════════════════════════════════════════════════════════════ */
static void     *s_enc        = NULL;   /* Opus encoder handle        */
static bool      s_active     = false;

static int       s_in_size    = 0;      /* bytes of PCM per Opus frame */
static int       s_out_size   = 0;      /* max bytes per encoded frame */

/* Staging buffer: exactly one Opus frame of PCM, so the encoder always
 * sees a whole frame at a known alignment even though the pipeline
 * hands us 64 ms DMA frames that are not a multiple of 20 ms. */
static uint8_t  *s_stage      = NULL;
static int       s_stage_len  = 0;

static uint8_t  *s_enc_out    = NULL;   /* one encoded frame           */

/* Bundle under construction: header + LIVE_FRAMES_PER_PACKET frames. */
static uint8_t  *s_pkt        = NULL;
static int       s_pkt_cap    = 0;
static int       s_pkt_len    = 0;      /* bytes used, incl. 4 B header */
static int       s_pkt_frames = 0;

static uint32_t  s_seq        = 0;      /* packet sequence number      */

/* Counters, reset per session. */
static uint32_t  s_tot_packets = 0;
static uint32_t  s_tot_bytes   = 0;
static uint32_t  s_tot_frames  = 0;
static uint32_t  s_pub_fail    = 0;

static TickType_t s_stats_next = 0;

/* ════════════════════════════════════════════════════════════════
 * Internals
 * ════════════════════════════════════════════════════════════════ */

static void live_free_buffers(void)
{
    if (s_stage)   { heap_caps_free(s_stage);   s_stage   = NULL; }
    if (s_enc_out) { heap_caps_free(s_enc_out); s_enc_out = NULL; }
    if (s_pkt)     { heap_caps_free(s_pkt);     s_pkt     = NULL; }
    s_stage_len  = 0;
    s_pkt_len    = 0;
    s_pkt_cap    = 0;
    s_pkt_frames = 0;
}

/*
 * Publish the bundle currently under construction, if it holds at
 * least one frame, and reset it for the next one.
 *
 * A publish failure is counted and swallowed: dropping 100 ms of live
 * audio is preferable to tearing down the session, and the receiver
 * sees the loss as a jump in seq.
 */
static void pkt_flush(void)
{
    if (s_pkt_frames == 0) return;

    s_pkt[0] = (uint8_t)( s_seq        & 0xFF);
    s_pkt[1] = (uint8_t)((s_seq >>  8) & 0xFF);
    s_pkt[2] = (uint8_t)((s_seq >> 16) & 0xFF);
    s_pkt[3] = (uint8_t)((s_seq >> 24) & 0xFF);

    if (audiosense_mqtt_live_packet(s_pkt, s_pkt_len)) {
        s_tot_packets++;
        s_tot_bytes += (uint32_t)s_pkt_len;
    } else {
        s_pub_fail++;
    }

    s_seq++;
    s_pkt_len    = 4;
    s_pkt_frames = 0;
}

/* Append one encoded Opus frame to the bundle, flushing when full. */
static void pkt_append(const uint8_t *frame, int len)
{
    if (len <= 0 || len > 0xFFFF) {
        ESP_LOGW(TAG, "encoded frame length %d out of range – skipped", len);
        return;
    }

    /* Defensive: out_size is the encoder's own recommendation, so this
     * should never trip, but a bundle overrun would corrupt the heap. */
    if (s_pkt_len + 2 + len > s_pkt_cap) {
        pkt_flush();
        if (s_pkt_len + 2 + len > s_pkt_cap) {
            ESP_LOGE(TAG, "frame %d B exceeds packet capacity %d B – dropped",
                     len, s_pkt_cap);
            return;
        }
    }

    s_pkt[s_pkt_len++] = (uint8_t)( (unsigned)len       & 0xFF);
    s_pkt[s_pkt_len++] = (uint8_t)(((unsigned)len >> 8) & 0xFF);
    memcpy(s_pkt + s_pkt_len, frame, (size_t)len);
    s_pkt_len += len;
    s_pkt_frames++;

    if (s_pkt_frames >= LIVE_FRAMES_PER_PACKET) pkt_flush();
}

/* Encode the full frame sitting in s_stage. */
static void encode_staged_frame(void)
{
    esp_audio_enc_in_frame_t in = {
        .buffer = s_stage,
        .len    = (uint32_t)s_in_size,
    };
    esp_audio_enc_out_frame_t out = {
        .buffer        = s_enc_out,
        .len           = (uint32_t)s_out_size,
        .encoded_bytes = 0,
    };

    esp_audio_err_t err = esp_opus_enc_process(s_enc, &in, &out);
    if (err != ESP_AUDIO_ERR_OK) {
        ESP_LOGW(TAG, "opus encode failed (%d)", (int)err);
        return;
    }

    s_tot_frames++;
    pkt_append(s_enc_out, (int)out.encoded_bytes);
}

/* ════════════════════════════════════════════════════════════════
 * Public API
 * ════════════════════════════════════════════════════════════════ */

esp_err_t live_audio_begin(int sample_rate, int bits_per_sample, int channels)
{
    if (s_active) {
        ESP_LOGW(TAG, "already active");
        return ESP_ERR_INVALID_STATE;
    }

    /*
     * Opus accepts only 16-bit input and a fixed set of sample rates.
     * The Sentinel runs 16 kHz / 16-bit mono, which is squarely inside
     * that, but settings.txt can change both — so refuse clearly here
     * rather than let the encoder fail obscurely on the first frame.
     */
    if (bits_per_sample != 16) {
        ESP_LOGE(TAG, "Opus needs 16-bit PCM, settings give %d – live mode "
                      "unavailable", bits_per_sample);
        return ESP_ERR_INVALID_ARG;
    }
    if (sample_rate != 8000  && sample_rate != 12000 &&
        sample_rate != 16000 && sample_rate != 24000 &&
        sample_rate != 48000) {
        ESP_LOGE(TAG, "Opus cannot encode %d Hz (needs 8/12/16/24/48 kHz) – "
                      "live mode unavailable", sample_rate);
        return ESP_ERR_INVALID_ARG;
    }
    if (channels != 1 && channels != 2) {
        ESP_LOGE(TAG, "Opus needs 1 or 2 channels, got %d", channels);
        return ESP_ERR_INVALID_ARG;
    }

    esp_opus_enc_config_t cfg = ESP_OPUS_ENC_CONFIG_DEFAULT();
    cfg.sample_rate      = sample_rate;
    cfg.channel          = channels;
    cfg.bits_per_sample  = bits_per_sample;
    cfg.bitrate          = LIVE_OPUS_BITRATE;
    cfg.frame_duration   = ESP_OPUS_ENC_FRAME_DURATION_20_MS;
    cfg.application_mode = ESP_OPUS_ENC_APPLICATION_VOIP;
    cfg.complexity       = LIVE_OPUS_COMPLEXITY;
    cfg.enable_fec       = false;
    cfg.enable_dtx       = false;
    cfg.enable_vbr       = true;

    esp_audio_err_t err = esp_opus_enc_open(&cfg, sizeof cfg, &s_enc);
    if (err != ESP_AUDIO_ERR_OK || !s_enc) {
        ESP_LOGE(TAG, "esp_opus_enc_open failed (%d)", (int)err);
        s_enc = NULL;
        return ESP_FAIL;
    }

    if (esp_opus_enc_get_frame_size(s_enc, &s_in_size, &s_out_size)
            != ESP_AUDIO_ERR_OK
        || s_in_size <= 0 || s_out_size <= 0) {
        ESP_LOGE(TAG, "esp_opus_enc_get_frame_size failed");
        esp_opus_enc_close(s_enc);
        s_enc = NULL;
        return ESP_FAIL;
    }

    /*
     * Staging and encoder-output buffers are small and touched 50×/s;
     * keep them in internal RAM. The bundle is larger and written once
     * per 100 ms, so PSRAM is fine for it.
     */
    s_stage   = heap_caps_malloc((size_t)s_in_size,
                                 MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    s_enc_out = heap_caps_malloc((size_t)s_out_size,
                                 MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);

    s_pkt_cap = 4 + LIVE_FRAMES_PER_PACKET * (2 + s_out_size);
    s_pkt     = heap_caps_malloc((size_t)s_pkt_cap,
                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_pkt) s_pkt = malloc((size_t)s_pkt_cap);   /* PSRAM optional */

    if (!s_stage || !s_enc_out || !s_pkt) {
        ESP_LOGE(TAG, "buffer alloc failed (stage=%d enc_out=%d pkt=%d)",
                 s_in_size, s_out_size, s_pkt_cap);
        live_free_buffers();
        esp_opus_enc_close(s_enc);
        s_enc = NULL;
        return ESP_ERR_NO_MEM;
    }

    s_stage_len   = 0;
    s_pkt_len     = 4;
    s_pkt_frames  = 0;
    s_seq         = 0;
    s_tot_packets = 0;
    s_tot_bytes   = 0;
    s_tot_frames  = 0;
    s_pub_fail    = 0;
    s_stats_next  = xTaskGetTickCount() + pdMS_TO_TICKS(STATS_LOG_INTERVAL_MS);

    /* Timestamp is generated at announce time, matching on_stream_start. */
    time_t  now;
    struct  tm ti;
    time(&now);
    now += g_timezone_offset_sec;
    localtime_r(&now, &ti);
    char ts[32];
    strftime(ts, sizeof ts, "%Y%m%d_%H%M%S", &ti);

    if (!audiosense_mqtt_live_start(g_device_id, ts,
                                    sample_rate, channels,
                                    LIVE_OPUS_BITRATE,
                                    LIVE_OPUS_FRAME_MS,
                                    LIVE_FRAMES_PER_PACKET)) {
        ESP_LOGE(TAG, "live/info publish failed – aborting live mode");
        live_free_buffers();
        esp_opus_enc_close(s_enc);
        s_enc = NULL;
        return ESP_FAIL;
    }

    s_active = true;

    ESP_LOGI(TAG,
        "begin  %dHz ch%d  opus %d bps  frame=%dms (in=%dB out<=%dB)  "
        "bundle=%d frames (%dms, cap %dB)",
        sample_rate, channels, LIVE_OPUS_BITRATE, LIVE_OPUS_FRAME_MS,
        s_in_size, s_out_size,
        LIVE_FRAMES_PER_PACKET,
        LIVE_FRAMES_PER_PACKET * LIVE_OPUS_FRAME_MS,
        s_pkt_cap);
    return ESP_OK;
}

esp_err_t live_audio_feed(const int16_t *pcm, int samples)
{
    if (!s_active) return ESP_ERR_INVALID_STATE;
    if (!pcm || samples <= 0) return ESP_ERR_INVALID_ARG;

    const uint8_t *src   = (const uint8_t *)pcm;
    int            avail = samples * (int)sizeof(int16_t);

    while (avail > 0) {
        int need = s_in_size - s_stage_len;
        int take = (avail < need) ? avail : need;

        memcpy(s_stage + s_stage_len, src, (size_t)take);
        s_stage_len += take;
        src         += take;
        avail       -= take;

        if (s_stage_len == s_in_size) {
            encode_staged_frame();
            s_stage_len = 0;
        }
    }

    if (xTaskGetTickCount() >= s_stats_next) {
        s_stats_next += pdMS_TO_TICKS(STATS_LOG_INTERVAL_MS);

        /* Audio seconds encoded so far, from the frame count rather than
         * wall clock, so the rate reflects the stream and not scheduling. */
        uint32_t audio_ms = s_tot_frames * (uint32_t)LIVE_OPUS_FRAME_MS;
        unsigned bps      = audio_ms ? (unsigned)((uint64_t)s_tot_bytes
                                                  * 1000u / audio_ms) : 0u;

        ESP_LOGI(TAG,
            "stats  frames=%u  packets=%u  bytes=%u  pub_fail=%u  "
            "audio=%u.%us  ~%u B/s",
            (unsigned)s_tot_frames, (unsigned)s_tot_packets,
            (unsigned)s_tot_bytes,  (unsigned)s_pub_fail,
            (unsigned)(audio_ms / 1000u), (unsigned)((audio_ms % 1000u) / 100u),
            bps);
    }

    return ESP_OK;
}

void live_audio_end(void)
{
    if (!s_active) {
        /* begin() failed or was never called – still release anything
         * a partial begin() may have left behind. */
        live_free_buffers();
        if (s_enc) { esp_opus_enc_close(s_enc); s_enc = NULL; }
        return;
    }

    /*
     * Any samples still in the staging buffer are less than one Opus
     * frame, so they cannot be encoded and are discarded: at most 19 ms
     * of audio at the very end of a session.
     */
    if (s_stage_len > 0) {
        ESP_LOGD(TAG, "discarding %d B partial frame at stop", s_stage_len);
        s_stage_len = 0;
    }

    pkt_flush();

    audiosense_mqtt_live_end(s_tot_packets, s_tot_bytes);

    ESP_LOGI(TAG,
        "end  frames=%u  packets=%u  bytes=%u  pub_fail=%u",
        (unsigned)s_tot_frames, (unsigned)s_tot_packets,
        (unsigned)s_tot_bytes,  (unsigned)s_pub_fail);

    live_free_buffers();
    esp_opus_enc_close(s_enc);
    s_enc    = NULL;
    s_active = false;
}

bool live_audio_is_active(void)
{
    return s_active;
}
