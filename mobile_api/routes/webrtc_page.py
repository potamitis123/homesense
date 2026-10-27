"""
The intercom page the Android app drives in a WebView.

Why a WebView at all, when the app already has a native peer connection:
esp_peer's SCTP and the community libwebrtc build the app links (M125)
do not agree on the state cookie, so the data channel never opens --

    dcsctp_transport.cc: OnError(error=PARSE_FAILED,
                                 message=Failed to parse state cookie)

-- and video rides that channel (video_over_data_channel), so nothing
gets sent while audio, on RTP, is fine. The role that would sidestep it
cannot be forced: an offerer must use a=setup:actpass (RFC 8842 5.2.1),
and libwebrtc enforces that on setRemoteDescription. There is no newer
release of the Android package to move to.

Chrome negotiates with this same firmware every day from the Visuals
tab. Android's System WebView *is* Chromium, so serving the dashboard's
own webrtc_intercom_v2.js here puts the tablet's camera on exactly the
implementation already known to interoperate, rather than on a second
one that has to be kept in step.

The script is served byte-for-byte from assets/, not forked. It hardcodes
the dashboard's unauthenticated /webrtc/api prefix, so the page installs
a fetch shim that rewrites those calls onto mobile_api's authenticated
/api/v1/webrtc endpoints and attaches the bearer token. Nothing about the
media path -- the JPEG pump, the byte budget, the PCMA munging -- is
touched or duplicated.

The shell itself carries no data and needs no auth; every call it makes
does. The token is injected by the app after load (see
VideoCallWebViewScreen) rather than passed in the URL, so it stays out of
history, logs and referrers.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse, Response

from mobile_api.logging_setup import get_logger, log_extra

logger = get_logger()

router = APIRouter(prefix="/webrtc", tags=["webrtc-page"])

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_INTERCOM_JS = os.path.join(_PROJECT_ROOT, "assets", "webrtc_intercom_v2.js")


@router.get("/intercom.js")
def intercom_js() -> Response:
    """The dashboard's own intercom script, unmodified."""
    try:
        with open(_INTERCOM_JS, "r", encoding="utf-8") as fh:
            body = fh.read()
    except OSError as e:
        logger.error(
            f"cannot read {_INTERCOM_JS}: {e}", extra=log_extra("MOBILE_WEBRTC")
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="intercom script is missing on the server",
        )
    # no-store: the app is updated by reinstalling, and a WebView holding
    # a stale copy of this file would be invisible and confusing.
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Sentinel intercom</title>
<style>
  html, body {
    margin: 0; padding: 0; height: 100%;
    background: #000; color: #eee;
    font-family: system-ui, sans-serif;
    overflow: hidden;
  }
  /* Both boxes are position:fixed with all four edges given, so their
     height comes straight from the viewport. The previous version nested
     an absolute video inside an absolute #wrap and sized it with
     calc(100% - 64px); this WebView did not apply the wrapper's `inset`
     shorthand, so the wrapper had no height, the percentage resolved
     against zero, and the preview measured 654x0 -- decoding perfectly and
     drawing nothing. No percentage heights here, and no `inset`. */
  #p4-local-video {
    /* height is given explicitly rather than implied by top+bottom. A
       <video> is a replaced element, so with height:auto it takes its
       intrinsic ratio and ignores the two offsets -- at 720x1280 that made
       it 1894px tall inside a 321px viewport. Percentages are safe here in
       a way they were not before, because position:fixed resolves them
       against the viewport rather than an ancestor of unknown height. */
    position: fixed; left: 0; right: 0; top: 0;
    width: 100%; height: calc(100% - 64px);
    object-fit: cover; background: #111;
    /* Mirrored the way a mirror is, for the local preview only -- what the
       sentinel receives is drawn from the unmirrored frame. */
    transform: scaleX(-1);
  }
  #bar {
    position: fixed; left: 0; right: 0; bottom: 0; height: 64px;
    box-sizing: border-box; padding: 10px 14px;
    font-size: 14px; background: #111;
  }
  #p4-status { font-weight: 600; }
  #p4-stats { opacity: 0.65; font-size: 12px; margin-top: 3px; }
  /* Present because the script addresses them; the sentinel sends no
     video back today (VIDEO_SEND_ENABLED is false). */
  #p4-device-canvas, #p4-device-empty { display: none; }
</style>
</head>
<body>
<div id="wrap">
  <video id="p4-local-video" autoplay muted playsinline></video>
  <audio id="p4-remote-audio" autoplay></audio>
  <canvas id="p4-device-canvas"></canvas>
  <div id="p4-device-empty"></div>
  <div id="bar">
    <div id="p4-status">Idle</div>
    <div id="p4-stats"></div>
  </div>
</div>

<script>
/* Rewrite the dashboard's endpoints onto mobile_api's, and authenticate
 * them. intercom.js is served verbatim and still calls /webrtc/api/... ,
 * which on this server is /api/v1/webrtc/... behind a bearer token. Doing
 * it here keeps that script identical to the one the browser runs. */
