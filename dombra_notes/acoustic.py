"""
Акустический профиль: объективные измерения звука домбры (уровень 1).

Для каждой распознанной ноты считаются:
  * высота и её стабильность (разброс и дрейф в центах);
  * атака: время нарастания до пика;
  * затухание: скорость в дБ/с и оценка T60 (время спада на 60 дБ);
  * гармоники: уровни партиалов 1–10 относительно основного тона и негармоничность B;
  * спектральный центроид и доли энергии в низкой, средней и высокой полосах;
  * доля непериодической (шумовой) составляющей и отношение гармоники/шум.

Для записи постукивания по деке (струны заглушены) оцениваются резонансы корпуса.

Все величины сравнимы между инструментами только при одинаковых условиях записи:
один микрофон, одно расстояние, одно помещение, один приём щипка.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from . import pitch

HARMONICS = 10
BANDS = ((0.0, 300.0), (300.0, 1500.0), (1500.0, 8000.0))
BAND_NAMES = ("low", "mid", "high")


def _round(value, digits=2):
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return round(float(value), digits)


@dataclass
class NoteProfile:
    start: float
    end: float
    midi: int
    name: str
    frequency: float
    cents: float
    pitch_std_cents: float          # разброс высоты в устойчивой части ноты
    pitch_drift_cents: float        # изменение высоты от начала к концу ноты
    peak_db: float                  # пиковый уровень, дБ относительно полной шкалы
    attack_ms: float                # нарастание от 10 % до 90 % пиковой амплитуды
    decay_db_per_s: float           # скорость затухания после пика
    t60_s: float                    # оценка времени спада на 60 дБ по скорости затухания
    measured_drop_db: float         # фактическое падение уровня к концу ноты
    harmonics_db: list              # уровни гармоник 1..10 относительно основного тона, дБ
    inharmonicity: float            # коэффициент негармоничности B
    spectral_centroid_hz: float
    band_balance: dict              # доли энергии low/mid/high
    noise_ratio: float              # медиана непериодичности 0..1
    hnr_db: float                   # отношение гармоники/шум

    def to_dict(self) -> dict:
        data = asdict(self)
        data["harmonics_db"] = [_round(v, 1) for v in self.harmonics_db]
        data["band_balance"] = {k: _round(v, 3) for k, v in self.band_balance.items()}
        for key in ("start", "end", "frequency", "cents", "pitch_std_cents", "pitch_drift_cents", "peak_db",
                    "attack_ms", "decay_db_per_s", "t60_s", "measured_drop_db", "spectral_centroid_hz", "noise_ratio", "hnr_db"):
            data[key] = _round(data[key], 4 if key in ("start", "end") else 2)
        data["inharmonicity"] = _round(self.inharmonicity, 6)
        return data


# ---------------------------------------------------------------------------
# Огибающая
# ---------------------------------------------------------------------------


def envelope_db(signal: np.ndarray, sample_rate: int, window: float = 0.02, hop: float = 0.005):
    """Огибающая уровня (RMS) в дБ относительно полной шкалы и времена центров окон."""
    size = max(8, int(round(window * sample_rate)))
    step = max(1, int(round(hop * sample_rate)))
    x = np.asarray(signal, dtype=np.float64)
    if len(x) < size:
        x = np.pad(x, (0, size - len(x)))
    frames = pitch._frames(x, size, step)
    rms = np.sqrt((frames * frames).mean(axis=1))
    level = 20.0 * np.log10(np.maximum(rms, 1e-9))
    times = (np.arange(len(level)) * step + size / 2) / sample_rate
    return times, level


def noise_floor_db(signal: np.ndarray, sample_rate: int) -> float:
    """Уровень шума записи: 5-й процентиль огибающей."""
    _, level = envelope_db(signal, sample_rate)
    return float(np.percentile(level, 5))


# ---------------------------------------------------------------------------
# Профиль ноты
# ---------------------------------------------------------------------------


def _harmonic_analysis(segment: np.ndarray, sample_rate: int, f0: float):
    """Уровни гармоник относительно основного тона, негармоничность, центроид, баланс полос."""
    n = len(segment)
    nfft = 1 << max(16, (8 * n - 1).bit_length())
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(n), nfft))
    freqs = np.fft.rfftfreq(nfft, 1.0 / sample_rate)
    power = spectrum ** 2
    total = power.sum() or 1.0
    # Пик считается гармоникой, только если он заметно выше «ковра» спектра.
    audible = freqs <= 8000.0
    spectral_floor = float(np.median(spectrum[audible])) * 4.0 if audible.any() else 0.0

    centroid = float((freqs * power).sum() / total)
    balance = {}
    for name, (lo, hi) in zip(BAND_NAMES, BANDS):
        mask = (freqs >= lo) & (freqs < hi)
        balance[name] = float(power[mask].sum() / total)

    levels, partial_freqs = [], {}
    for k in range(1, HARMONICS + 1):
        centre = k * f0
        width = centre * (0.03 + 0.004 * k)
        mask = (freqs >= centre - width) & (freqs <= centre + width)
        if centre + width >= sample_rate / 2 or not mask.any():
            levels.append(float("nan"))
            continue
        index = int(np.argmax(spectrum[mask])) + int(np.flatnonzero(mask)[0])
        # Пик должен быть локальным максимумом выше уровня шума, иначе это склон соседнего пика.
        is_peak = 0 < index < len(spectrum) - 1 and spectrum[index] >= spectrum[index - 1] and spectrum[index] >= spectrum[index + 1]
        if is_peak and spectrum[index] > spectral_floor:
            left, mid, right = np.log(spectrum[index - 1 : index + 2] + 1e-12)
            denominator = left - 2.0 * mid + right
            shift = 0.5 * (left - right) / denominator if denominator < 0 else 0.0   # уточнение пика параболой
            levels.append(float(spectrum[index]))
            partial_freqs[k] = float(freqs[index] + shift * (freqs[1] - freqs[0]))
        else:
            levels.append(float("nan"))
    reference = levels[0] if levels and not math.isnan(levels[0]) else float("nan")
    harmonics_db = [20.0 * math.log10(v / reference) if not (math.isnan(v) or math.isnan(reference)) and v > 0 else float("nan") for v in levels]
    # Слабее −60 дБ от основного тона гармоника уже неразличима на фоне шума и утечки окна.
    harmonics_db = [v if not math.isnan(v) and v > -60.0 else float("nan") for v in harmonics_db]

    inharmonicity = float("nan")
    ks = [k for k in partial_freqs if 2 <= k <= 8]
    if len(ks) >= 3 and 1 in partial_freqs:
        base = partial_freqs[1]
        xs = np.array([k * k for k in ks], dtype=float)
        ys = np.array([(partial_freqs[k] / (k * base)) ** 2 - 1.0 for k in ks])
        inharmonicity = float(max(0.0, (xs * ys).sum() / (xs * xs).sum()))
    return harmonics_db, inharmonicity, centroid, balance


def profile_note(signal: np.ndarray, sample_rate: int, note: pitch.Note, track: pitch.PitchTrack | None = None,
                 floor_db: float | None = None) -> NoteProfile:
    """Измерить акустические характеристики одной ноты."""
    x = np.asarray(signal, dtype=np.float64)
    if track is None:
        track = pitch.yin_track(x, sample_rate)
    if floor_db is None:
        floor_db = noise_floor_db(x, sample_rate)

    a = int(max(0, note.start) * sample_rate)
    b = int(min(len(x), note.end * sample_rate))
    segment = x[a:b]
    if len(segment) < int(0.05 * sample_rate):
        segment = np.pad(segment, (0, int(0.05 * sample_rate) - len(segment)))

    # --- высота: разброс и дрейф ---
    mask = (track.times >= note.start + 0.06) & (track.times <= note.end) & (track.aperiodicity < 0.3)
    cents = 100.0 * (pitch.hz_to_midi(np.maximum(track.frequency[mask], 1e-6)) - note.midi) if mask.any() else np.zeros(0)
    if len(cents) >= 4:
        third = max(1, len(cents) // 3)
        pitch_std = float(np.std(cents))
        drift = float(np.median(cents[-third:]) - np.median(cents[:third]))
    else:
        pitch_std, drift = float("nan"), float("nan")
    noise_values = track.aperiodicity[mask] if mask.any() else np.array([1.0])
    noise_ratio = float(np.median(noise_values))
    hnr = 10.0 * math.log10((1.0 - noise_ratio) / max(noise_ratio, 1e-4)) if noise_ratio < 1 else float("nan")

    # --- огибающая: атака и затухание ---
    times, level = envelope_db(segment, sample_rate)
    peak_index = int(np.argmax(level[: max(1, int(0.25 / 0.005))]))   # пик ищем в первых 250 мс
    peak_db = float(level[peak_index])

    # Атака измеряется по мелкой огибающей: окно в один период основного тона (иначе огибающая
    # «дышит» внутри периода), шаг 1 мс. Начало ноты по детектору может приходиться на середину
    # нарастания, поэтому берётся запас 60 мс до него, а точки 10 % и 90 % ищутся как последние
    # пересечения перед пиком.
    lookback = max(0, a - int(0.06 * sample_rate))
    fine_window = max(0.003, 1.0 / max(note.frequency, 1.0))
    fine_t, fine_level = envelope_db(x[lookback : a + int(0.3 * sample_rate)], sample_rate, window=fine_window, hop=0.001)
    fine_amp = 10 ** (fine_level / 20.0)
    fine_peak = int(np.argmax(fine_amp))
    rise = fine_amp[: fine_peak + 1]
    base = float(rise.min())                       # хвост предыдущей ноты или тишина
    below10 = np.flatnonzero(rise < base + 0.1 * (fine_amp[fine_peak] - base))
    below90 = np.flatnonzero(rise < base + 0.9 * (fine_amp[fine_peak] - base))
    i10 = below10[-1] if below10.size else 0
    i90 = min(fine_peak, below90[-1] + 1) if below90.size else fine_peak
    t10, t90 = fine_t[i10], fine_t[i90]
    attack_ms = float(max(0.0, (t90 - t10) * 1000.0))
    onset = lookback + int(t10 * sample_rate)

    after = level[peak_index:]
    after_t = times[peak_index:]
    usable = after > floor_db + 8.0                       # выше шума с запасом
    window = (after <= peak_db - 3.0) & (after >= peak_db - 25.0) & usable
    if window.sum() >= 6:
        slope = float(np.polyfit(after_t[window], after[window], 1)[0])
    else:
        slope = float("nan")
    decay_rate = -slope if not math.isnan(slope) and slope < 0 else float("nan")
    t60 = 60.0 / decay_rate if not math.isnan(decay_rate) and decay_rate > 0 else float("nan")
    tail = after[usable] if usable.any() else after
    measured_drop = float(peak_db - tail[-1]) if len(tail) else float("nan")

    # --- спектр: 200 мс после атаки ---
    seg_start = onset + int(0.08 * sample_rate)
    seg_end = min(len(x), seg_start + int(0.2 * sample_rate))
    spectral_segment = x[seg_start:seg_end]
    if len(spectral_segment) < int(0.05 * sample_rate):
        spectral_segment = segment[: int(0.2 * sample_rate)]
    harmonics_db, inharmonicity, centroid, balance = _harmonic_analysis(spectral_segment, sample_rate, note.frequency)

    return NoteProfile(
        start=note.start, end=note.end, midi=note.midi, name=note.name, frequency=note.frequency, cents=note.cents,
        pitch_std_cents=pitch_std, pitch_drift_cents=drift, peak_db=peak_db, attack_ms=attack_ms,
        decay_db_per_s=decay_rate, t60_s=t60, measured_drop_db=measured_drop, harmonics_db=harmonics_db,
        inharmonicity=inharmonicity, spectral_centroid_hz=centroid, band_balance=balance,
        noise_ratio=noise_ratio, hnr_db=hnr,
    )


def profile_signal(signal: np.ndarray, sample_rate: int, **options) -> tuple[list[pitch.Note], list[NoteProfile], float]:
    """Ноты, их профили и уровень шума записи."""
    x = np.asarray(signal, dtype=np.float64)
    notes = pitch.analyze(x, sample_rate, **options)
    if not notes:
        return [], [], noise_floor_db(x, sample_rate)
    track = pitch.yin_track(x, sample_rate)
    floor = noise_floor_db(x, sample_rate)
    return notes, [profile_note(x, sample_rate, note, track, floor) for note in notes], floor


def profile_file(path, **options):
    signal, sample_rate = pitch.load_audio(path)
    return profile_signal(signal, sample_rate, **options)


# ---------------------------------------------------------------------------
# Резонансы корпуса по постукиванию
# ---------------------------------------------------------------------------


@dataclass
class TapProfile:
    taps: int
    resonances: list = field(default_factory=list)   # [(частота, уровень относительно самого сильного пика, дБ)]

    def to_dict(self) -> dict:
        return {"taps": self.taps, "resonances": [{"hz": _round(f, 1), "db": _round(d, 1)} for f, d in self.resonances]}


def tap_resonances(signal: np.ndarray, sample_rate: int, fmin: float = 60.0, fmax: float = 2000.0, count: int = 6) -> TapProfile:
    """Усреднённый спектр ударов по деке и его главные пики."""
    x = np.asarray(signal, dtype=np.float64)
    onsets = pitch._onsets(x, sample_rate, 0.01)
    if len(onsets) == 0:
        return TapProfile(taps=0)
    length = int(0.25 * sample_rate)
    nfft = 1 << max(15, (2 * length - 1).bit_length())
    freqs = np.fft.rfftfreq(nfft, 1.0 / sample_rate)
    total = np.zeros(len(freqs))
    used = 0
    for t in onsets:
        start = int(t * sample_rate)
        piece = x[start : start + length]
        if len(piece) < length // 2:
            continue
        piece = piece * np.hanning(len(piece))
        total += 20.0 * np.log10(np.abs(np.fft.rfft(piece, nfft)) + 1e-9)
        used += 1
    if used == 0:
        return TapProfile(taps=0)
    spectrum = total / used
    kernel = max(3, int(round(8.0 / (freqs[1] - freqs[0]))) | 1)   # сглаживание около 8 Гц
    spectrum = np.convolve(spectrum, np.ones(kernel) / kernel, mode="same")
    band = (freqs >= fmin) & (freqs <= fmax)
    idx = np.flatnonzero(band)
    peaks = []
    half = max(1, int(round(15.0 / (freqs[1] - freqs[0]))))          # локальный максимум в окне ±15 Гц
    for i in idx[half:-half]:
        if spectrum[i] >= spectrum[i - half : i + half + 1].max():
            neighbourhood = spectrum[max(0, i - 8 * half) : i + 8 * half]
            prominence = spectrum[i] - np.median(neighbourhood)
            if prominence >= 6.0:
                peaks.append((float(freqs[i]), float(spectrum[i])))
    peaks.sort(key=lambda p: -p[1])
    top = peaks[:count]
    if not top:
        return TapProfile(taps=used)
    reference = top[0][1]
    return TapProfile(taps=used, resonances=sorted([(f, level - reference) for f, level in top]))
