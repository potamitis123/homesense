"""Remove duplicate captures of one utterance by several sentinels.

A single voice is often loud enough to trigger two or three sentinels in
adjacent rooms. Each writes its own event, so one sentence becomes several
rows -- in different rooms, with different (and usually worse) transcriptions
of the same words. Downstream that is not merely noise: ast_dash/llm_report
reads those rows as separate speech, which inflates participation counts and
files one conversation in two places.

WHY ACOUSTIC CORRELATION AND NOT TEXT

The obvious rule -- "same words, therefore same utterance" -- does not work
here, because the distant copy is exactly the one Whisper transcribes badly.
Measured over ten days of this household's recordings, 31 of the 42 real
duplicate pairs shared less than 60% of their words:

    xcorr 0.76, text 10%   "Θα τα κάνω άλλο πολύ καλά"
                           "Δεν ξέρω, θα τα κάνω όλα όπως έκανα"
    xcorr 0.71, text 50%   "Εντάξει ο Γιώργος είναι χαρούμενος"
                           "Ο δάρκος ο Ιώσου είναι χαρούμενος"

Correlating the amplitude envelopes instead compares the recordings rather
than the transcripts, so it is unaffected by ASR quality. Two microphones
hearing one voice produce correlated envelopes (median 0.64 in this house);
two people talking in different rooms at the same moment do not (median 0.26).

WHY SNR AND NOT RMS

The surviving copy is the one with the better speech-to-noise ratio. Raw RMS
would have been wrong: the box3 units read 4-5 dB hotter than the P4 and S3
units on comparable speech, so "loudest" would have meant "recorded by a box3"
regardless of who spoke where. SNR is a ratio and so is gain-independent.

THE SAME SECOND

Correlation is a weak witness when the copies differ in length. It normalises
over the whole clip, so a 2 s utterance inside an 18 s capture shares only a
sliver of that clip's energy with a 7 s copy of the same words: 17:41:35 on
2026-10-02 was heard by three sentinels, two were linked at 0.66, and the third
-- 17.7 s against 7.4 s -- scored 0.40 and survived, although comparing just
the first 2.5 s of each gave 0.84 against a maximum of 0.74 for unrelated pairs.
The length gate in the prefilter excluded it before correlation even ran.

So there is a second, independent rule that does not look at the audio at all:
captures from different sentinels stamped with the SAME SECOND are one moment
heard more than once, and only the one with the best SNR is kept. Sentinels
stamp the start of the recording, so an utterance that wakes two of them lands
on the same second far more often than two unrelated sounds would.

The price is that it cannot tell one voice heard twice from two different
sounds that happened to start in the same second in adjacent rooms, which the
correlation test was at least able to try. It is on by default and
`dedupe.same_second_enabled: false` turns it off without touching code. Rows
dated by the server rather than by the sentinel are not affected: every real
sentinel's `date` equals the stamp in its file name.

This deletes rows and audio files. It is deliberately conservative: it acts
only when both recordings are still on disk, so a decision can always be
checked against the evidence that produced it.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from misc.load_parameters import parameters
from misc.log_test import logger

LOG_D = {'module_name': 'dedupe'}

ASSETS_DIR = os.path.join(PROJECT_ROOT, 'assets')
# Where a recording may live. audio_mp3 holds the copy the dashboard plays.
AUDIO_DIRS = [os.path.join(ASSETS_DIR, 'audio_mp3'),
              os.path.join(ASSETS_DIR, 'audios'),
              os.path.join(ASSETS_DIR, 'audiosuser')]
SPECTRO_DIR = os.path.join(ASSETS_DIR, 'specrogram_images')

# Envelope sample rate for the correlation. 1 kHz keeps syllable rhythm, which
# is what survives a wall, and discards phase, which does not.
ENVELOPE_HZ = 1000


def _parse_stamp(value):
    """An ISO timestamp from parameters.json, or None if absent/unreadable.

    Unreadable is deliberately None rather than an error: a malformed stamp
    must not become a reason to delete recordings it was meant to protect --
    but see the caller, where None means the guard simply does not fire.
    """
    if not value:
        return None
    text = str(value).strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(text[:len(fmt) + 4], fmt)
        except ValueError:
            continue
    return None


def _cfg():
    section = parameters.get_parameters().get('dedupe', {}) or {}
    return {
        'enabled': bool(section.get('enabled', True)),
        'window_seconds': float(section.get('window_seconds', 10.0)),
        'min_correlation': float(section.get('min_correlation', 0.25)),
        # Reject a capture outright when its envelope dynamic range falls below
        # this, duplicate or not. Every room has its own sentinel, so a quiet
        # capture is the far unit overhearing a conversation the near one
        # recorded properly -- there is nothing to gain from transcribing it.
        # 0 disables the rule and leaves only the duplicate test.
        'min_snr_db': float(section.get('min_snr_db', 10.0)),
        # Stamped by Settings when the SNR threshold is saved. Recordings older
        # than this were made under a different rule and are never judged by
        # the new one. None means "no stamp yet", which is read as
        # "applies to everything the sweep is given".
        'min_snr_effective_from': _parse_stamp(
            section.get('min_snr_effective_from')),
        'min_content_words': int(section.get('min_content_words', 2)),
        # Left False so a door, a dropped pan or a burst of TV heard by two
        # sentinels is deduplicated like a voice. Set True to restrict the
        # pass to events that produced a transcript.
        'speech_only': bool(section.get('speech_only', False)),
        # Set False to switch the inbox prefilter off and go back to analysing
        # every capture and deleting the duplicates afterwards. The one knob
        # that reverts plan_pending without touching code.
        'prefilter_enabled': bool(section.get('prefilter_enabled', True)),
        # How much shorter than the longest capture in a group a recording may
        # be and still be allowed to win on SNR. See plan_pending.
        'prefilter_duration_tolerance': float(
            section.get('prefilter_duration_tolerance', 0.2)),

        # The prefilter's own window, length gate and correlation floor, kept
        # separate from window_seconds and min_correlation above so that
        # find_duplicates -- which judges database rows, some of them stamped
        # with server assembly time rather than capture time -- is not narrowed
        # by a threshold measured on filenames. Each reverts on its own:
        # set these three to 30.0, 1e9 and 0.5 to give the prefilter exactly
        # the old shared behaviour. See plan_pending for the measurements.
        'prefilter_window_seconds': float(
            section.get('prefilter_window_seconds', 3.0)),
        'prefilter_max_duration_diff': float(
            section.get('prefilter_max_duration_diff', 1.5)),
        'prefilter_min_correlation': float(
            section.get('prefilter_min_correlation', 0.35)),

        # Captures from different sentinels stamped with the same second are
        # one moment heard more than once: keep the best SNR, drop the rest,
        # whatever their correlation or length. See THE SAME SECOND above.
        'same_second_enabled': bool(section.get('same_second_enabled', True)),
    }


def db_path():
    return os.path.abspath(os.path.join(
        PROJECT_ROOT, parameters.get_parameters()['sqlite']['db_name']))


def find_audio(file_name):
    if not file_name:
        return None
    base = os.path.basename(str(file_name))
    stem, _ext = os.path.splitext(base)
    for folder in AUDIO_DIRS:
        for name in (base, stem + '.wav', stem + '.mp3'):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return path
    return None


def envelope(path):
    """Amplitude envelope at ENVELOPE_HZ, mean-removed and unit-normalised."""
    import soundfile as sf
    data, sr = sf.read(path, dtype='float32')
    if data.ndim > 1:
        data = data.mean(axis=1)
    step = max(int(sr / ENVELOPE_HZ), 1)
    smoothed = np.convolve(np.abs(data), np.ones(step) / step, mode='same')[::step]
    smoothed = smoothed - smoothed.mean()
    norm = np.linalg.norm(smoothed)
    return smoothed / norm if norm > 0 else smoothed


def snr_from_samples(data, sr):
    """Envelope dynamic range in dB, from samples already in memory.

    Not a true SNR: it is the ratio of the 90th to the 10th percentile of the
    amplitude envelope. That needs no VAD and no model, so it cannot disagree
    with the ones used elsewhere in the pipeline, and it is a *ratio*, so it
    is unaffected by the 4-5 dB gain difference between the box3 and P4/S3
    units -- which is what ruled out raw RMS for choosing between two captures.

    Reverberation fills the gaps between syllables, which raises the 10th
    percentile and lowers the result. The distant copy therefore scores worse
    almost by construction, which is the discrimination this is used for.

    Reads low on a clip of unbroken speech, where the 10th percentile is still
    speech. That matters little when ranking two recordings of one moment, but
    it does mean the absolute number is a dynamic range, not a signal-to-noise
    ratio. Observed range on this household's recordings: 12-40 dB.

    Cumulative-sum moving average rather than np.convolve, and the envelope is
    decimated before the percentile: measured at 1.3 ms against 7.1 ms for the
    convolution version, agreeing to 0.07 dB. The percentile itself was never
    the cost -- numpy uses introselect, not a sort, at 0.6 ms.
    """
    data = np.asarray(data, dtype=np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if data.size < 16:
        return None
    window = max(int(0.02 * sr), 1)
    if data.size <= window:
        return None
    env = np.abs(data)
    cumulative = np.cumsum(env)
    env = (cumulative[window:] - cumulative[:-window]) / window
    env = env[::8]
    if env.size < 10:
        return None
    lo_index, hi_index = env.size // 10, (9 * env.size) // 10
    lo, hi = np.partition(env, [lo_index, hi_index])[[lo_index, hi_index]]
    return float(20 * np.log10((float(hi) + 1e-9) / (float(lo) + 1e-9)))


def snr_db(path):
    """As snr_from_samples, for a file on disk."""
    import soundfile as sf
    data, sr = sf.read(path, dtype='float32')
    value = snr_from_samples(data, sr)
    return value if value is not None else 0.0


def correlation(env_a, env_b):
    """Peak normalised cross-correlation, searching all plausible lags."""
    if len(env_a) < 4 or len(env_b) < 4:
        return 0.0
    size = len(env_a) + len(env_b)
    spectrum = np.fft.rfft(env_a, size) * np.conj(np.fft.rfft(env_b, size))
    return float(np.abs(np.fft.irfft(spectrum, size)).max())


def _transcript_body(transcription):
    """The words, with the "[00:00.00 - 00:02.00] Speaker:" prefixes removed."""
    import re
    return re.sub(r'\[[^\]]*\]\s*[^:\[\]]*:', '', transcription or '')


def _content_words(transcription):
    """Words long enough to be worth counting, for the speech_only gate."""
    import re
    body = _transcript_body(transcription)
    return [w for w in re.sub(r'[^\w\s]', ' ', body.lower()).split() if len(w) > 2]


def _spoken_tokens(transcription):
    """Any word or number that was actually transcribed. Length-agnostic."""
    import re
    text = transcription or ''
    if not text.strip() or 'No Speech' in text:
        return []
    return re.sub(r'[^\w\s]', ' ', _transcript_body(text)).split()


def load_events(conn, since=None, until=None):
    """Every event with audio on disk, transcribed or not.

    Non-speech events are deduplicated too, and must be: a door closing or a
    dropped pan is one physical event that several sentinels hear, exactly as
    a voice is. Events 40238/40239 -- the same moment at 19:19:11, recorded by
    box3-1 and box3-2, both transcribing to "No Speech Detected (VAD)" -- were
    invisible to this pass while it looked only at speech, so both survived
    and the dashboard showed one event twice.

    Nothing here depends on the transcript, so excluding untranscribed events
    was never necessary: the correlation compares recordings.
    """
    query = ("SELECT id, date, device_name, room, file_name, spectrogram_image,"
             " transcription FROM ast_table WHERE 1=1")
    args = []
    if since:
        query += " AND date >= ?"
        args.append(since)
    if until:
        query += " AND date <= ?"
        args.append(until)
    query += " ORDER BY date"

    cfg = _cfg()
    events = []
    for row in conn.execute(query, args):
        text = row[6] or ''
        # "Did this capture record any words at all", used only to stop a
        # transcript being discarded in favour of a cleaner but wordless
        # recording. Deliberately NOT the >2-character content-word test used
        # for the speech_only gate: that scored "1 Α, 2 Β, 3 Γ." as wordless
        # and threw it away to keep "ΕΝΑ ΑΛΦΑ", which was both a worse
        # transcript and 7 dB noisier. Any token counts here.
        has_words = bool(_spoken_tokens(text))
        if cfg['speech_only'] and not has_words:
            continue
        audio = find_audio(row[4])
        if not audio:
            # Without the recording the correlation cannot be computed, and
            # deleting on the strength of the text alone is what this module
            # exists to avoid.
            continue
        try:
            stamp = datetime.fromisoformat(row[1])
        except (TypeError, ValueError):
            continue
        events.append({'id': row[0], 'time': stamp, 'device': row[2],
                       'room': row[3], 'file_name': row[4],
                       'spectrogram': row[5], 'text': text, 'audio': audio,
                       'has_words': has_words})
    return events


def find_duplicates(events, cfg=None):
    """Captures to reject, for either of two independent reasons.

    A capture is rejected when it is TOO QUIET, or when it DUPLICATES a better
    one. The quiet test runs first and on its own: a distant, reverberant
    capture is worthless whether or not a sibling recorded the same moment, and
    filtering it before the correlation pass also keeps it from being chosen as
    a group's winner.

    Correlated events are then collected into GROUPS before anything is chosen,
    rather than being resolved pair by pair. Deciding pairwise is
    order-dependent and unsound: on a first run over this household's data,
    event 36220 beat 36221 and was then itself beaten by 36223, so 36221 was
    deleted for duplicating a row that also got deleted. Grouping first means
    exactly one recording -- the best in the whole group -- survives.

    Returns a list of dicts: {loser, winner, correlation, snr_loser,
    snr_winner, reason}, where reason is 'low_snr' (winner None), 'same_second'
    (stamped with the same second as a better capture from another sentinel;
    correlation None, because the audio was not compared) or 'duplicate'.
    """
    cfg = cfg or _cfg()
    env_cache, snr_cache = {}, {}

    def env_of(event):
        if event['id'] not in env_cache:
            env_cache[event['id']] = envelope(event['audio'])
        return env_cache[event['id']]

    def snr_of(event):
        if event['id'] not in snr_cache:
            snr_cache[event['id']] = snr_db(event['audio'])
        return snr_cache[event['id']]

    decisions = []

    # --- too quiet to be worth keeping ------------------------------------
    # Applied before the correlation pass so a rejected capture can neither win
    # a group nor be reported twice. A threshold of 0 turns the rule off.
    #
    # It never applies retroactively. The threshold is a live setting, so
    # lowering it must not reach back and delete a month of recordings made
    # under the old one: `effective_from` is stamped when the value is saved,
    # and anything captured before that is left alone whatever its SNR. That
    # guard lives here rather than in the caller so a manual CLI sweep over an
    # old range obeys it too.
    floor = cfg.get('min_snr_db', 0.0)
    effective_from = cfg.get('min_snr_effective_from')
    if floor > 0:
        kept = []
        for event in events:
            if effective_from and event['time'] < effective_from:
                kept.append(event)
                continue
            try:
                value = snr_of(event)
            except Exception as exc:
                logger.error(f"Could not measure SNR for {event['id']}: {exc}",
                             extra=LOG_D)
                kept.append(event)          # unmeasurable is not the same as bad
                continue
            if value < floor:
                decisions.append({'loser': event, 'winner': None,
                                  'correlation': None, 'snr_loser': value,
                                  'snr_winner': None, 'reason': 'low_snr'})
            else:
                kept.append(event)
        events = kept

    # --- the same second ---------------------------------------------------
    # Different sentinels, one stamp: one moment heard more than once. Decided
    # before correlation and without it, so a copy whose length differs from
    # its siblings' -- the case correlation cannot score -- is still caught.
    # Losers leave `events` here, so the correlation pass below neither sees
    # them nor reports them a second time.
    #
    # The winner is chosen as for any other group: a capture that produced
    # words outranks a cleaner one that did not, then SNR decides. Deleting the
    # only transcript to keep a better recording of it is the mistake that
    # ordering exists to prevent. A deliberate upload ('ex') is not a sentinel
    # and is never judged by this rule.
    if cfg.get('same_second_enabled', True):
        by_second = {}
        for event in events:
            if event['device'] == 'ex':
                continue
            second = event['time'].replace(microsecond=0)
            by_second.setdefault(second, []).append(event)
        dropped = set()
        for members in by_second.values():
            if len({member['device'] for member in members}) < 2:
                continue
            try:
                scored = [(snr_of(member), member) for member in members]
            except Exception as exc:
                logger.error(f"Could not measure SNR for a same-second group: "
                             f"{exc}", extra=LOG_D)
                continue
            scored.sort(key=lambda pair: (bool(pair[1].get('has_words')), pair[0]),
                        reverse=True)
            best_snr, winner = scored[0]
            for snr_value, loser in scored[1:]:
                decisions.append({'loser': loser, 'winner': winner,
                                  'correlation': None, 'snr_loser': snr_value,
                                  'snr_winner': best_snr,
                                  'reason': 'same_second'})
                dropped.add(loser['id'])
        events = [event for event in events if event['id'] not in dropped]

    # Union-find over "these two recorded the same moment".
    parent = {event['id']: event['id'] for event in events}
    best_link = {}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for index, first in enumerate(events):
        for second in events[index + 1:]:
            gap = (second['time'] - first['time']).total_seconds()
            if gap > cfg['window_seconds']:
                break
            if first['device'] == second['device']:
                continue
            try:
                score = correlation(env_of(first), env_of(second))
            except Exception as exc:
                logger.error(f"Could not correlate {first['id']}/{second['id']}: {exc}",
                             extra=LOG_D)
                continue
            if score < cfg['min_correlation']:
                continue
            union(first['id'], second['id'])
            for node in (first['id'], second['id']):
                if score > best_link.get(node, 0.0):
                    best_link[node] = score

    groups = {}
    by_id = {event['id']: event for event in events}
    for event in events:
        groups.setdefault(find(event['id']), []).append(event)

    for members in groups.values():
        if len(members) < 2:
            continue
        try:
            scored = [(snr_of(member), member) for member in members]
        except Exception as exc:
            logger.error(f"Could not measure SNR for a group: {exc}", extra=LOG_D)
            continue
        # A transcript outranks a better SNR. Now that untranscribed events are
        # included, a group can hold one capture that produced words and one
        # that VAD rejected; picking on SNR alone would sometimes delete the
        # only record of what was said in order to keep a cleaner recording of
        # it. Among equals -- all with words, or none -- SNR decides as before.
        scored.sort(key=lambda pair: (bool(pair[1].get('has_words')), pair[0]),
                    reverse=True)
        best_snr, winner = scored[0]
        for snr_value, loser in scored[1:]:
            decisions.append({'loser': loser, 'winner': winner,
                              'correlation': best_link.get(loser['id'], 0.0),
                              'snr_loser': snr_value, 'snr_winner': best_snr,
                              'reason': 'duplicate'})
    _ = by_id
    decisions.sort(key=lambda d: d['loser']['time'])
    return decisions


# ── the inbox prefilter ──────────────────────────────────────────────
#
# find_duplicates above runs AFTER the pipeline, on rows already in the
# database, so it cannot give back the GPU time that produced the row it
# deletes. While a family is talking that cost dominates everything else: the
# inbox backs up and events surface up to twenty minutes late, most of that
# spent transcribing captures which are about to be thrown away.
#
# The backlog is also what makes a prefilter possible. dedupe_loop explains
# that it cannot be event-driven, because when the first sentinel's event is
# written the second sentinel has not finished recording yet. That is true at
# the moment a file ARRIVES -- but by the time a file reaches the front of a
# twenty-minute queue its siblings arrived long ago and are sitting in the
# same folder. Comparing the pending files against each other therefore sees
# the whole group at once, with no settle delay to wait out.
#
# Two rules here differ from find_duplicates, both because nothing has been
# analysed yet:
#
#   * its transcript tie-break cannot apply. No candidate has words yet, so
#     the choice rests on SNR alone.
#   * duration has to guard that choice. Sentinels trigger independently, so
#     copies do not cover the same span, and a capture that caught only the
#     loud middle of a sentence can hold the best SNR and the fewest words.
#     The winner is the loudest of those nearly as long as the longest, not
#     the loudest outright.
#
# Nothing is deleted here. A skipped capture is moved aside with its reasoning
# written next to it, which matters more than it does in find_duplicates: a
# capture dropped before the pipeline leaves no transcript behind, so a
# mis-set correlation threshold would otherwise erase the evidence of its own
# mistake. Recovery is moving the files back.

SKIPPED_DIR = os.path.join(ASSETS_DIR, 'audio_skipped')

# Keyed on (path, mtime, size), so a file edited or replaced under the same
# name is re-read rather than served stale.
_probe_cache = {}


def _stamp_and_device(path):
    """(datetime, device) from a name like 20260905_131154_p4-door-1_rec.wav."""
    parts = os.path.basename(path).split('_')
    if len(parts) < 3:
        return None, None
    try:
        stamp = datetime.strptime(parts[0] + parts[1], '%Y%m%d%H%M%S')
    except ValueError:
        return None, None
    return stamp, ('ex' if parts[2].startswith('ex') else parts[2])


def _probe(path):
    """Envelope, SNR and duration from a single read of the file."""
    try:
        key = (path, os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return None
    cached = _probe_cache.get(key)
    if cached is not None:
        return cached

    import soundfile as sf
    try:
        data, sr = sf.read(path, dtype='float32')
    except Exception as exc:
        logger.error(f"Could not read {os.path.basename(path)}: {exc}", extra=LOG_D)
        return None
    if data.ndim > 1:
        data = data.mean(axis=1)
    if data.size == 0 or sr <= 0:
        return None

    step = max(int(sr / ENVELOPE_HZ), 1)
    env = np.convolve(np.abs(data), np.ones(step) / step, mode='same')[::step]
    env = env - env.mean()
    norm = np.linalg.norm(env)
    value = snr_from_samples(data, sr)
    probe = {'envelope': env / norm if norm > 0 else env,
             'snr': float(value) if value is not None else 0.0,
             'duration': len(data) / float(sr)}
    if len(_probe_cache) > 2000:
        _probe_cache.clear()
    _probe_cache[key] = probe
    return probe


def plan_pending(paths, cfg=None):
    """Which of the files waiting in the inbox are worth analysing.

    Returns {path: decision} holding only the losers:

        {'action': 'skip', 'winner': path, 'correlation': float,
         'snr': float, 'snr_winner': float,
         'duration': float, 'duration_winner': float}

    A capture dropped by the same-second rule also carries 'rule':
    'same_second' and a correlation of None -- the audio was not compared, and
    a recorded 0.0 would read as "measured, and unrelated".

    A path that is absent is simply processed, so any failure to judge a file
    -- an unparsable name, an unreadable recording, a correlation that raises
    -- leaves it in the pipeline rather than dropping it silently.
    """
    cfg = cfg or _cfg()
    plan = {}
    if len(paths) < 2:
        return plan

    items = []
    for path in paths:
        stamp, device = _stamp_and_device(path)
        if stamp is not None:
            items.append({'path': path, 'time': stamp, 'device': device})
    items.sort(key=lambda item: item['time'])

    # The same second, before any gate. File names carry the sentinel's own
    # stamp to the second, so two captures from different sentinels with one
    # stamp are one moment heard twice: keep the best SNR. Length is NOT
    # consulted -- it is the gate below that kept a 17.7 s copy of a 7.4 s
    # utterance out of the group -- and neither is correlation. The winner is
    # the loudest outright, not the loudest of the longest, because that is the
    # rule being applied. Losers are removed from `items` so the gates below
    # never see them.
    if cfg.get('same_second_enabled', True):
        by_second = {}
        for item in items:
            if item['device'] == 'ex':        # a deliberate upload
                continue
            by_second.setdefault(item['time'], []).append(item)
        for members in by_second.values():
            if len({member['device'] for member in members}) < 2:
                continue
            probed = [(member, _probe(member['path'])) for member in members]
            probed = [(member, probe) for member, probe in probed
                      if probe is not None]
            if len({member['device'] for member, _p in probed}) < 2:
                continue
            probed.sort(key=lambda pair: pair[1]['snr'], reverse=True)
            winner, winner_probe = probed[0]
            for member, probe in probed[1:]:
                plan[member['path']] = {
                    'action': 'skip', 'rule': 'same_second',
                    'winner': winner['path'], 'correlation': None,
                    'snr': probe['snr'], 'snr_winner': winner_probe['snr'],
                    'duration': probe['duration'],
                    'duration_winner': winner_probe['duration']}
        items = [item for item in items if item['path'] not in plan]

    # Two geometric gates before the correlation, and a lower floor behind
    # them. One loose window forced the floor up, and the high floor is what
    # missed real duplicates.
    #
    # Measured over 457 cross-device pairs from one day, against 11 duplicates
    # confirmed by ear:
    #
    #     30 s window, xcorr >= 0.50    13 flagged   10 of 11 found
    #     30 s window, xcorr >= 0.35   139 flagged   11 of 11 found
    #     3 s + 1.5 s, xcorr >= 0.35    16 flagged   11 of 11 found
    #
    # Lowering the floor alone flags nearly nine times as many pairs; behind
    # the gates the same floor flags sixteen. Every confirmed duplicate began
    # within 2 s of its twin and differed in length by at most 1.22 s, so the
    # gates keep all of them with room to spare, while the pairs they drop are
    # the ones sitting 19-26 s apart that scored 0.505-0.524 -- correlations
    # above the old floor, from utterances that were simply not the same.
    #
    # Correlation still runs, though after gating it only removes two pairs of
    # the eighteen. It stays because two sentinels can trigger in the same
    # second on DIFFERENT sounds in adjacent rooms, and nothing else here can
    # tell that apart from one voice heard twice.
    window = cfg.get('prefilter_window_seconds', 3.0)
    floor = cfg.get('prefilter_min_correlation', 0.35)
    max_duration_diff = cfg.get('prefilter_max_duration_diff', 1.5)
    tolerance = cfg.get('prefilter_duration_tolerance', 0.2)

    parent = {item['path']: item['path'] for item in items}
    best_link = {}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for index, first in enumerate(items):
        probe_first = _probe(first['path'])
        if probe_first is None:
            continue
        for second in items[index + 1:]:
            if (second['time'] - first['time']).total_seconds() > window:
                break
            # One sentinel hearing two similar sounds in a row is not a
            # duplicate; only a second microphone on the same moment is.
            if first['device'] == second['device']:
                continue
            probe_second = _probe(second['path'])
            if probe_second is None:
                continue
            # Length gate, before the correlation because it is free. Two
            # microphones on one utterance never produce the same length --
            # of 10 confirmed pairs not one matched exactly, and the firmware
            # quantises durations so hard that 6204 pairs across a single day
            # share a frame count without being duplicates. What does hold is
            # that the difference stays small: median 0.33 s, worst 1.22 s.
            if abs(probe_first['duration']
                   - probe_second['duration']) > max_duration_diff:
                continue
            try:
                score = correlation(probe_first['envelope'],
                                    probe_second['envelope'])
            except Exception as exc:
                logger.error(f"Could not correlate "
                             f"{os.path.basename(first['path'])}/"
                             f"{os.path.basename(second['path'])}: {exc}",
                             extra=LOG_D)
                continue
            if score < floor:
                continue
            root_a, root_b = find(first['path']), find(second['path'])
            if root_a != root_b:
                parent[root_b] = root_a
            for node in (first['path'], second['path']):
                if score > best_link.get(node, 0.0):
                    best_link[node] = score

    groups = {}
    for item in items:
        groups.setdefault(find(item['path']), []).append(item)

    for members in groups.values():
        if len(members) < 2:
            continue
        probed = [(m, _probe(m['path'])) for m in members]
        probed = [(m, p) for m, p in probed if p is not None]
        if len(probed) < 2:
            continue
        longest = max(p['duration'] for _m, p in probed)
        # Loudest among those nearly as long as the longest. Falling back to
        # the whole group keeps a winner when every copy is short.
        eligible = [(m, p) for m, p in probed
                    if p['duration'] >= longest * (1.0 - tolerance)] or probed
        eligible.sort(key=lambda pair: pair[1]['snr'], reverse=True)
        winner, winner_probe = eligible[0]
        for member, probe in probed:
            if member['path'] == winner['path']:
                continue
            plan[member['path']] = {
                'action': 'skip',
                'winner': winner['path'],
                'correlation': best_link.get(member['path'], 0.0),
                'snr': probe['snr'], 'snr_winner': winner_probe['snr'],
                'duration': probe['duration'],
                'duration_winner': winner_probe['duration']}
    return plan


def shelve_duplicate(path, decision):
    """Move a skipped capture aside, with the reasoning beside it.

    Deliberately a move and not a delete. The whole risk of judging before
    analysing is that a wrong call leaves nothing to notice it by, so the
    audio stays on disk and every decision is appended to skipped.jsonl.
    """
    import json
    try:
        os.makedirs(SKIPPED_DIR, exist_ok=True)
        target = os.path.join(SKIPPED_DIR, os.path.basename(path))
        if os.path.exists(target):
            os.remove(target)
        os.replace(path, target)
    except Exception as exc:
        logger.error(f"Could not shelve {os.path.basename(path)}: {exc}",
                     extra=LOG_D)
        return False

    correlation = decision.get('correlation')
    row = {'when': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'file': os.path.basename(path),
           'winner': os.path.basename(decision.get('winner') or ''),
           'rule': decision.get('rule', 'correlation'),
           'correlation': (round(float(correlation), 3)
                           if correlation is not None else None),
           'snr': round(float(decision.get('snr') or 0.0), 2),
           'snr_winner': round(float(decision.get('snr_winner') or 0.0), 2),
           'duration': round(float(decision.get('duration') or 0.0), 2),
           'duration_winner': round(float(decision.get('duration_winner') or 0.0), 2)}
    try:
        with open(os.path.join(SKIPPED_DIR, 'skipped.jsonl'), 'a',
                  encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception as exc:
        logger.error(f"Could not log the skip of {row['file']}: {exc}",
                     extra=LOG_D)
    return True


def _remove_files(event):
    removed = []
    base = os.path.basename(str(event['file_name'] or ''))
    stem = os.path.splitext(base)[0]
    if base:
        for folder in AUDIO_DIRS:
            for name in (base, stem + '.wav', stem + '.mp3'):
                path = os.path.join(folder, name)
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        removed.append(path)
                    except OSError as exc:
                        logger.error(f"Could not delete {path}: {exc}", extra=LOG_D)
    spectro = event.get('spectrogram')
    if spectro:
        path = os.path.join(SPECTRO_DIR, os.path.basename(str(spectro)))
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                logger.error(f"Could not delete {path}: {exc}", extra=LOG_D)
    return removed


def run(since=None, until=None, dry_run=False):
    """Find and (unless dry_run) delete duplicate captures. Returns decisions."""
    cfg = _cfg()
    if not cfg['enabled'] and not dry_run:
        logger.info("Dedupe disabled in parameters.json", extra=LOG_D)
        return []

    conn = sqlite3.connect(db_path(), timeout=30)
    try:
        events = load_events(conn, since, until)
        decisions = find_duplicates(events, cfg)
        quiet = sum(1 for d in decisions if d['reason'] == 'low_snr')
        same = sum(1 for d in decisions if d['reason'] == 'same_second')
        logger.info(
            f"Dedupe scanned {len(events)} events, rejecting {len(decisions)} "
            f"({len(decisions) - quiet - same} duplicate, {same} same-second, "
            f"{quiet} below the SNR floor) "
            f"-- window {cfg['window_seconds']}s, correlation >= "
            f"{cfg['min_correlation']}, SNR >= {cfg['min_snr_db']} dB",
            extra=LOG_D)

        for decision in decisions:
            loser = decision['loser']
            if decision['reason'] == 'low_snr':
                summary = (
                    f"SNR {decision['snr_loser']:.1f} dB below the "
                    f"{cfg['min_snr_db']} dB floor, dropping event {loser['id']} "
                    f"({loser['device']}/{loser['room']}): "
                    f"{(loser['text'] or '')[:70]!r}")
            elif decision['reason'] == 'same_second':
                winner = decision['winner']
                summary = (
                    f"same-second capture of event {winner['id']} "
                    f"({winner['device']}/{winner['room']}, "
                    f"SNR {decision['snr_winner']:.1f} dB) -- both stamped "
                    f"{loser['time']:%H:%M:%S}, dropping event {loser['id']} "
                    f"({loser['device']}/{loser['room']}, "
                    f"SNR {decision['snr_loser']:.1f} dB): "
                    f"{(loser['text'] or '')[:70]!r}")
            else:
                winner = decision['winner']
                summary = (
                    f"duplicate of event {winner['id']} "
                    f"({winner['device']}/{winner['room']}, "
                    f"SNR {decision['snr_winner']:.1f} dB) -- correlation "
                    f"{decision['correlation']:.2f}, dropping event {loser['id']} "
                    f"({loser['device']}/{loser['room']}, "
                    f"SNR {decision['snr_loser']:.1f} dB): "
                    f"{(loser['text'] or '')[:70]!r}")
            if dry_run:
                logger.info(f"[dry-run] would delete {summary}", extra=LOG_D)
                continue
            files = _remove_files(loser)
            conn.execute("DELETE FROM ast_table WHERE id = ?", (loser['id'],))
            conn.commit()
            logger.info(f"Deleted {summary} (files: {len(files)})", extra=LOG_D)
        return decisions
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Delete duplicate captures of one utterance by several sentinels.")
    parser.add_argument('--since', help="ISO timestamp; default: last 24 hours")
    parser.add_argument('--until', help="ISO timestamp")
    parser.add_argument('--dry-run', action='store_true',
                        help="report what would be deleted, delete nothing")
    args = parser.parse_args()

    since = args.since or (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    decisions = run(since=since, until=args.until, dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    quiet = [d for d in decisions if d['reason'] == 'low_snr']
    same = [d for d in decisions if d['reason'] == 'same_second']
    dupes = [d for d in decisions if d['reason'] == 'duplicate']
    print(f"\n{verb} {len(decisions)} capture(s) since {since}: "
          f"{len(dupes)} duplicate, {len(same)} same-second, "
          f"{len(quiet)} below the SNR floor\n")
    for d in same:
        loser, winner = d['loser'], d['winner']
        print(f"  {loser['time']:%H:%M:%S}  "
              f"keep {winner['device']}/{winner['room']} "
              f"(SNR {d['snr_winner']:.1f} dB)  "
              f"drop {loser['device']}/{loser['room']} "
              f"(SNR {d['snr_loser']:.1f} dB)  [same second]")
        print(f"      KEEP {(winner['text'] or '')[:70]}")
        print(f"      DROP {(loser['text'] or '')[:70]}")
    for d in dupes:
        loser, winner = d['loser'], d['winner']
        print(f"  corr {d['correlation']:.2f}  "
              f"keep {winner['device']}/{winner['room']} "
              f"(SNR {d['snr_winner']:.1f} dB)  "
              f"drop {loser['device']}/{loser['room']} "
              f"(SNR {d['snr_loser']:.1f} dB)")
        print(f"      KEEP {(winner['text'] or '')[:70]}")
        print(f"      DROP {(loser['text'] or '')[:70]}")
    for d in quiet:
        loser = d['loser']
        print(f"  SNR {d['snr_loser']:.1f} dB  too quiet  "
              f"{loser['device']}/{loser['room']}")
        print(f"      DROP {(loser['text'] or '')[:70]}")


if __name__ == '__main__':
    main()
