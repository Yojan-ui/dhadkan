"""Machine-specific unsupervised detectors. Each one learns from ONE machine's normal readings only.

Input rows are reading summaries (edge/summary.py) with one extra column: the type-level autoencoder's
reconstruction score for that reading (NaN when the reading has none, e.g. vibration).

Every detector maps a row to a raw score (higher = less like this machine's normal). `MachineModel`
then puts the detectors on a common scale using out-of-fold scores of the machine's OWN normal readings
(5 contiguous folds, so time-ordered variability counts), and fuses the chosen ones:

    n_k   = (raw_k - median_oof_k) / (q95_oof_k - median_oof_k)   0 = typical normal, 1 = its 95th pct
    fused = mean_k n_k                                              (or max, for the "max" fusion)
    risk  = (fused - median_oof) / (alarm - median_oof)             alarm = 95th pct of out-of-fold fused

So risk 1.0 means "at this machine's alarm level", and about 1 normal reading in 20 crosses it by design.
Which detectors are fused is decided by edge/compare.py on selection units only (docs/RESULTS.md).
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sklearn.covariance import LedoitWolf
from sklearn.ensemble import IsolationForest

from .summary import DIM, S

AE_COL = DIM  # column of the autoencoder score
WIDTH = DIM + 1
ALARM_Q = 0.95
FOLDS = 5
MIN_ROWS = 10  # fewer normal readings than this: no model ("still learning")


def robust_center_scale(x: np.ndarray, floor: float = 1e-3) -> tuple[np.ndarray, np.ndarray]:
    med = np.median(x, axis=0)
    mad = 1.4826 * np.median(np.abs(x - med), axis=0)
    return med, np.maximum(mad, floor)


class Detector:
    name = ""
    needs_ae = False

    def fit(self, x: np.ndarray) -> "Detector":
        raise NotImplementedError

    def score(self, x: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class Rms(Detector):
    """Loudness against this machine's own normal, two-sided robust z."""

    name = "rms"

    def fit(self, x: np.ndarray) -> "Rms":
        self.c, self.s = robust_center_scale(x[:, S.log_rms])
        return self

    def score(self, x: np.ndarray) -> np.ndarray:
        return np.abs((x[:, S.log_rms] - self.c) / self.s)[:, 0]


class Spectral(Detector):
    """Shape of the average spectrum: RMS over mel bands of each band's robust z (loudness removed)."""

    name = "spectral"

    @staticmethod
    def _shape(x: np.ndarray) -> np.ndarray:
        m = x[:, S.mel_mean]
        return m - m.mean(axis=1, keepdims=True)  # level-free: a louder shed shifts every band equally

    def fit(self, x: np.ndarray) -> "Spectral":
        self.c, self.s = robust_center_scale(self._shape(x), floor=0.01)
        return self

    def band_z(self, x: np.ndarray) -> np.ndarray:
        return np.clip((self._shape(x) - self.c) / self.s, -10, 10)

    def score(self, x: np.ndarray) -> np.ndarray:
        return np.sqrt(np.mean(self.band_z(x) ** 2, axis=1))


class Transient(Detector):
    """Short clicks and knocks: how far each band's loudest frames rise above its average, max over bands."""

    name = "transient"

    @staticmethod
    def _peak(x: np.ndarray) -> np.ndarray:
        return x[:, S.mel_p95] - x[:, S.mel_mean]

    def fit(self, x: np.ndarray) -> "Transient":
        self.c, self.s = robust_center_scale(self._peak(x), floor=0.01)
        return self

    def score(self, x: np.ndarray) -> np.ndarray:
        return np.max(np.clip((self._peak(x) - self.c) / self.s, -10, 10), axis=1)


