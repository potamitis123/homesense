/*
 * Go to time, for the Audio Events grid.
 *
 * The server has the rows around a moment loaded (audio_details.plan_jump);
 * this file lands on them. Only the browser knows the grid's current sort,
 * column filters and page size -- the page size follows the grid's height --
 * so the page holding a moment can only be worked out here.
 *
 * Two entry points, wired as clientside callbacks in audio_details.py:
 *
 *   sx_grid.jump(request)    land on the event nearest request.at, or on the
 *                            first page when request.first_page
 *   sx_grid.watch_pages()    keep #grid-page-span saying what time the page
 *                            on screen covers
 *
 * Both write their text with dash_clientside.set_props, so neither needs a
 * declared output of its own.
 */
(function () {
    "use strict";

    var GRID_ID = "audio_grid_table";
    var NOTE_ID = "grid-jump-note";
    var SPAN_ID = "grid-page-span";
    var WAIT_MS = 8000;
    var POLL_MS = 100;
    var NOUNS = { speech: "speech event", other: "non-speech event" };

    var jumpedId = null;        // the row the last jump landed on, as a string
    var latestToken = null;     // a newer jump abandons an older one still waiting

    /* The last landing: {api, id, when, target, noun}. Kept so the grid can go
     * back to that row when its page size changes -- which it does once right
     * after loading: paginationAutoPageSize works the size out from the grid's
     * height, and until then AG Grid pages by 100. A jump made in that moment
     * lands on the right row of the wrong page. */
    var landing = null;

    /* Row class rule (rowClassRules in audio_details.py): the row landed on
     * stays marked until the next jump or Back to latest. */
    var dagfuncs = (window.dashAgGridFunctions = window.dashAgGridFunctions || {});
    dagfuncs.sxIsJumpedRow = function (params) {
        return jumpedId !== null && !!params.data && String(params.data.ID) === jumpedId;
    };

    /* ---------- time -------------------------------------------- */

    // "2026-09-16 14:05:36" as local time, like the timestamps the sentinels write.
    function parseTime(value) {
        if (value === null || value === undefined) return null;
        var t = new Date(String(value).trim().replace(" ", "T").slice(0, 19));
        return isNaN(t.getTime()) ? null : t;
    }

    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function clock(t) { return pad(t.getHours()) + ":" + pad(t.getMinutes()) + ":" + pad(t.getSeconds()); }
    function day(t) { return pad(t.getDate()) + "/" + pad(t.getMonth() + 1); }
    function stamp(t) { return day(t) + " " + clock(t); }

    function gapText(ms) {
        var s = Math.round(Math.abs(ms) / 1000);
        if (s < 60) return s + " s";
        var m = Math.round(s / 60);
        if (m < 60) return m + " min";
        var h = Math.floor(m / 60), rest = m % 60;
        if (h < 48) return h + " h" + (rest ? " " + rest + " min" : "");
        return Math.round(h / 24) + " days";
    }

    /* ---------- grid -------------------------------------------- */

    function gridApi() {
        try {
            return window.dash_ag_grid && window.dash_ag_grid.getApi
                ? window.dash_ag_grid.getApi(GRID_ID) : null;
        } catch (err) {
            return null;                              // not rendered yet
        }
    }

    function setText(id, text) {
        var dc = window.dash_clientside;
        if (dc && dc.set_props) dc.set_props(id, { children: text });
    }

    // Rows in the order they are shown: after the grid's own sort and filters.
    function shownRows(api) {
        var nodes = [];
        api.forEachNodeAfterFilterAndSort(function (node) {
            if (node.data) nodes.push(node);
        });
        return nodes;
    }

    /* The grid holds the rows a request was sent with. Rows arrive in the same
     * response as the request, and the grid applies them a moment later; a
     * request that reloaded nothing has no rows to wait for. */
    function holds(api, rows) {
        if (!(api.paginationGetPageSize() > 0)) return false;
        if (!rows) return true;
        var count = 0;
        api.forEachNode(function () { count += 1; });
        if (count !== rows.count) return false;
        return rows.count === 0 ||
            (!!api.getRowNode(String(rows.first)) && !!api.getRowNode(String(rows.last)));
    }

    function mark(api, node) {
        var before = jumpedId !== null ? api.getRowNode(jumpedId) : null;
        jumpedId = node ? String(node.data.ID) : null;
        var redraw = [before, node].filter(Boolean);
        if (redraw.length) api.redrawRows({ rowNodes: redraw });
    }

    // Open the page holding the landed row at the current page size, and say where it is.
    function showLanding(api) {
        var nodes = shownRows(api), index = -1;
        for (var i = 0; i < nodes.length; i++) {
            if (String(nodes[i].data.ID) === landing.id) { index = i; break; }
        }
        var size = api.paginationGetPageSize();
        landing.size = size;
        if (index < 0 || !(size > 0)) return;
        var page = Math.floor(index / size);
        if (api.paginationGetCurrentPage() !== page) api.paginationGoToPage(page);
        api.flashCells({ rowNodes: [nodes[index]] });

        var diff = landing.when - landing.target;
        var where = " · page " + (page + 1) + " of " + api.paginationGetTotalPages() + " · refresh paused";
        setText(NOTE_ID, Math.abs(diff) < 60000
            ? "Landed on " + stamp(landing.when) + where
            : "Nearest " + landing.noun + ": " + stamp(landing.when) + ", " + gapText(diff) +
              (diff < 0 ? " earlier" : " later") + where);
    }

    function land(api, request) {
        if (request.first_page) {
            landing = null;
            mark(api, null);
            api.paginationGoToFirstPage();
            return;
        }
        var target = parseTime(request.at);
        var nodes = shownRows(api);
        var best = -1, bestGap = Infinity;
        for (var i = 0; target && i < nodes.length; i++) {
            var t = parseTime(nodes[i].data.Timestamp);
            if (t && Math.abs(t - target) < bestGap) {
                best = i;
                bestGap = Math.abs(t - target);
            }
        }
        var noun = NOUNS[request.view] || "event";
        if (best < 0) {
            landing = null;
            mark(api, null);
            setText(NOTE_ID, "No " + noun + "s around that time in this view · refresh paused");
            return;
        }
        var node = nodes[best];
        landing = { api: api, id: String(node.data.ID), when: parseTime(node.data.Timestamp),
                    target: target, noun: noun, size: null };
        mark(api, node);
        showLanding(api);
    }

    /* ---------- the page's time span ----------------------------- */

    function describePage(api) {
        var size = api.paginationGetPageSize();
        if (!(size > 0)) return "";
        var first = api.paginationGetCurrentPage() * size;
        var lo = null, hi = null, index = 0;
        api.forEachNodeAfterFilterAndSort(function (node) {
            if (index >= first && index < first + size && node.data) {
                var t = parseTime(node.data.Timestamp);
                if (t && (!lo || t < lo)) lo = t;
                if (t && (!hi || t > hi)) hi = t;
            }
            index += 1;
        });
        if (!lo) return "";
        return "This page: " + stamp(lo) + " – " + (day(lo) === day(hi) ? clock(hi) : stamp(hi));
    }

    var watched = typeof WeakSet !== "undefined" ? new WeakSet() : null;

    function watch(api) {
        if (watched) {
            if (watched.has(api)) return;
            watched.add(api);
        }
        // Fires on a page change, on new rows, and when sorting, filtering or
        // the grid's height changes what a page holds. A changed page size
        // takes the grid back to the row it landed on.
        var update = function () {
            if (landing && landing.api === api && api.paginationGetPageSize() !== landing.size) {
                showLanding(api);
            }
            setText(SPAN_ID, describePage(api));
        };
        api.addEventListener("paginationChanged", update);
        update();
    }

    window.dash_clientside = Object.assign({}, window.dash_clientside, {
        sx_grid: {
            jump: function (request) {
                if (request && request.token) {
                    latestToken = request.token;
                    var started = Date.now();
                    (function poll() {
                        if (request.token !== latestToken) return;
                        var api = gridApi();
                        if (api && holds(api, request.rows)) return land(api, request);
                        if (Date.now() - started > WAIT_MS) {
                            setText(NOTE_ID, "The events did not load in time. Press Go again.");
                            return;
                        }
                        setTimeout(poll, POLL_MS);
                    })();
                }
                return window.dash_clientside.no_update;
            },

            // The grid is created again whenever the page is, so this runs on
            // every render of the grid and attaches to each new one once.
            watch_pages: function () {
                var started = Date.now();
                (function poll() {
                    var api = gridApi();
                    if (api) return watch(api);
                    if (Date.now() - started < 4 * WAIT_MS) setTimeout(poll, 250);
                })();
                return window.dash_clientside.no_update;
            }
        }
    });
})();
