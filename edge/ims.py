"""NASA IMS bearing run-to-failure data: a health score over days, days-to-failure checked against real
failures, and the CWRU-trained fault classifier tried on bearings it has never seen.

    python -m edge.ims          -> docs/runs/ims_<UTC>.json, edge/models/ims_replay.json and the demo pack
    python -m edge.ims --pack   -> only rebuild edge/models/ims_demo_pack.npz (Demo mode without data/raw)

IMS (Lee, Qiu, Yu, Lin, Rexnord / University of Cincinnati, via NASA PCoE): four Rexnord ZA-2115
bearings on one shaft at 2000 rpm, 1 s vibration snapshots at 20 kHz every 10 min (5 min early in
test 1), run until one or two bearings failed. Which bearing failed and how is from the IMS readme.

Health score: per bearing, the first 10% of its snapshots (healthy, by the readme) define normal;
each snapshot's log band energies are z-scored against them and averaged (mean z^2); dividing by
the 95th percentile of the healthy part makes 1.0 = alarm level (as on the dashboard).

Days to failure: the dashboard's trend estimator (server/forecast.py), run on log10(score), to the
score a bearing reaches when it fails. That level, the trend window and the range are calibrated on the
OTHER failed bearings (leave one out), then checked on the held-out one. Range calibration: the ratio
real-remaining / estimated-mid on the other bearings; the range is mid x their 10th-90th percentile.
The raw bootstrap range is reported too. There are only four failures.
"""

import itertools
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from server import forecast

from . import faulttype
from .config import MODELS, RAW, RUNS

FS = 20_000
SHAFT_HZ = 2000 / 60
# Rexnord ZA-2115 double-row roller bearing: 16 rollers per row, pitch diameter 2.815 in, roller diameter
# 0.331 in, contact angle 15.17 degrees. The IMS readme gives none of this; the values are from the paper
# it cites (Qiu, Lee, Lin, J. Sound Vib. 289 (2006) 1066-1090). The readme lists test 3 as 4,448 files
# ending 2004-04-04; the archive holds 6,324 files ending 2004-04-18, and we use all of them.
ZA2115 = faulttype.Bearing.from_geometry(
    "Rexnord ZA-2115 (IMS)", n=16, d=0.331, pitch=2.815, contact_deg=15.17
)
FAILED = {  # from the IMS readme
    ("1st_test", "b3"): "inner_race",
    ("1st_test", "b4"): "ball",  # "roller element defect"
    ("2nd_test", "b1"): "outer_race",
    ("3rd_test", "b3"): "outer_race",
}
HEALTHY = [("2nd_test", "b2"), ("2nd_test", "b3"), ("2nd_test", "b4")]
DEMO = ("2nd_test", "b1")
BASELINE_FRAC = 0.10
WINDOWS = (0.5, 1.0, 2.0, 3.0)  # trend windows (days) tried on the calibration bearings
EDGES = np.geomspace(50, 9_500, 33)  # 32 log-spaced bands


def load(test: str, b: str) -> tuple[np.ndarray, np.ndarray]:
    """(days since the first snapshot, snapshots float32)."""
    t = np.load(RAW / "ims" / f"{test}_t.npy")
    x = np.load(RAW / "ims" / f"{test}_{b}.npy").astype(np.float32)
    return (t - t[0]) / 86_400, x


def band_energies(x: np.ndarray) -> np.ndarray:
    spec = np.abs(np.fft.rfft(x * np.hanning(x.shape[1]), axis=1)) ** 2
    f = np.fft.rfftfreq(x.shape[1], 1 / FS)
    return np.stack([np.log10(spec[:, (f >= lo) & (f < hi)].mean(axis=1) + 1e-12)
                     for lo, hi in itertools.pairwise(EDGES)], axis=1)  # fmt: skip


