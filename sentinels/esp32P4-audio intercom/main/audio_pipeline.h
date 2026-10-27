/*
 * audio_pipeline.h  –  Decoupled producer/consumer PCM pipeline
 *
 * Architecture
 * ────────────
 *  capture_task   Core 1, prio 18  –  DMA read + SAD  →  ring buffer
 *  streamer_task  Core 0, prio  4  –  ring buffer     →  MQTT
 *
 * Ring-buffer item format
 * ───────────────────────
 *  [ pipeline_msg_hdr_t (12 B) ][ PCM payload (0 B or frame_bytes) ]
 *  Sent as one atomic NOSPLIT item.
 *
 * Drop policy
 * ───────────
 *  At every SAD trigger: free = xRingbufferGetCurFreeSize(ring).
 *  If free < drop_threshold_bytes (≈ one max-length utterance) →
 *  RECORDING_DROPPED: read frames, run silence/max tracking, zero ring
 *  writes.  The streamer never receives a partial utterance.
 */
#ifndef AUDIO_PIPELINE_H
#define AUDIO_PIPELINE_H

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_codec_dev.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ── Runtime settings (defined in main.c) ───────────────────── */
extern int  g_sample_rate;
extern int  g_bits_per_sample;
extern int  g_record_buffer_frames;
extern int  g_discard_samples;
extern int  g_rms_threshold;
extern int  g_silence_timeout_ms;
extern int  g_min_record_ms;
extern int  g_max_record_ms;
extern int  g_timezone_offset_sec;
extern int  g_cooldown_drain_ms;
extern int  g_cooldown_rms_checks;
extern int  g_post_record_delay_ms;
extern char g_device_id[32];

/* ── Ring-buffer message types ──────────────────────────────── */
typedef enum {
    PIPELINE_MSG_START = 0, /**< no payload; utterance begin              */
    PIPELINE_MSG_CHUNK,     /**< payload = one DMA frame of raw PCM       */
    PIPELINE_MSG_END,       /**< no payload; utterance complete           */
    PIPELINE_MSG_ABORT,     /**< no payload; ring overrun mid-utterance   */

    /* Live Audio Mode. Carried on the same ring as Event Mode so that
     * capture_task still never blocks on the network, and so the Opus
     * encode happens on streamer_task rather than at capture priority. */
    PIPELINE_MSG_LIVE_START,/**< no payload; live session begin           */
    PIPELINE_MSG_LIVE_CHUNK,/**< payload = one DMA frame of raw PCM       */
    PIPELINE_MSG_LIVE_END,  /**< no payload; live session end             */
} pipeline_msg_type_t;

/* ── Ring-buffer item header (12 bytes, 4-byte aligned) ─────── */
typedef struct {
    pipeline_msg_type_t type;           /* 4 B                             */
    uint32_t            utterance_idx;  /* 4 B  – monotonic, log correlation */
    uint16_t            payload_len;    /* 2 B  – 0 for non-CHUNK items    */
    uint16_t            _pad;           /* 2 B  – explicit alignment pad   */
} pipeline_msg_hdr_t;                   /* 12 B total                      */

/* ── Streaming callbacks (called from streamer_task) ────────── */
typedef bool (*pipeline_stream_start_fn)(int sample_rate,
                                          int bits_per_sample,
                                          int channels);
typedef bool (*pipeline_stream_chunk_fn)(const void *data, int len,
                                          uint32_t chunk_idx);
typedef bool (*pipeline_stream_end_fn)  (uint32_t total_chunks,
                                          uint32_t total_bytes);

/* ── Pipeline context ───────────────────────────────────────── */
typedef struct {
    esp_codec_dev_handle_t   mic_dev;   /**< already-initialised mic codec */

    /* Streaming – invoked from streamer_task                              */
    pipeline_stream_start_fn  stream_start;
    pipeline_stream_chunk_fn  stream_chunk;
    pipeline_stream_end_fn    stream_end;

    /* Display – invoked from capture_task                                 */
    void (*on_status)(const char *text, uint32_t colour);
    void (*on_rms)   (int16_t rms);

    /* Connectivity – invoked from capture_task                            */
    bool (*wifi_connected)(void);

    /* Playback queue drain – invoked from capture_task (~every 2 s)      */
    bool (*check_play)(void);
} audio_pipeline_ctx_t;

/* ── Public API ─────────────────────────────────────────────── */

/**
 * Allocate the PSRAM ring buffer and initialise shared state.
 * Must be called once before audio_pipeline_start().
 */
esp_err_t audio_pipeline_init(const audio_pipeline_ctx_t *ctx);

/**
 * Spawn capture_task and streamer_task.
 * audio_pipeline_init() must have been called first.
 */
esp_err_t audio_pipeline_start(void);

/**
 * True while the capture task is in RECORDING state
 * (utterance in-flight on the ring).
 */
bool audio_pipeline_is_busy(void);

/* ── Live Audio Mode ────────────────────────────────────────────────
 *
 * Request or release Live Audio Mode. Safe to call from any task; the
 * MQTT handler calls it directly from the control-topic dispatch.
 *
 * This only sets a request flag. capture_task acts on it at a frame
 * boundary, and deliberately not in the middle of an utterance: if a
 * recording is in flight when a start arrives, that utterance completes
 * and is streamed normally first, so Event Mode output is never
 * truncated by a mode switch.
 *
 * On leaving Live Audio Mode the pre-roll is cleared and the normal
 * cooldown runs, which is the same path an utterance exits through — so
 * Event Mode resumes in exactly the state it would have been in.
 */
void audio_pipeline_set_live(bool enable);

/** True once capture_task has actually entered Live Audio Mode. */
bool audio_pipeline_is_live(void);

#ifdef __cplusplus
}
#endif
#endif /* AUDIO_PIPELINE_H */