/* Operator side of the ESP32-P4 intercom.
 *
 * The peer connection lives here rather than in Python because everything a
 * peer needs — microphone, webcam, JPEG encoder, DTLS and SRTP — is already in
 * the browser. The Dash server only relays SDP and candidates to the sentinel
 * over MQTT (see ast_src/webrtc/signaling.py).
 *
 * The contract is fixed by sentinel/mqtt_signaling.c and sentinel/webrtc.c:
 *
 *   - The sentinel is always the callee. It answers, it never offers, so this
 *     side must send the offer and take the controlling ICE role.
 *   - Audio is duplex G.711 A-law at 8 kHz. The offer is munged down to PCMA
 *     alone so esp_peer cannot pick a codec it will then fail to decode.
 *   - Video runs one way, operator to sentinel, and it does not use RTP. With
 *     video_over_data_channel set, esp_webrtc keeps the video m-line out of
 *     the SDP entirely and treats every binary data-channel message as one
 *     complete JPEG (esp_webrtc.c, pc_on_data). There is no frame header: the
 *     SCTP message boundary is the frame boundary. So the offer carries no
 *     video track, and frames go out as whole JPEGs on the data channel.
 *   - Frames must be VIDEO_WIDTH x VIDEO_HEIGHT from settings.h. esp_webrtc
 *     declares the decoder's geometry from those constants instead of reading
 *     it off the wire, so a different size is decoded against the wrong stream
 *     info.
 *
 * The same data channel carries frames the other way. Nothing sends them today
 * (VIDEO_SEND_ENABLED is false), but when the sentinel starts publishing its
 * own low-rate JPEGs they arrive here as binary messages and land in the
 * device canvas without any further change on this side.
 */

