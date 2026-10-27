/*
 * audio_pipeline.c  –  Decoupled producer/consumer PCM pipeline
 *                       + PSRAM pre-roll buffer for onset recovery
 *
 * Replaces both audio_sad.c (capture / SAD) and audio_stream.c
 * (record task + streaming task).
 *
 * Task layout
 * ───────────
 *  capture_task   Core 1, prio 18  –  DMA read, SAD, ring push
 *  streamer_task  Core 0, prio  4  –  ring drain, MQTT publish
 *
 * Priority rationale
 * ──────────────────
 *  WiFi / LwIP      23  (PRO_CPU)
 *  capture_task     18  (APP_CPU)  must never miss a DMA frame;
 *                                  above MQTT/network, below WiFi
 *  MQTT client       5  (PRO_CPU)
 *  streamer_task     4  (APP_CPU)  allowed to be slow; below MQTT
 *                                  so ACK processing is not delayed
 *
 * Pre-roll
 * ────────
 *  A PSRAM circular buffer of PREROLL_FRAMES DMA frames rotates
 *  continuously while the capture task is in CAP_IDLE and during
 *  run_cooldown().  When SAD fires the trigger frame is appended to
 *  the pre-roll and then the whole pre-roll is flushed into the ring
 *  as CHUNK items ahead of the live recording.  The streamer sees
 *  them as ordinary CHUNKs; no other layer needs to change.
 *
 *  At defaults (64 ms / frame):
 *    PREROLL_FRAMES = 20  →  up to ~1.28 s of onset recovery
 *    PSRAM cost: 20 × 1024 samples × 2 B = 40 960 B
 *
 * Ring-buffer sizing
 * ──────────────────
 *  one_utterance = (max_frames + PREROLL_FRAMES) × chunk_item
 *                  + 2 × marker_item + 512 slop
 *  ring          = 2 × one_utterance  (PSRAM via CONFIG_SPIRAM_USE_MALLOC)
 *
 *  At defaults: one_utt ≈ 1 015 KB  →  ring ≈ 2.03 MiB
 */

#include "audio_pipeline.h"

#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "freertos/idf_additions.h"   /* xTaskCreatePinnedToCoreWithCaps */

#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_codec_dev.h"

#include "live_audio.h"

static const char *TAG = "pipeline";

/* ════════════════════════════════════════════════════════════════
 * Live Audio Mode request flag
 *
 * Written by audio_pipeline_set_live() from the MQTT task, read by
 * capture_task at frame boundaries. A plain volatile bool is
 * sufficient: it is a single word, there is exactly one writer, and
 * the reader only needs to observe the change eventually — not
 * atomically with respect to anything else.
 *
 * s_live_on is the acknowledged state (capture_task has actually
 * entered the mode), kept separate so audio_pipeline_is_live() reports
 * reality rather than intent.
 * ════════════════════════════════════════════════════════════════ */
static volatile bool s_live_req = false;
static volatile bool s_live_on  = false;

/* ── Task parameters ─────────────────────────────────────────── */
#define CAPTURE_TASK_PRIO      18
#define STREAMER_TASK_PRIO      4

/* PSRAM stacks: free ~16 KB of internal RAM for AES / mbedTLS.   */
#define CAPTURE_STACK_BYTES   8192u
/*
 * streamer_task now also opens/drives the Opus encoder for Live Audio
 * Mode (live_audio_begin/feed, esp_opus_enc_open/esp_opus_enc_process in
 * live_audio.c) on top of everything it already did (ring drain, JSON
 * formatting, MQTT publish). 8192 B was sized for the PCM-only path and
 * was not safe for the encoder call chain on top of it: entering Live
 * Audio Mode crashed the device outright with a stack overflow inside
 * live_audio_begin(). Doubling to 16384 B fixed *that* crash (encoder
 * opens, live/info + live:true publish correctly) but str_task still
 * overflows and reboots ~13s into continuous encode+publish, confirmed
 * live via serial monitor:
 *
 *   ***ERROR*** A stack overflow in task str_task has been detected.
 *   Backtrace: ... vApplicationStackOverflowHook ...
 *
 * So whatever deeper call path the ongoing encode/publish loop takes
 * (worst-case branch in the Opus encoder, or an mbedTLS/esp-mqtt publish
 * path not exercised by encoder-open or by the plain PCM stream/chunk
 * path) uses more than 16 KB total. Rather than tune this byte-by-byte
 * through repeated flash cycles, size it generously: stacks are
 * PSRAM-backed (TASK_MEM_CAPS below) and this device has several MB of
 * spare PSRAM, so 32 KB here is still noise against that budget.
 */
#define STREAMER_STACK_BYTES  32768u
#define TASK_MEM_CAPS         (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)

/* ── Aggregation ─────────────────────────────────────────────── */
#define AGGREGATE_FRAMES      4u

/* ── Ring-buffer internal overhead per item (NOSPLIT) ────────── */
#define RING_ITEM_OVERHEAD    8u

/* ── Status-label update throttle for capture_task ──────────── */
#define STATUS_THROTTLE_MS    250u

/* ── HWM log interval ────────────────────────────────────────── */
#define HWM_LOG_INTERVAL_MS   10000u

/* ── Backoff for streamer publish failures ───────────────────── */
#define BACKOFF_INITIAL_MS    20u
#define BACKOFF_MAX_MS        200u

/* ── Pre-roll size ───────────────────────────────────────────── *
 * 20 frames × ~64 ms/frame = ~1.28 s of onset recovery.         *
 * PSRAM cost: 20 × 1024 × 2 B = 40 960 B — generous but cheap.  *
 * Raise further if long pauses precede speech onset.             */
#define PREROLL_FRAMES        20u

/* ════════════════════════════════════════════════════════════════
 * Module state
 * ════════════════════════════════════════════════════════════════ */
typedef struct {
    audio_pipeline_ctx_t  cfg;

    RingbufHandle_t       ring;
    size_t                ring_total_bytes;
    size_t                drop_threshold_bytes;

    int                   frame_bytes;
    int                   frame_ms;

    volatile bool         busy;
    uint32_t              utterance_idx;

    size_t                ring_hwm_min_free;

    TaskHandle_t          capture_handle;
    TaskHandle_t          streamer_handle;
} pipeline_t;

static pipeline_t *s_pipe = NULL;

/* ════════════════════════════════════════════════════════════════
 * Internal helpers
 * ════════════════════════════════════════════════════════════════ */

static int16_t calc_rms(const int16_t *s, int n)
{
    if (n <= 0) return 0;
    int64_t ss = 0;
    for (int i = 0; i < n; i++) ss += (int64_t)s[i] * s[i];
    return (int16_t)sqrtf((float)ss / (float)n);
}

