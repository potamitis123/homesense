"""Audio in and out of an ESP32-S3-BOX3 sentinel.

This is the adapter the specification asks for. Everything above it deals
in "give me the next human utterance" and "say this"; only this file
knows about the transport.

It uses exactly the mechanism the dashboard's push-to-talk now uses, so
there is one audio path to reason about rather than two:

  * one session, opened once and held for the whole conversation, rather
    than opened and torn down around every turn
  * inbound audio discarded while the agent is speaking, which is what
    stops it hearing its own voice out of the loudspeaker and answering
    itself -- the server-side equivalent of "mic off while the speaker is
    on", and safe whether or not the firmware enforces it
  * turn boundaries taken from the device: the firmware ends a burst with
    `stream/complete`, which the transport surfaces as a frame with
    is_last=True. That is a better end-of-turn signal than any silence
    detection we could run afterwards, so there is no VAD here at all

Uplink frames arrive as raw PCM on audio/recordings/<id>/stream/*, not as
Opus on live/opus -- see the notes in streaming_transport.py.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Protocol

from ..config import SENTINEL_ROOT

if str(SENTINEL_ROOT) not in sys.path:
    sys.path.insert(0, str(SENTINEL_ROOT))

log = logging.getLogger(__name__)


class SentinelAudioIO(Protocol):
    async def receive_utterance(self, timeout: float) -> bytes: ...
    async def send_audio(self, chunks: Iterable[bytes],
                         source_rate: int | None = None) -> float: ...
    def close(self) -> None: ...


@dataclass
class UtteranceConfig:
    sample_rate: int = 16000
    max_utterance_seconds: float = 45.0
    # 960 = one 30 ms frame at 16 kHz 16-bit: what the dashboard's PTT
    # sends, and therefore what the firmware is proven to accept. Larger
    # chunks are heard as clipped speech.
    downlink_chunk_bytes: int = 960
    # Feed at playback speed; flooding the firmware faster than it drains
    # is the other way audio goes missing.
    downlink_pace: bool = True
    # A burst shorter than this is a cough, a door, or the tail of our own
    # voice -- not a turn.
    min_utterance_seconds: float = 0.4
    # After a burst ends, wait this long for another before deciding the
    # person has finished. The device ends a burst on any pause, and older
    # speakers pause mid-sentence; answering into that gap is rude and
    # loses the rest of what they were saying.
    continuation_grace_seconds: float = 2.5
    # Unused here (the device does the endpointing); kept so settings that
    # carry VAD values can be passed through unchanged.
    silence_ms: int = 700
    vad_threshold: float = 0.5


class SentinelAudioAdapter:
    """One open channel with one sentinel, for the length of a conversation."""

    def __init__(self, sentinel_id: str, cfg: UtteranceConfig | None = None):
        from ast_src.audio_transport import AudioSessionManager
        from ast_src.audio_transport.base import (
            AudioCodec, AudioFrame, AudioSessionConfig, SessionMode,
            TransportDirection,
        )

        self.sentinel_id = sentinel_id
        self.cfg = cfg or UtteranceConfig()
        self._AudioFrame = AudioFrame
        self._AudioCodec = AudioCodec

        self._loop = asyncio.get_event_loop()
        self._speaking = threading.Event()
        self._lock = threading.Lock()
        self._burst = bytearray()
        self._burst_rate = self.cfg.sample_rate
        self._burst_muted = False
        self.utterances: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue()

        # ── live diagnostics ─────────────────────────────────────────
        # Enough state to answer, from the log alone, the two questions a
        # broken turn always raises: did the device send anything, and if
        # it did, who threw it away. Cheap enough to leave on.
        self._t0 = time.monotonic()
        self._burst_chunks = 0
        self._burst_started_at = 0.0
        self._listening = False        # is anyone waiting for an utterance?
        self._speak_started_at = 0.0

        self._mgr = AudioSessionManager.get_instance()
        self._transport = self._mgr.get_transport("streaming")

        # Take delivery of uplink frames ourselves. This replaces the
        # manager's sink routing for the duration of the conversation,
        # which is deliberate: the agent's ear is Whisper, not the
        # server's loudspeakers.
        self._transport.set_receive_callback(self._on_frame)

        self._session_id = self._mgr.start_session(
            "streaming",
            AudioSessionConfig(device_ids=[sentinel_id],
                               direction=TransportDirection.DUPLEX,
                               mode=SessionMode.CONTINUOUS,
                               codec=AudioCodec.OPUS,
                               sample_rate=self.cfg.sample_rate))
        log.info("channel open: session %s -> %s", self._session_id, sentinel_id)

    # ── uplink ───────────────────────────────────────────────────────

    def _clock(self) -> float:
        """Seconds since the channel opened. The log's common timebase."""
        return time.monotonic() - self._t0

    def _on_frame(self, session_id: str, frame) -> None:
        """Assemble one burst. Runs on the MQTT thread."""
        speaking = self._speaking.is_set()
        with self._lock:
            if frame.is_last:
                audio = bytes(self._burst)
                rate = self._burst_rate
                muted, self._burst_muted = self._burst_muted, False
                chunks, self._burst_chunks = self._burst_chunks, 0
                started = self._burst_started_at
                self._burst = bytearray()
            else:
                if speaking:
                    # Captured while the agent was talking: almost certainly
                    # its own voice back through the room.
                    if not self._burst_muted:
                        log.info("[%6.2fs] UPLINK while agent speaking -> will "
                                 "be dropped (own voice)", self._clock())
                    self._burst_muted = True
                    return
                if not self._burst:
                    self._burst_started_at = self._clock()
                    log.info("[%6.2fs] UPLINK burst begins (listening=%s)",
                             self._burst_started_at, self._listening)
                self._burst_chunks += 1
                self._burst_rate = frame.sample_rate or self.cfg.sample_rate
                self._burst.extend(frame.pcm)
                return

        now = self._clock()
        seconds = len(audio) / (rate * 2) if rate else 0.0
        log.info("[%6.2fs] UPLINK complete: %d chunks, %d bytes, %.2fs @%dHz "
                 "(muted=%s speaking=%s listening=%s, began %+.2fs)",
                 now, chunks, len(audio), seconds, rate, muted, speaking,
                 self._listening, started - now if chunks else 0.0)

        if muted or speaking:
            log.warning("[%6.2fs] DROPPED: agent was speaking during this burst",
                        now)
            return
        if len(audio) < int(self.cfg.min_utterance_seconds * rate * 2):
            log.warning("[%6.2fs] DROPPED: burst is %.2fs, under the %.2fs "
                        "minimum", now, seconds, self.cfg.min_utterance_seconds)
            return

        self._loop.call_soon_threadsafe(self.utterances.put_nowait, (audio, rate))
        log.info("[%6.2fs] QUEUED %.2fs of speech (%d waiting)", now, seconds,
                 self.utterances.qsize() + 1)

    async def receive_utterance(self, timeout: float) -> bytes:
        """Wait for one complete utterance, as delimited by the device.

        The queue is deliberately NOT drained first. People start replying
        while the agent is still thinking, and that burst arrives before
        anyone calls this; throwing it away loses real speech. Anything
        captured during the agent's own turn has already been dropped by
        the gate in _on_frame, so what is queued here is genuine.
        """
        self._listening = True
        opened = self._clock()
        started = time.monotonic()
        deadline = started + timeout
        # A device that never sends stream/complete must not hold a turn
        # open forever.
        hard_stop = deadline + self.cfg.max_utterance_seconds + 5.0
        extended = False

        log.info("[%6.2fs] LISTEN window open (%.1fs to begin, %d already "
                 "queued)", opened, timeout, self.utterances.qsize())

        # `timeout` bounds how long they may take to BEGIN, not how long
        # they may speak. The device only delivers a burst once it has
        # ended, so a fixed timer expires in the middle of a long answer
        # and the agent asks whether anyone is there while the reply is
        # still being spoken. Mid-burst, the window stays open.
        while True:
            now = time.monotonic()
            if now >= deadline:
                with self._lock:
                    mid_burst = bool(self._burst)
                if not (mid_burst and now < hard_stop):
                    log.warning("[%6.2fs] LISTEN timed out after %.1fs with "
                                "nothing heard", self._clock(), now - started)
                    self._listening = False
                    return b""
                if not extended:
                    extended = True
                    log.info("[%6.2fs] still speaking -- holding the window "
                             "open", self._clock())
            # Always poll, including while extended: the burst completes
            # on the MQTT thread and lands in the queue, and sleeping
            # instead would leave it unread until the device happened to
            # go quiet.
            try:
                audio, rate = await asyncio.wait_for(self.utterances.get(), 0.15)
                break
            except asyncio.TimeoutError:
                continue

        # Keep collecting while they are still going: each further burst
        # inside the grace window is treated as the same turn.
        grace = self.cfg.continuation_grace_seconds
        while grace > 0:
            try:
                more, more_rate = await asyncio.wait_for(
                    self.utterances.get(), timeout=grace)
            except asyncio.TimeoutError:
                log.info("[%6.2fs] grace window (%.1fs) elapsed, turn is over",
                         self._clock(), grace)
                break
            if more_rate != rate:
                from .audio_utils import resample_pcm16
                more = resample_pcm16(more, more_rate, rate)
            audio += more
            log.info("...continued, now %.2fs", len(audio) / (rate * 2))

        cap = int(self.cfg.max_utterance_seconds * rate * 2)
        if len(audio) > cap:
            log.info("utterance clipped to the %.0fs cap",
                     self.cfg.max_utterance_seconds)
            audio = audio[:cap]

        if rate != self.cfg.sample_rate:
            from .audio_utils import resample_pcm16
            audio = resample_pcm16(audio, rate, self.cfg.sample_rate)
        self._listening = False
        log.info("[%6.2fs] LISTEN closed with %.2fs of speech (window was "
                 "%.2fs)", self._clock(), len(audio) / (self.cfg.sample_rate * 2),
                 self._clock() - opened)
        return audio

    # ── downlink ─────────────────────────────────────────────────────

    async def send_audio(self, chunks: Iterable[bytes],
                         source_rate: int | None = None) -> float:
        """Stream PCM to the loudspeaker. Returns seconds of audio sent.

        source_rate is what the chunks arrive at (Piper renders at 22050);
        anything other than the session rate is converted rather than left
        for the firmware to misinterpret.
        """
        from .audio_utils import resample_pcm16

        target = self.cfg.sample_rate
        size = self.cfg.downlink_chunk_bytes
        bytes_per_second = target * 2
        total = 0

        self._speaking.set()
        self._speak_started_at = self._clock()
        log.info("[%6.2fs] SPEAK begins (mic gated)", self._speak_started_at)
        try:
            pending = bytearray()
            for pcm in chunks:
                if not pcm:
                    continue
                if source_rate and source_rate != target:
                    pcm = resample_pcm16(pcm, source_rate, target)
                pending.extend(pcm)
                while len(pending) >= size:
                    total += await self._publish_chunk(bytes(pending[:size]),
                                                       bytes_per_second)
                    del pending[:size]

            if pending:
                total += await self._publish_chunk(bytes(pending), bytes_per_second)

            # The device plays from a queue and is still speaking after the
            # last publish returns; wait it out or we record our own tail.
            await asyncio.sleep(0.35)
            return total / bytes_per_second
        finally:
            self._speaking.clear()
            with self._lock:
                # Whatever is sitting in _burst was captured BEFORE the
                # gate closed -- chunks arriving while we spoke are
                # refused in _on_frame rather than appended, so nothing
                # here is our own voice. It is the person starting to
                # answer while the agent was still composing, which is
                # ordinary and common: they hear the question end, the
                # model takes a second, and they begin.
                #
                # This used to be thrown away wholesale, on the reasoning
                # that anything overlapping our turn was contaminated. It
                # is not, and the cost was measured at 4.6 s of real
                # speech in a three-minute conversation -- the largest
                # single source of loss in the system. Keep it, and let
                # the transcriber be the judge.
                pending = bytes(self._burst)
                rate = self._burst_rate
                self._burst = bytearray()
                self._burst_muted = False
                self._burst_chunks = 0

            kept = 0
            if len(pending) >= int(self.cfg.min_utterance_seconds * rate * 2):
                self.utterances.put_nowait((pending, rate))
                kept = len(pending)

            log.info("[%6.2fs] SPEAK ends after %.2fs, mic live again%s",
                     self._clock(), self._clock() - self._speak_started_at,
                     f" (kept {kept / (rate * 2):.2f}s captured while "
                     f"composing)" if kept else
                     (f" (dropped {len(pending)} B, under the minimum)"
                      if pending else ""))

    async def _publish_chunk(self, pcm: bytes, bytes_per_second: int) -> int:
        self._transport.send_audio_frame(
            self._AudioFrame(pcm=pcm, is_last=False,
                             codec=self._AudioCodec.PCM_RAW,
                             metadata={"source": "companion"}),
            self._session_id)
        await asyncio.sleep(len(pcm) / bytes_per_second
                            if self.cfg.downlink_pace else 0)
        return len(pcm)

    def close(self) -> None:
        try:
            self._mgr.stop_session(self._session_id)
        except Exception:
            log.debug("stop_session failed", exc_info=True)
        log.info("channel closed")


class MockSentinelAdapter:
    """Stand-in with no device: silence in, byte counting out."""

    def __init__(self, scripted: list[str] | None = None, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._scripted = list(scripted or [])
        self.spoken: list[float] = []

    async def receive_utterance(self, timeout: float) -> bytes:
        if self._scripted:
            self._scripted.pop(0)
        return b"\x00\x00" * self.sample_rate

    async def send_audio(self, chunks: Iterable[bytes],
                         source_rate: int | None = None) -> float:
        total = sum(len(c) for c in chunks)
        seconds = total / ((source_rate or self.sample_rate) * 2)
        self.spoken.append(seconds)
        return seconds

    def close(self) -> None:
        pass
