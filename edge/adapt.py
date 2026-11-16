"""Per-machine adaptation on the phone: learning period, drift-safe threshold updates, noisy-shed flag.

A new machine first *learns* (LEARNING_DAYS, an assumption in data/sim/cluster.json): it records
normal readings and raises no alarms ("Loom 2: din 4/7"). Its alarm level is then the 95th percentile
of those readings, like the per-machine alarm level in edge/train.py.

Afterwards the alarm level adapts slowly to genuine drift (belts bed in, seasons change), but only
from readings that are clearly normal: anything at or above FREEZE_AT of the alarm level is never
learned from. So a fault that grows slowly cannot teach the machine that it is normal. The level may
move at most MAX_DRIFT either way from the learned one.

Noisy shed: if the whole spectrum got louder by about the same amount (most bands up, small spread),
the change is more likely background noise (a neighbour's loom, a generator) than this machine, so
the reading is flagged low-confidence instead of being trusted. A fault usually changes some bands,
not all of them. The cut-offs are assumptions (below), not measurements.
"""

from collections import deque
from dataclasses import dataclass, field

import numpy as np

LEARNING_DAYS = 7  # default; data/sim/cluster.json "learning_days" overrides (the spec allows 3-7)
ALARM_QUANTILE = 0.95
FREEZE_AT = 0.8  # never learn from a reading at or above 80% of the alarm level
MAX_DRIFT = 0.25  # the alarm level stays within +-25% of the learned one
DRIFT_WINDOW = 200  # recent clearly-normal readings the adapted level is computed from
NOISY_MIN_BANDS = 0.75  # share of bands that must be louder
NOISY_MIN_DB = 3.0  # by at least this much (median band change, dB)
NOISY_MAX_SPREAD_DB = 3.0  # with this little spread between bands (IQR, dB)


def learning_progress(days_recorded: float, learning_days: float = LEARNING_DAYS) -> dict[str, float] | None:
    """{"day": k, "of": n} while learning (k counts from 1), None once learned."""
    if days_recorded >= learning_days:
        return None
    return {"day": int(days_recorded) + 1, "of": int(learning_days)}


@dataclass
class Baseline:
    learning_days: float = LEARNING_DAYS
    learned: list[float] = field(default_factory=list)
    threshold0: float | None = None
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=DRIFT_WINDOW))

    @property
    def threshold(self) -> float | None:
        if self.threshold0 is None:
            return None
        if len(self.recent) < 20:
            return self.threshold0
        adapted = float(np.quantile(np.asarray(self.recent), ALARM_QUANTILE))
        return float(np.clip(adapted, self.threshold0 * (1 - MAX_DRIFT), self.threshold0 * (1 + MAX_DRIFT)))

    def update(self, score: float, days_recorded: float) -> dict[str, object]:
        """Feed one reading. Returns {"state": "learning"|"monitoring", "risk", "learned_from"}."""
        if self.threshold0 is None:
            self.learned.append(score)
            if days_recorded < self.learning_days or len(self.learned) < 5:
                return {"state": "learning", "risk": None, "learned_from": True,
                        "progress": learning_progress(days_recorded, self.learning_days)}  # fmt: skip
            self.threshold0 = float(np.quantile(np.asarray(self.learned), ALARM_QUANTILE))
        thr = self.threshold
        assert thr is not None
        risk = score / thr
        learn = risk < FREEZE_AT  # freeze when high: a growing fault is never learned as normal
        if learn:
            self.recent.append(score)
        return {"state": "monitoring", "risk": risk, "learned_from": learn}


def noisy_shed(band_delta_db: np.ndarray | list[float]) -> bool:
    """Per mel band: change in level (dB) versus the learning period. True = probably background noise."""
    d = np.asarray(band_delta_db, dtype=float)
    up = float(np.mean(d >= NOISY_MIN_DB))
    spread = float(np.subtract(*np.percentile(d, [75, 25])))
    return up >= NOISY_MIN_BANDS and float(np.median(d)) >= NOISY_MIN_DB and spread <= NOISY_MAX_SPREAD_DB
