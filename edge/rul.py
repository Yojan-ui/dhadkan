"""Time to the maintenance threshold, with an interval, and honest about when it cannot say.

    rul.estimate(t_days, risk, threshold, ...)   -> one estimate for the dashboard / agent
    python -m edge.rul                            -> docs/runs/rul_<UTC>.json (NASA IMS, leave one bearing out)

What it estimates: the number of days until the machine's health score (risk; 1.0 = its own alarm
level) reaches the MAINTENANCE THRESHOLD, i.e. the level at which earlier machines needed repair. That is
NOT a failure date. On NASA IMS the threshold is the score the OTHER bearings had when their run ended,
so the evaluation reports both: the error against the time the held-out bearing actually reached that
threshold, and against the end of its run (failure).

When it says nothing (status, never a number):
  insufficient_data  fewer than MIN_READINGS usable readings, or they span less than MIN_SPAN of the
                     trend window, after dropping unusable readings and everything before the last gap,
                     restart or repair (the trend of a different operating period says nothing about this one)
  no_trend           no clearly rising trend (t-statistic of the slope below forecast.MIN_T_STAT)
  at_threshold       the last readings are already at the threshold
  estimate           low / mid / high days: the 10-90% range. With `ratios_log10` (errors measured on
                     other run-to-failure machines) the range is mid x their 10th-90th percentile;
                     otherwise it is the raw residual-bootstrap range, which is known to be too narrow.

Trend: a straight line through log10(risk) over the trend window (server/forecast.estimate). Wear often
comes in steps that no trend can predict; docs/RESULTS.md lists those failure cases.
"""

import json
import time
from typing import Any

import numpy as np

from server import forecast

MIN_READINGS = 6
MIN_SPAN = 0.25  # usable readings must span at least this share of the trend window
MEANING = "days until the health score reaches the maintenance threshold (not a failure date)"


def _log(v: np.ndarray) -> np.ndarray:
    return np.log10(np.maximum(v, 1e-3)) + 1.0  # alarm (risk 1.0) sits at 1.0, as forecast expects


def last_segment(t: np.ndarray, valid: np.ndarray, gap_days: float, events: list[float]) -> np.ndarray:
    """Indices of usable readings after the last gap longer than gap_days and after the last event."""
    idx = np.flatnonzero(valid)
    if len(idx) == 0:
        return idx
    tv = t[idx]
    cut = 0
    gaps = np.flatnonzero(np.diff(tv) > gap_days)
    if len(gaps):
        cut = int(gaps[-1]) + 1
    past = [e for e in events if e <= tv[-1]]
    if past:
        cut = max(cut, int(np.searchsorted(tv, max(past), side="left")))
    return idx[cut:]