class Mfcc(Detector):
    """Mahalanobis distance of MFCC statistics (+ centroid, flatness), shrunk covariance (Ledoit-Wolf)."""

    name = "mfcc"

    @staticmethod
    def _cols(x: np.ndarray) -> np.ndarray:
        return np.hstack([x[:, S.mfcc_mean], x[:, S.mfcc_std], x[:, S.centroid], x[:, S.flatness]])

    def fit(self, x: np.ndarray) -> "Mfcc":
        z = self._cols(x)
        self.mu, self.sd = z.mean(0), z.std(0) + 1e-6
        self.lw = LedoitWolf().fit((z - self.mu) / self.sd)
        return self

    def score(self, x: np.ndarray) -> np.ndarray:
        return np.sqrt(np.maximum(self.lw.mahalanobis((self._cols(x) - self.mu) / self.sd), 0))


class IForest(Detector):
    """Isolation Forest on loudness, spectrum and MFCC means (standardised on this machine)."""

    name = "iforest"

    @staticmethod
    def _cols(x: np.ndarray) -> np.ndarray:
        return np.hstack([x[:, S.log_rms], x[:, S.mel_mean], x[:, S.mel_std], x[:, S.mfcc_mean]])

    def fit(self, x: np.ndarray) -> "IForest":
        z = self._cols(x)
        self.mu, self.sd = z.mean(0), z.std(0) + 1e-6
        self.f = IsolationForest(n_estimators=200, random_state=0).fit((z - self.mu) / self.sd)
        return self

    def score(self, x: np.ndarray) -> np.ndarray:
        return -self.f.score_samples((self._cols(x) - self.mu) / self.sd)


class Autoencoder(Detector):
    """The type-level conv autoencoder (edge/runtime.py), calibrated to this machine: one-sided robust z."""

    name = "ae"
    needs_ae = True

    def fit(self, x: np.ndarray) -> "Autoencoder":
        self.c, self.s = robust_center_scale(x[:, [AE_COL]], floor=1e-6)
        return self

    def score(self, x: np.ndarray) -> np.ndarray:
        return ((x[:, [AE_COL]] - self.c) / self.s)[:, 0]


DETECTORS: dict[str, type[Detector]] = {
    d.name: d for d in (Rms, Spectral, Transient, Mfcc, IForest, Autoencoder)
}


@dataclass
class Calib:
    median: float
    q95: float

    def norm(self, raw: np.ndarray) -> np.ndarray:
        return (raw - self.median) / max(self.q95 - self.median, 1e-9)


def _folds(n: int, k: int = FOLDS) -> list[np.ndarray]:
    edges = np.linspace(0, n, min(k, n) + 1).astype(int)
    return [np.arange(a, b) for a, b in itertools.pairwise(edges) if b > a]


def _fuse_oof(parts: list[np.ndarray], fusion: str) -> np.ndarray:
    """Fuse out-of-fold normalised scores row by row. A part with a different length (the autoencoder
    calibrated on other rows) is resampled to the main length and paired by rank with the mean of the
    others."""
    main = [p for p in parts if len(p) == max(len(q) for q in parts)]
    n = len(main[0])
    order = np.argsort(np.argsort(np.mean(main, axis=0)))
    grid = np.linspace(0, 1, n)
    rows = [p if len(p) == n else np.quantile(p, grid)[order] for p in parts]
    stack = np.vstack(rows)
    return stack.max(0) if fusion == "max" else stack.mean(0)


