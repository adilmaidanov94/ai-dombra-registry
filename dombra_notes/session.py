"""
Обработка папки с записями одной сессии: ноты, акустические профили, точность ладов,
сводка по инструменту.

    python -m dombra_notes.session recordings/2026-09-18_DOM001
    python -m dombra_notes.session recordings/2026-09-18_DOM001 --tuning D=D3,G=G3

Имена файлов по протоколу (recordings/PROTOCOL.md):
    инструмент_струна_содержание_громкость_устройство_дубль
    DOM001_sD_frets_mf_iphone_t1.m4a   струна ре, все лады подряд: точность ладов и тембр
    DOM001_sG_decay_mf_mac_t1.wav      затухание до тишины: время спада T60
    DOM001_sD_dyn_iphone_t1.m4a        тихо, средне, громко: динамический диапазон
    DOM001_poly_strums_mf_iphone_t1    удары по двум струнам (пока только для справки)
    DOM001_tap_iphone_t1               постукивание по деке: резонансы корпуса
    DOM001_room_iphone                 тишина помещения: уровень шума

Результат в той же папке: notes.csv (строка на ноту) и profile.json (сводка).
Величины тембра сравнимы только между записями одним устройством в одних условиях,
поэтому в сводке они сгруппированы по устройству.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import acoustic, pitch

AUDIO_SUFFIXES = {".wav", ".flac", ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".aif", ".aiff", ".caf", ".3gp", ".mp4"}
DEFAULT_TUNING = {"D": 50, "G": 55, "A": 57, "E": 52, "C": 48, "F": 53, "B": 47}   # открытые струны, малая октава
DYNAMICS = {"p", "mf", "f"}
STRING_TOKEN = re.compile(r"^s([A-G])(#?)(\d)?$")
TAKE_TOKEN = re.compile(r"^t(\d+)$")
MAX_FRET = 30
BETWEEN_FRETS_CENTS = 35.0   # сильнее этого нота считается «между ладами»


@dataclass
class FileInfo:
    path: Path
    instrument: str
    string: str | None = None        # 'D', 'G', 'poly' ...
    content: str = ""
    dynamic: str | None = None
    device: str | None = None
    take: int | None = None
    open_midi: int | None = None


def parse_name(path: Path, tuning: dict) -> FileInfo:
    tokens = path.stem.split("_")
    info = FileInfo(path=path, instrument=tokens[0])
    rest = []
    for token in tokens[1:]:
        match = STRING_TOKEN.match(token)
        if match and info.string is None:
            letter, sharp, octave = match.groups()
            info.string = letter + sharp
            base = pitch.NOTE_NAMES_EN.index(letter + sharp)
            info.open_midi = (int(octave) + 1) * 12 + base if octave else tuning.get(letter + sharp)
        elif token == "poly" and info.string is None:
            info.string = "poly"
        elif token in DYNAMICS:
            info.dynamic = token
        elif TAKE_TOKEN.match(token):
            info.take = int(token[1:])
        else:
            rest.append(token)
    if rest:
        info.content = rest[0]
        if len(rest) > 1:
            info.device = rest[1]
    return info


def parse_tuning(text: str | None) -> dict:
    tuning = dict(DEFAULT_TUNING)
    if not text:
        return tuning
    for item in text.split(","):
        key, _, value = item.partition("=")
        key, value = key.strip().lstrip("s"), value.strip()
        match = re.fullmatch(r"([A-G]#?)(\d)", value)
        if not key or not match:
            raise SystemExit(f"Непонятная настройка «{item}». Пример: --tuning D=D3,G=G3")
        tuning[key] = (int(match.group(2)) + 1) * 12 + pitch.NOTE_NAMES_EN.index(match.group(1))
    return tuning


# ---------------------------------------------------------------------------
# Обработка файлов
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "file", "instrument", "string", "content", "dynamic", "device", "take", "index", "fret",
    "expected", "detected", "in_order", "frequency_hz", "cents", "start_s", "duration_s",
    "peak_db", "attack_ms", "decay_db_per_s", "t60_s", "drop_db", "hnr_db", "noise_ratio",
    "pitch_std_cents", "pitch_drift_cents", "centroid_hz", "band_low", "band_mid", "band_high",
] + [f"h{k}_db" for k in range(1, acoustic.HARMONICS + 1)] + ["inharmonicity"]


def _fmt(value, digits=2):
    if value is None:
        return ""
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.{digits}f}"
    return value


def _fret_warnings(name: str, frets: list[int], notes: list[pitch.Note]) -> list[str]:
    """Проверка записи «все лады подряд»: пропуски, повторы, ноты вне диапазона, ноты между ладами."""
    warnings = []
    if frets and frets[0] != 0 and len(frets) >= 3 and frets == list(range(frets[0], frets[0] + len(frets))):
        shift = frets[0]
        warnings.append(
            f"{name}: лады идут подряд, но первая нота {notes[0].name} на {abs(shift)} полутон(а) "
            f"{'выше' if shift > 0 else 'ниже'} ожидаемой открытой струны; строй отличается от заданного, укажите его через --tuning"
        )
        return warnings
    valid = [f for f in frets if 0 <= f <= MAX_FRET]
    if len(valid) < len(frets):
        warnings.append(f"{name}: {len(frets) - len(valid)} нот вне диапазона струны; проверьте строй или укажите его через --tuning")
    if valid:
        expected = set(range(0, max(valid) + 1))
        missing = sorted(expected - set(valid))
        repeated = sorted({f for f in valid if valid.count(f) > 1})
        if missing:
            warnings.append(f"{name}: не найдены лады {', '.join(map(str, missing))}")
        if repeated:
            warnings.append(f"{name}: лады {', '.join(map(str, repeated))} встречаются больше одного раза")
        if valid != sorted(valid):
            warnings.append(f"{name}: лады идут не по порядку")
    between = [f for f, note in zip(frets, notes) if abs(note.cents) > BETWEEN_FRETS_CENTS]
    if between:
        warnings.append(f"{name}: ноты на ладах {', '.join(map(str, between))} отклоняются больше {BETWEEN_FRETS_CENTS:.0f} центов")
    return warnings


def process_file(info: FileInfo, a4: float) -> tuple[list[dict], dict, list[str]]:
    """Строки CSV, служебные результаты (шум, резонансы) и предупреждения для одного файла."""
    warnings, extra = [], {}
    signal, sample_rate = pitch.load_audio(info.path)

    if info.content == "room":
        extra["room_noise_db"] = acoustic.noise_floor_db(signal, sample_rate)
        return [], extra, warnings
    if info.content == "tap":
        tap = acoustic.tap_resonances(signal, sample_rate)
        extra["tap"] = tap
        if tap.taps == 0:
            warnings.append(f"{info.path.name}: удары по деке не найдены")
        return [], extra, warnings

    notes = pitch.analyze(signal, sample_rate, a4=a4)
    if not notes:
        warnings.append(f"{info.path.name}: ноты не найдены")
        return [], extra, warnings
    with_profile = info.string != "poly"
    profiles = []
    if with_profile:
        track = pitch.yin_track(signal, sample_rate)
        floor = acoustic.noise_floor_db(signal, sample_rate)
        profiles = [acoustic.profile_note(signal, sample_rate, note, track, floor) for note in notes]
    frets = [note.midi - info.open_midi for note in notes] if info.content == "frets" and info.open_midi is not None else None

    rows = []
    for index, note in enumerate(notes):
        row = {
            "file": info.path.name, "instrument": info.instrument, "string": info.string or "", "content": info.content,
            "dynamic": info.dynamic or "", "device": info.device or "", "take": info.take if info.take is not None else "",
            "index": index, "detected": note.name, "frequency_hz": _fmt(note.frequency), "cents": _fmt(note.cents, 1),
            "start_s": _fmt(note.start), "duration_s": _fmt(note.duration),
        }
        if frets is not None:
            # Лад определяется по распознанной высоте, поэтому повтор или пропуск ноты не сдвигает всю таблицу.
            row["fret"] = frets[index]
            row["expected"] = pitch.note_name(info.open_midi + index)
            row["in_order"] = int(frets[index] == index)
        if with_profile:
            p = profiles[index]
            row.update({
                "peak_db": _fmt(p.peak_db, 1), "attack_ms": _fmt(p.attack_ms, 1), "decay_db_per_s": _fmt(p.decay_db_per_s, 1),
                "t60_s": _fmt(p.t60_s), "drop_db": _fmt(p.measured_drop_db, 1), "hnr_db": _fmt(p.hnr_db, 1),
                "noise_ratio": _fmt(p.noise_ratio, 3), "pitch_std_cents": _fmt(p.pitch_std_cents, 1),
                "pitch_drift_cents": _fmt(p.pitch_drift_cents, 1), "centroid_hz": _fmt(p.spectral_centroid_hz, 0),
                "band_low": _fmt(p.band_balance["low"], 3), "band_mid": _fmt(p.band_balance["mid"], 3),
                "band_high": _fmt(p.band_balance["high"], 3), "inharmonicity": _fmt(p.inharmonicity, 6),
            })
            for k, value in enumerate(p.harmonics_db, start=1):
                row[f"h{k}_db"] = _fmt(value, 1)
        rows.append(row)

    if frets is not None:
        warnings += _fret_warnings(info.path.name, frets, notes)
    return rows, extra, warnings


# ---------------------------------------------------------------------------
# Сводка по инструменту
# ---------------------------------------------------------------------------


def _values(values):
    out = []
    for v in values:
        if v in ("", None):
            continue
        v = float(v)
        if not math.isnan(v):
            out.append(v)
    return out


def _mean(values, digits=2):
    values = _values(values)
    return round(statistics.fmean(values), digits) if values else None


def _timbre(rows: list[dict]) -> dict:
    """Средние тембровые величины по группе нот (одна струна, одно устройство)."""
    low = [r for r in rows if r.get("fret", "") == "" or int(r["fret"]) <= 7]   # гармоники: нижние лады, где партиалов больше
    return {
        "notes": len(rows),
        "peak_db": _mean(r["peak_db"] for r in rows),
        "attack_ms": _mean(r["attack_ms"] for r in rows),
        "t60_s": _mean(r["t60_s"] for r in rows),
        "hnr_db": _mean(r["hnr_db"] for r in rows),
        "pitch_std_cents": _mean(r["pitch_std_cents"] for r in rows),
        "centroid_hz": _mean((r["centroid_hz"] for r in rows), 0),
        "band_balance": {name: _mean((r[f"band_{name}"] for r in rows), 3) for name in acoustic.BAND_NAMES},
        "harmonics_db": [_mean((r[f"h{k}_db"] for r in low), 1) for k in range(1, acoustic.HARMONICS + 1)],
        "inharmonicity": _mean((r["inharmonicity"] for r in low), 6),
    }


def summarize(rows: list[dict], extras: list[dict], instrument: str) -> dict:
    mono = [r for r in rows if r["string"] != "poly"]
    poly = [r for r in rows if r["string"] == "poly"]
    profile = {
        "instrument": instrument,
        "notes_total": len(mono),
        "poly_notes": len(poly),
        "strings": {},
        "balance": {},
    }
    devices = sorted({r["device"] for r in mono})
    for string in sorted({r["string"] for r in mono}):
        rows_s = [r for r in mono if r["string"] == string]
        frets_rows = [r for r in rows_s if r["content"] == "frets" and r.get("fret", "") != ""]
        entry = {"expected_open": next((r["expected"] for r in frets_rows if r.get("in_order") == 1), ""), "notes": len(rows_s)}
        if frets_rows:
            by_fret = {}
            for r in frets_rows:
                fret = int(r["fret"])
                if 0 <= fret <= MAX_FRET:
                    by_fret.setdefault(fret, []).append(r)
            intonation = {fret: _mean((r["cents"] for r in group), 1) for fret, group in sorted(by_fret.items())}
            deviations = [v for v in intonation.values() if v is not None]
            entry.update({
                "frets_recorded": len(by_fret),
                "in_order": f"{sum(int(r['in_order']) for r in frets_rows)}/{len(frets_rows)}",
                "intonation_cents": intonation,
                "mean_deviation_cents": round(statistics.fmean(deviations), 1) if deviations else None,
                "max_abs_deviation_cents": round(max(abs(v) for v in deviations), 1) if deviations else None,
                "deviation_std_cents": round(statistics.pstdev(deviations), 1) if len(deviations) > 1 else None,
            })
        timbre = {}
        for device in devices:
            group = [r for r in frets_rows if r["device"] == device and r["dynamic"] in ("mf", "")]
            if group:
                timbre[device] = _timbre(group)
        if timbre:
            entry["timbre"] = timbre
        decay = {}
        for device in devices:
            group = [r for r in rows_s if r["content"] == "decay" and r["device"] == device]
            if group:
                decay[device] = {"notes": len(group), "t60_s": _mean(r["t60_s"] for r in group), "drop_db": _mean((r["drop_db"] for r in group), 1)}
        if decay:
            entry["decay"] = decay
        dynamic = {}
        for device in devices:
            peaks = _values(r["peak_db"] for r in rows_s if r["content"] == "dyn" and r["device"] == device)
            if len(peaks) >= 2:
                dynamic[device] = {"notes": len(peaks), "range_db": round(max(peaks) - min(peaks), 1)}
        if dynamic:
            entry["dynamic"] = dynamic
        profile["strings"][string] = entry

    names = [s for s in profile["strings"] if profile["strings"][s].get("timbre")]
    if len(names) >= 2:
        a, b = names[0], names[1]
        for device in devices:
            ta, tb = profile["strings"][a]["timbre"].get(device), profile["strings"][b]["timbre"].get(device)
            if not (ta and tb):
                continue
            diff = lambda key, digits=1: round(ta[key] - tb[key], digits) if ta[key] is not None and tb[key] is not None else None
            profile["balance"][device] = {
                "strings": [a, b],
                "level_difference_db": diff("peak_db"),
                "t60_difference_s": diff("t60_s", 2),
                "centroid_difference_hz": diff("centroid_hz", 0),
            }
    for extra in extras:
        if "room_noise_db" in extra:
            profile["room_noise_db"] = round(extra["room_noise_db"], 1)
        if "tap" in extra:
            profile["body_resonances"] = extra["tap"].to_dict()
    return profile


def _num(value, digits=1, unit=""):
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}"


def format_summary(profile: dict, warnings: list[str]) -> str:
    lines = [f"Инструмент {profile['instrument']}: нот распознано {profile['notes_total']}"]
    if profile.get("poly_notes"):
        lines.append(f"Записи двух струн (poly): строк в CSV {profile['poly_notes']}; распознавание двух струн пока не поддерживается, данные только для справки")
    for string, s in profile["strings"].items():
        header = f"\nСтруна {string}"
        if s.get("expected_open"):
            header += f" (открытая {s['expected_open']})"
        lines.append(header + f": нот {s['notes']}")
        if "intonation_cents" in s:
            cells = [f"{fret}:{value:+.0f}" for fret, value in s["intonation_cents"].items() if value is not None]
            lines.append(f"  ладов {s['frets_recorded']}, по порядку {s['in_order']}; отклонение от равномерной темперации по ладам, центы:")
            lines.append("    " + " ".join(cells))
            lines.append(
                f"    среднее {_num(s['mean_deviation_cents'])}, макс. {_num(s['max_abs_deviation_cents'])}, разброс {_num(s['deviation_std_cents'])}"
            )
        for device, t in s.get("timbre", {}).items():
            lines.append(
                f"  тембр ({device}, нот {t['notes']}): пик {_num(t['peak_db'], 1, ' дБ')}, атака {_num(t['attack_ms'], 1, ' мс')}, "
                f"T60 по коротким нотам {_num(t['t60_s'], 1, ' с')}, гармоники/шум {_num(t['hnr_db'], 1, ' дБ')}, "
                f"стабильность высоты {_num(t['pitch_std_cents'], 1, ' ц')}, центроид {_num(t['centroid_hz'], 0, ' Гц')}"
            )
            harmonics = ["—" if v is None else f"{v:+.0f}" for v in t["harmonics_db"]]
            balance = t["band_balance"]
            lines.append("    гармоники 1–10 относительно основного тона, дБ: " + " ".join(harmonics))
            lines.append(
                f"    доли энергии низ/середина/верх: {_num(balance['low'], 2)}/{_num(balance['mid'], 2)}/{_num(balance['high'], 2)}, "
                f"негармоничность B {_num(t['inharmonicity'], 6)}"
            )
        for device, d in s.get("decay", {}).items():
            lines.append(f"  затухание ({device}, нот {d['notes']}): T60 {_num(d['t60_s'], 1, ' с')}, спад к концу записи {_num(d['drop_db'], 1, ' дБ')}")
        for device, d in s.get("dynamic", {}).items():
            lines.append(f"  динамика ({device}, нот {d['notes']}): диапазон тихо–громко {_num(d['range_db'], 1, ' дБ')}")
    for device, b in profile.get("balance", {}).items():
        lines.append(
            f"\nБаланс струн {b['strings'][0]}/{b['strings'][1]} ({device}): уровень {_num(b['level_difference_db'], 1, ' дБ')}, "
            f"T60 {_num(b['t60_difference_s'], 2, ' с')}, центроид {_num(b['centroid_difference_hz'], 0, ' Гц')}"
        )
    if "room_noise_db" in profile:
        lines.append(f"\nШум помещения: {profile['room_noise_db']} дБ относительно полной шкалы")
    if profile.get("body_resonances"):
        res = profile["body_resonances"]
        if res["resonances"]:
            lines.append("Резонансы корпуса (ударов %d): " % res["taps"] + ", ".join(f"{r['hz']:.0f} Гц ({r['db']:+.0f} дБ)" for r in res["resonances"]))
        else:
            lines.append("Резонансы корпуса: ударов %d, выраженных пиков не найдено" % res["taps"])
    if warnings:
        lines.append("\nПредупреждения:")
        lines += [f"  - {w}" for w in warnings]
    return "\n".join(lines)


def run(folder: Path, tuning: dict, a4: float = 440.0, quiet: bool = False) -> tuple[dict, list[dict], list[str]]:
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES and not p.name.startswith("."))
    if not files:
        raise SystemExit(f"В папке {folder} нет звуковых файлов.")
    rows, extras, warnings, instruments = [], [], [], set()
    for path in files:
        info = parse_name(path, tuning)
        instruments.add(info.instrument)
        try:
            file_rows, extra, file_warnings = process_file(info, a4)
        except pitch.AudioError as error:
            warnings.append(f"{path.name}: {error}")
            continue
        rows += file_rows
        extras.append(extra)
        warnings += file_warnings
        if not quiet:
            print(f"{path.name}: {'нот ' + str(len(file_rows)) if file_rows else 'служебная запись'}", file=sys.stderr)
    profile = summarize(rows, extras, "/".join(sorted(instruments)))
    profile["files"] = [p.name for p in files]
    profile["warnings"] = warnings
    profile["settings"] = {
        "a4": a4,
        "tuning": {name: pitch.note_name(midi) for name, midi in tuning.items()},
        "generated": datetime.now().isoformat(timespec="seconds"),
    }

    with (folder / "notes.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: row.get(k, "") for k in CSV_FIELDS} for row in rows)
    (folder / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return profile, rows, warnings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dombra_notes.session", description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", help="папка с записями сессии")
    parser.add_argument("--tuning", help="открытые струны, например D=D3,G=G3 (по умолчанию малая октава)")
    parser.add_argument("--a4", type=float, default=440.0, help="частота ля первой октавы, по умолчанию 440")
    args = parser.parse_args(argv)
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Папка не найдена: {folder}", file=sys.stderr)
        return 1
    profile, rows, warnings = run(folder, parse_tuning(args.tuning), args.a4)
    print()
    print(format_summary(profile, warnings))
    print(f"\nСохранено: {folder / 'notes.csv'}, {folder / 'profile.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