def estimate(
    t_days: list[float] | np.ndarray,
    risk: list[float] | np.ndarray,
    threshold: float,
    *,
    window_days: float = 2.0,
    valid: list[bool] | np.ndarray | None = None,
    gap_days: float = 0.5,
    events: list[float] | None = None,
    ratios_log10: list[float] | None = None,
) -> dict[str, Any]:
    t = np.asarray(t_days, dtype=float)
    r = np.asarray(risk, dtype=float)
    ok = np.isfinite(r) if valid is None else (np.asarray(valid, dtype=bool) & np.isfinite(r))
    base = {"target": "maintenance_threshold", "meaning": MEANING, "threshold": round(float(threshold), 3),
            "window_days": window_days, "interval": "10-90%"}  # fmt: skip
    if len(t) == 0:
        return {**base, "status": "insufficient_data", "reason": "no readings", "n_used": 0}
    seg = last_segment(t, ok, gap_days, events or [])
    seg = seg[t[seg] >= t[seg][-1] - window_days] if len(seg) else seg
    n_excluded = int((~ok).sum())
    if len(seg) < MIN_READINGS:
        return {**base, "status": "insufficient_data", "n_used": len(seg), "n_unusable": n_excluded,
                "reason": f"{len(seg)} usable readings since the last gap/restart/repair (need {MIN_READINGS})"}  # fmt: skip
    span = float(t[seg][-1] - t[seg][0])
    if span < MIN_SPAN * window_days:
        return {**base, "status": "insufficient_data", "n_used": len(seg), "n_unusable": n_excluded,
                "reason": f"readings span {span:.2f} d (need {MIN_SPAN * window_days:.2f} d)"}  # fmt: skip
    est = forecast.estimate(t[seg].tolist(), _log(r[seg]).tolist(), float(_log(np.array([threshold]))[0]),
                            window=window_days)  # fmt: skip
    status = {"estimate": "estimate", "at_fault_level": "at_threshold", "no_trend": "no_trend",
              "too_few": "insufficient_data", "not_separable": "insufficient_data"}[est["status"]]  # fmt: skip
    out = {**base, "status": status, "n_used": len(seg), "n_unusable": n_excluded,
           "segment_start_day": round(float(t[seg][0]), 3)}  # fmt: skip
    if status == "at_threshold":
        return {**out, "low": 0.0, "mid": 0.0, "high": 0.0}
    if status != "estimate":
        return {**out, "slope_per_day": est.get("slope_per_day"), "t_stat": est.get("t_stat")}
    out.update(slope_per_day=est["slope_per_day"], t_stat=est["t_stat"], raw_low=est["low"], mid=est["mid"],
               raw_high=est["high"])  # fmt: skip
    if ratios_log10 and est["mid"] is not None:
        lo, hi = np.percentile(ratios_log10, [10, 90])
        out.update(low=round(est["mid"] * 10**lo, 2), high=round(est["mid"] * 10**hi, 2), calibrated=True)
    else:
        out.update(low=est["low"], high=est["high"], calibrated=False)
    return out


# ---------------------------------------------------------------- evaluation on NASA IMS (leave one out)


def _first_at(t: np.ndarray, r: np.ndarray, level: float, hold: int = 3) -> float | None:
    from .ims import rolling3

    above = rolling3(r) >= level
    for i in range(len(above) - hold + 1):
        if above[i : i + hold].all():
            return float(t[i])
    return None


def _gap_days(t: np.ndarray) -> float:
    return float(max(6 * np.median(np.diff(t)), 1 / 24))  # 6 missed snapshots, at least an hour


