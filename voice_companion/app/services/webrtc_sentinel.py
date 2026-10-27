"""Audio in and out of a WebRTC sentinel (the P4 units).

The MQTT adapter in sentinel.py takes turn boundaries from the device:
the firmware decides when someone is speaking, records a burst, and ends
it with `stream/complete`. That is convenient, and it is the source of
every turn-taking bug measured against the BOX-3 -- the device's own
loudspeaker sets off its sound trigger, so it records and uploads the
agent's voice, and the server is left guessing which bursts to believe.

WebRTC has none of that. The stream is continuous and duplex, nothing on
the device decides what a turn is, and audio arrives as it is spoken
rather than flushed in backlogs. The price is that endpointing becomes
ours, which is what most of this file is.

Modelled on the Visuals tab, which already holds bilateral audio calls
with these devices -- see assets/webrtc_intercom_v2.js. Nothing outside
voice_companion is modified or has to change.

Three things copied from it deliberately:

  * **PCMA, not Opus.** The browser rewrites its own offer to strip
    every audio payload type but G.711 A-law, because that is what
    esp_peer will answer. `_force_pcma` below is that function in
    Python. Get it wrong and the device answers nothing at all.
  * **We offer, the device answers.** It reports `is_initiator = false`
    and never offers.
  * **Hanging up is not optional.** Signalling is independent of the
    media path, so without an explicit bye the device goes on believing
    the call is up, refuses the next offer, and needs a power cycle.
    `close()` sends DENY_CALL and closes the session even when
    everything else has already failed.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import sys
import time
from typing import Iterable

import numpy as np

from ..config import SENTINEL_ROOT

if str(SENTINEL_ROOT) not in sys.path:
    sys.path.insert(0, str(SENTINEL_ROOT))

log = logging.getLogger(__name__)

# The wire format esp_peer answers with. Everything above this class
# deals in 16 kHz, which is what Whisper and Piper want.
WIRE_RATE = 8000
FRAME_MS = 20
WIRE_FRAME_SAMPLES = WIRE_RATE * FRAME_MS // 1000        # 160
WIRE_FRAME_BYTES = WIRE_FRAME_SAMPLES * 2

# How long after an utterance should have finished playing we keep
# waiting for the track to drain before declaring the far end deaf.
SPEAK_DRAIN_GRACE = 5.0


class WebRtcTransportUnavailable(RuntimeError):
    pass


def _force_pcma(sdp: str) -> str:
    """Reduce the audio m-line to G.711 A-law only.

    A port of forcePcma() from webrtc_intercom_v2.js. The reason is in
    the comment there: esp_peer cannot build an answer to an offer
    carrying codecs it does not have, so offering the full list buys
    silence rather than a negotiation.
    """
    eol = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.replace("\r\n", "\n").split("\n")

    m_index = next((i for i, l in enumerate(lines)
                    if l.startswith("m=audio ")), -1)
    if m_index < 0:
        return sdp

    end = next((i for i in range(m_index + 1, len(lines))
                if lines[i].startswith("m=")), len(lines))

    pcma = None
    for i in range(m_index + 1, end):
        line = lines[i]
        if line.lower().startswith("a=rtpmap:"):
            payload, _, rest = line[len("a=rtpmap:"):].partition(" ")
            if rest.split("/")[0].strip().lower() == "pcma":
                pcma = payload.strip()
                break
    if pcma is None:
        log.warning("no PCMA in the offer; leaving the codec list alone")
        return sdp

    header = lines[m_index].split(" ")
    lines[m_index] = " ".join(header[:3] + [pcma])

    kept = []
    for i in range(m_index + 1, end):
        line = lines[i]
        drop = False
        for attr in ("a=rtpmap:", "a=fmtp:", "a=rtcp-fb:"):
            if line.startswith(attr):
                payload = line[len(attr):].split(" ")[0].split("/")[0].strip()
                drop = payload != pcma
                break
        if not drop:
            kept.append(line)

    return eol.join(lines[:m_index + 1] + kept + lines[end:])


def _build_agent_track():
    """The agent's voice as a track the peer connection can pull from.

    Defined inside a function so that importing this module never
    requires aiortc: transport.py has to be able to route to it and
    report a clean error when the dependency is missing.
    """
    from aiortc.mediastreams import MediaStreamTrack

    class AgentAudioTrack(MediaStreamTrack):
        """Always yields a frame. A track that stops yielding stalls the
        connection rather than merely going quiet, so silence has to be
        sent explicitly whenever there is nothing to say."""

        kind = "audio"

        def __init__(self):
            super().__init__()
            self._pending = bytearray()       # 8 kHz s16 mono, ready to go
            self._pts = 0
            self._started = None

        def queue(self, pcm: bytes) -> None:
            self._pending.extend(pcm)

        def pending_seconds(self) -> float:
            return len(self._pending) / (WIRE_RATE * 2)

        def drop_pending(self) -> None:
            self._pending.clear()

        async def recv(self):
            import av

            # Pace to the wall clock. Without this the connection drains
            # the track as fast as it can and the far end hears a
            # chipmunk.
            if self._started is None:
                self._started = time.monotonic()
            delay = (self._started + self._pts / WIRE_RATE) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

            if self._pending:
                chunk = bytes(self._pending[:WIRE_FRAME_BYTES])
                del self._pending[:WIRE_FRAME_BYTES]
                # An utterance almost never divides evenly into 20 ms
                # frames, so the last one is short. Pad it and send it:
                # waiting for a full frame that will never arrive strands
                # the remainder, and `pending` then never reaches zero.
                # That leftover -- 34 bytes of a 4.3 second greeting --
                # is what hung the first real call.
                if len(chunk) < WIRE_FRAME_BYTES:
                    chunk += bytes(WIRE_FRAME_BYTES - len(chunk))
            else:
                chunk = bytes(WIRE_FRAME_BYTES)          # silence

            samples = np.frombuffer(chunk, dtype=np.int16).reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(samples, format="s16",
                                               layout="mono")
            frame.sample_rate = WIRE_RATE
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, WIRE_RATE)
            self._pts += WIRE_FRAME_SAMPLES
            return frame

    return AgentAudioTrack()


class Endpointer:
    """Turns a continuous stream into utterances.

    Energy-gated with an adaptive noise floor, deliberately rather than a
    neural VAD: this runs on every 20 ms frame for the whole length of
    every conversation, the GPU is already contended by Whisper, and the
    decision is coarse -- speech or not -- on audio that has already been
    through the device's own gain control.

    `silence_ms` closes an utterance and `vad_threshold` scales how far
    above the measured noise floor a frame must sit to count as speech.
    Both come from UtteranceConfig, where they have been carried unused
    since Phase 1 for exactly this moment.
    """

    def __init__(self, silence_ms: int, threshold: float,
                 min_utterance_seconds: float, max_utterance_seconds: float,
                 sample_rate: int = 16000):
        self.silence_frames = max(1, int(silence_ms) // FRAME_MS)
        self.threshold = threshold
        self.min_seconds = min_utterance_seconds
        self.max_seconds = max_utterance_seconds
        self.sample_rate = sample_rate
        self._noise = 200.0
        self._speech = bytearray()
        self._quiet_run = 0
        self._in_speech = False

    @staticmethod
    def _rms(pcm: bytes) -> float:
        if not pcm:
            return 0.0
        a = np.frombuffer(pcm, dtype=np.int16).astype("float32")
        return float(np.sqrt((a * a).mean())) if a.size else 0.0

    def _seconds(self, pcm) -> float:
        return len(pcm) / (self.sample_rate * 2)

    def feed(self, pcm: bytes) -> bytes | None:
        """Add one frame. Returns a finished utterance, or None."""
        level = self._rms(pcm)
        gate = self._noise * (1.0 + max(0.1, self.threshold) * 2.0) + 120.0

        if level > gate:
            self._in_speech = True
            self._quiet_run = 0
            self._speech.extend(pcm)
        elif self._in_speech:
            self._quiet_run += 1
            # Keep the quiet frames. Dropping them clips the ends of
            # words and hands Whisper a stump to guess from.
            self._speech.extend(pcm)
            if self._quiet_run >= self.silence_frames:
                return self._finish()
        else:
            # Adapt the floor only while nobody is talking, or a long
            # sentence slowly raises the bar until it excludes itself.
            self._noise = 0.95 * self._noise + 0.05 * level

        if self._seconds(self._speech) >= self.max_seconds:
            log.info("utterance hit the %.0fs cap", self.max_seconds)
            return self._finish()
        return None

    def _finish(self) -> bytes | None:
        audio, self._speech = bytes(self._speech), bytearray()
        self._in_speech, self._quiet_run = False, 0
        if self._seconds(audio) < self.min_seconds:
            return None
        return audio

    @property
    def in_speech(self) -> bool:
        """Is someone talking at this instant?

        The listening window needs this. An utterance cannot be emitted
        until it ends, so a fixed timer expires in the middle of a long
        answer and the agent asks whether anyone is there while the
        reply is still being spoken.
        """
        return self._in_speech

    def reset(self) -> None:
        self._speech.clear()
        self._in_speech, self._quiet_run = False, 0


class WebRtcSentinelAdapter:
    """One WebRTC call with one sentinel, for the length of a conversation."""

    def __init__(self, sentinel_id: str, settings=None, cfg=None):
        try:
            from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
        except ImportError as exc:                        # pragma: no cover
            raise WebRtcTransportUnavailable(
                "the WebRTC transport needs aiortc:\n"
                "    pip install aiortc==1.10.1\n"
                "or talk to a BOX-3 instead: --sentinel box3-2") from exc

        from ast_src.webrtc.signaling import get_bridge

        from .sentinel import UtteranceConfig

        self.sentinel_id = sentinel_id
        self.cfg = cfg or UtteranceConfig()
        self._t0 = time.monotonic()
        self._speaking = False
        self._closed = False
        self._spoke_at = 0.0
        self._meter_at = 0.0
        self._meter_peak = 0.0
        # How long before the last utterance was captured the agent
        # stopped talking. None until it has spoken once.
        self.seconds_since_agent_spoke = None

        self.utterances: asyncio.Queue[bytes] = asyncio.Queue()
        self._endpointer = Endpointer(
            self.cfg.silence_ms, self.cfg.vad_threshold,
            self.cfg.min_utterance_seconds, self.cfg.max_utterance_seconds,
            self.cfg.sample_rate)

        self._bridge = get_bridge()
        self._session_id = self._bridge.open_session(sentinel_id)

        ice = [RTCIceServer(**s) if isinstance(s, dict) else s
               for s in (self._bridge.ice_servers() or [])]
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))
        self._track = _build_agent_track()
        self._pc.addTrack(self._track)
        self._pc.on("track", self._on_track)
        self._pc.on("connectionstatechange", self._on_state)

        self._remote_set = False
        self._parked: list[str] = []
        self._tasks: list[asyncio.Task] = []
        self._connected = asyncio.Event()

        log.info("WebRTC session %s -> %s", self._session_id[:8], sentinel_id)

    def _clock(self) -> float:
        return time.monotonic() - self._t0

    # ── negotiation ──────────────────────────────────────────────────

    async def connect(self, timeout: float = 25.0) -> None:
        """Offer, wait for the answer, wait for media. Call once."""
        from aiortc import RTCSessionDescription

        offer = await self._pc.createOffer()
        # aiortc finishes gathering before setLocalDescription returns,
        # so our offer already carries its candidates and there is no
        # trickle to arrange on this side. Only the device trickles.
        await self._pc.setLocalDescription(
            RTCSessionDescription(sdp=_force_pcma(offer.sdp), type=offer.type))

        self._tasks.append(asyncio.create_task(self._poll_loop()))
        self._send({"type": "offer", "sdp": self._pc.localDescription.sdp})
        # Drives the announcement tone only; the firmware answers anyway.
        self._send({"type": "customized", "data": "RING"})
        log.info("[%6.2fs] ringing %s", self._clock(), self.sentinel_id)

        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            await self.aclose()
            raise WebRtcTransportUnavailable(
                f"{self.sentinel_id} did not answer within {timeout:.0f}s. "
                "If the Visuals tab has a call open, that call owns the "
                "device -- esp_peer answers one peer at a time.")

        if self._closed:
            raise WebRtcTransportUnavailable(
                f"{self.sentinel_id} refused or dropped the call")

        # What each side agreed to actually do. Worth a line every call:
        # "connected" says the transport came up, not that the device
        # agreed to listen to us, and the difference is silent otherwise.
        for t in self._pc.getTransceivers():
            if t.kind != "audio":
                continue
            current = getattr(t, "currentDirection", None) or t.direction
            log.info("[%6.2fs] audio negotiated as %s", self._clock(), current)
            if current in ("recvonly", "inactive"):
                log.error("the sentinel will not accept audio from us "
                          "(direction %s), so it can hear nothing the agent "
                          "says. The call is up; the conversation cannot be.",
                          current)
        log.info("[%6.2fs] media connected", self._clock())

    def _send(self, message: dict) -> None:
        try:
            self._bridge.send(self._session_id, message)
        except Exception:
            log.debug("signalling send failed", exc_info=True)

    async def _poll_loop(self) -> None:
        while not self._closed:
            try:
                messages = await asyncio.to_thread(
                    self._bridge.poll, self._session_id, 5.0)
            except KeyError:
                return
            except Exception:
                log.debug("poll failed", exc_info=True)
                await asyncio.sleep(0.5)
                continue
            for msg in messages or []:
                try:
                    await self._handle(msg)
                except Exception:
                    log.debug("bad signalling message %r", msg, exc_info=True)

    async def _handle(self, msg: dict) -> None:
        from aiortc import RTCSessionDescription

        kind = (msg or {}).get("type")

        if kind == "answer":
            await self._pc.setRemoteDescription(
                RTCSessionDescription(sdp=msg["sdp"], type="answer"))
            self._remote_set = True
            for parked in self._parked:
                await self._add_candidate(parked)
            self._parked.clear()
            log.info("[%6.2fs] answered, connecting media", self._clock())

        elif kind == "candidate" and msg.get("candidate"):
            if self._remote_set:
                await self._add_candidate(msg["candidate"])
            else:
                # Candidates that beat the answer cannot be added yet.
                self._parked.append(msg["candidate"])

        elif kind == "customized" and msg.get("data") == "DENY_CALL":
            log.warning("[%6.2fs] the sentinel hung up", self._clock())
            self._closed = True
            self._connected.set()

    async def _add_candidate(self, raw: str) -> None:
        from aiortc.sdp import candidate_from_sdp

        try:
            text = raw[len("candidate:"):] if raw.startswith("candidate:") else raw
            cand = candidate_from_sdp(text)
            cand.sdpMid = "0"
            cand.sdpMLineIndex = 0
            await self._pc.addIceCandidate(cand)
        except Exception:
            log.debug("rejected candidate %r", str(raw)[:80], exc_info=True)

    def _on_state(self) -> None:
        state = self._pc.connectionState
        log.info("[%6.2fs] peer connection %s", self._clock(), state)
        if state == "connected":
            self._connected.set()
        elif state in ("failed", "closed"):
            self._closed = True
            self._connected.set()

    # ── uplink ───────────────────────────────────────────────────────

    def _on_track(self, track) -> None:
        if track.kind != "audio":
            return
        log.info("[%6.2fs] inbound audio track", self._clock())
        self._tasks.append(asyncio.create_task(self._drain(track)))

    async def _drain(self, track) -> None:
        """Pull frames forever, endpoint them, queue finished utterances."""
        from .audio_utils import resample_pcm16

        while not self._closed:
            try:
                frame = await track.recv()
            except Exception:
                log.info("[%6.2fs] inbound track ended", self._clock())
                return

            pcm = frame.to_ndarray().astype("<i2").tobytes()
            rate = frame.sample_rate or WIRE_RATE
            if rate != self.cfg.sample_rate:
                pcm = resample_pcm16(pcm, rate, self.cfg.sample_rate)

            if self._speaking:
                # Half duplex, and unlike the MQTT path this is exact:
                # we know when our own audio started and stopped instead
                # of inferring it from when a burst happened to arrive.
                self._endpointer.reset()
                continue

            self._meter(pcm)

            utterance = self._endpointer.feed(pcm)
            if utterance:
                self.utterances.put_nowait(utterance)
                log.info("[%6.2fs] QUEUED %.2fs of speech (%d waiting)",
                         self._clock(),
                         len(utterance) / (self.cfg.sample_rate * 2),
                         self.utterances.qsize())

    def _meter(self, pcm: bytes) -> None:
        """Print a level bar a few times a second while listening.

        Asked for directly, and it earns its place: without it the only
        evidence that a microphone is working is a transcript appearing,
        which is exactly what fails. A bar that moves when someone
        speaks separates "not heard" from "heard and misjudged" in one
        glance, and those two have completely different fixes.
        """
        if not getattr(self.cfg, "show_input_level", True) or self._speaking:
            return
        now = time.monotonic()
        level = Endpointer._rms(pcm)
        self._meter_peak = max(getattr(self, "_meter_peak", 0.0), level)
        if now - getattr(self, "_meter_at", 0.0) < 0.25:
            return
        self._meter_at = now
        peak, self._meter_peak = self._meter_peak, 0.0
        # 2000 RMS is a comfortable speaking level at this gain.
        filled = min(24, int(peak / 2000 * 24))
        log.info("    mic |%s%s| %5.0f", "#" * filled, "." * (24 - filled), peak)

    async def receive_utterance(self, timeout: float) -> bytes:
        """Wait for one utterance.

        `timeout` bounds how long the person may take to *begin*, not
        how long they may speak. Once they have started the window stays
        open until they finish, because cutting someone off mid-sentence
        and then asking whether they are there is the single rudest
        thing this system can do -- and it is what a fixed timer does to
        anyone who answers in a full sentence.
        """
        started = time.monotonic()
        deadline = started + timeout
        # A stuck detector must not hold a turn open forever.
        hard_stop = deadline + self.cfg.max_utterance_seconds + 5.0
        extended = False

        log.info("[%6.2fs] LISTEN window open (%.1fs to begin, %d queued)",
                 self._clock(), timeout, self.utterances.qsize())
        while True:
            now = time.monotonic()
            if now >= deadline:
                if not (self._endpointer.in_speech and now < hard_stop):
                    log.warning("[%6.2fs] LISTEN timed out after %.1fs",
                                self._clock(), now - started)
                    return b""
                if not extended:
                    extended = True
                    log.info("[%6.2fs] still speaking -- holding the window "
                             "open", self._clock())
            # Always poll, including while extended. Sleeping instead
            # leaves the finished utterance sitting in the queue unread
            # until the speaker happens to stop, which is the very thing
            # this loop exists to avoid.
            try:
                audio = await asyncio.wait_for(self.utterances.get(), 0.15)
                break
            except asyncio.TimeoutError:
                continue
        self.seconds_since_agent_spoke = (
            time.monotonic() - self._spoke_at if self._spoke_at else None)
        log.info("[%6.2fs] LISTEN closed with %.2fs of speech", self._clock(),
                 len(audio) / (self.cfg.sample_rate * 2))
        return audio

    # ── downlink ─────────────────────────────────────────────────────

    async def send_audio(self, chunks: Iterable[bytes],
                         source_rate: int | None = None) -> float:
        from .audio_utils import resample_pcm16

        source = source_rate or self.cfg.sample_rate
        total = 0
        self._speaking = True
        log.info("[%6.2fs] SPEAK begins (mic gated)", self._clock())
        try:
            for pcm in chunks:
                if not pcm:
                    continue
                if source != WIRE_RATE:
                    pcm = resample_pcm16(pcm, source, WIRE_RATE)
                self._track.queue(pcm)
                total += len(pcm)

            # The track drains in real time, so the agent is still
            # talking after the last chunk is handed over. Returning now
            # would open the microphone into our own tail.
            #
            # Bounded, and the bound is not paranoia: if the peer never
            # negotiated a receiving direction, nothing pulls from the
            # track, it never drains, and an unbounded wait here hangs
            # the whole conversation with no diagnostic beyond silence.
            # That is exactly what happened on the first real call.
            queued = self._track.pending_seconds()
            deadline = time.monotonic() + queued + SPEAK_DRAIN_GRACE
            while self._track.pending_seconds() > 0.0 and not self._closed:
                if time.monotonic() > deadline:
                    stuck = self._track.pending_seconds()
                    self._track.drop_pending()
                    log.error("%.2fs of %.2fs never left the track. The "
                              "sender has stopped pulling: check the "
                              "negotiated audio direction and whether the "
                              "call is still up.", stuck, queued)
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.15)         # jitter buffer at the far end
            return total / (WIRE_RATE * 2)
        finally:
            # The far end is still playing what we sent: it has a jitter
            # buffer, and its loudspeaker shares a room with its
            # microphone. Opening the mic now captures our own voice,
            # which is transcribed and answered as though the person had
            # said it. Stay shut until the room is ours again.
            guard = getattr(self.cfg, "echo_guard_seconds", 1.2)
            if guard > 0:
                await asyncio.sleep(guard)
            self._speaking = False
            self._endpointer.reset()
            self._spoke_at = time.monotonic()
            log.info("[%6.2fs] SPEAK ends (+%.1fs echo guard), mic live again",
                     self._clock(), guard)

    # ── teardown ─────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if not self._session_id:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        # The bye matters more than anything else here: without it the
        # device believes the call is still up, refuses the next offer,
        # and needs a power cycle.
        self._send({"type": "customized", "data": "DENY_CALL"})
        session_id, self._session_id = self._session_id, ""
        try:
            await self._pc.close()
        except Exception:
            log.debug("peer close failed", exc_info=True)
        try:
            self._bridge.close_session(session_id)
        except Exception:
            log.debug("session close failed", exc_info=True)
        log.info("[%6.2fs] call closed", self._clock())

    def close(self) -> None:
        """Synchronous close, to match the SentinelAudioIO protocol."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.aclose())
            else:
                loop.run_until_complete(self.aclose())
        except Exception:
            log.debug("close failed", exc_info=True)
