"""
Проверка акустического профиля на сигналах с заранее известными параметрами.

    python -m unittest dombra_notes.tests.test_acoustic -v

Каждая величина проверяется на синтетике, где правильный ответ известен точно:
скорость затухания, уровни гармоник, негармоничность, глубина вибрато, время атаки,
резонансы корпуса, реакция на шум.
"""

import json
import math
import unittest

import numpy as np

from dombra_notes import acoustic, pitch
from dombra_notes.tests import synth

SR = 44100


def hz(midi, cents=0.0):
    return 440.0 * 2 ** ((midi - 69 + cents / 100) / 12)


def decaying_tone(frequency, seconds, tau, levels_db=(0.0, -6.0, -12.0, -20.0, -30.0), inharmonicity=0.0,
                  vibrato_cents=0.0, vibrato_hz=5.0, ramp=0.002, sample_rate=SR):
    """Сумма партиалов с общей экспонентой затухания; уровни партиалов заданы в дБ относительно основного тона."""
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    modulation = vibrato_cents / 1200.0 * np.sin(2 * np.pi * vibrato_hz * t)
    phase_base = 2 * np.pi * np.cumsum(frequency * 2 ** modulation) / sample_rate
    signal = np.zeros_like(t)
    for k, level in enumerate(levels_db, start=1):
        stretch = math.sqrt(1.0 + inharmonicity * k * k)
        signal += 10 ** (level / 20.0) * np.sin(k * stretch * phase_base + 0.3 * k)
    signal *= np.exp(-t / tau) * np.minimum(1.0, t / ramp)
    return 0.5 * signal / np.max(np.abs(signal))


def single_profile(signal, sample_rate=SR, allow_extra=False):
    notes, profiles, _ = acoustic.profile_signal(signal, sample_rate)
    assert notes and (allow_extra or len(notes) == 1), [n.name for n in notes]
    return profiles[0]


class DecayTests(unittest.TestCase):
    def test_t60_matches_known_decay_rate(self):
        for tau in (0.35, 0.5, 1.0):
            with self.subTest(tau=tau):
                p = single_profile(decaying_tone(hz(55), 4.0, tau))
                expected_rate = 20 * math.log10(math.e) / tau      # дБ/с
                self.assertAlmostEqual(p.decay_db_per_s, expected_rate, delta=0.06 * expected_rate)
                self.assertAlmostEqual(p.t60_s, 60.0 / expected_rate, delta=0.06 * 60.0 / expected_rate)

    def test_t60_survives_moderate_noise(self):
        clean = decaying_tone(hz(50), 4.0, 0.5)
        p = single_profile(synth.add_noise(clean, 30))
        self.assertAlmostEqual(p.t60_s, 3.45, delta=0.4)
        self.assertGreater(p.measured_drop_db, 25.0)

    def test_attack_time(self):
        for midi in (50, 62):
            for ramp, low, high in ((0.002, 0.0, 5.0), (0.010, 5.0, 12.0), (0.020, 11.0, 21.0), (0.050, 30.0, 48.0)):
                with self.subTest(note=pitch.note_name(midi), ramp=ramp):
                    p = single_profile(decaying_tone(hz(midi), 1.5, 0.8, ramp=ramp))
                    self.assertGreaterEqual(p.attack_ms, low)
                    self.assertLessEqual(p.attack_ms, high)


class SpectrumTests(unittest.TestCase):
    def test_harmonic_levels(self):
        levels = (0.0, -6.0, -12.0, -20.0, -30.0)
        p = single_profile(decaying_tone(hz(55), 1.5, 1.0, levels_db=levels))
        for k, expected in enumerate(levels):
            self.assertAlmostEqual(p.harmonics_db[k], expected, delta=1.5, msg=f"гармоника {k + 1}")
        for k in range(len(levels), acoustic.HARMONICS):
            self.assertTrue(math.isnan(p.harmonics_db[k]), f"гармоника {k + 1} должна отсутствовать")
        self.assertLess(p.inharmonicity, 5e-5)

    def test_inharmonicity(self):
        levels = (0.0, -3.0, -6.0, -9.0, -12.0, -15.0, -18.0, -21.0)
        for b in (0.0002, 0.0005, 0.001):
            with self.subTest(B=b):
                p = single_profile(decaying_tone(hz(50), 1.5, 1.0, levels_db=levels, inharmonicity=b))
                self.assertAlmostEqual(p.inharmonicity, b, delta=0.15 * b)

    def test_centroid_and_band_balance(self):
        dark = single_profile(decaying_tone(hz(50), 1.2, 1.0, levels_db=(0.0, -30.0)))
        bright = single_profile(decaying_tone(hz(50), 1.2, 1.0, levels_db=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)))
        self.assertLess(dark.spectral_centroid_hz, 200.0)
        self.assertGreater(bright.spectral_centroid_hz, 400.0)
        self.assertGreater(dark.band_balance["low"], 0.95)
        self.assertGreater(bright.band_balance["mid"], 0.5)
        for p in (dark, bright):
            self.assertAlmostEqual(sum(p.band_balance.values()), 1.0, delta=0.02)


