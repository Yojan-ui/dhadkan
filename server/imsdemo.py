"""Demo mode's data: the NASA IMS run-to-failure replayed as one machine's readings.

A machine whose source is `ims/<test>/<bearing>` plays real IMS vibration instead of DCASE clips. Its
score is the IMS health score from edge/ims.py (1.0 = alarm level), its sound is the 1 s vibration
snapshot resampled to 16 kHz and looped to 10 s, and its timestamps are the real IMS ones. Nothing is
synthesised.

Everything is read from edge/models/ims_demo_pack.npz (committed, ~3 MB), which `python -m edge.ims`
builds from data/raw/ims: one real snapshot per replay reading, its health score, and the CWRU-trained
classifier's output on the last hour of vibration up to it. So Demo mode runs from a fresh clone
without the 1 GB IMS download.

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

READINGS = 120  # the replay is shown as about this many readings (every k-th reading of the pack)


class ImsSource:
    def __init__(self) -> None:
        self.meta: dict[str, Any] = json.loads((MODELS / "ims_replay.json").read_text(encoding="utf-8"))
        pack = np.load(ims.PACK)
        k = max(1, math.ceil(len(pack["t_days"]) / READINGS))
        self.t = pack["t_days"][::k]
        self.x = pack["snap"][::k]
        self.score = pack["score"][::k].astype(np.float64)
        self.probs = pack["classifier_p"][::k]
        self.classes = [str(c) for c in pack["classes"]]
        self.provenance = json.loads(str(pack["provenance"]))
        self.start_day = float(pack["learning_window_end_day"])
        self.unit = f"ims/{self.meta['test']}/{self.meta['bearing']}"

    @property
    def readings(self) -> int:
        return len(self.t)

    @property
    def tick_hours(self) -> float:
        return float(np.median(np.diff(self.t))) * 24

    def index(self, tick: int) -> int:
        return min(len(self.t) - 1, max(0, tick))

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

    def classify(self, tick: int) -> dict[str, Any]:
        """The CWRU-trained classifier on the last hour of real vibration up to this reading (from the pack)."""
        p = self.probs[self.index(tick)]
        return {
            "class": self.classes[int(np.argmax(p))],
            "p": {c: round(float(v), 3) for c, v in zip(self.classes, p, strict=True)},
        }
