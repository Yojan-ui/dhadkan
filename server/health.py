"""Machine health: readings -> status, severity, trend, urgency, confidence and the next inspection.

Input is one machine's recent history from edge/baseline.BaselineStore (risk per reading against ITS
own learned normal, usable/running flags, after-restart flags), the explanation of the latest reading,
the time-to-threshold estimate (edge/rul.py) and the machine's maintenance history.

What this is NOT: a probability of failure or a "health %". An anomaly means "this machine sounds
different from how it sounded when it was learned"; only an inspection confirms a mechanical fault.
Every cut-off below is an assumption (ASSUMPTIONS), shown in docs/RESULTS.md and the dashboard.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import librosa
import numpy as np

from edge.config import N_MELS, SR

ASSUMPTIONS = {
    "recent": 3,  # readings that decide persistence
    "persistent_min": 2,  # at least 2 of the last 3 at or above the alarm level
    "marked": 2.0,  # median recent risk (x alarm level) for a marked change
    "severe": 4.0,  # ... and for a severe one
    "rul_inspect_days": 7.0,  # time to threshold (low end) under this: inspect
    "rul_urgent_days": 2.0,  # under this: urgent
    "min_baseline_readings": 20,  # fewer in the active baseline: confidence at most medium
    "routine_days": {"powerloom": 14, "flour_mill": 14, "pump_set": 30, "machine_shop": 30},
}
URGENCY = ("none", "monitor", "measure_again", "inspect", "maintenance", "urgent_owner_attention")
STATUS_FOR = {"none": "normal", "monitor": "watch", "measure_again": "watch", "inspect": "inspect",
              "maintenance": "repair_soon", "urgent_owner_attention": "urgent"}  # fmt: skip
NOT_A_DIAGNOSIS = "Sound changed against this machine's own normal. Only an inspection confirms a fault."

_MEL_HZ = librosa.mel_frequencies(n_mels=N_MELS + 2, fmax=SR / 2)[1:-1]


def band_hz(band: int) -> int:
    return int(round(float(_MEL_HZ[band]), -1))


@dataclass
class Health:
    machine: str
    status: str
    urgency: str
    severity: str
    trend: str
    confidence: str
    confidence_reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    next_inspection: str | None = None
    recent_risk: list[float] = field(default_factory=list)
    time_to_threshold: dict[str, Any] | None = None
    note: str = NOT_A_DIAGNOSIS

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def explain_text(ex: dict[str, Any] | None) -> list[str]:
    """Plain lines from detectors.MachineModel.explain()."""
    if not ex:
        return []
    out = []
    db = ex.get("loudness_db")
    if db is not None and abs(db) >= 1.0:
        out.append(f"{'Louder' if db > 0 else 'Quieter'} than its learned normal by {abs(db):.1f} dB "
                   f"({ex.get('loudness_z', 0):+.1f} x its usual spread)")  # fmt: skip
    bands = [b for b in ex.get("bands", []) if abs(b["z"]) >= 3]
    if bands:
        up = sorted({band_hz(b["band"]) for b in bands if b["z"] > 0})
        down = sorted({band_hz(b["band"]) for b in bands if b["z"] < 0})
        if up:
            out.append("Stronger than normal around " + ", ".join(f"{h} Hz" for h in up))
        if down:
            out.append("Weaker than normal around " + ", ".join(f"{h} Hz" for h in down))
    return out


def assess(
    machine: str,
    history: list[dict[str, Any]],
    *,
    now: datetime,
    preset: str = "flour_mill",
    explanation: dict[str, Any] | None = None,
    rul: dict[str, Any] | None = None,
    baseline_rows: int | None = None,
    noisy_shed: bool = False,
    repairs: list[dict[str, Any]] | None = None,
    a: dict[str, Any] | None = None,
) -> Health:
    """history: BaselineStore.history rows, oldest first."""
    a = {**ASSUMPTIONS, **(a or {})}
    if not history:
        return Health(machine, "no_signal", "measure_again", "none", "unknown", "low", ["no readings yet"])
    last = history[-1]
    if last["risk"] is None and last.get("excluded") is None and last.get("running"):
        return Health(machine, "learning", "none", "none", "unknown", "low", ["still learning its normal"])
    if last.get("excluded") == "not_running":
        return Health(machine, "not_running", "none", "none", "unknown", "medium", ["machine was off"])
    scored = [h for h in history if h["risk"] is not None]
    recent = [float(h["risk"]) for h in scored[-a["recent"] :]]
    if not recent:
        return Health(machine, "no_signal", "measure_again", "none", "unknown", "low",
                      ["no usable reading since learning ended"])  # fmt: skip
    above = sum(r >= 1.0 for r in recent)
    med = float(np.median(recent))
    persistent = above >= a["persistent_min"]
    severity = ("none" if not persistent else "severe" if med >= a["severe"] else "marked" if med >= a["marked"]
                else "mild")  # fmt: skip
    status_rul = (rul or {}).get("status")
    if status_rul == "estimate":
        trend = "rising"
    elif status_rul == "at_threshold":
        trend = "at_threshold"
    elif status_rul == "no_trend":
        trend = "steady"
    else:
        trend = "unknown"
    low = (rul or {}).get("low")

    # urgency: what the agent should do next (it never stops a machine)
    if not persistent:
        urgency = "measure_again" if above else "none"
    elif trend == "at_threshold" or (
        severity == "severe" and low is not None and low <= a["rul_urgent_days"]
    ):
        urgency = "urgent_owner_attention"
    elif severity == "severe" or (low is not None and low <= a["rul_urgent_days"]):
        urgency = "maintenance"
    elif severity == "marked" or trend == "rising" or (low is not None and low <= a["rul_inspect_days"]):
        urgency = "inspect"
    else:
        urgency = "monitor"

    # confidence: how much to trust THIS assessment
    reasons = []
    unusable = sum(1 for h in history[-10:] if (h.get("excluded") or "").startswith("quality"))
    if unusable >= 3:
        reasons.append(f"{unusable} of the last 10 readings were unusable (clipped or broken)")
    if any(h.get("after_gap") for h in scored[-a["recent"] :]):
        reasons.append("machine restarted after a long stop; the first readings after a restart often differ")
    if noisy_shed:
        reasons.append("the whole spectrum got louder evenly: more likely background noise than this machine")
    if baseline_rows is not None and baseline_rows < a["min_baseline_readings"]:
        reasons.append(f"its normal was learned from only {baseline_rows} readings")
    if len(scored) < a["recent"]:
        reasons.append("fewer than 3 readings since learning ended")
    confidence = "high" if not reasons else "medium" if len(reasons) == 1 else "low"
    if noisy_shed or unusable >= 3:
        confidence = "low"
    if confidence == "low" and urgency in ("monitor", "inspect"):
        urgency = "measure_again"  # check the reading before sending anyone

    evidence = [f"{above} of the last {len(recent)} readings at or above its alarm level (median {med:.2f} x)",
                *(explain_text(explanation) if above else [])]  # fmt: skip
    if rul and rul.get("status") == "estimate":
        evidence.append(f"Rising trend: maintenance threshold in {rul['low']:.1f}-{rul['high']:.1f} days "
                        f"(10-90% range; {rul['meaning']})")  # fmt: skip
    if repairs:
        lastrep = repairs[-1]
        evidence.append(f"Last repair: {lastrep.get('what', 'repair')} on {lastrep.get('date', '?')}, "
                        f"result: {lastrep.get('result', 'not verified')}")  # fmt: skip

    days = {
        "urgent_owner_attention": 0,
        "maintenance": 1,
        "inspect": 2,
        "measure_again": 1,
        "monitor": 3,
    }.get(urgency, a["routine_days"].get(preset, 14))
    return Health(
        machine=machine, status=STATUS_FOR[urgency], urgency=urgency, severity=severity, trend=trend,
        confidence=confidence, confidence_reasons=reasons, evidence=evidence,
        next_inspection=(now + timedelta(days=days)).date().isoformat(),
        recent_risk=[round(r, 3) for r in recent], time_to_threshold=rul,
    )  # fmt: skip