class PitchStabilityTests(unittest.TestCase):
    def test_vibrato_depth(self):
        steady = single_profile(decaying_tone(hz(62), 2.0, 1.5))
        vibrato = single_profile(decaying_tone(hz(62), 2.0, 1.5, vibrato_cents=15.0))
        self.assertLess(steady.pitch_std_cents, 2.0)
        self.assertAlmostEqual(vibrato.pitch_std_cents, 15.0 / math.sqrt(2), delta=2.5)

    def test_hnr_falls_with_noise(self):
        clean = decaying_tone(hz(55), 1.5, 1.0)
        # При сильном шуме хвост ноты может распознаться как лишняя нота; здесь важна только первая.
        hnr = [single_profile(synth.add_noise(clean, snr), allow_extra=True).hnr_db for snr in (40, 20, 10)]
        self.assertGreater(hnr[0], hnr[1])
        self.assertGreater(hnr[1], hnr[2])
        self.assertGreater(hnr[0], 25.0)
        self.assertLess(hnr[2], 20.0)


class UtilityTests(unittest.TestCase):
    def test_noise_floor(self):
        rng = np.random.default_rng(0)
        for level in (-40.0, -60.0):
            with self.subTest(level=level):
                noise = rng.normal(0, 10 ** (level / 20.0), 3 * SR)
                self.assertAlmostEqual(acoustic.noise_floor_db(noise, SR), level, delta=2.0)

    def test_tap_resonances(self):
        rng = np.random.default_rng(3)
        modes = (180.0, 319.0, 560.0)
        parts = [np.zeros(int(0.3 * SR))]
        for _ in range(4):
            t = np.arange(int(0.5 * SR)) / SR
            tap = sum((1.0 / (j + 1)) * np.exp(-t / 0.1) * np.sin(2 * np.pi * f * t) for j, f in enumerate(modes))
            tap = tap + rng.normal(0, 0.5, len(t)) * np.exp(-t / 0.003)
            parts += [0.6 * tap / np.max(np.abs(tap)), np.zeros(int(0.4 * SR))]
        profile = acoustic.tap_resonances(synth.add_noise(np.concatenate(parts), 45), SR)
        self.assertEqual(profile.taps, 4)
        found = sorted(f for f, _ in profile.resonances)
        for mode in modes:
            self.assertTrue(any(abs(f - mode) <= 5.0 for f in found), f"резонанс {mode} Гц не найден: {found}")
        self.assertEqual(profile.resonances[0][1], 0.0)    # уровни отсчитываются от самого сильного пика
        self.assertEqual(acoustic.tap_resonances(np.zeros(SR), SR).taps, 0)

    def test_profile_serialises_to_json(self):
        p = single_profile(decaying_tone(hz(55), 1.0, 1.0, levels_db=(0.0, -6.0)))
        data = json.loads(json.dumps(p.to_dict(), ensure_ascii=False))
        self.assertEqual(data["name"], "G3")
        self.assertIsNone(data["harmonics_db"][9])           # отсутствующая гармоника → null, не NaN
        self.assertEqual(set(data["band_balance"]), set(acoustic.BAND_NAMES))
        self.assertIn("t60_s", data)

    def test_profiles_follow_notes_of_a_melody(self):
        melody = synth.melody([(hz(m), 0.6) for m in (50, 55, 57, 62)], SR, gap=0.3, decay=1.0)
        notes, profiles, floor = acoustic.profile_signal(synth.add_noise(melody, 50), SR)
        self.assertEqual([n.name for n in notes], ["D3", "G3", "A3", "D4"])
        self.assertEqual([p.name for p in profiles], ["D3", "G3", "A3", "D4"])
        self.assertLess(floor, -45.0)
        for p in profiles:
            self.assertGreater(p.peak_db, -20.0)
            self.assertAlmostEqual(p.harmonics_db[0], 0.0)
            self.assertLess(p.harmonics_db[1], 0.0)
            self.assertGreater(p.hnr_db, 15.0)


if __name__ == "__main__":
    unittest.main()
