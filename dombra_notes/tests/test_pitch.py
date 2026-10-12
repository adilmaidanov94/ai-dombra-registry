"""
Проверка распознавания на синтезированных щипковых звуках с известной высотой.

    python -m unittest dombra_notes.tests.test_pitch -v

Синтетика не заменяет проверку на настоящей домбре, но ловит ошибки алгоритма:
неверную ноту, ошибку на октаву, пропуск повторного щипка, дробление долгой ноты.
"""

import os
import struct
import tempfile
import unittest

import numpy as np

from dombra_notes import pitch
from dombra_notes.tests import synth

SR = 44100
MELODY = [50, 55, 57, 62, 59, 55]          # D3 G3 A3 D4 B3 G3
SCALE = [50, 52, 53, 55, 57, 58, 60, 62]


def hz(midi, cents=0.0):
    return 440.0 * 2 ** ((midi - 69 + cents / 100) / 12)


def midis(notes):
    return [note.midi for note in notes]


class SingleNoteTests(unittest.TestCase):
    def test_pitch_and_cents_over_dombra_range(self):
        for sample_rate in (16000, 44100, 48000):
            for midi, cents in [(50, 12), (55, -8), (57, 0), (62, 25), (67, -17), (74, 5), (79, -30)]:
                with self.subTest(sample_rate=sample_rate, note=pitch.note_name(midi)):
                    notes = pitch.analyze(synth.pluck(hz(midi, cents), 0.9, sample_rate), sample_rate)
                    self.assertEqual(midis(notes), [midi])
                    self.assertAlmostEqual(notes[0].cents, cents, delta=3.0)

    def test_names(self):
        self.assertEqual(pitch.note_name(50), "D3")
        self.assertEqual(pitch.note_name_ru(50), "ре малой октавы")
        self.assertEqual(pitch.note_name_ru(55), "соль малой октавы")
        self.assertEqual(pitch.note_name(60), "C4")
        self.assertEqual(pitch.note_name_ru(69), "ля первой октавы")

    def test_other_reference_pitch(self):
        notes = pitch.analyze(synth.pluck(442.0, 0.8, SR), SR, a4=442.0)
        self.assertEqual(midis(notes), [69])
        self.assertAlmostEqual(notes[0].cents, 0.0, delta=2.0)

    def test_long_note_is_not_split(self):
        for midi in (50, 55, 67):
            with self.subTest(note=pitch.note_name(midi)):
                long_note = synth.pluck(hz(midi), 2.5, SR, decay=1.5)
                self.assertEqual(midis(pitch.analyze(long_note, SR)), [midi])
                self.assertEqual(midis(pitch.analyze(synth.add_noise(long_note, 20), SR)), [midi])


class MelodyTests(unittest.TestCase):
    def setUp(self):
        self.clean = synth.melody([(hz(m), 0.4) for m in MELODY], SR)

    def test_melody_notes_and_timing(self):
        notes = pitch.analyze(self.clean, SR)
        self.assertEqual(midis(notes), MELODY)
        for index, note in enumerate(notes):
            self.assertAlmostEqual(note.start, index * 0.4, delta=0.04)

    def test_repeated_note(self):
        self.assertEqual(midis(pitch.analyze(synth.melody([(hz(55), 0.3)] * 5, SR), SR)), [55] * 5)
        self.assertEqual(midis(pitch.analyze(synth.melody([(hz(62), 0.15)] * 6, SR), SR)), [62] * 6)

    def test_fast_passage(self):
        for duration in (0.12, 0.08):
            with self.subTest(duration=duration):
                signal = synth.melody([(hz(m), duration) for m in SCALE], SR)
                self.assertEqual(midis(pitch.analyze(signal, SR)), SCALE)

    def test_noise(self):
        for snr in (30, 20, 10):
            with self.subTest(snr=snr):
                self.assertEqual(midis(pitch.analyze(synth.add_noise(self.clean, snr), SR)), MELODY)

    def test_weak_fundamental_gives_no_octave_error(self):
        for fundamental in (0.2, 0.0):
            with self.subTest(fundamental=fundamental):
                signal = synth.melody([(hz(m), 0.4) for m in MELODY], SR, fundamental=fundamental)
                self.assertEqual(midis(pitch.analyze(signal, SR)), MELODY)

    def test_quiet_clipped_and_echo(self):
        self.assertEqual(midis(pitch.analyze(self.clean * 0.01 + 0.2, SR)), MELODY)
        self.assertEqual(midis(pitch.analyze(np.clip(self.clean * 4, -1, 1), SR)), MELODY)
        echo = self.clean.copy()
        for delay, gain in ((0.045, 0.35), (0.11, 0.2)):
            shift = int(delay * SR)
            echo[shift:] += gain * self.clean[:-shift]
        self.assertEqual(midis(pitch.analyze(echo, SR)), MELODY)

    def test_pauses_between_notes(self):
        signal = synth.melody([(hz(m), 0.3) for m in MELODY], SR, gap=0.25)
        self.assertEqual(midis(pitch.analyze(signal, SR)), MELODY)

    def test_silence_and_noise_give_no_notes(self):
        rng = np.random.default_rng(3)
        self.assertEqual(pitch.analyze(np.zeros(SR), SR), [])
        self.assertEqual(pitch.analyze(rng.normal(0, 0.1, SR * 2), SR), [])


class FileTests(unittest.TestCase):
    def test_compressed_formats(self):
        import soundfile as sf

        signal = synth.melody([(hz(m), 0.4) for m in MELODY], 48000)
        with tempfile.TemporaryDirectory() as folder:
            for name, options in (("voice.ogg", dict(format="OGG", subtype="OPUS")), ("rec.mp3", dict(format="MP3")), ("rec.wav", {})):
                with self.subTest(file=name):
                    path = os.path.join(folder, name)
                    sf.write(path, signal, 48000, **options)
                    self.assertEqual(midis(pitch.analyze_file(path)), MELODY)

    def test_missing_file(self):
        with self.assertRaises(pitch.AudioError):
            pitch.load_audio("нет-такого-файла.wav")

    def test_midi_file(self):
        notes = pitch.analyze(synth.melody([(hz(m), 0.4) for m in MELODY], SR), SR)
        with tempfile.TemporaryDirectory() as folder:
            path = pitch.write_midi(notes, os.path.join(folder, "melody.mid"))
            data = path.read_bytes()
        self.assertEqual(data[:4], b"MThd")
        length, midi_format, tracks, division = struct.unpack(">IHHH", data[4:14])
        self.assertEqual((length, midi_format, tracks, division), (6, 0, 1, 480))
        self.assertEqual(data[14:18], b"MTrk")
        self.assertEqual(struct.unpack(">I", data[18:22])[0], len(data) - 22)
        self.assertEqual(data[-3:], b"\xff\x2f\x00")
        note_on = [data[i + 1] for i in range(22, len(data) - 2) if data[i] == 0x90 and data[i + 2] > 0 and data[i + 1] in MELODY]
        self.assertGreaterEqual(len(note_on), len(MELODY))

    def test_report_and_json(self):
        notes = pitch.analyze(synth.melody([(hz(m), 0.4) for m in MELODY], SR), SR)
        report = pitch.format_report(notes)
        self.assertIn("D3 G3 A3 D4 B3 G3", report)
        self.assertIn("ре малой октавы", report)
        self.assertIn('"name": "D3"', pitch.notes_to_json(notes))
        self.assertIn("не найдены", pitch.format_report([]))


if __name__ == "__main__":
    unittest.main()