def health(x: np.ndarray) -> np.ndarray:
    """Score per snapshot; 1.0 = the 95th percentile of the bearing's own healthy start."""
    e = band_energies(x)
    n0 = max(10, int(len(e) * BASELINE_FRAC))
    mu, sd = e[:n0].mean(axis=0), e[:n0].std(axis=0) + 1e-6
    raw = (((e - mu) / sd) ** 2).mean(axis=1)
    return raw / np.percentile(raw[:n0], 95)


def rolling3(r: np.ndarray) -> np.ndarray:
    return np.convolve(r, np.ones(3) / 3, mode="full")[: len(r)]


def first_alarm(t: np.ndarray, r: np.ndarray, hold: int = 6) -> float | None:
    """First time the 3-reading mean stays at or above 1.0 for `hold` readings (an hour at 10 min)."""
    above = rolling3(r) >= 1.0
    for i in range(len(above) - hold):
        if above[i : i + hold].all() and i >= int(len(r) * BASELINE_FRAC):
            return float(t[i])
    return None


def failure_level(r: np.ndarray) -> float:
    return float(np.median(r[-max(3, len(r) // 50) :]))  # the score over the last 2% before the end


def estimates(
    t: np.ndarray, r: np.ndarray, level: float, window: float, start: float
) -> list[dict[str, Any]]:
    """Days-to-failure estimates from `start` to the end, vs the real remaining time."""
    lr = (
        np.log10(np.maximum(r, 1e-3)) + 1.0
    )  # log score; alarm (score 1.0) sits at 1.0 as the estimator expects
    lvl = np.log10(level) + 1.0
    saved = forecast.WINDOW_DAYS
    forecast.WINDOW_DAYS = window
    out = []
    try:
        step = max(1, len(t) // 400)
        for i in range(int(np.searchsorted(t, start)), len(t), step):
            est = forecast.estimate(t[: i + 1].tolist(), lr[: i + 1].tolist(), lvl)
            remaining = float(t[-1] - t[i])
            row = {"day": round(float(t[i]), 3), "remaining": round(remaining, 3), "status": est["status"]}
            if est["status"] == "estimate":
                hi = est["high"] if est["high"] is not None else np.inf
                row.update(
                    low=est["low"],
                    mid=est["mid"],
                    high=est["high"],
                    inside=bool(est["low"] <= remaining <= hi),
                )
            out.append(row)
    finally:
        forecast.WINDOW_DAYS = saved
    return out


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    made = [r for r in rows if r["status"] == "estimate"]
    if not made:
        return {"estimates": 0, "checks": len(rows)}
    err = [abs(r["mid"] - r["remaining"]) for r in made if r["mid"] is not None]
    return {"checks": len(rows), "estimates": len(made), "inside_range": sum(r["inside"] for r in made),
            "coverage": round(sum(r["inside"] for r in made) / len(made), 3),
            "median_abs_error_days": round(float(np.median(err)), 3) if err else None,
            "calibrated_inside": sum(r.get("cal_inside", False) for r in made),
            "calibrated_coverage": round(sum(r.get("cal_inside", False) for r in made) / len(made), 3)}  # fmt: skip


def log_ratios(rows: list[dict[str, Any]]) -> list[float]:
    """log10(real remaining time / estimated mid) for every estimate (both > 0)."""
    return [float(np.log10(r["remaining"] / r["mid"])) for r in rows
            if r["status"] == "estimate" and r["mid"] and r["remaining"] > 0]  # fmt: skip


def calibrate(rows: list[dict[str, Any]], band: tuple[float, float] | None) -> list[dict[str, Any]]:
    """Add the calibrated range: mid x 10^(10th..90th percentile of the error ratios on other bearings)."""
    for r in rows:
        if r["status"] == "estimate" and band and r["mid"] is not None:
            lo, hi = r["mid"] * 10 ** band[0], r["mid"] * 10 ** band[1]
            r.update(cal_low=round(lo, 3), cal_high=round(hi, 3), cal_inside=bool(lo <= r["remaining"] <= hi))
    return rows


PACK = MODELS / "ims_demo_pack.npz"
PACK_READINGS = 120  # Demo mode shows about this many readings; server/imsdemo.py may take every k-th


def demo_pack(clf: Any = None) -> Path:
    """What Demo mode needs, so a fresh clone can run it without the 1 GB IMS download.

    For each replay reading (after the learning window, as server/imsdemo.py plays it): the real 1 s
    snapshot (float16), its health score (from ims_replay.json), its timestamp, and the CWRU-trained
    classifier's output on the last 6 snapshots up to it (the last hour). All computed from data/raw/ims.
    """
    import hashlib

    meta = json.loads((MODELS / "ims_replay.json").read_text(encoding="utf-8"))
    t, x = load(meta["test"], meta["bearing"])
    score = np.asarray(meta["score"], dtype=np.float32)
    start = int(len(t) * BASELINE_FRAC)
    stride = max(1, -(-(len(t) - start) // PACK_READINGS))
    idx = np.arange(start, len(t), stride)
    clf = clf or faulttype.load()
    probs = []
    for i in idx:
        g = faulttype.predict(
            x[max(0, i - 5) : i + 1].reshape(-1).astype(np.float64), FS, SHAFT_HZ, ZA2115, clf
        )
        probs.append([g["p"][c] for c in faulttype.CLASSES])
    model_sha = hashlib.sha256((MODELS / "faulttype.joblib").read_bytes()).hexdigest()
    np.savez_compressed(
        PACK, t_days=t[idx].astype(np.float64), snap=x[idx].astype(np.float16), score=score[idx],
        classifier_p=np.asarray(probs, dtype=np.float32), classes=np.asarray(faulttype.CLASSES),
        snapshot_index=idx, learning_window_end_day=np.float64(t[start]),
        provenance=np.asarray(json.dumps({"source": meta["source_run"], "test": meta["test"], "bearing": meta["bearing"],
                                          "faulttype_sha256": model_sha, "classifier_input": "last 6 snapshots (1 h)"})),
    )  # fmt: skip
    return PACK


def main() -> None:
    meta = json.loads((RAW / "ims" / "meta.json").read_text(encoding="utf-8"))
    series: dict[tuple[str, str], dict[str, Any]] = {}
    for key in [*FAILED, *HEALTHY]:
        if not (RAW / "ims" / f"{key[0]}_{key[1]}.npy").exists():
            continue
        t, x = load(*key)
        series[key] = {"t": t, "x": x, "r": health(x)}
    record: dict[str, Any] = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "source": meta.get("source"),
                              "bearing": ZA2115.__dict__, "baseline_frac": BASELINE_FRAC, "bearings": {}}  # fmt: skip
    failed = [k for k in FAILED if k in series]
    levels = {k: failure_level(series[k]["r"]) for k in failed}
    clf = faulttype.load()
    cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

    def rows_for(k: tuple[str, str], lv: float, w: float) -> list[dict[str, Any]]:
        """Raw estimates for bearing k from its first alarm (cached: the nested leave-one-out reuses them)."""
        if (k, lv, w) not in cache:
            so = series[k]
            a = first_alarm(so["t"], so["r"])
            cache[(k, lv, w)] = estimates(so["t"], so["r"], lv, w, a if a is not None else so["t"][-1])
        return cache[(k, lv, w)]

    def level_without(*excl: tuple[str, str]) -> float:
        return float(np.median([levels[j] for j in failed if j not in excl]))

    for key in failed:
        s = series[key]
        t, r = s["t"], s["r"]
        others = [k for k in failed if k != key]
        level = level_without(key)
        # window and range calibration from the OTHER failed bearings only; the held-out one never tunes itself
        scores, ratios_by_w = {}, {}
        for w in WINDOWS:
            ratios = []
            for k in others:
                ratios += log_ratios(rows_for(k, level_without(key, k), w))
            ratios_by_w[w] = ratios
            scores[w] = float(np.median(np.abs(ratios))) if ratios else np.inf
        window = min(scores, key=lambda w: (scores[w], w))
        cal = ratios_by_w[window]
        band = (float(np.percentile(cal, 10)), float(np.percentile(cal, 90))) if cal else None
        alarm = first_alarm(t, r)
        rows = calibrate(estimates(t, r, level, window, alarm if alarm is not None else t[-1]), band)
        late = s["x"][-max(3, len(s["x"]) // 20) :]
        early = s["x"][: max(3, len(s["x"]) // 20)]
        pred_late = faulttype.predict(late.reshape(-1).astype(np.float64), FS, SHAFT_HZ, ZA2115, clf)
        pred_early = faulttype.predict(early.reshape(-1).astype(np.float64), FS, SHAFT_HZ, ZA2115, clf)
        record["bearings"][f"{key[0]}/{key[1]}"] = {
            "failed": True, "true_fault": FAILED[key], "days": round(float(t[-1]), 2), "snapshots": len(t),
            "first_alarm_day": alarm, "warned_days_before_end": None if alarm is None else round(float(t[-1] - alarm), 2),
            "failure_level_used": round(level, 2), "own_failure_level": round(levels[key], 2),
            "window_days_chosen": window, "window_median_abs_log10_error_on_others": scores,
            "calibration_log10_ratio_band": band, "calibration_log10_ratios": [round(v, 4) for v in cal],
            "days_to_failure": summarise(rows), "estimates": rows,
            "classifier_last_5pct": pred_late, "classifier_first_5pct": pred_early,
        }  # fmt: skip
    for key in HEALTHY:
        if key in series:
            r = series[key]["r"]
            n0 = int(len(r) * BASELINE_FRAC)
            record["bearings"][f"{key[0]}/{key[1]}"] = {
                "failed": False, "snapshots": len(r),
                "alarm_rate_after_baseline": round(float((rolling3(r)[n0:] >= 1.0).mean()), 3),
                "first_alarm_day": first_alarm(series[key]["t"], r),
            }  # fmt: skip
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"ims_{record['run_id']}.json"
    out.write_text(json.dumps(record, indent=1, default=float), encoding="utf-8")
    if (
        DEMO in series
    ):  # the replay Demo mode plays: score per snapshot + timestamps (raw audio stays in data/raw)
        s = series[DEMO]
        b = record["bearings"][f"{DEMO[0]}/{DEMO[1]}"]
        replay = {"test": DEMO[0], "bearing": DEMO[1], "t_days": np.round(s["t"], 4).tolist(),
                  "score": np.round(s["r"], 3).tolist(), "failure_level": b["failure_level_used"],
                  "window_days": b["window_days_chosen"], "ratio_band_log10": b["calibration_log10_ratio_band"],
                  "ratios_log10": b["calibration_log10_ratios"], "first_alarm_day": b["first_alarm_day"],
                  "true_fault": FAILED[DEMO], "source_run": out.name, "first_snapshot": meta["tests"][DEMO[0]]["first"]}  # fmt: skip
        Path(MODELS / "ims_replay.json").write_text(json.dumps(replay), encoding="utf-8")
        demo_pack(clf)
    print(out)
    for k, v in record["bearings"].items():
        print(
            k,
            {
                kk: v.get(kk)
                for kk in (
                    "true_fault",
                    "days",
                    "first_alarm_day",
                    "warned_days_before_end",
                    "window_days_chosen",
                    "alarm_rate_after_baseline",
                )
            },
            v.get("days_to_failure", ""),
            (v.get("classifier_last_5pct") or {}).get("class", ""),
        )


if __name__ == "__main__":
    import sys

    print(demo_pack()) if "--pack" in sys.argv else main()