@dataclass
class MachineModel:
    """The detectors one machine uses, fitted on its normal rows, with out-of-fold calibration."""

    names: Sequence[str]
    fusion: str = "mean"  # mean | max
    dets: dict[str, Detector] = field(default_factory=dict)
    calib: dict[str, Calib] = field(default_factory=dict)
    fused_median: float = 0.0
    alarm: float = 1.0
    n_fit: int = 0
    oof_risk: np.ndarray | None = None
    shape: Spectral = field(default_factory=Spectral)
    loud: tuple[np.ndarray, np.ndarray] = (np.zeros(1), np.ones(1))

    def _usable(self, x: np.ndarray) -> list[str]:
        has_ae = bool(np.isfinite(x[:, AE_COL]).all())
        return [n for n in self.names if has_ae or not DETECTORS[n].needs_ae]

    def fit(self, x: np.ndarray, ae_rows: np.ndarray | None = None) -> "MachineModel":
        """x: (n, WIDTH) normal rows of this machine, oldest first. ae_rows: rows to calibrate the
        autoencoder on instead of x (evaluation only: the autoencoder was trained on x itself)."""
        if len(x) < MIN_ROWS:
            raise ValueError(f"need at least {MIN_ROWS} normal readings, got {len(x)}")
        self.names = self._usable(x)
        oof = {}
        for n in self.names:
            rows = ae_rows if (DETECTORS[n].needs_ae and ae_rows is not None) else x
            raw = np.empty(len(rows))
            for idx in _folds(len(rows)):
                train = np.setdiff1d(np.arange(len(rows)), idx)
                raw[idx] = DETECTORS[n]().fit(rows[train]).score(rows[idx])
            self.calib[n] = Calib(float(np.median(raw)), float(np.quantile(raw, ALARM_Q)))
            self.dets[n] = DETECTORS[n]().fit(rows)
            oof[n] = self.calib[n].norm(raw)
        # Fused out-of-fold scores. When the autoencoder was calibrated on other rows (evaluation only),
        # its out-of-fold scores are paired with the others by rank (an approximation, documented in
        # docs/RESULTS.md); false alarms are always measured on test normals, never taken from here.
        fused = _fuse_oof([oof[n] for n in self.names], self.fusion)
        self.fused_median = float(np.median(fused))
        self.alarm = float(np.quantile(fused, ALARM_Q))
        self.oof_risk = self._risk(fused)
        self.n_fit = len(x)
        # always fitted for the explanation, whether or not they are part of the alarm
        self.shape = Spectral().fit(x)
        self.loud = robust_center_scale(x[:, S.log_rms])
        return self

    def _risk(self, fused: np.ndarray) -> np.ndarray:
        return (fused - self.fused_median) / max(self.alarm - self.fused_median, 1e-9)

    def parts(self, x: np.ndarray) -> dict[str, np.ndarray]:
        """Per detector, normalised score (1.0 = that detector's 95th pct of this machine's normal)."""
        return {n: self.calib[n].norm(self.dets[n].score(x)) for n in self.names}

    def risk(self, x: np.ndarray) -> np.ndarray:
        p = np.vstack(list(self.parts(x).values()))
        return self._risk(p.max(0) if self.fusion == "max" else p.mean(0))

    def explain(self, x_row: np.ndarray) -> dict[str, Any]:
        """For one reading: each alarm detector's share, the loudness change in dB against this machine's
        learned normal, and which mel bands changed shape most (robust z, loudness removed)."""
        x = x_row[None, :]
        z = self.shape.band_z(x)[0]
        top = np.argsort(-np.abs(z))[:5]
        return {
            "detectors": {n: round(float(v[0]), 3) for n, v in self.parts(x).items()},
            "loudness_db": round(float(20 * (x[0, S.log_rms][0] - self.loud[0][0])), 2),
            "loudness_z": round(float((x[0, S.log_rms][0] - self.loud[0][0]) / self.loud[1][0]), 2),
            "shape_change": round(float(self.shape.score(x)[0]), 3),
            "bands": [{"band": int(b), "z": round(float(z[b]), 2)} for b in top],
        }


def chosen(default: Sequence[str] = ("rms",)) -> dict[str, Any]:
    """The detector set edge/compare.py chose on its selection units (newest record), with the record path."""
    import json

    from .config import RUNS

    runs = sorted(RUNS.glob("compare_*.json"))
    if not runs:
        return {"names": list(default), "fusion": "mean", "record": None}
    rec = json.loads(runs[-1].read_text())
    cfg = rec["candidates"][rec["chosen"]]
    return {
        "names": cfg["names"],
        "fusion": cfg.get("fusion", "mean"),
        "record": f"docs/runs/{runs[-1].name}",
        "candidate": rec["chosen"],
    }
