"""Audio -> 1-second log-mel windows."""

from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from .config import FRAMES, HOP, HOP_WIN_S, N_FFT, N_MELS, SR

_MEL_FB = librosa.filters.mel(sr=SR, n_fft=N_FFT, n_mels=N_MELS)


def load(path: str | Path) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    audio = audio[:, 0]
    if sr != SR:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SR)
    return audio


def log_mel(audio: np.ndarray) -> np.ndarray:
    """(samples,) -> (N_MELS, frames) in dB-like log power."""
    spec = np.abs(librosa.stft(audio, n_fft=N_FFT, hop_length=HOP, center=False)) ** 2
    return np.log10(_MEL_FB @ spec + 1e-10).astype(np.float32)


def windows(audio: np.ndarray) -> np.ndarray:
    """(samples,) -> (n, N_MELS, FRAMES): ~1 s windows, ~50% overlap."""
    mel = log_mel(audio)
    step = int(HOP_WIN_S * SR / HOP)
    starts = range(0, mel.shape[1] - FRAMES + 1, step)
    return np.stack([mel[:, s : s + FRAMES] for s in starts])


def onset_mix(normal: np.ndarray, abnormal: np.ndarray, mix: float) -> np.ndarray:
    """SYNTHETIC fault onset: overlay an abnormal recording on a normal one.

    Power-preserving (sqrt weights), so loudness stays roughly constant and only
    the character of the sound changes. MIMII has no recordings of faults
    developing over time; this is a stand-in and is labelled as such.
    """
    n = min(len(normal), len(abnormal))
    return np.sqrt(1 - mix) * normal[:n] + np.sqrt(mix) * abnormal[:n]
