"""How many days until a machine sounds like its recorded fault, as a range.

Fault level: the median score, relative to this machine's own alarm level, of
the recorded *faulty* clips of the same unit in the evaluation record. Where
that level is not above the alarm level the model cannot tell faulty from
healthy well enough on those recordings, and no estimate is given.

Estimate: a straight line through the last WINDOW_DAYS of readings, projected
to the fault level. The range is the 10th-90th percentile of the crossing time
over a residual bootstrap of that line. It is an extrapolation of the trend, not
a failure model: MIMII has no run-to-failure recordings, and the onsets in the
demo are SYNTHETIC.
"""

import json
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from edge.config import RUNS

WINDOW_DAYS = 6.0  # chosen on the demo replay; see README "Days to fault level"
MIN_READINGS = 6
MIN_T_STAT = 3.0  # slope must be clearly positive (0.4% false trends on pure noise)
N_BOOT = 400
HORIZON_DAYS = 30.0


@cache
def fault_levels(eval_path: Path | None = None) -> dict[str, dict[str, float]]:
    """{unit: {"level": median faulty risk, "healthy": median healthy risk, "n": clips}} from the eval record."""
    path = eval_path or sorted(RUNS.glob("eval_*.json"))[-1]
    ev = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for rep in ev["types"].values():
        cond = rep["headline_condition"]
        for unit, thr in rep["thresholds"].items():
            rows = [r for r in rep["clip_scores"] if r["unit"] == unit and r["condition"] == cond]
            bad = [r["score"] / thr for r in rows if r["label"] == 1]
            good = [r["score"] / thr for r in rows if r["label"] == 0 and r["split"] == "test"]
            out[unit] = {"level": float(np.median(bad)), "healthy": float(np.median(good)), "n": len(bad)}
    return out