(function () {
    "use strict";

    const API = "/webrtc/api";

    /* The sentinel's receive cache is DATA_CH_SEND_CACHE_SIZE, 400 kB. Letting
     * the local buffer grow past a fraction of that trades latency for frames
     * nobody will see, so a backed-up channel drops frames instead. */
    const MAX_BUFFERED_BYTES = 128 * 1024;

    /* How long a peer may stay 'disconnected' before the call is given
     * up. Long enough to ride out a roam or a lost beacon, short enough
     * that the sentinel is released while the operator is still looking
     * at the screen wondering why nothing happened. */
    const DISCONNECT_GRACE_MS = 12000;

    const els = {
        localVideo: "p4-local-video",
        remoteAudio: "p4-remote-audio",
        deviceCanvas: "p4-device-canvas",
        deviceEmpty: "p4-device-empty",
        status: "p4-status",
        stats: "p4-stats",
        micSelect: "p4-mic-select",
        camSelect: "p4-cam-select",
        spkSelect: "p4-spk-select"
    };

    const state = {
        sessionId: null,
        deviceId: null,
        pc: null,
        dc: null,
        deviceDc: null,
        localStream: null,
        remoteStream: null,
        pendingCandidates: [],
        remoteDescriptionSet: false,
        polling: false,
        disconnectTimer: null,
        txTimer: null,
        statsTimer: null,
        canvas: null,
        // True while a file from the video library is driving the uplink
        // instead of the camera. The pump reads the same <video> element
        // either way, so only its start condition needs to know.
        fileMode: false,
        // Mic track parked while a clip's soundtrack is on air, so it can be
        // put back when the clip ends or the operator returns to the camera.
        micTrack: null,
        fileStream: null,
        // maxEdge MUST stay 0. The sentinel does not negotiate inbound video
        // geometry: with video_over_data_channel there is no video m-line in
        // the SDP, and esp_webrtc decodes every inbound JPEG against the
        // compile-time VIDEO_WIDTH x VIDEO_HEIGHT from its settings.h
        // (currently 720x1280). Frames of any other size are decoded against
        // the wrong stream info -- they arrive intact and come out as
        // pixelated bands, and the device eventually resets.
        //
        // So the uplink cannot be made cheaper by scaling here. Sending
        // smaller frames means lowering VIDEO_WIDTH/VIDEO_HEIGHT in
        // settings.h AND webrtc.video_width/height in parameters.json
        // together, then reflashing. Only fps and quality are safe to tune
        // from this side.
        // byteBudget caps each JPEG so a heavy clip costs the link no more
        // than a camera frame does; qLive is the working quality the pump
        // moves between 0.1 and `quality` to stay inside it. 0 disables the
        // budget and pins quality to the slider.
        video: { width: 640, height: 480, fps: 8, quality: 0.6, maxEdge: 0,
                 byteBudget: 14000, qLive: 0.4 },
        /* Send the clip's soundtrack in place of the microphone. Turned off
         * for a while when the sentinel was resetting, but that bisect
         * cleared it -- the resets were frame geometry, and the picture broke
         * up just the same with audio disabled. Back on, because hearing the
         * clip is how the operator knows it has finished.
         *     window.p4SetClipAudio(false)   // to rule it out again
         *     window.p4SetMaxEdge(720)       // 0 = profile geometry */
        clipAudio: true,
        counters: { txFrames: 0, txBytes: 0, dropped: 0, rxFrames: 0 }
    };

    /* ── small helpers ──────────────────────────────────────────────── */

    function el(key) {
        return document.getElementById(els[key]);
    }

    function setStatus(text, tone) {
        const node = el("status");
        if (!node) return;
        node.textContent = text;
        node.className = "fw-bold " + (tone || "text-secondary");
    }

    function setStats(text) {
        const node = el("stats");
        if (node) node.textContent = text;
    }

    async function postJson(path, body) {
        const res = await fetch(API + path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || res.statusText);
        return data;
    }

    function signal(message) {
        if (!state.sessionId) return Promise.resolve();
        return postJson("/signal", {
            session_id: state.sessionId,
            message: message
        }).catch(function (err) {
            console.warn("[p4] signaling send failed:", err);
        });
    }

    /* ── SDP ────────────────────────────────────────────────────────── */

    /* Reduce the audio m-line to PCMA alone.
     *
     * Chrome offers Opus first and esp_peer is configured for G.711 A-law. It
     * would most likely pick PCMA out of the list anyway, but "most likely" on
     * a codec mismatch means silence with no error, so the choice is removed. */
    function forcePcma(sdp) {
        const eol = sdp.indexOf("\r\n") >= 0 ? "\r\n" : "\n";
        const lines = sdp.split(/\r?\n/);

        let mIndex = -1;
        for (let i = 0; i < lines.length; i++) {
            if (lines[i].indexOf("m=audio ") === 0) { mIndex = i; break; }
        }
        if (mIndex < 0) return sdp;

        let end = lines.length;
        for (let i = mIndex + 1; i < lines.length; i++) {
            if (lines[i].indexOf("m=") === 0) { end = i; break; }
        }

        let pcma = null;
        for (let i = mIndex + 1; i < end; i++) {
            const match = /^a=rtpmap:(\d+)\s+([^/]+)\//i.exec(lines[i]);
            if (match && match[2].toLowerCase() === "pcma") {
                pcma = match[1];
                break;
            }
        }
        if (!pcma) {
            console.warn("[p4] no PCMA in the offer, leaving the codec list alone");
            return sdp;
        }

        const header = lines[mIndex].split(" ");
        lines[mIndex] = header.slice(0, 3).concat([pcma]).join(" ");

        const kept = [];
        for (let i = mIndex + 1; i < end; i++) {
            const match = /^a=(?:rtpmap|fmtp|rtcp-fb):(\d+)/.exec(lines[i]);
            if (match && match[1] !== pcma) continue;
            kept.push(lines[i]);
        }
        return lines.slice(0, mIndex + 1).concat(kept, lines.slice(end)).join(eol);
    }

    /* ── device pickers ─────────────────────────────────────────────── */

    async function populateDevicePickers() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
        let devices;
        try {
            devices = await navigator.mediaDevices.enumerateDevices();
        } catch (err) {
            return;
        }

        const fill = function (node, kind, fallbackLabel) {
            if (!node) return;
            const previous = node.value;
            node.innerHTML = "";
            const auto = document.createElement("option");
            auto.value = "";
            auto.textContent = fallbackLabel;
            node.appendChild(auto);
            devices.filter(function (d) { return d.kind === kind; })
                .forEach(function (d, i) {
                    const opt = document.createElement("option");
                    opt.value = d.deviceId;
                    // Labels stay blank until a permission grant, so fall back
                    // to an index rather than showing a row of empty entries.
                    opt.textContent = d.label || (fallbackLabel + " " + (i + 1));
                    node.appendChild(opt);
                });
            if (previous) node.value = previous;
        };

        fill(el("micSelect"), "audioinput", "System default microphone");
        fill(el("camSelect"), "videoinput", "System default camera");
        fill(el("spkSelect"), "audiooutput", "System default speaker");
    }

    /* ── media ──────────────────────────────────────────────────────── */

    async function openLocalMedia() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error("This browser exposes no camera or microphone. " +
                "Open the dashboard over HTTPS, not by IP over plain HTTP.");
        }

        const micNode = el("micSelect");
        const camNode = el("camSelect");
        const micId = micNode && micNode.value;
        const camId = camNode && camNode.value;

        const audio = {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            channelCount: 1
        };
        if (micId) audio.deviceId = { exact: micId };

        const video = {
            width: { ideal: state.video.width },
            height: { ideal: state.video.height },
            frameRate: { ideal: state.video.fps, max: 15 }
        };
        if (camId) video.deviceId = { exact: camId };

        let stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({ audio: audio, video: video });
        } catch (err) {
            // A missing or busy camera must not block the call: audio is the
            // half the sentinel actually needs.
            console.warn("[p4] camera unavailable, continuing audio only:", err);
            stream = await navigator.mediaDevices.getUserMedia({ audio: audio });
        }

        state.localStream = stream;
        const preview = el("localVideo");
        // When a library clip is driving the uplink, the camera is still
        // opened -- the call needs its microphone -- but the preview keeps
        // showing the clip. Attaching the camera stream here would silently
        // replace it and the sentinel would receive the webcam instead.
        if (preview && !state.fileMode) {
            preview.srcObject = stream;
            preview.muted = true;
            preview.playsInline = true;
            preview.play().catch(function () { /* autoplay policy, harmless */ });
        }
        // Labels only become readable once permission has been granted.
        populateDevicePickers();
        return stream;
    }

    function stopLocalMedia() {
        if (state.localStream) {
            state.localStream.getTracks().forEach(function (t) { t.stop(); });
            state.localStream = null;
        }
        const preview = el("localVideo");
        if (preview) preview.srcObject = null;
    }

    /* ── video uplink ───────────────────────────────────────────────── */

    function startVideoPump() {
        stopVideoPump();
        // A library clip is a valid source even when no camera was ever
        // opened -- the pump only ever reads pixels off the <video> element.
        if (!state.fileMode &&
            (!state.localStream || state.localStream.getVideoTracks().length === 0)) return;

        if (!state.canvas) state.canvas = document.createElement("canvas");
        const ctx = state.canvas.getContext("2d");
        let sized = false;

        // One encode-and-send at a time. toBlob() and blob.arrayBuffer() are
        // both asynchronous, so a fixed-interval pump starts a new encode
        // without waiting for the last to finish. A webcam frame of a static
        // room encodes fast and evenly, so those never overlap and the camera
        // path looks perfect. A detailed, moving clip encodes slower and
        // unevenly, several finish together, and they leave as a burst --
        // which is what fills the sentinel's receive cache and makes its
        // renderer drop sync a couple of seconds in. Serialising here makes a
        // clip behave the way the camera already does.
        let inFlight = false;

        state.txTimer = setInterval(function () {
            const dc = state.dc;
            const source = el("localVideo");
            // Ticks vs frames sent tells throttling apart from slow encoding.
            // Chromium throttles timers hard in a page it thinks is hidden,
            // and an occluded WebView looks exactly like a jammed encoder
            // from the counters alone.
            state.counters.ticks = (state.counters.ticks || 0) + 1;
            if (inFlight) { state.counters.inflightSkips =
                                (state.counters.inflightSkips || 0) + 1; return; }
            if (!dc || dc.readyState !== "open") return;
            if (!source || source.readyState < 2) return;

            // Size the canvas from the source the first time it is readable.
            // Doing this up front used to force every frame to the profile's
            // 720x1280 even when the source was smaller, so a 400x720 clip
            // was upscaled and we paid ~2x the bytes to encode invented
            // pixels. Fit inside the profile box, keep the aspect ratio, and
            // never scale up.
            if (!sized && source.videoWidth && source.videoHeight) {
                // maxEdge 0 restores the pre-change behaviour exactly: the
                // profile's own dimensions, no scaling, no alignment.
                if (!state.video.maxEdge) {
                    state.canvas.width = state.video.width;
                    state.canvas.height = state.video.height;
                    sized = true;
                    console.log("[p4] uplink at profile size " +
                                state.canvas.width + "x" + state.canvas.height);
                    return;
                }
                const box = Math.max(64, state.video.maxEdge || 480);
                const sw = source.videoWidth, sh = source.videoHeight;
                const scale = Math.min(box / Math.max(sw, sh), 1);
                // Snap to a multiple of 16, not merely to an even number.
                // The sentinel decodes these JPEGs in hardware, and its
                // decoder works in 16-pixel MCU blocks: a width like 266
                // decodes as torn, pixelated bands. 720x1280 happened to be
                // 16-aligned, which is why the original size looked right.
                const align = function (v) {
                    return Math.max(16, Math.round(v / 16) * 16);
                };
                state.canvas.width = align(sw * scale);
                state.canvas.height = align(sh * scale);
                sized = true;
                console.log("[p4] uplink " + sw + "x" + sh + " -> " +
                            state.canvas.width + "x" + state.canvas.height);
            }
            if (!sized) return;

            // Pace on drain, not on the clock. Sending at a fixed rate put
            // ~420 KB/s into a link that carries about 70, so the sentinel's
            // SCTP receive cache backed up (visible as "Cached: 16240"), its
            // renderer reported "Video lost sync", and a picture that started
            // clean fell apart after a couple of seconds. Keeping at most one
            // frame in flight lets the transmitter run at exactly whatever
            // the device can absorb: fewer frames, but all of them decodable.
            // Pace against roughly one frame in flight, NOT against zero.
            // Requiring a fully drained channel starved the pump completely:
            // SCTP rarely reports bufferedAmount back down to exactly 0, so
            // the sentinel logged d_out:0 / din:0 for a whole call and no
            // video was sent at all -- including from the camera.
            // How much may be outstanding at once. This is a separate dial
            // from byteBudget, which sizes a frame: tying the two together
            // meant the only way to allow more in flight was to send fatter
            // frames. What the uplink sustains is (bytes in flight / round
            // trip), so with ~18 kB allowed and a couple of hundred ms of
            // RTT it cannot exceed roughly 70 kB/s however fast the network
            // underneath actually is -- a bandwidth-delay limit, not a link
            // one. Default keeps the historical byteBudget * 2.
            var inflightMax = state.video.inflightMax ||
                              (state.video.byteBudget || 16000) * 2;
            if (dc.bufferedAmount > inflightMax) {
                state.counters.dropped++;
                return;
            }
            if (dc.bufferedAmount > MAX_BUFFERED_BYTES) {
                state.counters.dropped++;
                return;
            }

            const encodeStartedAt = performance.now();
            ctx.drawImage(source, 0, 0, state.canvas.width, state.canvas.height);
            state.counters.drawMs = Math.round(performance.now() - encodeStartedAt);
            inFlight = true;
            state.canvas.toBlob(function (blob) {
                if (!blob || !state.dc || state.dc.readyState !== "open") {
                    inFlight = false;
                    return;
                }

                // Hold each frame under a byte budget rather than at a fixed
                // quality. The webcam and a library clip travel the identical
                // path -- there is no video track, both are JPEGs on this data
                // channel at the same geometry -- so the only reason a clip
                // stalls where the camera does not is that it encodes heavier:
                // a detailed, moving scene runs ~35 kB a frame where a still
                // room compresses to a fraction of it. Trading sharpness for
                // a steady rate is the explicit intent here.
                const budget = state.video.byteBudget || 0;
                if (budget > 0) {
                    if (blob.size > budget && state.video.qLive > 0.1) {
                        state.video.qLive = Math.max(0.1, state.video.qLive - 0.05);
                    } else if (blob.size < budget * 0.6 &&
                               state.video.qLive < state.video.quality) {
                        state.video.qLive = Math.min(state.video.quality,
                                                     state.video.qLive + 0.02);
                    }
                }

                blob.arrayBuffer().then(function (buf) {
                    try {
                        state.dc.send(buf);
                        state.counters.encodeMs =
                            Math.round(performance.now() - encodeStartedAt);
                        state.counters.txFrames++;
                        state.counters.txBytes += buf.byteLength;
                        state.counters.lastBytes = buf.byteLength;
                    } catch (err) {
                        state.counters.dropped++;
                    }
                    inFlight = false;
                }, function () {
                    state.counters.dropped++;
                    inFlight = false;   // never leave the pump wedged
                });
            }, "image/jpeg", state.video.qLive || state.video.quality);
        }, Math.max(40, Math.round(1000 / state.video.fps)));
    }

    function stopVideoPump() {
        if (state.txTimer) {
            clearInterval(state.txTimer);
            state.txTimer = null;
        }
    }

    /* ── video library uplink ───────────────────────────────────────── */

    /* Point the preview element at a file and let the existing JPEG pump do
     * the rest. Nothing about the peer connection, the data channel or the
     * firmware changes: the sentinel keeps receiving whole JPEGs and cannot
     * tell whether they came from a camera or a clip.
     *
     * The clip is muted deliberately. Its soundtrack is not part of the
     * uplink -- the call carries the operator's microphone, and browsers
     * block autoplay of anything audible. */

    /* Put the clip's own soundtrack on the call in place of the microphone.
     * The sentinel's audio m-line is already negotiated, so swapping the
     * sender's track keeps the same RTP stream and needs no renegotiation.
     * The mic track is parked so it can be restored afterwards. */
    function swapToFileAudio() {
        const v = el("localVideo");
        if (!state.clipAudio) return;      // see state.clipAudio above
        if (!v || !state.pc) return;
        try {
            if (!state.fileStream) {
                state.fileStream = v.captureStream ? v.captureStream()
                    : (v.mozCaptureStream ? v.mozCaptureStream() : null);
            }
            const clipAudio = state.fileStream && state.fileStream.getAudioTracks()[0];
            if (!clipAudio) {
                console.warn("[p4] clip has no audio track");
                return;
            }
            const sender = state.pc.getSenders().find(function (s) {
                return s.track && s.track.kind === "audio";
            });
            if (!sender) return;
            if (!state.micTrack) state.micTrack = sender.track;
            sender.replaceTrack(clipAudio).catch(function (err) {
                console.warn("[p4] could not put clip audio on the call:", err);
            });
        } catch (err) {
            console.warn("[p4] cannot capture clip audio:", err);
        }
    }

    function restoreMicAudio() {
        if (state.pc && state.micTrack) {
            const sender = state.pc.getSenders().find(function (s) {
                return s.track && s.track.kind === "audio";
            });
            if (sender) sender.replaceTrack(state.micTrack).catch(function () { });
        }
        state.micTrack = null;
        state.fileStream = null;
    }

    function playFileToSentinel(sel, deviceId, fps, quality) {
        const v = el("localVideo");
        if (!v) return "The local preview element is missing.";

        v.srcObject = null;
        // The element carries autoplay for the camera preview, which would
        // start the clip the instant src is set -- i.e. while the call is
        // still being negotiated. Those first seconds would play out locally
        // and never reach the sentinel. Hold playback until there is a data
        // channel to carry it.
        v.autoplay = false;
        v.src = sel.url;
        // Play once, not on a loop: the clip running out is what ends the
        // session.
        v.loop = false;
        // Muted unless its soundtrack is actually being sent. Unmuting play
        // the clip through the operator's speakers and straight back into the
        // microphone, which puts a continuous audio stream on the call. The
        // sentinel renders video against that audio clock while our frames
        // carry no PTS at all ("Video lost sync now:0 audio:..."), so the
        // louder the room, the sooner the picture breaks up.
        v.muted = !state.clipAudio;
        v.playsInline = true;
        v.crossOrigin = "anonymous";
        state.fileMode = true;
        state.fileStream = null;

        v.onended = function () {
            state.fileMode = false;
            restoreMicAudio();
            hangup("Clip finished");
            const node = document.getElementById("p4_lib_status");
            if (node) node.textContent = sel.name + " finished — call ended.";
        };

        // Already talking to the sentinel: start straight away and swap both
        // what the pump reads and what the audio sender carries.
        if (state.dc && state.dc.readyState === "open") {
            startClipPlayback(sel);
            swapToFileAudio();
            startVideoPump();
            return "Sending " + sel.name + " (with sound) to " +
                   (state.deviceId || "the sentinel") + ".";
        }

        // Otherwise place the call ourselves rather than making the operator
        // do it in the right order. fileMode is already set, so the data
        // channel's onopen handler starts the pump on the clip, and
        // openLocalMedia leaves the preview alone.
        if (!deviceId) {
            return "Pick a sentinel first — there is nobody to send " + sel.name + " to.";
        }

        // Held at the first frame; the data channel's onopen starts it, so
        // the sentinel sees the clip from the beginning rather than joining
        // it several seconds in.
        state.pendingClip = sel;
        call(deviceId, { fps: fps, quality: quality });
        return "Calling " + deviceId + " — " + sel.name +
               " starts as soon as the call connects.";
    }

    /* Begin playback and report a genuine failure. A rejected play() is not
     * always one: browsers reject it in throttled background tabs and then
     * resume by themselves, so only complain if the clip really is stopped. */
    function startClipPlayback(sel) {
        const v = el("localVideo");
        if (!v) return;
        state.pendingClip = null;
        v.play().catch(function (err) {
            console.warn("[p4] cannot play library clip:", err);
            setTimeout(function () {
                if (!v.paused) return;
                const node = document.getElementById("p4_lib_status");
                if (node) {
                    node.textContent = "Could not play " +
                        ((sel && sel.name) || "this file") + ": " + err.message;
                }
            }, 800);
        });
    }

    function restoreCameraUplink() {
        const v = el("localVideo");
        state.fileMode = false;
        restoreMicAudio();

        state.pendingClip = null;
        if (v) {
            v.onended = null;
            v.pause();
            v.removeAttribute("src");
            v.loop = false;
            v.muted = true;
            v.autoplay = true;      // the camera preview wants it back
            v.load();               // drops the decoded file, frees the buffer
        }

        if (state.localStream && state.localStream.getVideoTracks().length) {
            if (v) {
                v.srcObject = state.localStream;
                v.play().catch(function () { /* autoplay policy, harmless */ });
            }
            startVideoPump();
            return "Uplink is back on the camera.";
        }

        stopVideoPump();
        return "Stopped. No camera is open, so nothing is being sent.";
    }

    /* ── video downlink (reserved for the sentinel's own low-rate feed) ── */

    function renderDeviceFrame(buffer) {
        const canvas = el("deviceCanvas");
        if (!canvas) return;
        const blob = new Blob([buffer], { type: "image/jpeg" });
        createImageBitmap(blob).then(function (bitmap) {
            const placeholder = el("deviceEmpty");
            if (placeholder) placeholder.style.display = "none";
            canvas.style.display = "block";
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
            canvas.getContext("2d").drawImage(bitmap, 0, 0);
            bitmap.close();
            state.counters.rxFrames++;
        }).catch(function () { /* a truncated frame, wait for the next one */ });
    }

    function attachChannel(channel, isDeviceChannel) {
        channel.binaryType = "arraybuffer";
        channel.onmessage = function (event) {
            if (typeof event.data === "string") {
                console.log("[p4] channel says:", event.data);
                return;
            }
            renderDeviceFrame(event.data);
        };
        if (!isDeviceChannel) {
            channel.onopen = function () {
                // A clip held back for this moment starts now, so the sentinel
                // receives it from its first frame.
                if (state.pendingClip) startClipPlayback(state.pendingClip);
                // When a clip triggered this call, its soundtrack replaces the
                // microphone as soon as there is a sender to swap.
                if (state.fileMode) swapToFileAudio();
                startVideoPump();
            };
            channel.onclose = function () { stopVideoPump(); };
        }
    }

    /* ── signaling loop ─────────────────────────────────────────────── */

    async function handleMessage(msg) {
        if (!msg || !msg.type) return;

        if (msg.type === "answer") {
            await state.pc.setRemoteDescription({ type: "answer", sdp: msg.sdp });
            state.remoteDescriptionSet = true;
            // Candidates that arrived while the answer was in flight cannot be
            // added until now, so they were parked.
            const parked = state.pendingCandidates.splice(0);
            for (const candidate of parked) {
                await state.pc.addIceCandidate(candidate).catch(function () {});
            }
            setStatus("Answered, connecting media\u2026", "text-info");
            return;
        }

        if (msg.type === "candidate" && msg.candidate) {
            const candidate = {
                candidate: msg.candidate,
                sdpMid: "0",
                sdpMLineIndex: 0
            };
            if (!state.remoteDescriptionSet) {
                state.pendingCandidates.push(candidate);
            } else {
                await state.pc.addIceCandidate(candidate).catch(function (err) {
                    console.warn("[p4] rejected candidate:", err);
                });
            }
            return;
        }

        if (msg.type === "customized") {
            if (msg.data === "ACCEPT_CALL") setStatus("Sentinel accepted the call", "text-info");
            else if (msg.data === "DENY_CALL") await hangup("Sentinel hung up");
            return;
        }

        if (msg.type === "bye") {
            await hangup("Sentinel ended the call");
        }
    }

    async function pollLoop() {
        state.polling = true;
        while (state.polling && state.sessionId) {
            const sessionId = state.sessionId;
            let data;
            try {
                const res = await fetch(
                    API + "/poll?session_id=" + encodeURIComponent(sessionId) + "&timeout=20");
                if (res.status === 404) break;   // reaped server-side
                data = await res.json();
            } catch (err) {
                if (!state.polling) break;
                // A dropped poll is normal on a page suspend; back off and retry.
                await new Promise(function (r) { setTimeout(r, 1000); });
                continue;
            }
            for (const msg of (data.messages || [])) {
                try {
                    await handleMessage(msg);
                } catch (err) {
                    console.warn("[p4] could not apply", msg.type, err);
                }
            }
        }
        state.polling = false;
    }

    /* ── stats ──────────────────────────────────────────────────────── */

    function startStatsLoop() {
        stopStatsLoop();
        state.statsTimer = setInterval(async function () {
            if (!state.pc) return;
            let rtt = null;
            let inboundKbps = null;
            try {
                const report = await state.pc.getStats();
                report.forEach(function (s) {
                    if (s.type === "candidate-pair" && s.nominated && s.currentRoundTripTime != null) {
                        rtt = Math.round(s.currentRoundTripTime * 1000);
                    }
                    if (s.type === "inbound-rtp" && s.kind === "audio" && s.bytesReceived != null) {
                        const now = s.timestamp;
                        if (state._lastAudio && now > state._lastAudio.t) {
                            const bits = (s.bytesReceived - state._lastAudio.b) * 8;
                            inboundKbps = Math.round(bits / (now - state._lastAudio.t));
                        }
                        state._lastAudio = { b: s.bytesReceived, t: now };
                    }
                });
            } catch (err) { /* stats are cosmetic */ }

            const c = state.counters;
            const bits = [
                "uplink " + c.txFrames + " frames",
                (c.txBytes / 1024).toFixed(0) + " kB",
                c.dropped + " dropped"
            ];
            if (c.rxFrames) bits.push("downlink " + c.rxFrames + " frames");
            if (rtt != null) bits.push(rtt + " ms rtt");
            if (inboundKbps != null) bits.push(inboundKbps + " kbps audio in");
            setStats(bits.join("  \u00b7  "));
        }, 1000);
    }

    function stopStatsLoop() {
        if (state.statsTimer) {
            clearInterval(state.statsTimer);
            state.statsTimer = null;
        }
        state._lastAudio = null;
    }

    /* ── call control ───────────────────────────────────────────────── */

    async function call(deviceId, options) {
        if (state.pc) await hangup();
        if (!deviceId) {
            setStatus("Select a sentinel first", "text-warning");
            return;
        }

        state.counters = { txFrames: 0, txBytes: 0, dropped: 0, rxFrames: 0 };
        state.pendingCandidates = [];
        state.remoteDescriptionSet = false;
        state.deviceId = deviceId;

        setStatus("Opening signaling\u2026", "text-info");
        let session;
        try {
            session = await postJson("/session", { device_id: deviceId });
        } catch (err) {
            setStatus("Signaling unavailable: " + err.message, "text-danger");
            return;
        }
        state.sessionId = session.session_id;

        // Geometry comes from the server because it has to match settings.h;
        // frame rate and quality are the operator's to trade off.
        Object.assign(state.video, session.video || {});
        if (options && options.fps) state.video.fps = options.fps;
        if (options && options.quality) state.video.quality = options.quality;

        try {
            await openLocalMedia();
        } catch (err) {
            setStatus(err.message, "text-danger");
            await hangup();
            return;
        }

        const pc = new RTCPeerConnection({ iceServers: session.ice_servers || [] });
        state.pc = pc;

        const audioTrack = state.localStream.getAudioTracks()[0];
        if (audioTrack) pc.addTrack(audioTrack, state.localStream);

        // Deliberately no video track: with video_over_data_channel the
        // sentinel negotiates no video m-line, and offering one invites an
        // answer esp_peer cannot build.
        state.dc = pc.createDataChannel("video", { ordered: true });
        attachChannel(state.dc, false);

        pc.ondatachannel = function (event) {
            state.deviceDc = event.channel;
            attachChannel(event.channel, true);
        };

        pc.ontrack = function (event) {
            state.remoteStream = event.streams[0];
            const sink = el("remoteAudio");
            if (!sink) return;
            sink.srcObject = state.remoteStream;
            const spk = el("spkSelect");
            if (spk && spk.value && sink.setSinkId) {
                sink.setSinkId(spk.value).catch(function () { /* not permitted */ });
            }
            sink.play().catch(function () { /* autoplay policy */ });
        };

        pc.onicecandidate = function (event) {
            if (event.candidate) {
                signal({ type: "candidate", candidate: event.candidate.candidate });
            }
        };

        /* A connection that dies has to be hung up, not merely reported.
         *
         * Signaling runs over MQTT and is completely independent of the ICE
         * path, so a bye still reaches the sentinel long after media has
         * stopped flowing. Without one it goes on believing the call is up:
         * it then refuses the next offer -- ringing forever with no answer --
         * and only a power cycle clears it. Every "the P4 needs rebooting
         * after a failure" is this.
         *
         * failed is terminal, so hang up at once. disconnected often is not
         * -- a lost Wi-Fi beacon or a network change recovers by itself
         * within seconds -- so give it a grace period and only hang up if it
         * has not come back, otherwise a brief blip ends a good call. */
        pc.onconnectionstatechange = function () {
            const s = pc.connectionState;
            if (s !== "disconnected" && state.disconnectTimer) {
                clearTimeout(state.disconnectTimer);
                state.disconnectTimer = null;
            }
            if (s === "connected") setStatus("Connected", "text-success");
            else if (s === "connecting") setStatus("Negotiating\u2026", "text-info");
            else if (s === "failed") {
                setStatus("Connection failed", "text-danger");
                hangup("Connection failed");
            } else if (s === "disconnected") {
                setStatus("Peer unreachable", "text-warning");
                if (!state.disconnectTimer) {
                    state.disconnectTimer = setTimeout(function () {
                        state.disconnectTimer = null;
                        if (state.pc && state.pc.connectionState === "disconnected") {
                            hangup("Peer unreachable");
                        }
                    }, DISCONNECT_GRACE_MS);
                }
            }
        };

        const offer = await pc.createOffer({
            offerToReceiveAudio: true,
            offerToReceiveVideo: false
        });
        offer.sdp = forcePcma(offer.sdp);
        await pc.setLocalDescription(offer);

        setStatus("Ringing " + deviceId + "\u2026", "text-info");
        pollLoop();
        await signal({ type: "offer", sdp: pc.localDescription.sdp });
        // RING only drives the announcement tone; webrtc.c answers regardless.
        await signal({ type: "customized", data: "RING" });
        startStatsLoop();
    }

    async function hangup(reason) {
        if (state.disconnectTimer) {
            clearTimeout(state.disconnectTimer);
            state.disconnectTimer = null;
        }
        stopVideoPump();
        stopStatsLoop();
        state.polling = false;

        if (state.sessionId) {
            await signal({ type: "customized", data: "DENY_CALL" });
            const sessionId = state.sessionId;
            state.sessionId = null;
            await postJson("/close", { session_id: sessionId }).catch(function () {});
        }

        if (state.dc) { try { state.dc.close(); } catch (e) {} state.dc = null; }
        if (state.deviceDc) { try { state.deviceDc.close(); } catch (e) {} state.deviceDc = null; }
        if (state.pc) { try { state.pc.close(); } catch (e) {} state.pc = null; }

        stopLocalMedia();
        const sink = el("remoteAudio");
        if (sink) sink.srcObject = null;

        const canvas = el("deviceCanvas");
        if (canvas) canvas.style.display = "none";
        const placeholder = el("deviceEmpty");
        if (placeholder) placeholder.style.display = "";

        setStats("");
        setStatus(reason || "Idle", "text-secondary");
    }

    function setMicEnabled(enabled) {
        if (!state.localStream) return;
        state.localStream.getAudioTracks().forEach(function (t) { t.enabled = enabled; });
    }

    function setCameraEnabled(enabled) {
        if (!state.localStream) return;
        state.localStream.getVideoTracks().forEach(function (t) { t.enabled = enabled; });
        if (enabled) startVideoPump(); else stopVideoPump();
    }

    window.addEventListener("beforeunload", function () {
        if (state.sessionId) {
            navigator.sendBeacon(
                API + "/close",
                new Blob([JSON.stringify({ session_id: state.sessionId })],
                    { type: "application/json" }));
        }
    });

    window.P4Intercom = {
        call: call,
        hangup: hangup,
        setMicEnabled: setMicEnabled,
        setCameraEnabled: setCameraEnabled,
        populateDevicePickers: populateDevicePickers,
        isActive: function () { return state.pc !== null; }
    };

    /* ── Dash bindings ──────────────────────────────────────────────── */

    window.dash_clientside = Object.assign({}, window.dash_clientside, {
        p4intercom: {
            control: function (callClicks, hangupClicks, deviceId, fps, quality) {
                const ctx = window.dash_clientside.callback_context;
                const trigger = ctx && ctx.triggered && ctx.triggered.length
                    ? ctx.triggered[0].prop_id.split(".")[0]
                    : null;

                if (trigger === "p4_call_btn") {
                    call(deviceId, { fps: fps, quality: quality });
                    return "active";
                }
                if (trigger === "p4_hangup_btn") {
                    hangup("Call ended");
                    return "idle";
                }
                return window.dash_clientside.no_update;
            },

            toggles: function (micOn, camOn) {
                setMicEnabled(!!micOn);
                setCameraEnabled(!!camOn);
                return window.dash_clientside.no_update;
            },

            listDevices: function () {
                populateDevicePickers();
                return window.dash_clientside.no_update;
            },

            playFile: function (_clicks, selected, deviceId, fps, quality) {
                if (!selected || !selected.url) return "Pick a clip first.";
                return playFileToSentinel(selected, deviceId, fps, quality);
            },

            playCamera: function () {
                return restoreCameraUplink();
            }
        }
    });

    /* Console handles for bisecting the sentinel reset without an edit and a
     * reload between each attempt. Both take effect on the next Send. */
    window.p4SetClipAudio = function (on) {
        state.clipAudio = !!on;
        console.log("[p4] clip audio " + (state.clipAudio ? "ON" : "OFF"));
    };
    window.p4SetMaxEdge = function (px) {
        state.video.maxEdge = Number(px) || 0;
        console.log("[p4] uplink maxEdge = " + (state.video.maxEdge || "profile size"));
    };
    window.p4SetBudget = function (bytes) {
        state.video.byteBudget = Number(bytes) || 0;
        console.log("[p4] frame budget = " +
                    (state.video.byteBudget || "off (fixed quality)"));
    };
    /* Set the working quality directly, and optionally the ceiling it may
     * climb back to.
     *
     * qLive starts at 0.4, which at 720x1280 encodes to about 40 kB -- four
     * times a typical budget. Two of those fill the send buffer before the
     * budget loop, which only steps down 0.05 per *sent* frame, has any
     * chance to react; the channel is then jammed for about ten seconds
     * while it drains, and the call opens on a frozen picture. Starting at
     * the quality the link can actually carry skips that entirely, and the
     * loop climbs back up if there turns out to be room.
     *
     * Call it before P4Intercom.call(): the profile the server sends sets
     * `quality` but never qLive, so a value set here survives. */
    window.p4SetInflight = function (bytes) {
        state.video.inflightMax = Number(bytes) || 0;
        console.log("[p4] in-flight allowance = " +
                    (state.video.inflightMax || "default (budget x2)"));
    };
    window.p4SetQuality = function (q, ceiling) {
        state.video.qLive = Math.min(1, Math.max(0.05, Number(q) || 0.1));
        if (ceiling) {
            state.video.quality = Math.min(1, Math.max(0.05, Number(ceiling)));
        }
        console.log("[p4] quality now " + state.video.qLive +
                    ", ceiling " + state.video.quality);
    };
    window.p4Stats = function () {
        return {
            counters: state.counters,
            lastFrameKB: state.counters.lastBytes
                ? +(state.counters.lastBytes / 1024).toFixed(1) : null,
            qualityLive: state.video.qLive,
            byteBudget: state.video.byteBudget,
            inflightMax: state.video.inflightMax,
            ticks: state.counters.ticks,
            inflightSkips: state.counters.inflightSkips,
            drawMs: state.counters.drawMs,
            encodeMs: state.counters.encodeMs,
            canvas: state.canvas ? (state.canvas.width + "x" + state.canvas.height) : null,
            clipAudio: state.clipAudio,
            maxEdge: state.video.maxEdge,
            dcState: state.dc ? state.dc.readyState : null,
            buffered: state.dc ? state.dc.bufferedAmount : null
        };
    };

})();
