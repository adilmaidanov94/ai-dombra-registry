"""Синтез щипкового звука с точно известной высотой — для проверки распознавания."""

import numpy as np


def pluck(frequency: float, duration: float, sample_rate: int = 44100, *, harmonics: int = 14,
          fundamental: float = 1.0, decay: float = 0.9, seed: int = 0) -> np.ndarray:
    """
    Затухающая струна: сумма гармоник, старшие затухают быстрее, в начале — короткий шум щипка.
    fundamental < 1 ослабляет основной тон (трудный случай: возможна ошибка на октаву).
    """
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration * sample_rate)) / sample_rate
    tone = np.zeros_like(t)
    for k in range(1, harmonics + 1):
        if k * frequency > 0.45 * sample_rate:
            break
        amplitude = (fundamental if k == 1 else 1.0) / k ** 1.2
        tone += amplitude * np.exp(-t / (decay / k ** 0.7)) * np.sin(2 * np.pi * k * frequency * t + rng.uniform(0, 2 * np.pi))
    attack = rng.normal(0, 0.4, len(t)) * np.exp(-t / 0.004)
    envelope = np.minimum(1.0, t / 0.002)
    signal = (tone + attack) * envelope
    return signal / np.max(np.abs(signal))


def melody(notes, sample_rate: int = 44100, *, gap: float = 0.0, level: float = 0.8, **kwargs) -> np.ndarray:
    """notes: список (частота, длительность). Каждая следующая нота глушит предыдущую, как на одной струне."""
    parts = []
    for index, (frequency, duration) in enumerate(notes):
        parts.append(level * pluck(frequency, duration, sample_rate, seed=index, **kwargs))
        if gap:
            parts.append(np.zeros(int(gap * sample_rate)))
    return np.concatenate(parts)


def add_noise(signal: np.ndarray, snr_db: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    power = np.mean(signal ** 2)
    noise = rng.normal(0, np.sqrt(power / 10 ** (snr_db / 10)), len(signal))
    return signal + noise
