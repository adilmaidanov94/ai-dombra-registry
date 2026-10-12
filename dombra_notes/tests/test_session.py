"""
Проверка режима сессии: разбор имён файлов, обработка папки с записями, сводка и предупреждения.

    python -m unittest dombra_notes.tests.test_session -v
"""

import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from dombra_notes import session
from dombra_notes.tests import synth

SR = 44100


def hz(midi, cents=0.0):
    return 440.0 * 2 ** ((midi - 69 + cents / 100) / 12)


def sequence(midis_and_cents, level=0.6, seed=0, note_seconds=0.9, gap=0.35):
    parts = [np.zeros(int(0.3 * SR))]
    for i, (midi, cents) in enumerate(midis_and_cents):
        parts.append(level * synth.pluck(hz(midi, cents), note_seconds, SR, seed=seed + i, decay=1.2))
        parts.append(np.zeros(int(gap * SR)))
    return synth.add_noise(np.concatenate(parts), 45, seed=seed + 100)


def taps(modes=(180.0, 319.0, 560.0), count=3):
    rng = np.random.default_rng(3)
    parts = [np.zeros(int(0.3 * SR))]
    for _ in range(count):
        t = np.arange(int(0.5 * SR)) / SR
        tap = sum((1.0 / (j + 1)) * np.exp(-t / 0.1) * np.sin(2 * np.pi * f * t) for j, f in enumerate(modes))
        tap = tap + rng.normal(0, 0.5, len(t)) * np.exp(-t / 0.003)
        parts += [0.6 * tap / np.max(np.abs(tap)), np.zeros(int(0.4 * SR))]
    return synth.add_noise(np.concatenate(parts), 45)


class NameParsingTests(unittest.TestCase):
    def test_protocol_names(self):
        tuning = session.parse_tuning(None)
        info = session.parse_name(Path("DOM001_sD_frets_mf_iphone_t1.m4a"), tuning)
        self.assertEqual((info.instrument, info.string, info.content, info.dynamic, info.device, info.take, info.open_midi),
                         ("DOM001", "D", "frets", "mf", "iphone", 1, 50))
        info = session.parse_name(Path("DOM-KZ-2026-000001_sG3_decay_f_mac_t2.wav"), tuning)
        self.assertEqual((info.instrument, info.string, info.content, info.dynamic, info.device, info.take, info.open_midi),
                         ("DOM-KZ-2026-000001", "G", "decay", "f", "mac", 2, 55))
        info = session.parse_name(Path("DOM001_poly_strums_mf_iphone_t1.wav"), tuning)
        self.assertEqual((info.string, info.content, info.device), ("poly", "strums", "iphone"))
        info = session.parse_name(Path("DOM001_room_iphone.wav"), tuning)
        self.assertEqual((info.string, info.content, info.device, info.take), (None, "room", "iphone", None))
        info = session.parse_name(Path("DOM001_tap_iphone_t1.wav"), tuning)
        self.assertEqual((info.content, info.device, info.take), ("tap", "iphone", 1))
        info = session.parse_name(Path("DOM001_sG_melody1_mf_android_t1.m4a"), tuning)
        self.assertEqual((info.string, info.content, info.device), ("G", "melody1", "android"))

    def test_tuning_option(self):
        self.assertEqual(session.parse_tuning("D=D3,G=G3")["D"], 50)
        self.assertEqual(session.parse_tuning("sG=F#3")["G"], 54)
        self.assertEqual(session.parse_tuning("D=C3")["G"], 55)          # остальные струны остаются по умолчанию
        with self.assertRaises(SystemExit):
            session.parse_tuning("D=ре")


