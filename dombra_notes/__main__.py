"""
Запуск из папки проекта:

    python -m dombra_notes запись.wav
    python -m dombra_notes запись.m4a --midi            # рядом появится запись.mid
    python -m dombra_notes запись.wav --json ноты.json --a4 442
"""

import argparse
import sys
from pathlib import Path

from . import pitch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dombra_notes",
        description="Распознаёт ноты в одноголосной записи домбры (WAV, OGG, MP3, FLAC; M4A — через ffmpeg или afconvert).",
    )
    parser.add_argument("files", nargs="+", help="звуковые файлы")
    parser.add_argument("--midi", nargs="?", const="", metavar="ФАЙЛ", help="сохранить MIDI (без имени — рядом с записью)")
    parser.add_argument("--json", metavar="ФАЙЛ", help="сохранить результат в JSON")
    parser.add_argument("--a4", type=float, default=440.0, help="частота ля первой октавы, Гц (по умолчанию 440)")
    parser.add_argument("--fmin", type=float, default=70.0, help="нижняя граница поиска, Гц")
    parser.add_argument("--fmax", type=float, default=1200.0, help="верхняя граница поиска, Гц")
    parser.add_argument("--min-note", type=float, default=0.05, help="минимальная длительность ноты, с")
    parser.add_argument("--sensitivity", type=float, default=1.0,
                        help="чувствительность к повторным щипкам (больше 1 — делит ноты охотнее)")
    args = parser.parse_args(argv)

    status = 0
    for name in args.files:
        path = Path(name)
        print(f"== {path.name} ==")
        try:
            signal, sample_rate = pitch.load_audio(path)
            notes = pitch.analyze(
                signal, sample_rate, a4=args.a4, fmin=args.fmin, fmax=args.fmax,
                min_duration=args.min_note, onset_sensitivity=args.sensitivity,
            )
        except pitch.AudioError as error:
            print(f"Ошибка: {error}", file=sys.stderr)
            status = 1
            continue
        print(f"Длительность записи: {len(signal) / sample_rate:.1f} с, частота дискретизации {sample_rate} Гц")
        print(pitch.format_report(notes))
        if args.midi is not None and notes:
            target = Path(args.midi) if args.midi else path.with_suffix(".mid")
            pitch.write_midi(notes, target)
            print(f"MIDI сохранён: {target}")
        if args.json:
            Path(args.json).write_text(pitch.notes_to_json(notes), encoding="utf-8")
            print(f"JSON сохранён: {args.json}")
        print()
    return status


if __name__ == "__main__":
    sys.exit(main())
