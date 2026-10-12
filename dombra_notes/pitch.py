"""
Распознавание нот в одноголосной записи домбры: звук → последовательность нот.

Как это работает
  1. Запись читается и сводится в моно (load_audio).
  2. Для каждого кадра (шаг 10 мс) алгоритм YIN оценивает основную частоту
     и «непериодичность» — чем она меньше, тем увереннее оценка (yin_track).
  3. Кадры объединяются в ноты: новая нота начинается при смене высоты или
     при новом щипке, который виден по скачку громкости (segment_notes).
  4. Для каждой ноты считаются название, частота и отклонение в центах от
     равномерно темперированного строя (ля первой октавы = 440 Гц).

Ограничение: алгоритм рассчитан на один звук в каждый момент времени.
Если звучат две струны сразу, результат будет ненадёжным.

Зависимости: numpy, soundfile. Для форматов, которые soundfile не читает
(например, m4a из «Диктофона»), используется ffmpeg или afconvert (есть в macOS).
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

NOTE_NAMES_EN = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
NOTE_NAMES_RU = ("до", "до♯", "ре", "ре♯", "ми", "фа", "фа♯", "соль", "соль♯", "ля", "ля♯", "си")
OCTAVES_RU = {
    0: "субконтроктавы", 1: "контроктавы", 2: "большой октавы", 3: "малой октавы",
    4: "первой октавы", 5: "второй октавы", 6: "третьей октавы", 7: "четвёртой октавы",
    8: "пятой октавы",
}


class AudioError(Exception):
    """Файл не удалось прочитать как звук."""


# ---------------------------------------------------------------------------
# Ноты
# ---------------------------------------------------------------------------


def hz_to_midi(frequency, a4: float = 440.0):
    """Частота → дробный номер MIDI (69 = ля первой октавы)."""
    return 69.0 + 12.0 * np.log2(np.asarray(frequency, dtype=float) / a4)


def midi_to_hz(midi, a4: float = 440.0):
    return a4 * 2.0 ** ((np.asarray(midi, dtype=float) - 69.0) / 12.0)


def note_name(midi: int) -> str:
    """Научная запись: 50 → 'D3', 60 → 'C4'."""
    return f"{NOTE_NAMES_EN[midi % 12]}{midi // 12 - 1}"


def note_name_ru(midi: int) -> str:
    """Русское название: 50 → 'ре малой октавы'."""
    octave = OCTAVES_RU.get(midi // 12 - 1, f"октавы {midi // 12 - 1}")
    return f"{NOTE_NAMES_RU[midi % 12]} {octave}"


@dataclass
class Note:
    start: float        # начало, с
    end: float          # конец, с
    midi: int           # номер ноты MIDI
    frequency: float    # измеренная частота, Гц
    cents: float        # отклонение от точной высоты ноты, центы
    confidence: float   # уверенность 0..1
    level_db: float     # громкость относительно самого громкого места записи, дБ

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def name(self) -> str:
        return note_name(self.midi)

    @property
    def name_ru(self) -> str:
        return note_name_ru(self.midi)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(name=self.name, name_ru=self.name_ru, duration=self.duration)
        return {key: (round(value, 4) if isinstance(value, float) else value) for key, value in data.items()}


# ---------------------------------------------------------------------------
# Чтение звука
# ---------------------------------------------------------------------------


def _convert_to_wav(path: Path, target: Path) -> None:
    """Перевод в WAV внешней программой: ffmpeg, а на Mac — встроенный afconvert."""
    if shutil.which("ffmpeg"):
        command = ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-ac", "1", "-f", "wav", str(target)]
    elif shutil.which("afconvert"):
        command = ["afconvert", "-f", "WAVE", "-d", "LEI16", str(path), str(target)]
    else:
        raise AudioError(
            f"Формат файла «{path.name}» не читается напрямую. Установите ffmpeg "
            "или сохраните запись в WAV."
        )
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not target.exists():
        raise AudioError(f"Не удалось преобразовать «{path.name}»: {result.stderr.strip()[:300]}")


def load_audio(path) -> tuple[np.ndarray, int]:
    """Прочитать запись. Возвращает (моно-сигнал float32 в диапазоне −1..1, частота дискретизации)."""
    import soundfile as sf

    path = Path(path)
    if not path.exists():
        raise AudioError(f"Файл не найден: {path}")
    try:
        samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception:
        with tempfile.TemporaryDirectory() as folder:
            wav_path = Path(folder) / "converted.wav"
            _convert_to_wav(path, wav_path)
            samples, sample_rate = sf.read(str(wav_path), dtype="float32", always_2d=True)
    if samples.size == 0:
        raise AudioError(f"В файле «{path.name}» нет звука.")
    return samples.mean(axis=1).astype(np.float32), int(sample_rate)


# ---------------------------------------------------------------------------
# YIN: оценка основной частоты по кадрам
# ---------------------------------------------------------------------------


@dataclass
class PitchTrack:
    times: np.ndarray          # время центра кадра, с
    frequency: np.ndarray      # оценка частоты, Гц
    aperiodicity: np.ndarray   # 0 — чистый тон, 1 — шум
    level_db: np.ndarray       # громкость кадра относительно максимума, дБ
    hop: float                 # шаг между кадрами, с


def _frames(signal: np.ndarray, size: int, hop: int) -> np.ndarray:
    count = 1 + (len(signal) - size) // hop
    index = np.arange(size)[None, :] + hop * np.arange(count)[:, None]
    return signal[index]


def yin_track(
    signal: np.ndarray,
    sample_rate: int,
    *,
    fmin: float = 70.0,
    fmax: float = 1200.0,
    window: float = 0.03,
    hop: float = 0.01,
    threshold: float = 0.15,
) -> PitchTrack:
    """Покадровая оценка высоты алгоритмом YIN (de Cheveigné, Kawahara, 2002)."""
    signal = np.asarray(signal, dtype=np.float64)
    w = int(round(window * sample_rate))
    hop_n = max(1, int(round(hop * sample_rate)))
    tau_min = max(2, int(sample_rate / fmax))
    tau_max = int(math.ceil(sample_rate / fmin))
    size = w + tau_max + 1
    if len(signal) < size:
        signal = np.pad(signal, (0, size - len(signal)))
    nfft = 1 << (size - 1).bit_length()

    count = 1 + (len(signal) - size) // hop_n
    frequency = np.zeros(count)
    aperiodicity = np.ones(count)
    energy = np.zeros(count)
    taus = np.arange(1, tau_max + 1)

    chunk = 1024  # кадры обрабатываются порциями, чтобы не занимать много памяти
    for begin in range(0, count, chunk):
        stop = min(count, begin + chunk)
        segment = signal[begin * hop_n : (stop - 1) * hop_n + size]
        x = _frames(segment, size, hop_n)
        x = x - x.mean(axis=1, keepdims=True)

        # Разностная функция d(τ) = Σ (x[j] − x[j+τ])² через энергию и корреляцию.
        head = np.zeros_like(x)
        head[:, :w] = x[:, :w]
        correlation = np.fft.irfft(np.conj(np.fft.rfft(head, nfft)) * np.fft.rfft(x, nfft), nfft)[:, : tau_max + 1]
        squares = np.concatenate([np.zeros((x.shape[0], 1)), np.cumsum(x * x, axis=1)], axis=1)
        energy_tau = squares[:, w : w + tau_max + 1] - squares[:, : tau_max + 1]
        difference = energy_tau[:, :1] + energy_tau - 2.0 * correlation
        difference[:, 0] = 0.0
        np.maximum(difference, 0.0, out=difference)

        # Нормированная разностная функция d'(τ).
        running = np.cumsum(difference[:, 1:], axis=1)
        normalized = np.ones_like(difference)
        normalized[:, 1:] = difference[:, 1:] * taus / np.maximum(running, 1e-12)

        energy[begin:stop] = energy_tau[:, 0] / w
        for row in range(x.shape[0]):
            curve = normalized[row]
            below = np.flatnonzero(curve[tau_min:tau_max] < threshold)
            if below.size:
                tau = int(below[0]) + tau_min
                while tau + 1 < tau_max and curve[tau + 1] < curve[tau]:
                    tau += 1
            else:
                tau = int(np.argmin(curve[tau_min:tau_max])) + tau_min
            refined = float(tau)
            if 1 <= tau < tau_max:
                left, mid, right = curve[tau - 1], curve[tau], curve[tau + 1]
                denominator = left - 2.0 * mid + right
                if denominator > 1e-12:
                    refined = tau + 0.5 * (left - right) / denominator
            frequency[begin + row] = sample_rate / refined
            aperiodicity[begin + row] = min(1.0, max(0.0, float(curve[tau])))

    level = 10.0 * np.log10(np.maximum(energy, 1e-14))
    level_db = level - level.max() if count else level
    times = (np.arange(count) * hop_n + w / 2) / sample_rate
    return PitchTrack(times, frequency, aperiodicity, level_db, hop_n / sample_rate)


# ---------------------------------------------------------------------------
# Кадры → ноты
# ---------------------------------------------------------------------------


def _onsets(signal: np.ndarray, sample_rate: int, hop: float, sensitivity: float = 1.0) -> np.ndarray:
    """
    Моменты щипков по «спектральному потоку»: в момент щипка во всём спектре разом
    появляется новая энергия (шум атаки и вернувшиеся верхние гармоники). Это заметно
    даже тогда, когда общая громкость почти не выросла, например при повторе той же ноты.
    Возвращает время щипков в секундах.
    """
    signal = np.asarray(signal, dtype=np.float64)
    peak = np.max(np.abs(signal)) if len(signal) else 0.0
    hop_n = max(1, int(round(hop * sample_rate)))
    size = 1 << int(round(math.log2(0.023 * sample_rate)))
    if peak <= 0 or len(signal) < size + 3 * hop_n:
        return np.zeros(0)
    # Тишина в начале нужна, чтобы первый щипок записи тоже был виден как рост энергии.
    lead = size
    signal = np.concatenate([np.zeros(lead), signal])
    frames = _frames(signal / peak, size, hop_n) * np.hanning(size)
    spectrum = np.log1p(100.0 * np.abs(np.fft.rfft(frames, axis=1)))
    flux = np.zeros(len(spectrum))
    flux[2:] = np.maximum(spectrum[2:] - spectrum[:-2], 0.0).sum(axis=1)

    # Насколько пик выделяется над соседними кадрами (фоновый шум даёт ровный высокий уровень).
    prominence = np.zeros_like(flux)
    for i in range(len(flux)):
        prominence[i] = flux[i] - np.median(flux[max(0, i - 15) : i + 16])
    floor = max(0.2 * spectrum.shape[1], 0.12 * prominence.max()) / sensitivity

    accepted: list[tuple[int, float]] = []
    for i in range(2, len(flux) - 1):
        lo, hi = max(0, i - 3), min(len(flux), i + 4)
        if flux[i] < flux[lo:hi].max() or prominence[i] < floor:
            continue
        if accepted:
            last_frame, last_strength = accepted[-1]
            # Отражение звука от стен даёт слабую копию атаки сразу после настоящего щипка.
            if (i - last_frame) * hop < 0.12 and prominence[i] < 0.8 * last_strength:
                continue
        accepted.append((i, float(prominence[i])))
    frames_found = np.asarray([frame for frame, _ in accepted], dtype=float)
    # Пик потока приходится на кадр, в который атака уже вошла: её время — ближе к концу окна.
    return np.maximum(0.0, (frames_found * hop_n + 0.75 * size - lead) / sample_rate)


def segment_notes(
    track: PitchTrack,
    onset_times: np.ndarray,
    *,
    a4: float = 440.0,
    max_aperiodicity: float = 0.2,
    min_level_db: float = -42.0,
    min_duration: float = 0.05,
    pitch_tolerance: float = 0.6,
) -> list[Note]:
    voiced = (track.aperiodicity < max_aperiodicity) & (track.level_db > min_level_db)
    midi = np.where(voiced, hz_to_midi(np.maximum(track.frequency, 1e-6), a4), np.nan)
    onset_frames = set(int(i) for i in np.searchsorted(track.times, onset_times))

    groups, current, gap = [], [], 0
    for i in range(len(midi)):
        if np.isnan(midi[i]):
            gap += 1
            if current and gap > 3:
                groups.append(current)
                current = []
            continue
        if current:
            reference = float(np.median(midi[current][-8:]))
            new_pluck = i in onset_frames and len(current) >= 3
            if abs(midi[i] - reference) > pitch_tolerance or new_pluck:
                groups.append(current)
                current = []
        current.append(i)
        gap = 0
    if current:
        groups.append(current)

    notes = []
    for frames in groups:
        frames = np.asarray(frames)
        start = track.times[frames[0]] - track.hop / 2
        end = track.times[frames[-1]] + track.hop / 2
        if end - start < min_duration:
            continue
        core = frames[len(frames) // 4 : max(len(frames) // 4 + 1, int(len(frames) * 0.9))]
        value = float(np.median(midi[core]))
        number = int(round(value))
        notes.append(
            Note(
                start=float(max(0.0, start)),
                end=float(end),
                midi=number,
                frequency=float(midi_to_hz(value, a4)),
                cents=float((value - number) * 100.0),
                confidence=float(1.0 - np.median(track.aperiodicity[core])),
                level_db=float(track.level_db[frames].max()),
            )
        )

    # Склеиваем куски одной ноты, разорванные кратким сбоем оценки (без нового щипка между ними).
    merged: list[Note] = []
    for note in notes:
        previous = merged[-1] if merged else None
        if previous and previous.midi == note.midi and note.start - previous.end < 0.045:
            plucked = np.any((onset_times >= previous.end - 0.03) & (onset_times <= note.start + 0.03))
            if not plucked:
                weight_a, weight_b = previous.duration, note.duration
                previous.frequency = (previous.frequency * weight_a + note.frequency * weight_b) / (weight_a + weight_b)
                previous.cents = (previous.cents * weight_a + note.cents * weight_b) / (weight_a + weight_b)
                previous.end = note.end
                continue
        merged.append(note)
    return merged


def analyze(signal: np.ndarray, sample_rate: int, *, a4: float = 440.0, fmin: float = 70.0, fmax: float = 1200.0,
            min_duration: float = 0.05, onset_sensitivity: float = 1.0) -> list[Note]:
    """Сигнал → список нот."""
    peak = float(np.max(np.abs(signal))) if len(signal) else 0.0
    if peak < 1e-4:
        return []
    track = yin_track(signal, sample_rate, fmin=fmin, fmax=fmax)
    onsets = _onsets(signal, sample_rate, track.hop, onset_sensitivity)
    return segment_notes(track, onsets, a4=a4, min_duration=min_duration)


def analyze_file(path, **options) -> list[Note]:
    signal, sample_rate = load_audio(path)
    return analyze(signal, sample_rate, **options)


# ---------------------------------------------------------------------------
# Вывод: текст, JSON, MIDI
# ---------------------------------------------------------------------------


def format_report(notes: list[Note]) -> str:
    if not notes:
        return "Ноты не найдены: запись слишком тихая, шумная или в ней звучит несколько струн одновременно."
    lines = [
        f"Найдено нот: {len(notes)}",
        "Последовательность: " + " ".join(note.name for note in notes),
        "",
        f"{'№':>3}  {'начало':>7}  {'длит.':>6}  {'нота':<5} {'название':<22} {'частота':>9}  {'отклонение':>10}  {'увер.':>5}",
    ]
    for index, note in enumerate(notes, start=1):
        lines.append(
            f"{index:>3}  {note.start:>6.2f}с  {note.duration:>5.2f}с  {note.name:<5} {note.name_ru:<22} "
            f"{note.frequency:>7.2f}Гц  {note.cents:>+7.1f} ц.  {note.confidence:>5.2f}"
        )
    return "\n".join(lines)


def notes_to_json(notes: list[Note]) -> str:
    return json.dumps([note.to_dict() for note in notes], ensure_ascii=False, indent=2)


def _varlen(value: int) -> bytes:
    """Число в формате переменной длины стандарта MIDI."""
    chunks = [value & 0x7F]
    value >>= 7
    while value:
        chunks.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(chunks))


def write_midi(notes: list[Note], path, *, program: int = 24, ticks_per_beat: int = 480, bpm: int = 120) -> Path:
    """Сохранить ноты в MIDI-файл (одна дорожка, тембр по умолчанию — нейлоновая гитара)."""
    ticks_per_second = ticks_per_beat * bpm / 60.0
    events = []
    for note in notes:
        velocity = int(np.clip(round(100 + 1.5 * note.level_db), 30, 120))
        events.append((int(round(note.start * ticks_per_second)), 1, bytes([0x90, note.midi & 0x7F, velocity])))
        events.append((int(round(note.end * ticks_per_second)), 0, bytes([0x80, note.midi & 0x7F, 0])))
    events.sort(key=lambda item: (item[0], item[1]))

    body = bytearray()
    body += _varlen(0) + b"\xff\x51\x03" + struct.pack(">I", int(60_000_000 / bpm))[1:]
    body += _varlen(0) + bytes([0xC0, program & 0x7F])
    clock = 0
    for tick, _, message in events:
        body += _varlen(max(0, tick - clock)) + message
        clock = max(clock, tick)
    body += _varlen(0) + b"\xff\x2f\x00"

    path = Path(path)
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ticks_per_beat)
    path.write_bytes(header + b"MTrk" + struct.pack(">I", len(body)) + bytes(body))
    return path
