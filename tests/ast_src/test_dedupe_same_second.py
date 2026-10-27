"""The same-second dedupe rule: one stamp, several sentinels, keep the best SNR.

The correlation floor is set out of reach in most tests (0.999), so nothing can
be linked by audio and any decision observed is the same-second rule's own.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

import numpy as np
import soundfile as sf

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from ast_src import dedupe_events as de  # noqa: E402

SR = 16000
STAMP = datetime(2026, 10, 2, 17, 41, 35)


def make_wav(path, seconds, kind, seed):
    """Gated tone over a noise floor. 'clean' has a low floor, so a high
    envelope dynamic range; 'noisy' a high floor, so a low one."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    gate = (np.sin(2 * np.pi * 1.5 * t + seed) > 0).astype(np.float32)
    tone = 0.5 * np.sin(2 * np.pi * 300 * t) * gate
    floor = {'clean': 0.001, 'noisy': 0.08}[kind]
    sf.write(path, (tone + floor * rng.standard_normal(t.size)).astype(np.float32), SR)


def cfg(**over):
    base = {'min_snr_db': 0.0, 'min_snr_effective_from': None,
            'window_seconds': 30.0, 'min_correlation': 0.999,
            'same_second_enabled': True}
    base.update(over)
    return base


class SameSecondSweep(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def event(self, ident, device, kind, seconds, when=STAMP, words=True, seed=None):
        path = os.path.join(self.dir, f'{ident}_{device}.wav')
        make_wav(path, seconds, kind, seed if seed is not None else ident)
        return {'id': ident, 'time': when, 'device': device, 'room': 'R',
                'file_name': os.path.basename(path), 'spectrogram': None,
                'text': 'a b c' if words else '', 'audio': path,
                'has_words': words}

    def test_the_cleaner_capture_survives_even_when_lengths_differ(self):
        """The 17:41:35 case: 7 s and 18 s, uncorrelated, one stamp."""
        short_clean = self.event(1, 'p4-door-1', 'clean', 7.4)
        long_noisy = self.event(2, 'box3-2', 'noisy', 17.7)
        self.assertGreater(de.snr_db(short_clean['audio']), de.snr_db(long_noisy['audio']))

        decisions = de.find_duplicates([short_clean, long_noisy], cfg())

        self.assertEqual(len(decisions), 1)
        d = decisions[0]
        self.assertEqual(d['reason'], 'same_second')
        self.assertEqual(d['winner']['id'], 1)
        self.assertEqual(d['loser']['id'], 2)
        self.assertIsNone(d['correlation'])          # the audio was not compared
        self.assertGreater(d['snr_winner'], d['snr_loser'])

    def test_three_sentinels_leave_exactly_one(self):
        events = [self.event(1, 'box3-1', 'noisy', 7.8, seed=11),
                  self.event(2, 'p4-door-1', 'clean', 7.4),
                  self.event(3, 'box3-2', 'noisy', 17.7, seed=33)]
        decisions = de.find_duplicates(events, cfg())
        self.assertEqual({d['reason'] for d in decisions}, {'same_second'})
        self.assertEqual(sorted(d['loser']['id'] for d in decisions), [1, 3])
        self.assertEqual({d['winner']['id'] for d in decisions}, {2})

    def test_a_different_second_is_left_alone(self):
        a = self.event(1, 'p4-door-1', 'clean', 7.4)
        b = self.event(2, 'box3-2', 'noisy', 7.4, when=STAMP + timedelta(seconds=1))
        self.assertEqual(de.find_duplicates([a, b], cfg()), [])

    def test_one_sentinel_is_not_its_own_duplicate(self):
        a = self.event(1, 'box3-2', 'clean', 7.4)
        b = self.event(2, 'box3-2', 'noisy', 7.4, seed=9)
        self.assertEqual(de.find_duplicates([a, b], cfg()), [])

    def test_a_deliberate_upload_is_exempt(self):
        a = self.event(1, 'ex', 'clean', 7.4)
        b = self.event(2, 'box3-2', 'noisy', 7.4)
        self.assertEqual(de.find_duplicates([a, b], cfg()), [])

    def test_sub_second_stamps_still_count_as_one_second(self):
        a = self.event(1, 'p4-door-1', 'clean', 7.4, when=STAMP.replace(microsecond=100))
        b = self.event(2, 'box3-2', 'noisy', 7.4, when=STAMP.replace(microsecond=900000))
        self.assertEqual(len(de.find_duplicates([a, b], cfg())), 1)

    def test_the_setting_switches_it_off(self):
        a = self.event(1, 'p4-door-1', 'clean', 7.4)
        b = self.event(2, 'box3-2', 'noisy', 17.7)
        self.assertEqual(de.find_duplicates([a, b], cfg(same_second_enabled=False)), [])

    def test_a_transcript_outranks_a_cleaner_recording_of_it(self):
        """Unchanged from every other group: never delete the only words."""
        clean_silent = self.event(1, 'p4-door-1', 'clean', 7.4, words=False)
        noisy_words = self.event(2, 'box3-2', 'noisy', 7.4, words=True)
        d, = de.find_duplicates([clean_silent, noisy_words], cfg())
        self.assertEqual((d['winner']['id'], d['loser']['id']), (2, 1))

    def test_a_loser_is_not_reported_again_by_correlation(self):
        """With the correlation floor at zero everything would link; the
        same-second loser must already be gone from that pass."""
        a = self.event(1, 'p4-door-1', 'clean', 7.4)
        b = self.event(2, 'box3-2', 'noisy', 7.4)
        c = self.event(3, 'box3-1', 'noisy', 7.4, when=STAMP + timedelta(seconds=5), seed=77)
        decisions = de.find_duplicates([a, b, c], cfg(min_correlation=0.0))
        losers = [d['loser']['id'] for d in decisions]
        self.assertEqual(len(losers), len(set(losers)))
        self.assertEqual(len(losers), 2)             # three captures, one survivor
        self.assertEqual(decisions[0]['reason'] == 'same_second'
                         or decisions[1]['reason'] == 'same_second', True)


class SameSecondPrefilter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def capture(self, stamp, device, kind, seconds, seed):
        path = os.path.join(self.dir, f'{stamp}_{device}_rec.wav')
        make_wav(path, seconds, kind, seed)
        return path

    def prefilter_cfg(self, **over):
        base = {'prefilter_window_seconds': 3.0, 'prefilter_max_duration_diff': 1.5,
                'prefilter_min_correlation': 0.999, 'prefilter_duration_tolerance': 0.2,
                'same_second_enabled': True}
        base.update(over)
        return base

    def test_it_skips_the_noisier_copy_although_the_length_gate_would_not(self):
        clean = self.capture('20261002_174135', 'p4-door-1', 'clean', 7.4, 1)
        noisy = self.capture('20261002_174135', 'box3-2', 'noisy', 17.7, 2)
        plan = de.plan_pending([clean, noisy], self.prefilter_cfg())
        self.assertEqual(list(plan), [noisy])
        self.assertEqual(plan[noisy]['rule'], 'same_second')
        self.assertEqual(plan[noisy]['winner'], clean)
        self.assertIsNone(plan[noisy]['correlation'])
        # With the rule off, that same pair is let through: the length gate
        # (10.3 s > 1.5 s) is what kept it out of the group before.
        self.assertEqual(de.plan_pending([clean, noisy],
                                         self.prefilter_cfg(same_second_enabled=False)), {})

    def test_the_winner_is_the_loudest_not_the_longest(self):
        clean_short = self.capture('20261002_174135', 'p4-door-1', 'clean', 7.4, 1)
        noisy_long = self.capture('20261002_174135', 'box3-2', 'noisy', 17.7, 2)
        plan = de.plan_pending([noisy_long, clean_short], self.prefilter_cfg())
        self.assertNotIn(clean_short, plan)

    def test_different_seconds_are_not_grouped(self):
        a = self.capture('20261002_174135', 'p4-door-1', 'clean', 7.4, 1)
        b = self.capture('20261002_174136', 'box3-2', 'noisy', 7.4, 2)
        self.assertEqual(de.plan_pending([a, b], self.prefilter_cfg()), {})

    def test_a_skip_without_a_correlation_can_be_shelved_and_logged(self):
        clean = self.capture('20261002_174135', 'p4-door-1', 'clean', 7.4, 1)
        noisy = self.capture('20261002_174135', 'box3-2', 'noisy', 17.7, 2)
        plan = de.plan_pending([clean, noisy], self.prefilter_cfg())

        original = de.SKIPPED_DIR
        de.SKIPPED_DIR = os.path.join(self.dir, 'skipped')
        self.addCleanup(setattr, de, 'SKIPPED_DIR', original)
        self.assertTrue(de.shelve_duplicate(noisy, plan[noisy]))

        self.assertFalse(os.path.exists(noisy))
        with open(os.path.join(de.SKIPPED_DIR, 'skipped.jsonl'), encoding='utf-8') as fh:
            row = json.loads(fh.readline())
        self.assertEqual(row['rule'], 'same_second')
        self.assertIsNone(row['correlation'])        # not 0.0: it was never measured


if __name__ == '__main__':
    unittest.main()