class SessionRunTests(unittest.TestCase):
    def test_folder_gives_csv_profile_and_summary(self):
        offsets_d = [-8.0, -4.0, 0.0, 4.0, 8.0, -8.0]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            sf.write(folder / "DOM001_sD_frets_mf_iphone_t1.wav", sequence([(50 + i, c) for i, c in enumerate(offsets_d)]), SR)
            sf.write(folder / "DOM001_sD_frets_mf_iphone_t2.wav", sequence([(50 + i, c + 2.0) for i, c in enumerate(offsets_d)], seed=50), SR)
            sf.write(folder / "DOM001_sG_frets_mf_iphone_t1.wav", sequence([(55 + i, 0.0) for i in range(4)], level=0.4, seed=90), SR)
            sf.write(folder / "DOM001_sG_decay_mf_iphone_t1.wav", sequence([(55, 0.0)], seed=70, note_seconds=3.0), SR)
            sf.write(folder / "DOM001_sD_dyn_iphone_t1.wav", np.concatenate([
                sequence([(50, 0.0)], level=0.15, seed=7), sequence([(50, 0.0)], level=0.6, seed=7), sequence([(50, 0.0)], level=0.9, seed=7)]), SR)
            sf.write(folder / "DOM001_poly_strums_mf_iphone_t1.wav", sequence([(50, 0.0), (55, 0.0)], seed=30), SR)
            sf.write(folder / "DOM001_tap_iphone_t1.wav", taps(), SR)
            rng = np.random.default_rng(1)
            sf.write(folder / "DOM001_room_iphone.wav", rng.normal(0, 10 ** (-55 / 20), 3 * SR), SR)
            (folder / "notes.txt").write_text("заметки", encoding="utf-8")

            profile, rows, warnings = session.run(folder, session.parse_tuning(None), quiet=True)

            self.assertEqual(warnings, [])
            self.assertEqual(profile["instrument"], "DOM001")
            self.assertEqual(profile["poly_notes"], 2)
            self.assertEqual(set(profile["strings"]), {"D", "G"})

            d = profile["strings"]["D"]
            self.assertEqual((d["expected_open"], d["frets_recorded"], d["in_order"]), ("D3", 6, "12/12"))
            for fret, expected in enumerate(offsets_d):
                self.assertAlmostEqual(d["intonation_cents"][fret], expected + 1.0, delta=2.5, msg=f"лад {fret}")
            self.assertAlmostEqual(d["max_abs_deviation_cents"], 9.0, delta=2.5)
            self.assertIn("iphone", d["timbre"])
            self.assertEqual(d["timbre"]["iphone"]["notes"], 12)
            self.assertEqual(d["timbre"]["iphone"]["harmonics_db"][0], 0.0)
            self.assertLess(d["timbre"]["iphone"]["harmonics_db"][1], -3.0)
            self.assertAlmostEqual(d["dynamic"]["iphone"]["range_db"], 20 * np.log10(0.9 / 0.15), delta=1.0)

            g = profile["strings"]["G"]
            self.assertEqual(g["frets_recorded"], 4)
            self.assertAlmostEqual(g["decay"]["iphone"]["t60_s"], 8.3, delta=1.5)   # exp(-t/1.2) → 7.24 дБ/с
            self.assertGreater(g["decay"]["iphone"]["drop_db"], 15.0)

            balance = profile["balance"]["iphone"]
            self.assertEqual(balance["strings"], ["D", "G"])
            self.assertGreater(balance["level_difference_db"], 1.0)        # струна D записана громче
            self.assertAlmostEqual(profile["room_noise_db"], -55.0, delta=2.0)
            self.assertEqual(profile["body_resonances"]["taps"], 3)
            self.assertTrue(any(abs(r["hz"] - 180.0) < 5 for r in profile["body_resonances"]["resonances"]))
            self.assertEqual(profile["settings"]["tuning"]["D"], "D3")

            with (folder / "notes.csv").open(encoding="utf-8") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(len(csv_rows), len(rows))
            self.assertEqual(csv_rows[0].keys(), set(session.CSV_FIELDS))
            fret_rows = [r for r in csv_rows if r["content"] == "frets" and r["string"] == "D" and r["take"] == "1"]
            self.assertEqual([r["fret"] for r in fret_rows], [str(i) for i in range(6)])
            self.assertEqual([r["detected"] for r in fret_rows], ["D3", "D#3", "E3", "F3", "F#3", "G3"])
            self.assertTrue(all(r["t60_s"] for r in fret_rows))
            poly_rows = [r for r in csv_rows if r["string"] == "poly"]
            self.assertEqual(poly_rows[0]["t60_s"], "")                    # без профиля

            saved = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["strings"]["D"]["frets_recorded"], 6)
            text = session.format_summary(profile, warnings)
            self.assertIn("Струна D (открытая D3)", text)
            self.assertIn("Резонансы корпуса", text)
            self.assertIn("диапазон тихо–громко", text)

    def test_warnings_for_imperfect_fret_recordings(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            # лад 2 сыгран дважды, лад 4 пропущен, лад 6 на +45 центов
            played = [(50, 0.0), (51, 0.0), (52, 0.0), (52, 0.0), (53, 0.0), (55, 0.0), (56, 45.0)]
            sf.write(folder / "DOM002_sD_frets_mf_iphone_t1.wav", sequence(played), SR)
            # струна «соль» настроена на полтона ниже
            sf.write(folder / "DOM002_sG_frets_mf_iphone_t1.wav", sequence([(54 + i, 0.0) for i in range(4)], seed=40), SR)
            profile, rows, warnings = session.run(folder, session.parse_tuning(None), quiet=True)
            text = "\n".join(warnings)
            self.assertIn("не найдены лады 4", text)
            self.assertIn("лады 2 встречаются больше одного раза", text)
            self.assertIn("ладах 6 отклоняются больше 35 центов", text)
            self.assertIn("F#3 на 1 полутон(а) ниже", text)
            self.assertEqual(profile["strings"]["D"]["frets_recorded"], 6)
            self.assertEqual(profile["strings"]["D"]["in_order"], "5/7")   # индексы 3 и 4 сдвинуты повтором

            # с указанным строем предупреждение о струне соль исчезает
            profile, rows, warnings = session.run(folder, session.parse_tuning("G=F#3"), quiet=True)
            self.assertFalse(any("полутон" in w for w in warnings))
            self.assertEqual(profile["strings"]["G"]["in_order"], "4/4")

    def test_empty_folder_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                session.run(Path(tmp), session.parse_tuning(None), quiet=True)


if __name__ == "__main__":
    unittest.main()