/* ════════════════════════════════════════════════════════════════
 * Adaptive noise floor
 * ════════════════════════════════════════════════════════════════
 * A simplified, self-tuning noise-floor tracker: every CAP_IDLE
 * frame whose RMS is at or below the current threshold is treated
 * as ambient noise (not speech) and folded into a slow exponential
 * moving average. g_rms_threshold is then re-derived as a fixed
 * multiple of that average, so it rises in a loud room and falls
 * back down in a quiet one instead of staying pinned at whatever
 * value settings.txt happened to specify at boot.
 *
 * s_noise_alpha = 0.001 → time constant ≈ 1/alpha frames ≈ 700
 * frames ≈ 45 s at the ~64 ms/frame default, so short transients
 * (a cough, a door) barely move it, only the sustained background
 * level does. Called only from capture_task, so plain statics
 * (no locking) are fine.
 * ──────────────────────────────────────────────────────────────── */
static float s_noise_ema    = 300.0f;   /* initial guess              */
static float s_noise_alpha  = 0.001f;   /* ~700 frames = ~45 s at 64ms/frame */
#define NOISE_TRIGGER_RATIO  2.5f       /* threshold = noise_ema × this      */
#define NOISE_FLOOR_MIN      150.0f     /* never below mic self-noise        */
#define NOISE_FLOOR_MAX      800.0f     /* don't adapt to a loud room as quiet */
#define NOISE_HYSTERESIS     30         /* only move g_rms_threshold if the  */
                                         /* recomputed value differs by more  */
                                         /* than this, to avoid jitter        */

static void noise_ema_update(float rms_sample)
{
    s_noise_ema = s_noise_alpha * rms_sample
                + (1.0f - s_noise_alpha) * s_noise_ema;
    s_noise_ema = fmaxf(s_noise_ema, NOISE_FLOOR_MIN);
    s_noise_ema = fminf(s_noise_ema, NOISE_FLOOR_MAX);

    int new_thr = (int)(s_noise_ema * NOISE_TRIGGER_RATIO);
    if (abs(new_thr - g_rms_threshold) > NOISE_HYSTERESIS) {
        g_rms_threshold = new_thr;
    }
}

/*
 * Push a header-only item (START / END / ABORT) to the ring.
 * Non-blocking (timeout = 0): called only from capture_task.
 */
static bool ring_push_marker(pipeline_t          *p,
                              pipeline_msg_type_t  type,
                              uint32_t             utt_idx)
{
    pipeline_msg_hdr_t hdr = {
        .type          = type,
        .utterance_idx = utt_idx,
        .payload_len   = 0,
        ._pad          = 0,
    };
    return xRingbufferSend(p->ring, &hdr, sizeof hdr, 0) == pdTRUE;
}

/*
 * Push a marker, retrying briefly if the ring is momentarily full.
 *
 * Used only for the Live Audio Mode session markers. A dropped
 * LIVE_START or LIVE_END would desynchronise the streamer's state
 * machine from the capture task's for the rest of the session, which is
 * a far worse outcome than the few hundred milliseconds of latency this
 * costs. Ordinary CHUNK loss is recoverable and stays non-blocking.
 */