def evaluate() -> dict[str, Any]:
    from . import ims

    series = {}
    for key in ims.FAILED:
        t, x = ims.load(*key)
        series[key] = (t, ims.health(x))
    healthy = {}
    for key in ims.HEALTHY:
        t, x = ims.load(*key)
        healthy[key] = (t, ims.health(x))
    levels = {k: ims.failure_level(r) for k, (t, r) in series.items()}
    out: dict[str, Any] = {}
    for key, (t, r) in series.items():
        others = [k for k in series if k != key]
        threshold = float(np.median([levels[k] for k in others]))
        # window and interval calibration from the other bearings (each against its own end of run)
        best = None
        for w in ims.WINDOWS:
            ratios = []
            for k in others:
                tk, rk = series[k]
                lvl = float(np.median([levels[j] for j in series if j not in (key, k)]))
                a = ims.first_alarm(tk, rk) or tk[-1]
                for i in range(int(np.searchsorted(tk, a)), len(tk), max(1, len(tk) // 200)):
                    e = estimate(tk[: i + 1], rk[: i + 1], lvl, window_days=w, gap_days=_gap_days(tk))
                    if e["status"] == "estimate" and e["mid"] and tk[-1] - tk[i] > 0:
                        ratios.append(float(np.log10((tk[-1] - tk[i]) / e["mid"])))
            score = float(np.median(np.abs(ratios))) if ratios else np.inf
            if best is None or score < best[0]:
                best = (score, w, ratios)
        assert best is not None
        _, window, ratios = best
        alarm = ims.first_alarm(t, r)
        t_thr = _first_at(t, r, threshold)
        rows = []
        start = alarm if alarm is not None else t[-1]
        for i in range(int(np.searchsorted(t, start)), len(t), max(1, len(t) // 400)):
            e = estimate(t[: i + 1], r[: i + 1], threshold, window_days=window, gap_days=_gap_days(t),
                         ratios_log10=ratios)  # fmt: skip
            row = {"day": round(float(t[i]), 3), "status": e["status"],
                   "to_end": round(float(t[-1] - t[i]), 3),
                   "to_threshold": None if t_thr is None or t_thr < t[i] else round(t_thr - float(t[i]), 3)}  # fmt: skip
            if e["status"] == "estimate":
                row.update(low=e["low"], mid=e["mid"], high=e["high"])
            rows.append(row)
        out[f"{key[0]}/{key[1]}"] = {
            "threshold": round(threshold, 2), "own_end_level": round(levels[key], 2), "window_days": window,
            "first_alarm_day": alarm, "reached_threshold_day": t_thr, "end_day": round(float(t[-1]), 3),
            "lead_time_days": None if alarm is None else round(float(t[-1] - alarm), 2),
            "metrics": {"vs_threshold": _metrics(rows, "to_threshold"), "vs_end_of_run": _metrics(rows, "to_end")},
            "rows": rows,
        }  # fmt: skip
    false_alarms = {}
    for key, (t, r) in healthy.items():
        n0 = int(len(r) * ims.BASELINE_FRAC)
        false_alarms[f"{key[0]}/{key[1]}"] = {
            "alarm_share_after_baseline": round(float((ims.rolling3(r)[n0:] >= 1.0).mean()), 3),
            "first_alarm_day": ims.first_alarm(t, r),
            "note": "did not fail; shares a shaft with 2nd_test/b1, which did",
        }
    return {"bearings": out, "healthy_bearings": false_alarms}


def _metrics(rows: list[dict[str, Any]], truth: str) -> dict[str, Any]:
    n = len(rows)
    made = [
        x for x in rows if x["status"] == "estimate" and x.get(truth) is not None and x["mid"] is not None
    ]
    states = {s: sum(x["status"] == s for x in rows) for s in ("estimate", "no_trend", "insufficient_data",
                                                                  "at_threshold")}  # fmt: skip
    if not made:
        return {"checks": n, "states": states, "estimates_scored": 0}
    err = np.array([x["mid"] - x[truth] for x in made])
    inside = [x["low"] <= x[truth] <= (x["high"] if x["high"] is not None else np.inf) for x in made]
    return {"checks": n, "states": states, "estimates_scored": len(made),
            "mae_days": round(float(np.mean(np.abs(err))), 3), "rmse_days": round(float(np.sqrt(np.mean(err**2))), 3),
            "median_abs_error_days": round(float(np.median(np.abs(err))), 3),
            "bias_days": round(float(np.mean(err)), 3),
            "interval_coverage": round(float(np.mean(inside)), 3), "nominal_coverage": 0.8}  # fmt: skip


def main() -> None:
    from .config import RUNS
    from .train import _code_hash, _git_rev

    t0 = time.time()
    rec = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "git_rev": _git_rev(),
           "code_sha256": _code_hash(), "meaning": MEANING,
           "protocol": "leave one failed bearing out: threshold, trend window and interval calibration from the "
                       "other three; estimates every ~1/400 of the run from the held-out bearing's first alarm",
           **evaluate()}  # fmt: skip
    rec["seconds"] = round(time.time() - t0, 1)
    out = RUNS / f"rul_{rec['run_id']}.json"
    out.write_text(json.dumps(rec, indent=1, default=float), encoding="utf-8")
    print(out)
    for k, b in rec["bearings"].items():
        m1, m2 = b["metrics"]["vs_threshold"], b["metrics"]["vs_end_of_run"]
        print(f"{k}: lead {b['lead_time_days']} d | vs threshold MAE {m1.get('mae_days')} RMSE {m1.get('rmse_days')} "
              f"cov {m1.get('interval_coverage')} | vs end MAE {m2.get('mae_days')} cov {m2.get('interval_coverage')}"
              f" | states {m1['states']}")  # fmt: skip


if __name__ == "__main__":
    main()