(function () {
  "use strict";
  var DASH_PREFIX = "/webrtc/api";
  var MOBILE_PREFIX = "/api/v1/webrtc";
  var token = null;

  var nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = (typeof input === "string") ? input : (input && input.url);
    if (typeof url === "string" && url.indexOf(DASH_PREFIX) === 0) {
      url = MOBILE_PREFIX + url.slice(DASH_PREFIX.length);
      init = Object.assign({}, init);
      init.headers = Object.assign({}, (init && init.headers) || {});
      if (token) init.headers["Authorization"] = "Bearer " + token;
      return nativeFetch(url, init);
    }
    return nativeFetch(input, init);
  };

  /* The script hangs up on unload with sendBeacon, which cannot carry an
   * Authorization header. Route it through the authenticated fetch instead
   * so the session is actually closed rather than left to time out. */
  var nativeBeacon = navigator.sendBeacon && navigator.sendBeacon.bind(navigator);
  navigator.sendBeacon = function (url, data) {
    if (typeof url === "string" && url.indexOf(DASH_PREFIX) === 0) {
      window.fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: data,
        keepalive: true
      }).catch(function () {});
      return true;
    }
    return nativeBeacon ? nativeBeacon(url, data) : false;
  };

  /* Called by the app once the page has loaded (see the WebView screen).
   * The token arrives here rather than in the URL so it stays out of
   * history and logs. */
  /* Uplink tuning. The sentinel decodes against a compile-time 720x1280,
   * so the frame size cannot be reduced to buy frame rate -- the only knobs
   * are how often a frame is sent and how many bytes it may cost. The link
   * carries roughly 70 kB/s, so these two multiplied out have to stay under
   * that or the sentinel's 400 kB receive cache backs up and the picture
   * stops. A smaller budget therefore buys a higher rate, paid for in
   * sharpness. */
  var UPLINK_FPS = 15;
  var UPLINK_BUDGET = 9000;
  /* Measured: 0.1 encodes to ~8 kB at 720x1280, 0.3 to ~42 kB. Open at
   * the value the link carries and let the budget loop climb if it can. */
  var UPLINK_START_Q = 0.1;
  var UPLINK_MAX_Q = 0.2;
  /* How many bytes may be outstanding at once. What the uplink sustains
   * is (bytes in flight / round trip), so the old ~18 kB capped it near
   * 70 kB/s regardless of the network. Raised to test whether the ceiling
   * was ever the link: if the frame rate scales with this, it was not.
   * Still far under the sentinel's 400 kB receive cache and the script's
   * own 128 kB hard stop. */
  var UPLINK_INFLIGHT = 48000;

  var statsTimer = null;
  var lastTick = 0, lastTx = 0, lastTicks = 0;

  function startStatsLog() {
    stopStatsLog();
    /* The script reports its counters to the DOM only, which is invisible
     * from logcat -- when the picture froze after about a minute there was
     * no way to tell a closed channel from a backed-up one. This puts the
     * same numbers where the app can see them. */
    statsTimer = setInterval(function () {
      if (!window.p4Stats) return;
      var s = window.p4Stats();
      var now = Date.now();
      var txNow = (s.counters && s.counters.txFrames) || 0;
      var fps = lastTick ? ((txNow - lastTx) / ((now - lastTick) / 1000)) : 0;
      var ticksNow = s.ticks || 0;
      var v = document.getElementById("p4-local-video");
      var r = v ? v.getBoundingClientRect() : null;
      console.log("[stats] " + JSON.stringify({
        // Laid-out size of the preview, not just the decoded size: the
        // element reported healthy video (ready=4, not paused) while the
        // operator still saw black, which only a zero-sized or off-screen
        // box explains.
        box: r ? (Math.round(r.width) + "x" + Math.round(r.height) +
                  " @" + Math.round(r.left) + "," + Math.round(r.top)) : "none",
        view: document.documentElement.clientWidth + "x" +
              document.documentElement.clientHeight,
        tx: txNow,
        fps: Math.round(fps * 10) / 10,
        inflight: s.inflightMax,
        // ticks/s at ~15 means the pump timer is running and the encode
        // is the limit; far below means Chromium is throttling it.
        tps: lastTick ? Math.round(((s.ticks || 0) - lastTicks) /
                                   ((now - lastTick) / 1000) * 10) / 10 : 0,
        skips: s.inflightSkips,
        drawMs: s.drawMs,
        encMs: s.encodeMs,
        dropped: s.counters && s.counters.dropped,
        kb: s.lastFrameKB,
        q: s.qualityLive,
        dc: s.dcState,
        buffered: s.buffered,
        canvas: s.canvas,
        vid: v ? (v.videoWidth + "x" + v.videoHeight +
                  " ready=" + v.readyState + " paused=" + v.paused) : "none"
      }));
      lastTick = now; lastTx = txNow; lastTicks = ticksNow;
    }, 3000);
  }

  function stopStatsLog() {
    if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
  }

  window.__sentinelStart = function (bearer, deviceId, fps, quality) {
    token = bearer;
    if (!window.P4Intercom) {
      document.getElementById("p4-status").textContent =
        "intercom.js did not load";
      return false;
    }
    /* onPageFinished can fire more than once, and the script's call() hangs
     * up any call already running -- so a second start would tear down a
     * working one. Start only when genuinely idle. */
    if (window.P4Intercom.isActive()) {
      console.log("[intercom] already in a call, ignoring duplicate start");
      return true;
    }
    if (window.p4SetBudget) window.p4SetBudget(UPLINK_BUDGET);
    if (window.p4SetQuality) window.p4SetQuality(UPLINK_START_Q, UPLINK_MAX_Q);
    if (window.p4SetInflight) window.p4SetInflight(UPLINK_INFLIGHT);

    /* Wrap hangup once the script is loaded, so every teardown says who
     * asked for it. The reason string alone is ambiguous -- the sentinel's
     * bye and the relay's reaped-session bye produce the same one. */
    if (!window.P4Intercom.__wrapped) {
      var realHangup = window.P4Intercom.hangup;
      window.P4Intercom.hangup = function (reason) {
        console.log("[intercom] HANGUP reason=" + reason + " stack=" +
                    ((new Error()).stack || "").split("\\n").slice(1, 4).join(" | "));
        stopStatsLog();
        return realHangup.apply(this, arguments);
      };
      window.P4Intercom.__wrapped = true;
    }
    var opts = { fps: fps || UPLINK_FPS };
    if (quality) opts.quality = quality;
    window.P4Intercom.call(deviceId, opts);
    startStatsLog();
    return true;
  };

  window.__sentinelHangup = function () {
    stopStatsLog();
    if (window.P4Intercom) window.P4Intercom.hangup("Call ended");
  };

  window.__sentinelSetMic = function (on) {
    if (window.P4Intercom) window.P4Intercom.setMicEnabled(!!on);
  };

  window.__sentinelSetCamera = function (on) {
    if (window.P4Intercom) window.P4Intercom.setCameraEnabled(!!on);
  };

  /* Close the relay session when this page goes away.
   *
   * A reload leaves the old session behind with nobody polling it, and the
   * relay reaps it 90 seconds later -- publishing a bye to the device topic
   * that ends whichever call is running by then, not the abandoned one. The
   * relay no longer lets a stale session do that, but hanging up here as
   * well means the session goes immediately rather than lingering for a
   * minute and a half. pagehide fires where beforeunload is not guaranteed
   * to in a WebView. */
  /* Deliberately NOT hanging up here.
   *
   * This used to release the session on pagehide, guarded on e.persisted so
   * that a freeze would be ignored and only a real unload would end the
   * call. That guard is not trustworthy on Android: the WebView is frozen
   * whenever the screen blanks or the app goes to the background, and the
   * call died about ninety seconds in -- a tablet's screen timeout, with
   * nobody touching it during a call -- with the app, not the sentinel,
   * sending DENY_CALL.
   *
   * Nothing needs this any more. The reason it existed was that an
   * abandoned session got reaped ninety seconds later and its bye, which
   * goes to the device topic rather than to a session, ended whatever call
   * was running by then; SignalingBridge.close_session now stays quiet
   * while another session still holds the device, so a stale session
   * expires harmlessly. Ending a live call to avoid a problem that no
   * longer exists is the worse trade. */
  window.addEventListener("pagehide", function (e) {
    console.log("[intercom] pagehide persisted=" + e.persisted + " (not hanging up)");
  });

  /* Who ends a call is otherwise invisible: the sentinel, the relay's idle
   * reaper and this page all converge on the same hangup(), and the only
   * trace is a DENY_CALL on the wire that looks identical whoever sent it.
   * Log the reason and the stack at the moment it is called. */
  window.addEventListener("pageshow", function (e) {
    console.log("[intercom] pageshow persisted=" + e.persisted);
  });
  document.addEventListener("visibilitychange", function () {
    console.log("[intercom] visibility=" + document.visibilityState);
  });

  /* Mirror status into the app's log so a failed call is diagnosable from
   * logcat, the same place the native path reported from. */
  var statusNode = document.getElementById("p4-status");
  if (window.MutationObserver && statusNode) {
    new MutationObserver(function () {
      console.log("[intercom] " + statusNode.textContent);
    }).observe(statusNode, { childList: true, characterData: true, subtree: true });
  }
})();
</script>
<script src="intercom.js"></script>
</body>
</html>
"""


@router.get("/app", response_class=HTMLResponse)
def intercom_page() -> HTMLResponse:
    """The shell the WebView loads. Carries no data; every call it makes is authenticated."""
    return HTMLResponse(content=_PAGE, headers={"Cache-Control": "no-store"})
