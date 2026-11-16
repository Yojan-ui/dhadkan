"""Demo mode's data: the NASA IMS run-to-failure replayed as one machine's readings.

A machine whose source is `ims/<test>/<bearing>` reads 1 s vibration snapshots from data/raw/ims
instead of DCASE clips. Its score is the IMS health score from edge/ims.py (1.0 = alarm level), its
sound is the vibration snapshot resampled to 16 kHz and looped to 10 s, and its timestamps are the
real IMS ones. Nothing is synthesised.

The replay starts after the learning window (edge/ims.py's first BASELINE_FRAC of snapshots): that is
where the health score learned this bearing's normal, so it is not monitored. The bearing's run-in
transient sits in that window and would otherwise raise an alarm on the first reading.
"""

import json
import math
from typing import Any

import numpy as np
from scipy import signal

from edge import ims
from edge.config import MODELS, SR

READINGS = 120  # the replay is shown as about this many readings


class ImsSource:
    def __init__(self) -> None:
        self.meta: dict[str, Any] = json.loads((MODELS / "ims_replay.json").read_text())
        self.t, self.x = ims.load(self.meta["test"], self.meta["bearing"])
        self.score = np.asarray(self.meta["score"])
        self.start = int(len(self.t) * ims.BASELINE_FRAC)  # first monitored snapshot
        self.stride = max(1, math.ceil((len(self.t) - self.start) / READINGS))
        self.unit = f"ims/{self.meta['test']}/{self.meta['bearing']}"

    @property
    def readings(self) -> int:
        return math.ceil((len(self.t) - self.start) / self.stride)

    @property
    def start_day(self) -> float:
        """Days from the first IMS snapshot to the first monitored one (the learning window)."""
        return float(self.t[self.start])

    @property
    def tick_hours(self) -> float:
        return float(np.median(np.diff(self.t))) * 24 * self.stride

    def index(self, tick: int) -> int:
        return min(len(self.t) - 1, self.start + max(0, tick) * self.stride)

    def audio(self, tick: int) -> np.ndarray:
        snap = self.x[self.index(tick)].astype(np.float64)
        y = signal.resample_poly(snap, 4, 5)  # 20 kHz -> 16 kHz
        y = y / (np.max(np.abs(y)) + 1e-9) * 0.5
        return np.resize(y, 10 * SR).astype(np.float32)

    def result(self, tick: int) -> dict[str, Any]:
        """Shaped like EdgeModel.score_audio's output; no registry fingerprint for a vibration channel."""
        s = float(self.score[self.index(tick)])
        return {
            "score": s,
            "threshold": 1.0,
            "risk": s,
            "window_errors": [s] * 19,
            "embedding": None,
            "band_z": [0.0] * 64,
        }

    def clip(self, tick: int) -> str:
        day = float(self.t[self.index(tick)])
        return f"NASA IMS {self.meta['test']} {self.meta['bearing']}, day {day:.2f}"

    def snapshots_up_to(self, tick: int, n: int = 6) -> np.ndarray:
        i = self.index(tick)
        return self.x[max(0, i - n + 1) : i + 1].reshape(-1).astype(np.float64)
