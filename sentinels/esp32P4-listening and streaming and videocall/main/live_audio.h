/*
 * live_audio.h  –  Opus encode + packet bundling for Live Audio Mode
 *
 * Role
 * ────
 * Live Audio Mode is the Sentinel's second operating state: instead of
 * waiting for an RMS trigger and shipping discrete utterances, the mic
 * runs continuously and every frame is Opus-encoded and published.
 *
 * This module owns only the encode-and-publish half of that. It does no
 * I2S work and spawns no tasks: the PCM arrives from the existing
 * capture/streamer pipeline, so Live Audio Mode inherits the same
 * "capture never blocks on the network" property Event Mode relies on.
 *
 * Call context
 * ────────────
 * All three functions run on streamer_task (core 0, prio 4) — the task
 * that already performs MQTT publishes. They must never be called from
 * capture_task: Opus encoding plus an MQTT publish at prio 18 would
 * stall DMA reads whenever the broker is slow, which is exactly the
 * failure the ring buffer exists to prevent.
 *
 * Rate mismatch
 * ─────────────
 * The capture pipeline delivers g_record_buffer_frames samples per DMA
 * frame (1024 = 64 ms at 16 kHz). Opus at 16 kHz wants 320-sample
 * (20 ms) frames. live_audio_feed() therefore accumulates PCM and
 * drains it 20 ms at a time; leftovers carry into the next call, so no
 * samples are lost or duplicated across the boundary.
 *
 * Wire format
 * ───────────
 * Opus frames are variable length, so a bundle has to delimit them.
 * LIVE_FRAMES_PER_PACKET encoded frames are packed into one MQTT
 * message as:
 *
 *   offset 0 : uint32_le  seq            packet counter, from 0
 *   offset 4 : uint16_le  len_0          byte length of frame 0
 *              uint8[]    frame_0
 *              uint16_le  len_1
 *              uint8[]    frame_1
 *              ... repeated to end of payload
 *
 * The frame count is implied by the payload length, so a short final
 * bundle at stop time needs no special case. seq lets the receiver spot
 * gaps caused by a publish failure or an MQTT reconnect; it is not a
 * retransmission scheme.
 */
#ifndef LIVE_AUDIO_H
#define LIVE_AUDIO_H

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ── Encoder parameters ──────────────────────────────────────────── *
 * 24 kbps VBR at 16 kHz mono is transparent enough for speech and
 * leaves the MQTT payload around 60 bytes per 20 ms frame, so a
 * 5-frame bundle is ~310 bytes including the length prefixes — about
 * 3 kB/s on the wire against 32 kB/s for the raw PCM Event Mode
 * sends.
 *
 * DTX is deliberately off. It would cut bandwidth further during the
 * long silences typical of monitoring a sleeping elder, but it makes
 * the stream non-continuous, which complicates timeline
 * reconstruction on the laptop. Turn it on only if bandwidth becomes
 * the binding constraint.
 *
 * FEC is off because MQTT rides TCP: within a connection there is no
 * packet loss for in-band FEC to repair.
 * ─────────────────────────────────────────────────────────────────── */
#define LIVE_OPUS_BITRATE          24000
#define LIVE_OPUS_FRAME_MS         20
#define LIVE_OPUS_COMPLEXITY       5
#define LIVE_FRAMES_PER_PACKET     5      /* 5 × 20 ms = 100 ms/message */

/**
 * Open the Opus encoder and announce the stream on MQTT.
 *
 * @param sample_rate      must be one of 8000/12000/16000/24000/48000
 * @param bits_per_sample  must be 16; Opus supports nothing else here
 * @param channels         1 or 2
 *
 * On any failure nothing is left allocated and the caller may simply
 * not call live_audio_feed(); live_audio_end() is still safe to call.
 */
esp_err_t live_audio_begin(int sample_rate, int bits_per_sample,
                           int channels);

/**
 * Encode and publish one DMA frame's worth of PCM.
 *
 * Consumes as many whole 20 ms Opus frames as @p pcm plus any carried
 * remainder allows, bundling them LIVE_FRAMES_PER_PACKET at a time.
 *
 * Returns ESP_OK even when a publish fails: Live Audio Mode prefers
 * staying live over reporting every dropped bundle upward. Failures are
 * counted and logged, and show up on the receiver as a gap in seq.
 */
esp_err_t live_audio_feed(const int16_t *pcm, int samples);

/**
 * Flush any partial bundle, publish the completion message, close the
 * encoder and release all buffers.
 *
 * Idempotent and safe to call when live_audio_begin() failed or was
 * never called, so the pipeline's exit path needs no guard.
 */
void live_audio_end(void);

/** True between a successful live_audio_begin() and live_audio_end(). */
bool live_audio_is_active(void);

#ifdef __cplusplus
}
#endif
#endif /* LIVE_AUDIO_H */
