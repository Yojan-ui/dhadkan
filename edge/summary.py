"""One reading -> a small summary vector: what each machine-specific detector learns from.

A reading is ~10 s of sound (phone mic) or a vibration snapshot. The summary keeps only numbers, never
audio, so it is cheap to store for every reading and is the machine's history:

    log_rms     loudness (log10 RMS of the waveform)
    mel_mean    per mel band, mean log power over the reading       (64)
    mel_std     per mel band, frame-to-frame spread                  (64)
    mel_p95     per mel band, 95th percentile frame (short clicks)   (64)
    mfcc_mean   20 cepstral coefficients of the log-mel frames, mean (20)
    mfcc_std    ... and their spread                                 (20)
    centroid    mean spectral centroid (mel-band index)               (1)
    flatness    mean spectral flatness (0 = tonal, 1 = noise-like)    (1)

Signal quality is measured separately (`quality`): clipping, drop-outs and whether the machine is running
at all. A poor-quality reading is stored but never used to learn and never raises an alarm on its own.
"""

from dataclasses import dataclass

import numpy as np
from scipy.fft import dct

from . import features
from .config import N_MELS

N_MFCC = 20
CLIP_LEVEL = 0.99  # |sample| at or above this counts as clipped
CLIP_MAX_SHARE = 0.001  # more than 0.1% clipped samples = distorted reading
DROPOUT_MAX_SHARE = 0.2  # more than 20% near-silent frames = microphone covered / recording broke
SILENT_FRAME_RMS = 1e-4  # frame RMS below this (about -80 dBFS) = digital silence / mic dropped out
FRAME = 512
MIN_SECONDS = 2.0  # shorter readings are rejected

FIELDS = (
    ("log_rms", 1),
    ("mel_mean", N_MELS),
    ("mel_std", N_MELS),
    ("mel_p95", N_MELS),
    ("mfcc_mean", N_MFCC),
    ("mfcc_std", N_MFCC),
    ("centroid", 1),
    ("flatness", 1),
)
DIM = sum(n for _, n in FIELDS)


@dataclass(frozen=True)
class Slices:
    log_rms: slice
    mel_mean: slice
    mel_std: slice
    mel_p95: slice
    mfcc_mean: slice
    mfcc_std: slice
    centroid: slice
    flatness: slice


def _slices() -> Slices:
    out, i = {}, 0
    for name, n in FIELDS:
        out[name] = slice(i, i + n)
        i += n
    return Slices(**out)


S = _slices()


def summarise_mel(mel: np.ndarray, log_rms: float) -> np.ndarray:
    """mel: (N_MELS, frames) log10 power, as features.log_mel returns it."""
    db = 10.0 * mel
    mfcc = dct(db, type=2, axis=0, norm="ortho")[:N_MFCC]
    power = 10.0**mel
    idx = np.arange(N_MELS)[:, None]
    centroid = float(np.mean((power * idx).sum(0) / (power.sum(0) + 1e-20)))
    flatness = float(np.mean(np.exp(np.mean(np.log(power + 1e-20), 0)) / (np.mean(power, 0) + 1e-20)))
    v = np.concatenate(
        [
            [log_rms],
            mel.mean(1),
            mel.std(1),
            np.percentile(mel, 95, axis=1),
            mfcc.mean(1),
            mfcc.std(1),
            [centroid],
            [flatness],
        ]
    )
    return v.astype(np.float64)


def summarise(audio: np.ndarray) -> np.ndarray:
    """(samples,) at features.SR -> (DIM,) summary."""
    a = np.asarray(audio, dtype=np.float32)
    rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-12)
    return summarise_mel(features.log_mel(a), float(np.log10(rms)))


def quality(audio: np.ndarray, sr: int = features.SR) -> dict[str, object]:
    """Is this reading usable? {ok, reasons[], clipped_share, dropout_share, seconds}."""
    a = np.asarray(audio, dtype=np.float32)
    seconds = len(a) / sr
    reasons = []
    if seconds < MIN_SECONDS:
        return {
            "ok": False,
            "reasons": ["too_short"],
            "clipped_share": 0.0,
            "dropout_share": 1.0,
            "seconds": seconds,
        }
    clipped = float(np.mean(np.abs(a) >= CLIP_LEVEL))
    n = len(a) // FRAME
    frame_rms = np.sqrt(np.mean(a[: n * FRAME].astype(np.float64).reshape(n, FRAME) ** 2, axis=1))
    dropout = float(np.mean(frame_rms < SILENT_FRAME_RMS))
    if clipped > CLIP_MAX_SHARE:
        reasons.append("clipped")
    if dropout > DROPOUT_MAX_SHARE:
        reasons.append("dropout")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "clipped_share": round(clipped, 5),
        "dropout_share": round(dropout, 4),
        "seconds": round(seconds, 2),
    }
