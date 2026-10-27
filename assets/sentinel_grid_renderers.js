/* =====================================================================
   Sentinel - AG Grid cell renderers
   ---------------------------------------------------------------------
   Custom cell components for the Audio Events grid.

   These run entirely in the browser, inside AG Grid's render pass, after
   rowData has already arrived from the Dash callback. They change how a
   cell LOOKS; they never change what data flows. No Dash callback, ID or
   prop is involved, so nothing here can break the callback graph.

   Registered under window.dashAgGridComponentFunctions, which dash-ag-grid
   looks up by name from a column's "cellRenderer" key.
   ===================================================================== */

var dagcomponentfuncs = (window.dashAgGridComponentFunctions =
    window.dashAgGridComponentFunctions || {});

(function () {
    var e = React.createElement;

    /* ---------- shared helpers ---------------------------------- */

    // Confidence bands. Mirrors the legend under the grid.
    function band(p) {
        if (p === null || p === undefined || isNaN(p)) return 'none';
        if (p > 0.7) return 'high';
        if (p >= 0.3) return 'med';
        return 'low';
    }

    function num(v) {
        var n = parseFloat(v);
        return isNaN(n) ? null : n;
    }

    // "Nothing here" values arrive from SQL in several shapes.
    function blank(v) {
        return (
            v === null ||
            v === undefined ||
            v === '' ||
            v === 'None' ||
            v === 'nan' ||
            v === 'NaN'
        );
    }

    function relTime(ts) {
        if (blank(ts)) return '';
        var then = new Date(String(ts).replace(' ', 'T'));
        if (isNaN(then.getTime())) return '';
        var mins = Math.floor((Date.now() - then.getTime()) / 60000);
        if (mins < 0) return '';
        if (mins < 1) return 'just now';
        if (mins < 60) return mins + ' min ago';
        var hrs = Math.floor(mins / 60);
        if (hrs < 24) return hrs + (hrs === 1 ? ' hour ago' : ' hours ago');
        var days = Math.floor(hrs / 24);
        return days + (days === 1 ? ' day ago' : ' days ago');
    }

    /* ---------- TIME -------------------------------------------- */
    /* Clock time large, date + relative age small underneath, with a
       left accent bar tinted by the top detection's confidence.      */

    dagcomponentfuncs.SentinelTime = function (props) {
        var raw = props.value;
        if (blank(raw)) return e('div', { className: 'sx-cell' }, '');

        var str = String(raw);
        var parts = str.split(' ');
        var datePart = parts[0] || '';
        var timePart = (parts[1] || '').split('.')[0];

        var p = num(props.data && props.data['Prob 1']);

        return e(
            'div',
            { className: 'sx-cell sx-time' },
            e('span', { className: 'sx-time-accent sx-band-' + band(p) }),
            e(
                'div',
                { className: 'sx-time-body' },
                e('div', { className: 'sx-time-clock' }, timePart || datePart),
                e('div', { className: 'sx-time-meta' }, relTime(raw) || datePart)
            )
        );
    };

    /* ---------- DEVICE ------------------------------------------ */

    /* Room first, device second. Where something happened is what the
     * operator is reading for; the device id only matters when two sentinels
     * share a room or one is misbehaving. Events recorded before a device was
     * assigned a room have none, so those lead with the device id instead of
     * showing an empty line. */
    dagcomponentfuncs.SentinelDevice = function (props) {
        var name = blank(props.value) ? 'unknown' : String(props.value);
        var room = props.data && props.data.room;
        var hasRoom = !blank(room);

        // Stored with underscores so they survive as identifiers; shown with
        // spaces because this is a label a person reads.
        var roomLabel = hasRoom ? String(room).replace(/_/g, ' ') : null;

        return e(
            'div',
            { className: 'sx-cell sx-device' },
            e('span', { className: 'sx-device-icon' },
                e('i', { className: 'fas fa-microchip' })),
            e(
                'div',
                { className: 'sx-device-body' },
                e('div', { className: 'sx-device-name', title: name },
                    hasRoom ? roomLabel : name),
                e('div', { className: 'sx-device-meta' },
                    hasRoom
                        ? name
                        : (blank(props.data && props.data.rms)
                            ? ''
                            : 'RMS ' + Number(props.data.rms).toFixed(0)))
            )
        );
    };

    /* ---------- TRANSCRIPTION (+ play + emotion) ---------------- */
    /* The play affordance sits directly beside the text. Clicking any
       cell in the row already triggers the existing cellClicked
       callback that loads and plays the recording, so this button is
       a visual affordance over behaviour that is already there.      */

    var EMOJI = {
        NEUTRAL: '😐',
        HAPPY: '🙂',
        ANGRY: '😠',
        SAD: '😢'
    };

    dagcomponentfuncs.SentinelTranscript = function (props) {
        var d = props.data || {};
        var text = props.value;
        var emotion = d.emotion || d.Emotion;

        var hasText = !blank(text);
        var label = hasText ? String(text) : '(no speech detected)';

        // Diarised transcriptions arrive as
        //   "[00:00.00 - 00:02.00] Rafael: some words"
        // possibly repeated per segment. The row already carries its own
        // timestamp, so the per-segment clock is noise here -- drop it and
        // let the speaker name carry the line. Nothing is discarded from the
        // data; this is display only, and the full text is kept in the
        // tooltip.
        var speaker = null;
        if (hasText) {
            var cleaned = label.replace(/\[\s*[\d:.\s]+-[\d:.\s]+\]\s*/g, '').trim();
            var m = cleaned.match(/^([^:\n]{1,32}):\s*([\s\S]*)$/);
            if (m) {
                speaker = m[1].trim();
                cleaned = m[2].trim();
            }
            if (cleaned) label = cleaned;
        }

        var children = [
            e(
                'button',
                {
                    className: 'sx-play',
                    type: 'button',
                    title: 'Play this recording',
                    'aria-label': 'Play this recording'
                },
                e('i', { className: 'fas fa-play' })
            )
        ];

        var lines = [
            e(
                'div',
                {
                    key: 'txt',
                    className: 'sx-transcript-text' + (hasText ? '' : ' sx-muted'),
                    title: hasText ? String(text) : ''
                },
                speaker
                    ? e('span', { className: 'sx-speaker' }, speaker + ': ')
                    : null,
                label
            )
        ];

        /* Emotion and capture quality share one line. SNR is shown for every
           event, speech or not: it describes the recording rather than the
           content, so a silent clip still has one and it is still worth
           seeing. It is the envelope's dynamic range in dB, not a true
           signal-to-noise ratio -- roughly 12-40 dB on these sentinels. */
        var snr = d.snr;
        var hasSnr = snr !== undefined && snr !== null && snr !== '' && isFinite(snr);
        var meta = [];

        if (!blank(emotion)) {
            var em = String(emotion).toUpperCase().split(' ')[0].split('(')[0].trim();
            meta.push(e('span', { key: 'emoji', className: 'sx-emotion-emoji' },
                        EMOJI[em] || '😐'));
            meta.push(e('span', { key: 'label', className: 'sx-emotion-label' },
                        em.charAt(0) + em.slice(1).toLowerCase()));
        }

        if (hasSnr) {
            if (meta.length) {
                meta.push(e('span', { key: 'sep', className: 'sx-meta-sep' }, ' · '));
            }
            meta.push(
                e(
                    'span',
                    {
                        key: 'snr',
                        className: 'sx-snr',
                        title: 'Envelope dynamic range of this recording (higher is cleaner)'
                    },
                    'SNR ' + Number(snr).toFixed(1) + ' dB'
                )
            );
        }

        if (meta.length) {
            lines.push(e('div', { key: 'meta', className: 'sx-emotion' }, meta));
        }

        children.push(e('div', { className: 'sx-transcript-body' }, lines));

        return e('div', { className: 'sx-cell sx-transcript' }, children);
    };

    /* ---------- TOP DETECTIONS ---------------------------------- */
    /* Rank 1 always shown. Ranks 2 and 3 only clear a configurable
       threshold (cellRendererParams.threshold, default 0.10) -- below
       it they are hidden completely rather than shown as noise.      */

    dagcomponentfuncs.SentinelDetections = function (props) {
        var d = props.data || {};
        var thr =
            props.threshold !== undefined && props.threshold !== null
                ? parseFloat(props.threshold)
                : 0.1;

        var badges = [];
        for (var i = 1; i <= 3; i++) {
            var cls = d['Class ' + i];
            var p = num(d['Prob ' + i]);
            if (blank(cls)) continue;
            if (i > 1 && (p === null || p < thr)) continue;

            badges.push(
                e(
                    'span',
                    { key: 'b' + i, className: 'sx-badge sx-band-' + band(p) },
                    e('span', { className: 'sx-badge-rank' }, i),
                    e('span', { className: 'sx-badge-label', title: String(cls) }, String(cls)),
                    e('span', { className: 'sx-badge-prob' },
                        p === null ? '–' : p.toFixed(2))
                )
            );
        }

        if (!badges.length) {
            badges.push(
                e('span', { key: 'nb', className: 'sx-badge sx-band-none' },
                    e('span', { className: 'sx-badge-label sx-muted' }, 'no detection'))
            );
        }

        return e('div', { className: 'sx-cell sx-detections' }, badges);
    };

    /* ---------- CONFIDENCE -------------------------------------- */
    /* Bar + exact value + band name. The number stays visible. */

    var BAND_TEXT = { high: 'High', med: 'Medium', low: 'Low', none: '–' };

    dagcomponentfuncs.SentinelConfidence = function (props) {
        var p = num(props.value);
        if (p === null) {
            return e('div', { className: 'sx-cell sx-conf' },
                e('span', { className: 'sx-muted' }, '–'));
        }

        var b = band(p);
        var pct = Math.max(0, Math.min(100, p * 100));

        return e(
            'div',
            { className: 'sx-cell sx-conf' },
            e('div', { className: 'sx-conf-track' },
                e('div', { className: 'sx-conf-fill sx-band-' + b,
                           style: { width: pct.toFixed(1) + '%' } })),
            e(
                'div',
                { className: 'sx-conf-meta' },
                e('span', { className: 'sx-conf-value' }, p.toFixed(2)),
                e('span', { className: 'sx-conf-band sx-band-text-' + b }, BAND_TEXT[b])
            )
        );
    };
})();