def _fit(t: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """OLS y = a + b t -> (a, b, t-statistic of b)."""
    tm, ym = t.mean(), y.mean()
    sxx = float(((t - tm) ** 2).sum())
    b = float(((t - tm) * (y - ym)).sum() / sxx)
    a = ym - b * tm
    resid = y - (a + b * t)
    se = float(np.sqrt((resid**2).sum() / max(1, len(t) - 2) / sxx))
    return a, b, (b / se if se > 0 else np.inf)


def estimate(
    days: list[float], risk: list[float], level: float, seed: int = 0, window: float | None = None
) -> dict[str, Any]:
    """days: reading times in days; risk: score / alarm level per reading. Times in the result are days from the last reading."""
    window = WINDOW_DAYS if window is None else window
    out: dict[str, Any] = {"level": round(level, 3), "window_days": window}
    if level <= 1.0:
        return {**out, "status": "not_separable"}
    t, y = np.asarray(days, float), np.asarray(risk, float)
    keep = t >= t[-1] - window if len(t) else t.astype(bool)
    t, y = t[keep], y[keep]
    if len(t) < MIN_READINGS:
        return {**out, "status": "too_few", "readings": len(t)}
    now = t[-1]
    if y[-3:].mean() >= level:
        return {**out, "status": "at_fault_level", "low": 0.0, "mid": 0.0, "high": 0.0}
    a, b, tstat = _fit(t, y)
    out.update(slope_per_day=round(b, 4), t_stat=round(float(tstat), 2))
    if b <= 0 or tstat < MIN_T_STAT:
        return {**out, "status": "no_trend"}
    rng = np.random.default_rng(seed)
    resid = y - (a + b * t)
    ys = a + b * t + rng.choice(resid, (N_BOOT, len(t)))  # residual bootstrap, all fits at once
    tc = t - t.mean()
    slopes = (ys - ys.mean(axis=1, keepdims=True)) @ tc / float(tc @ tc)
    icepts = ys.mean(axis=1) - slopes * t.mean()
    lines = np.stack([icepts, slopes], axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        cross = np.where(slopes > 0, np.maximum(0.0, (level - icepts) / slopes - now), np.inf)
    lo, mid, hi = np.percentile(cross, [10, 50, 90])
    end = float(min(HORIZON_DAYS, hi if np.isfinite(hi) else HORIZON_DAYS))
    grid = np.linspace(0, max(end, 0.5), 25)
    pred = lines[:, :1] + lines[:, 1:] * (now + grid)
    band = [
        {
            "day": round(float(d), 2),
            "lo": round(float(p10), 3),
            "fit": round(float(a + b * (now + d)), 3),
            "hi": round(float(p90), 3),
        }
        for d, p10, p90 in zip(grid, *np.percentile(pred, [10, 90], axis=0), strict=True)
    ]
    return {**out, "status": "estimate", "low": _finite(lo), "mid": _finite(mid), "high": _finite(hi),
            "share_beyond_horizon": round(float(np.mean(cross > HORIZON_DAYS)), 3),
            "crossings": np.sort(np.where(np.isfinite(cross), cross, 1e9)).round(2).tolist(),
            "band": band}  # fmt: skip


# Units outside the evaluation record (the IMS bearing Demo mode replays): their failure level,
# trend window and whether to work on log10(score), from edge/ims.py's calibration.
EXTRA: dict[str, dict[str, Any]] = {}


def register(
    unit: str, level: float, window_days: float, log: bool = True, ratios_log10: list[float] | None = None
) -> None:
    """ratios_log10: log10(real remaining / estimated mid) measured on other run-to-failure bearings; when
    given, the range becomes mid x their 10th-90th percentile (edge/ims.py's calibration)."""
    EXTRA[unit] = {"level": level, "window": window_days, "log": log, "ratios": ratios_log10 or None}


def _calibrated(est: dict[str, Any], ratios: list[float]) -> dict[str, Any]:
    if est.get("status") != "estimate" or est.get("mid") is None:
        return est
    f = 10 ** np.asarray(ratios, float)
    lo, hi = np.percentile(f, [10, 90])
    mid = float(est["mid"])
    return {**est, "low": round(float(mid * lo), 1), "high": round(float(mid * hi), 1), "raw_low": est["low"], "raw_high": est["high"],
            "crossings": np.sort(mid * f).round(2).tolist(), "calibrated_on": "NASA IMS (other bearings)"}  # fmt: skip


def known(unit: str) -> bool:
    return unit in EXTRA or unit in fault_levels()


def from_history(hist: list[dict[str, Any]], tick_hours: float, unit: str) -> dict[str, Any]:
    """Estimate for one machine from its readings (sim.Cluster.history rows)."""
    if unit in EXTRA:
        x = EXTRA[unit]
        rows = hist[-(int(x["window"] * 24 / tick_hours) + 1) :]
        tr = (lambda v: np.log10(max(v, 1e-3)) + 1.0) if x["log"] else (lambda v: v)
        global WINDOW_DAYS
        saved, WINDOW_DAYS = WINDOW_DAYS, x["window"]
        try:
            est = estimate(
                [h["tick"] * tick_hours / 24 for h in rows], [tr(h["risk"]) for h in rows], tr(x["level"])
            )
        finally:
            WINDOW_DAYS = saved
        if x.get("ratios"):
            est = _calibrated(est, x["ratios"])
        return {**est, "level": x["level"], "scale": "log10" if x["log"] else "linear"}
    keep = int(WINDOW_DAYS * 24 / tick_hours) + 1  # only the window matters
    rows = tuple((h["tick"], round(h["risk"], 6)) for h in hist[-keep:])
    return _cached(rows, tick_hours, fault_levels()[unit]["level"])


@lru_cache(maxsize=4096)
def _cached(rows: tuple[tuple[int, float], ...], tick_hours: float, level: float) -> dict[str, Any]:
    return estimate([tk * tick_hours / 24 for tk, _ in rows], [r for _, r in rows], level)


def public(est: dict[str, Any]) -> dict[str, Any]:
    """Drop the raw bootstrap samples (used only for probabilities)."""
    return {k: v for k, v in est.items() if k != "crossings"}


def _finite(v: float) -> float | None:
    return round(float(v), 1) if np.isfinite(v) else None


def p_before(est: dict[str, Any], days: float) -> float:
    """Share of the bootstrap that reaches the fault level within `days` (0 when there is no estimate)."""
    if est.get("status") == "at_fault_level":
        return 1.0
    if est.get("status") != "estimate":
        return 0.0
    c = np.asarray(est["crossings"])
    return float(np.mean(c <= days))