static bool ring_push_marker_retry(pipeline_t          *p,
                                    pipeline_msg_type_t  type,
                                    uint32_t             utt_idx,
                                    int                  attempts)
{
    for (int i = 0; i < attempts; i++) {
        if (ring_push_marker(p, type, utt_idx)) return true;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    return false;
}

/*
 * Push a CHUNK item via the zero-copy acquire/complete API.
 * RINGBUF_TYPE_NOSPLIT only.  Non-blocking (timeout = 0).
 */
static bool ring_push_chunk_typed(pipeline_t          *p,
                                   pipeline_msg_type_t  type,
                                   uint32_t             utt_idx,
                                   const int16_t       *pcm,
                                   uint16_t             pcm_bytes)
{
    const size_t item_size =
        sizeof(pipeline_msg_hdr_t) + (size_t)pcm_bytes;
    void *slot = NULL;

    if (xRingbufferSendAcquire(p->ring, &slot, item_size, 0) != pdTRUE)
        return false;

    pipeline_msg_hdr_t *hdr = (pipeline_msg_hdr_t *)slot;
    hdr->type          = type;
    hdr->utterance_idx = utt_idx;
    hdr->payload_len   = pcm_bytes;
    hdr->_pad          = 0;
    memcpy((uint8_t *)slot + sizeof(pipeline_msg_hdr_t), pcm, pcm_bytes);

    xRingbufferSendComplete(p->ring, slot);
    return true;
}

/* Event Mode chunk – the overwhelmingly common case. */
static inline bool ring_push_chunk(pipeline_t    *p,
                                    uint32_t       utt_idx,
                                    const int16_t *pcm,
                                    uint16_t       pcm_bytes)
{
    return ring_push_chunk_typed(p, PIPELINE_MSG_CHUNK,
                                 utt_idx, pcm, pcm_bytes);
}

/* ════════════════════════════════════════════════════════════════
 * Pre-roll circular buffer
 * Allocated in PSRAM; written by capture_task only (no locking).
 *
 * IMPORTANT: ring_push_marker and ring_push_chunk must be defined
 * above this section so that preroll_flush can call ring_push_chunk
 * without a forward declaration.
 * ════════════════════════════════════════════════════════════════ */
typedef struct {
    int16_t  *buf;           /* PSRAM: PREROLL_FRAMES × frame_samples */
    uint32_t  head;          /* next write slot (0 … PREROLL_FRAMES-1) */
    uint32_t  count;         /* valid frames stored (0 … PREROLL_FRAMES) */
    int       frame_samples; /* = g_record_buffer_frames                 */
} preroll_t;

/*
 * Append one DMA frame to the circular pre-roll.
 * Overwrites the oldest frame when the buffer is full.
 */
static inline void preroll_write(preroll_t *pr, const int16_t *frame)
{
    int16_t *slot = pr->buf + (size_t)pr->head * (size_t)pr->frame_samples;
    memcpy(slot, frame, (size_t)pr->frame_samples * sizeof(int16_t));
    pr->head = (pr->head + 1u) % PREROLL_FRAMES;
    if (pr->count < PREROLL_FRAMES) pr->count++;
}

/*
 * Flush the pre-roll (oldest → newest) into the ring as CHUNK items.
 * Must be called AFTER ring_push_marker(START) and only when
 * drop_threshold_bytes (which accounts for PREROLL_FRAMES extra chunks)
 * is satisfied — so ring_push_chunk cannot fail here.
 * Clears the buffer after flushing.
 */
static void preroll_flush(pipeline_t *p, preroll_t *pr, uint32_t utt_idx)
{
    if (!pr->buf || pr->count == 0) return;

    uint32_t oldest =
        (pr->head + PREROLL_FRAMES - pr->count) % PREROLL_FRAMES;
    uint16_t frame_bytes =
        (uint16_t)((size_t)pr->frame_samples * sizeof(int16_t));

    for (uint32_t i = 0; i < pr->count; i++) {
        uint32_t slot_idx = (oldest + i) % PREROLL_FRAMES;
        const int16_t *fp =
            pr->buf + (size_t)slot_idx * (size_t)pr->frame_samples;
        if (!ring_push_chunk(p, utt_idx, fp, frame_bytes)) {
            /*
             * Should be impossible: drop_threshold_bytes was sized to
             * include all PREROLL_FRAMES chunks.  Log and continue so
             * at least the later pre-roll and live frames are intact.
             */
            ESP_LOGE(TAG,
                "[cap] preroll_flush: ring_push_chunk failed "
                "utt=%" PRIu32 " preroll_frame=%" PRIu32 "/%" PRIu32,
                utt_idx, i, pr->count);
        }
    }

    pr->count = 0;
    pr->head  = 0;
}

/* ════════════════════════════════════════════════════════════════
 * run_cooldown
 *
 * Writes every frame it reads into the pre-roll buffer so that
 * audio which begins during cooldown is not silently discarded.
 * ════════════════════════════════════════════════════════════════ */
static void run_cooldown(pipeline_t *p, int16_t *dma_buf, preroll_t *pr)
{
    ESP_LOGI(TAG, "[cap] cooldown start");
    p->cfg.on_status("Cooldown...", 0x888888);

    int        q  = 0;
    TickType_t t0 = xTaskGetTickCount();
    TickType_t mx = pdMS_TO_TICKS((uint32_t)g_cooldown_drain_ms);

    while (q < g_cooldown_rms_checks) {
        if (esp_codec_dev_read(p->cfg.mic_dev, dma_buf,
                               (size_t)p->frame_bytes) == ESP_OK) {
            int16_t r = calc_rms(dma_buf, g_record_buffer_frames);
            p->cfg.on_rms(r);
            q = (r <= g_rms_threshold) ? q + 1 : 0;

            /* Preserve cooldown frames in case speech onset follows */
            if (pr->buf) preroll_write(pr, dma_buf);
        }
        if (xTaskGetTickCount() - t0 > mx) {
            ESP_LOGW(TAG, "[cap] cooldown timeout");
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    ESP_LOGI(TAG, "[cap] cooldown done (q=%d)", q);
    p->cfg.on_status("Idle", 0x4ecca3);
}

/* ════════════════════════════════════════════════════════════════
 * capture_task  –  SAD state machine + ring producer
 *
 * States
 * ──────
 *  CAP_IDLE
 *    Reads DMA, writes every frame to pre-roll, calls on_rms.
 *    On RMS trigger: appends trigger frame to pre-roll.
 *    If ring has headroom → push START → flush pre-roll as CHUNKs
 *    → CAP_RECORDING.
 *    If ring is full → CAP_RECORDING_DROPPED (clears pre-roll).
 *
 *  CAP_RECORDING
 *    Reads DMA, pushes CHUNK each frame.  Tracks silence and
 *    duration.  On end → push END → post-delay → cooldown → IDLE.
 *    On ring_push_chunk failure → push ABORT → IDLE.
 *
 *  CAP_RECORDING_DROPPED
 *    Same as CAP_RECORDING but no ring writes.  Tracks silence and
 *    duration only, then runs cooldown.
 *
 * Stack / TCB in PSRAM.  Self-deletes with vTaskDeleteWithCaps(NULL).
 * ════════════════════════════════════════════════════════════════ */
typedef enum {
    CAP_IDLE,
    CAP_RECORDING,
    CAP_RECORDING_DROPPED,
    CAP_LIVE,               /**< Live Audio Mode: no SAD, every frame sent */
} cap_state_t;

static void capture_task(void *arg)
{
    pipeline_t *p = (pipeline_t *)arg;

    /* ── Codec open ──────────────────────────────────────────── */
    if (!p->cfg.mic_dev) {
        ESP_LOGE(TAG, "[cap] no mic device");
        p->cfg.on_status("MIC ERROR", 0xff0000);
        vTaskDeleteWithCaps(NULL);
        return;
    }

    esp_codec_dev_sample_info_t fs = {
        .sample_rate     = (uint32_t)g_sample_rate,
        .channel         = 1,
        .bits_per_sample = (uint32_t)g_bits_per_sample,
    };
    if (esp_codec_dev_open(p->cfg.mic_dev, &fs) != ESP_OK) {
        ESP_LOGE(TAG, "[cap] codec open failed");
        p->cfg.on_status("MIC OPEN ERR", 0xff0000);
        vTaskDeleteWithCaps(NULL);
        return;
    }

    /* ── DMA staging buffer (internal DMA-capable RAM) ───────── */
    int16_t *dma_buf = heap_caps_malloc(
        (size_t)p->frame_bytes,
        MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!dma_buf) {
        ESP_LOGE(TAG, "[cap] DMA buffer alloc failed");
        p->cfg.on_status("OOM DMA", 0xff0000);
        vTaskDeleteWithCaps(NULL);
        return;
    }

    /* ── Pre-roll buffer (PSRAM) ─────────────────────────────── */
    const size_t preroll_bytes =
        (size_t)PREROLL_FRAMES
        * (size_t)g_record_buffer_frames
        * sizeof(int16_t);

    preroll_t pr = {
        .buf = heap_caps_malloc(preroll_bytes,
                                MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT),
        .head          = 0,
        .count         = 0,
        .frame_samples = g_record_buffer_frames,
    };

    if (!pr.buf) {
        /*
         * Non-fatal: onset recovery is simply disabled.
         * The guard (pr.buf != NULL) is checked before every
         * preroll_write / preroll_flush call.
         */
        ESP_LOGE(TAG,
            "[cap] pre-roll alloc failed (%u B) – operating without onset "
            "recovery (check PSRAM availability)",
            (unsigned)preroll_bytes);
    } else {
        ESP_LOGI(TAG,
            "[cap] pre-roll ready: %u frames × %u B = %u B in PSRAM "
            "(~%" PRIu32 " ms onset recovery)",
            PREROLL_FRAMES,
            (unsigned)((size_t)g_record_buffer_frames * sizeof(int16_t)),
            (unsigned)preroll_bytes,
            (uint32_t)PREROLL_FRAMES * (uint32_t)p->frame_ms);
    }

    /* ── Codec warm-up: discard initial frames ───────────────── */
    for (int d = 0; d < g_discard_samples; ) {
        if (esp_codec_dev_read(p->cfg.mic_dev, dma_buf,
                               (size_t)p->frame_bytes) == ESP_OK)
            d += g_record_buffer_frames;
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    ESP_LOGI(TAG, "[cap] warm-up done (%d samples discarded)",
             g_discard_samples);

    ESP_LOGI(TAG,
        "[cap] ready  frame=%dB/%dms  rms_thr=%d  "
        "ring_total=%u B  drop_thr=%u B  preroll=%u frames",
        p->frame_bytes, p->frame_ms,
        g_rms_threshold,
        (unsigned)p->ring_total_bytes,
        (unsigned)p->drop_threshold_bytes,
        PREROLL_FRAMES);
    p->cfg.on_status("Idle", 0x4ecca3);

    /* ── Loop variables ─────────────────────────────────────── */
    cap_state_t state       = CAP_IDLE;
    uint32_t    n_frames    = 0;
    uint32_t    silence_run = 0;
    uint32_t    utt_idx     = 0;

    int      log_div        = 0;
    int      play_div       = 0;
    uint32_t last_status_ms = 0;
    uint32_t live_drops     = 0;

    TickType_t hwm_log_next =
        xTaskGetTickCount() + pdMS_TO_TICKS(HWM_LOG_INTERVAL_MS);

    /* ══════════════════════ MAIN LOOP ══════════════════════════ */
    while (1) {

        /* ── Ring HWM log every HWM_LOG_INTERVAL_MS ─────────── */
        if (xTaskGetTickCount() >= hwm_log_next) {
            hwm_log_next += pdMS_TO_TICKS(HWM_LOG_INTERVAL_MS);
            size_t cur_free = xRingbufferGetCurFreeSize(p->ring);
            if (cur_free < p->ring_hwm_min_free)
                p->ring_hwm_min_free = cur_free;
            ESP_LOGI(TAG,
                "[cap] ring HWM  state=%d  cur_free=%u  "
                "min_ever=%u  peak_used=%u  total=%u  preroll_held=%u",
                (int)state,
                (unsigned)cur_free,
                (unsigned)p->ring_hwm_min_free,
                (unsigned)(p->ring_total_bytes - p->ring_hwm_min_free),
                (unsigned)p->ring_total_bytes,
                (unsigned)pr.count);
        }

        /* ══════════ Live Audio Mode transitions ════════════════
         *
         * Evaluated only from CAP_IDLE and CAP_LIVE. A start request
         * arriving mid-utterance is intentionally not acted on here:
         * CAP_RECORDING runs to its normal END and cooldown first, so
         * the utterance the server is already receiving is never
         * truncated. The request is still pending when the state
         * machine returns to CAP_IDLE, and is picked up then.
         * ──────────────────────────────────────────────────────── */
        if (state == CAP_IDLE && s_live_req) {
            utt_idx++;
            if (ring_push_marker_retry(p, PIPELINE_MSG_LIVE_START,
                                       utt_idx, 25)) {
                ESP_LOGI(TAG, "[cap] LIVE enter  session=%" PRIu32, utt_idx);
                p->cfg.on_status("LIVE", 0x00bfff);
                live_drops = 0;
                s_live_on  = true;
                state      = CAP_LIVE;
            } else {
                /* Ring completely full means the streamer is wedged.
                 * Leave the request pending and retry next iteration. */
                ESP_LOGE(TAG,
                    "[cap] LIVE_START push failed (ring full, streamer "
                    "stalled) – will retry");
            }
            continue;
        }

        if (state == CAP_LIVE && !s_live_req) {
            if (!ring_push_marker_retry(p, PIPELINE_MSG_LIVE_END,
                                        utt_idx, 25)) {
                ESP_LOGE(TAG,
                    "[cap] LIVE_END push failed – streamer may remain in "
                    "live state until the ring drains");
            }
            s_live_on = false;
            ESP_LOGI(TAG,
                "[cap] LIVE exit  session=%" PRIu32
                "  dropped_frames=%" PRIu32, utt_idx, live_drops);

            /*
             * The pre-roll still holds the frames captured just before
             * live mode began, which by now may be hours old. Clear it
             * so a trigger shortly after Event Mode resumes cannot
             * prepend stale audio to a fresh utterance; run_cooldown
             * then refills it with current audio, exactly as it does
             * after an ordinary utterance.
             */
            pr.count = 0;
            pr.head  = 0;

            state = CAP_IDLE;
            run_cooldown(p, dma_buf, &pr);
            continue;
        }

        /* ── Playback queue check (IDLE only, ~every 2 s) ────── */
        if (state == CAP_IDLE && ++play_div >= 40) {
            play_div = 0;
            if (p->cfg.check_play()) {
                run_cooldown(p, dma_buf, &pr);
                continue;
            }
        }

        /* ── DMA read ────────────────────────────────────────── */
        if (esp_codec_dev_read(p->cfg.mic_dev, dma_buf,
                               (size_t)p->frame_bytes) != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        int16_t rms = calc_rms(dma_buf, g_record_buffer_frames);
        p->cfg.on_rms(rms);

        /* ── Periodic RMS log (every 20 frames ≈ 1.28 s) ────── */
        if (++log_div >= 20) {
            log_div = 0;
            ESP_LOGI(TAG,
                "[cap] state=%d  RMS=%d (thr=%d, noise_ema=%.1f)  "
                "ring_free=%u  preroll=%u",
                (int)state, rms, g_rms_threshold, (double)s_noise_ema,
                (unsigned)xRingbufferGetCurFreeSize(p->ring),
                (unsigned)pr.count);
        }

        /* ════════════ STATE: LIVE ══════════════════════════════ *
         * No RMS trigger, no pre-roll, no noise-floor adaptation and
         * no playback-queue drain: every frame goes straight onto the
         * ring for streamer_task to Opus-encode.
         *
         * The push is non-blocking, as everywhere else in this task. If
         * the ring is full the streamer is behind a slow broker, and
         * for live audio the correct response is to drop the frame and
         * stay current rather than stall the DMA read. Event Mode's
         * admit-or-drop-whole-utterance logic does not apply here
         * because there is no utterance boundary to keep intact.
         * ──────────────────────────────────────────────────────── */
        if (state == CAP_LIVE) {
            if (!ring_push_chunk_typed(p, PIPELINE_MSG_LIVE_CHUNK,
                                       utt_idx, dma_buf,
                                       (uint16_t)p->frame_bytes)) {
                /* Log the first drop and then every 50th, so a sustained
                 * stall is visible without flooding the console. */
                if ((live_drops++ % 50u) == 0u)
                    ESP_LOGW(TAG,
                        "[cap] LIVE frame dropped (ring full) "
                        "session=%" PRIu32 "  drops=%" PRIu32,
                        utt_idx, live_drops);
            }
            continue;
        }

        /* ════════════ STATE: IDLE ══════════════════════════════ */
        if (state == CAP_IDLE) {
            if (rms <= g_rms_threshold) {
                /*
                 * Below threshold: this frame is ambient noise, not
                 * speech – fold it into the adaptive noise-floor EMA
                 * (may adjust g_rms_threshold for future frames).
                 */
                noise_ema_update((float)rms);

                /*
                 * Store frame in pre-roll so it is available if a
                 * trigger follows shortly.
                 */
                if (pr.buf) preroll_write(&pr, dma_buf);
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }

            /* ── SAD trigger ─────────────────────────────────── */
            if (!p->cfg.wifi_connected()) {
                ESP_LOGW(TAG, "[cap] trigger – no WiFi, skipping");
                p->cfg.on_status("No WiFi!", 0xff6b6b);
                if (pr.buf) preroll_write(&pr, dma_buf);
                vTaskDelay(pdMS_TO_TICKS(1500));
                run_cooldown(p, dma_buf, &pr);
                continue;
            }

            utt_idx++;
            size_t free = xRingbufferGetCurFreeSize(p->ring);
            n_frames    = 0;
            silence_run = 0;
            last_status_ms = 0;
            play_div    = 0;

            /*
             * Append the trigger frame to the pre-roll so it becomes
             * the last (most recent) frame replayed before live
             * recording begins.  The frame that crossed the threshold
             * is never lost.
             */
            if (pr.buf) preroll_write(&pr, dma_buf);

            if (free < p->drop_threshold_bytes) {
                /* ── DROP: ring full ────────────────────────── */
                ESP_LOGW(TAG,
                    "[cap] DROP utt=%" PRIu32
                    "  ring_free=%u  need=%u  (streamer lagging)",
                    utt_idx,
                    (unsigned)free,
                    (unsigned)p->drop_threshold_bytes);
                p->cfg.on_status("Drop!", 0xff6b6b);
                /* Clear pre-roll: stale frames must not leak into
                 * the next admitted utterance.                    */
                pr.count = 0;
                pr.head  = 0;
                state = CAP_RECORDING_DROPPED;
            } else {
                /* ── ADMIT: push START then flush pre-roll ───── */
                if (!ring_push_marker(p, PIPELINE_MSG_START, utt_idx)) {
                    ESP_LOGE(TAG,
                        "[cap] START push failed utt=%" PRIu32
                        " despite headroom – dropping",
                        utt_idx);
                    p->cfg.on_status("Drop!", 0xff6b6b);
                    pr.count = 0;
                    pr.head  = 0;
                    state = CAP_RECORDING_DROPPED;
                } else {
                    uint32_t n_pr = pr.count;
                    /*
                     * Flush pre-roll into ring as CHUNK items.
                     * drop_threshold_bytes is sized to include
                     * PREROLL_FRAMES chunks, so this cannot fail.
                     */
                    preroll_flush(p, &pr, utt_idx);

                    ESP_LOGI(TAG,
                        "[cap] START utt=%" PRIu32
                        "  ring_free=%u  preroll_flushed=%" PRIu32
                        " frames (~%" PRIu32 " ms onset recovered)",
                        utt_idx, (unsigned)free,
                        n_pr,
                        n_pr * (uint32_t)p->frame_ms);

                    p->cfg.on_status("Triggered!", 0xffff00);
                    p->busy = true;
                    state   = CAP_RECORDING;
                }
            }
            continue;
        }

        /* ════ STATE: RECORDING / RECORDING_DROPPED ════════════ */
        n_frames++;
        uint32_t elapsed_ms = (uint32_t)n_frames * (uint32_t)p->frame_ms;

        if (state == CAP_RECORDING) {

            /* Status label – throttled to ~4 Hz */
            if (elapsed_ms - last_status_ms >= STATUS_THROTTLE_MS
                    || elapsed_ms < last_status_ms) {
                last_status_ms = elapsed_ms;
                char     msg[24];
                unsigned secs   = elapsed_ms / 1000u;
                unsigned tenths = (elapsed_ms % 1000u) / 100u;
                snprintf(msg, sizeof msg, "Rec %u.%us", secs, tenths);
                p->cfg.on_status(msg, 0xff6b6b);
            }

            if (!ring_push_chunk(p, utt_idx,
                                 dma_buf, (uint16_t)p->frame_bytes)) {
                ESP_LOGE(TAG,
                    "[cap] CHUNK push failed mid-utterance "
                    "utt=%" PRIu32 " frame=%" PRIu32 " – ABORT",
                    utt_idx, n_frames);
                ring_push_marker(p, PIPELINE_MSG_ABORT, utt_idx);
                p->busy = false;
                state   = CAP_IDLE;
                vTaskDelay(pdMS_TO_TICKS(g_post_record_delay_ms));
                run_cooldown(p, dma_buf, &pr);
                continue;
            }
        }
        /* In CAP_RECORDING_DROPPED: frame is silently discarded here */

        /* ── Silence / duration tracking ────────────────────── */
        if (rms < (int16_t)g_rms_threshold)
            silence_run++;
        else
            silence_run = 0;

        uint32_t silence_ms  = silence_run * (uint32_t)p->frame_ms;
        bool     silence_hit = (silence_ms  >= (uint32_t)g_silence_timeout_ms)
                               && (elapsed_ms >= (uint32_t)g_min_record_ms);
        bool     max_hit     = (elapsed_ms  >= (uint32_t)g_max_record_ms);

        if (silence_hit || max_hit) {

            if (state == CAP_RECORDING) {
                if (max_hit)
                    ESP_LOGW(TAG,
                        "[cap] max_record reached  utt=%" PRIu32
                        " elapsed=%" PRIu32 "ms",
                        utt_idx, elapsed_ms);
                else
                    ESP_LOGI(TAG,
                        "[cap] silence end  utt=%" PRIu32
                        " elapsed=%" PRIu32 "ms  silence=%" PRIu32 "ms",
                        utt_idx, elapsed_ms, silence_ms);

                ring_push_marker(p, PIPELINE_MSG_END, utt_idx);
                p->busy = false;

                ESP_LOGI(TAG,
                    "[cap] END pushed  utt=%" PRIu32
                    " frames=%" PRIu32 " elapsed=%" PRIu32 "ms",
                    utt_idx, n_frames, elapsed_ms);
            } else {
                /* CAP_RECORDING_DROPPED */
                ESP_LOGI(TAG,
                    "[cap] dropped utterance complete "
                    "utt=%" PRIu32 " frames=%" PRIu32,
                    utt_idx, n_frames);
            }

            state = CAP_IDLE;
            vTaskDelay(pdMS_TO_TICKS(g_post_record_delay_ms));
            /*
             * run_cooldown writes its frames to pr, so by the time
             * we return to the IDLE loop the pre-roll is already
             * partially populated with post-utterance audio.
             */
            run_cooldown(p, dma_buf, &pr);
        }

    } /* while(1) */

    /* Unreachable */
    if (pr.buf) heap_caps_free(pr.buf);
    heap_caps_free(dma_buf);
    vTaskDeleteWithCaps(NULL);
}

/* ════════════════════════════════════════════════════════════════
 * streamer_task  –  ring consumer + MQTT publish
 *
 * Unchanged: pre-roll CHUNKs look identical to live CHUNKs from
 * the streamer's perspective.  No modifications required.
 * ════════════════════════════════════════════════════════════════ */
typedef enum {
    STR_WAIT_START = 0,
    STR_STREAMING,
    STR_DRAINING_ABORTED,
    STR_LIVE,               /**< Live Audio Mode: Opus-encode and publish */
} streamer_state_t;

static void streamer_task(void *arg)
{
    pipeline_t *p = (pipeline_t *)arg;

    /* Aggregate buffer in PSRAM */
    const size_t agg_cap = (size_t)AGGREGATE_FRAMES
                           * (size_t)p->frame_bytes;
    uint8_t *agg_buf = heap_caps_malloc(agg_cap,
                           MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!agg_buf) {
        agg_buf = malloc(agg_cap); /* fallback */
    }
    if (!agg_buf) {
        ESP_LOGE(TAG, "[str] agg_buf alloc failed – task exiting");
        vTaskDeleteWithCaps(NULL);
        return;
    }

    ESP_LOGI(TAG,
        "[str] streamer_task ready  agg_cap=%u B (%u frames × %u B)",
        (unsigned)agg_cap, AGGREGATE_FRAMES, (unsigned)p->frame_bytes);

    streamer_state_t  state         = STR_WAIT_START;
    uint32_t          cur_utt_idx   = 0;
    uint32_t          chunk_idx     = 0;
    uint32_t          total_bytes   = 0;
    size_t            agg_used      = 0;
    bool              stream_active = false;
    uint32_t          backoff_ms    = BACKOFF_INITIAL_MS;

    while (1) {
        size_t item_size = 0;
        void  *item = xRingbufferReceive(p->ring, &item_size,
                                         portMAX_DELAY);
        if (!item) continue;

        if (item_size < sizeof(pipeline_msg_hdr_t)) {
            ESP_LOGE(TAG,
                "[str] item too small: %u B (min %u) – discarding",
                (unsigned)item_size,
                (unsigned)sizeof(pipeline_msg_hdr_t));
            vRingbufferReturnItem(p->ring, item);
            continue;
        }

        const pipeline_msg_hdr_t *hdr =
            (const pipeline_msg_hdr_t *)item;
        const uint8_t *pcm =
            (const uint8_t *)item + sizeof(pipeline_msg_hdr_t);

        /* ──────── Helper: flush partial aggregate ──────────── */
        #define FLUSH_AGG() \
            do { \
                if (agg_used > 0 && stream_active \
                        && p->cfg.stream_chunk) { \
                    bool ok = p->cfg.stream_chunk(agg_buf, \
                                (int)agg_used, chunk_idx); \
                    if (ok) { \
                        total_bytes += (uint32_t)agg_used; \
                        chunk_idx++; \
                        backoff_ms = BACKOFF_INITIAL_MS; \
                    } else { \
                        ESP_LOGW(TAG, "[str] flush chunk failed " \
                                 "utt=%" PRIu32, cur_utt_idx); \
                    } \
                } \
                agg_used = 0; \
            } while (0)

        switch (state) {

        /* ────────────── WAIT_START ────────────────────────── */
        case STR_WAIT_START:
            /*
             * Live Audio Mode session opening. The Opus encoder is
             * created here, on this task, so that neither the encode nor
             * the MQTT publish it feeds ever runs at capture priority.
             */
            if (hdr->type == PIPELINE_MSG_LIVE_START) {
                ESP_LOGI(TAG,
                    "[str] LIVE_START session=%" PRIu32
                    "  stack_free_before_encoder_open=%u B",
                    hdr->utterance_idx,
                    (unsigned)uxTaskGetStackHighWaterMark(NULL));
                cur_utt_idx = hdr->utterance_idx;
                if (live_audio_begin(g_sample_rate,
                                     g_bits_per_sample, 1) == ESP_OK) {
                    ESP_LOGI(TAG,
                        "[str] encoder open OK  stack_free=%u B",
                        (unsigned)uxTaskGetStackHighWaterMark(NULL));
                    state = STR_LIVE;
                } else {
                    /*
                     * Encoder or MQTT announce failed. Discard the live
                     * frames as they arrive rather than leaving them to
                     * fill the ring, and wait for LIVE_END.
                     */
                    ESP_LOGE(TAG,
                        "[str] live_audio_begin failed session=%" PRIu32
                        " – releasing live mode", cur_utt_idx);
                    p->cfg.on_status("Live fail!", 0xff6b6b);
                    /*
                     * Release the request as well as draining the ring.
                     * Without this capture_task stays in CAP_LIVE until
                     * an explicit stop_live_audio arrives, and because
                     * CAP_LIVE bypasses the RMS trigger the device would
                     * go silent in *both* directions — no live audio and
                     * no Event Mode utterances either. Clearing the flag
                     * makes capture_task push LIVE_END on its next
                     * iteration, which this drain state consumes, and
                     * Event Mode resumes on its own.
                     */
                    s_live_req = false;
                    state = STR_DRAINING_ABORTED;
                }
                break;
            }

            if (hdr->type != PIPELINE_MSG_START) {
                ESP_LOGW(TAG,
                    "[str] BUG: type %d in WAIT_START "
                    "utt=%" PRIu32 " – discarding",
                    (int)hdr->type, hdr->utterance_idx);
                break;
            }

            cur_utt_idx   = hdr->utterance_idx;
            chunk_idx     = 0;
            total_bytes   = 0;
            agg_used      = 0;
            stream_active = false;
            backoff_ms    = BACKOFF_INITIAL_MS;

            ESP_LOGI(TAG,
                "[str] START utt=%" PRIu32 " – calling stream_start",
                cur_utt_idx);

            {
                bool ok = p->cfg.stream_start
                          ? p->cfg.stream_start(g_sample_rate,
                                                 g_bits_per_sample, 1)
                          : true;
                if (!ok) {
                    ESP_LOGE(TAG,
                        "[str] stream_start failed utt=%" PRIu32
                        " – draining and dropping utterance",
                        cur_utt_idx);
                    p->cfg.on_status("No MQTT!", 0xff6b6b);
                    stream_active = false;
                    state         = STR_DRAINING_ABORTED;
                } else {
                    stream_active = true;
                    state         = STR_STREAMING;
                    ESP_LOGI(TAG,
                        "[str] stream_start OK  utt=%" PRIu32,
                        cur_utt_idx);
                }
            }
            break;

        /* ────────────── STREAMING ──────────────────────────── */
        case STR_STREAMING:
            switch (hdr->type) {

            case PIPELINE_MSG_START:
                ESP_LOGE(TAG,
                    "[str] BUG: nested START utt=%" PRIu32
                    " while streaming utt=%" PRIu32,
                    hdr->utterance_idx, cur_utt_idx);
                FLUSH_AGG();
                if (stream_active && p->cfg.stream_end)
                    p->cfg.stream_end(chunk_idx, total_bytes);
                stream_active = false;
                agg_used      = 0;

                cur_utt_idx  = hdr->utterance_idx;
                chunk_idx    = 0;
                total_bytes  = 0;
                backoff_ms   = BACKOFF_INITIAL_MS;
                {
                    bool ok = p->cfg.stream_start
                              ? p->cfg.stream_start(g_sample_rate,
                                                     g_bits_per_sample, 1)
                              : true;
                    if (!ok) {
                        state = STR_DRAINING_ABORTED;
                    } else {
                        stream_active = true;
                        state         = STR_STREAMING;
                    }
                }
                break;

            case PIPELINE_MSG_ABORT:
                ESP_LOGW(TAG,
                    "[str] ABORT utt=%" PRIu32
                    " – discarding %u B aggregate, closing session",
                    hdr->utterance_idx, (unsigned)agg_used);
                agg_used = 0;
                if (stream_active && p->cfg.stream_end)
                    p->cfg.stream_end(chunk_idx, total_bytes);
                stream_active = false;
                p->cfg.on_status("Aborted", 0xff6b6b);
                state = STR_WAIT_START;
                break;

            case PIPELINE_MSG_CHUNK:
                if (!stream_active || hdr->payload_len == 0) break;

                if (agg_used + hdr->payload_len > agg_cap) {
                    ESP_LOGW(TAG,
                        "[str] agg overflow  used=%u payload=%u cap=%u"
                        " – flushing early",
                        (unsigned)agg_used,
                        (unsigned)hdr->payload_len,
                        (unsigned)agg_cap);
                    FLUSH_AGG();
                }

                memcpy(agg_buf + agg_used, pcm, hdr->payload_len);
                agg_used += hdr->payload_len;

                if (agg_used >= agg_cap) {
                    bool ok = p->cfg.stream_chunk
                              ? p->cfg.stream_chunk(agg_buf,
                                                     (int)agg_used,
                                                     chunk_idx)
                              : true;
                    if (ok) {
                        total_bytes += (uint32_t)agg_used;
                        chunk_idx++;
                        backoff_ms = BACKOFF_INITIAL_MS;
                    } else {
                        ESP_LOGW(TAG,
                            "[str] chunk %"PRIu32" publish failed "
                            "utt=%"PRIu32" – back-off %"PRIu32"ms",
                            chunk_idx, cur_utt_idx, backoff_ms);
                        vTaskDelay(pdMS_TO_TICKS(backoff_ms));
                        backoff_ms *= 2;
                        if (backoff_ms > BACKOFF_MAX_MS)
                            backoff_ms = BACKOFF_MAX_MS;
                    }
                    agg_used = 0;
                }
                break;

            case PIPELINE_MSG_END:
                FLUSH_AGG();

                if (stream_active && p->cfg.stream_end)
                    p->cfg.stream_end(chunk_idx, total_bytes);

                ESP_LOGI(TAG,
                    "[str] END  utt=%" PRIu32
                    "  mqtt_chunks=%" PRIu32
                    "  bytes=%" PRIu32,
                    cur_utt_idx, chunk_idx, total_bytes);

                p->cfg.on_status("Sent!", 0x00ff00);
                stream_active = false;
                state         = STR_WAIT_START;
                break;

            default:
                ESP_LOGW(TAG,
                    "[str] unknown type %d in STREAMING",
                    (int)hdr->type);
                break;
            }
            break;

        /* ────────────── LIVE ───────────────────────────────── */
        case STR_LIVE:
            switch (hdr->type) {

            case PIPELINE_MSG_LIVE_CHUNK:
                if (hdr->payload_len > 0)
                    live_audio_feed((const int16_t *)pcm,
                                    hdr->payload_len / (int)sizeof(int16_t));
                break;

            case PIPELINE_MSG_LIVE_END:
                live_audio_end();
                ESP_LOGI(TAG,
                    "[str] LIVE_END session=%" PRIu32, hdr->utterance_idx);
                state = STR_WAIT_START;
                break;

            default:
                /*
                 * Event Mode markers cannot legitimately appear here:
                 * capture_task never leaves CAP_LIVE without first
                 * pushing LIVE_END. Close the session defensively rather
                 * than encode Event Mode audio into the live stream.
                 */
                ESP_LOGE(TAG,
                    "[str] BUG: type %d during LIVE session=%" PRIu32
                    " – closing live session",
                    (int)hdr->type, cur_utt_idx);
                live_audio_end();
                state = STR_WAIT_START;
                break;
            }
            break;

        /* ────────────── DRAINING_ABORTED ───────────────────── */
        case STR_DRAINING_ABORTED:
            if (hdr->type == PIPELINE_MSG_END
                    || hdr->type == PIPELINE_MSG_ABORT
                    || hdr->type == PIPELINE_MSG_LIVE_END) {
                ESP_LOGI(TAG,
                    "[str] drain complete  utt=%" PRIu32
                    "  (utterance discarded)",
                    hdr->utterance_idx);
                state = STR_WAIT_START;
            }
            /* CHUNK items are silently discarded here */
            break;

        default:
            break;
        }

        #undef FLUSH_AGG

        vRingbufferReturnItem(p->ring, item);
    }

    /* Unreachable */
    heap_caps_free(agg_buf);
    vTaskDeleteWithCaps(NULL);
}

/* ════════════════════════════════════════════════════════════════
 * Public API
 * ════════════════════════════════════════════════════════════════ */

esp_err_t audio_pipeline_init(const audio_pipeline_ctx_t *ctx)
{
    if (!ctx || !ctx->mic_dev)  return ESP_ERR_INVALID_ARG;
    if (s_pipe) {
        ESP_LOGE(TAG, "already initialised – call deinit first");
        return ESP_ERR_INVALID_STATE;
    }

    pipeline_t *p = calloc(1, sizeof(pipeline_t));
    if (!p) return ESP_ERR_NO_MEM;

    p->cfg           = *ctx;
    p->busy          = false;
    p->utterance_idx = 0;

    /* ── Frame geometry ──────────────────────────────────────── */
    p->frame_bytes = g_record_buffer_frames * (int)sizeof(int16_t);
    p->frame_ms    = (g_record_buffer_frames * 1000) / g_sample_rate;
    if (p->frame_ms < 1) p->frame_ms = 1;

    /* ── Ring-buffer sizing ───────────────────────────────────
     *
     *  chunk_item_B  = RING_ITEM_OVERHEAD + sizeof(hdr) + frame_bytes
     *               = 8 + 12 + 2048  =  2068 B  (4-B aligned)
     *
     *  marker_item_B = RING_ITEM_OVERHEAD + sizeof(hdr)
     *                = 8 + 12  =  20 B
     *
     *  max_frames    = ceil(g_max_record_ms / frame_ms) + 2  ≈  473
     *
     *  one_utterance_B
     *    = (max_frames + PREROLL_FRAMES) × chunk_item_B
     *      + 2 × marker_item_B
     *      + 512 slop
     *    ≈  (473 + 20) × 2068 + 40 + 512
     *    ≈  1 015 KB
     *
     *  ring_size = 2 × one_utterance_B  ≈  2.03 MiB
     *
     *  drop_threshold = one_utterance_B
     *    Guarantees that once admitted, the full pre-roll flush
     *    + full worst-case recording all fit without any push failing.
     * ──────────────────────────────────────────────────────────── */
    {
        int max_frames = (g_max_record_ms / p->frame_ms) + 2;

        size_t chunk_item = (size_t)p->frame_bytes
                            + sizeof(pipeline_msg_hdr_t)
                            + RING_ITEM_OVERHEAD;
        chunk_item = (chunk_item + 3u) & ~3u;

        size_t marker_item = sizeof(pipeline_msg_hdr_t)
                             + RING_ITEM_OVERHEAD;
        marker_item = (marker_item + 3u) & ~3u;

        /*
         * Include PREROLL_FRAMES in the per-utterance budget so that
         * preroll_flush() is guaranteed to succeed after the headroom
         * check at SAD trigger time.
         */
        size_t one_utt = (size_t)(max_frames + (int)PREROLL_FRAMES)
                         * chunk_item
                         + 2u * marker_item
                         + 512u;

        p->drop_threshold_bytes = one_utt - 32u;  /* FreeRTOS ringbuf has ~8B internal overhead; 32B gives safe slack */

        size_t ring_sz = 2u * one_utt;
        if (ring_sz < 1u * 1024u * 1024u)
            ring_sz = 1u * 1024u * 1024u;
        ring_sz = (ring_sz + 3u) & ~3u;

        p->ring_total_bytes  = ring_sz;
        p->ring_hwm_min_free = ring_sz;

        ESP_LOGI(TAG,
            "init  frame=%dB/%dms  max_frames=%d  preroll=%u  "
            "chunk_item=%uB  one_utt=%uB(%.1fKB)  "
            "ring=%uB(%.1fKB)  drop_thr=%uB",
            p->frame_bytes, p->frame_ms,
            max_frames, PREROLL_FRAMES,
            (unsigned)chunk_item,
            (unsigned)one_utt, one_utt / 1024.0f,
            (unsigned)ring_sz,  ring_sz  / 1024.0f,
            (unsigned)p->drop_threshold_bytes);

        p->ring = xRingbufferCreate(ring_sz, RINGBUF_TYPE_NOSPLIT);
        if (!p->ring) {
            ESP_LOGE(TAG,
                "ring buffer alloc failed (need %u B – PSRAM available?)",
                (unsigned)ring_sz);
            free(p);
            return ESP_ERR_NO_MEM;
        }
    }

    s_pipe = p;
    ESP_LOGI(TAG, "init complete");
    return ESP_OK;
}

esp_err_t audio_pipeline_start(void)
{
    if (!s_pipe) return ESP_ERR_INVALID_STATE;
    pipeline_t *p = s_pipe;

    BaseType_t rc;

    /* capture_task: Core 1 (APP_CPU) – dedicated DMA / audio core */
    rc = xTaskCreatePinnedToCoreWithCaps(
        capture_task, "cap_task",
        CAPTURE_STACK_BYTES, p,
        CAPTURE_TASK_PRIO,
        &p->capture_handle,
        APP_CPU_NUM,
        TASK_MEM_CAPS);
    if (rc != pdPASS) {
        ESP_LOGE(TAG, "capture_task create failed (%d)", (int)rc);
        p->capture_handle = NULL;
        return ESP_ERR_NO_MEM;
    }

    /* streamer_task: Core 0 (PRO_CPU) – co-located with WiFi / MQTT */
    rc = xTaskCreatePinnedToCoreWithCaps(
        streamer_task, "str_task",
        STREAMER_STACK_BYTES, p,
        STREAMER_TASK_PRIO,
        &p->streamer_handle,
        PRO_CPU_NUM,
        TASK_MEM_CAPS);
    if (rc != pdPASS) {
        ESP_LOGE(TAG, "streamer_task create failed (%d)", (int)rc);
        p->streamer_handle = NULL;
        ESP_LOGE(TAG,
            "WARNING: capture_task running without streamer_task. "
            "Ring will fill and recording will drop all utterances.");
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG,
        "tasks started  "
        "cap_task(core1,prio%d,stack%u)  "
        "str_task(core0,prio%d,stack%u)  "
        "stacks=PSRAM",
        CAPTURE_TASK_PRIO,  CAPTURE_STACK_BYTES,
        STREAMER_TASK_PRIO, STREAMER_STACK_BYTES);
    return ESP_OK;
}

bool audio_pipeline_is_busy(void)
{
    return s_pipe && s_pipe->busy;
}

void audio_pipeline_set_live(bool enable)
{
    if (s_live_req == enable) {
        ESP_LOGI(TAG, "live mode already %s – ignoring",
                 enable ? "requested" : "released");
        return;
    }
    ESP_LOGI(TAG, "live mode %s", enable ? "REQUESTED" : "RELEASED");
    s_live_req = enable;
}

bool audio_pipeline_is_live(void)
{
    return s_live_on;
}