"""Which unsupervised detector (or fusion) should each machine use? Chosen on selection units, reported
once on held-out units.

    python -m edge.compare            -> docs/runs/compare_<UTC>.json

Protocol (no test-split tuning on the units we report):
  * Every detector is fitted on ONE machine's normal training readings only (edge/detectors.py).
  * SELECTION units: DCASE 2020 IDs 00 and 02 of pump/fan/valve/slider + MIMII DUE gearbox sections 00, 01.
    The candidate with the best mean pAUC over these 10 units is chosen (AUC breaks ties).
  * HELD-OUT units: IDs 04 and 06 + gearbox section 02 (9 units). The chosen candidate, and every other
    candidate for transparency, is scored on them once. The choice never looks at them.
  * Operating point: alarm at risk >= 1 (95th pct of the machine's own out-of-fold normal scores).
    Precision, recall, F1 and the false-alarm rate are measured on the test clips at that point.
    Precision depends on how many faulty clips a test set has (DCASE has ~1:1 or more), so it is
    higher than it would be in a shed where faults are rare; the false-alarm rate does not have that problem.
  * The autoencoder was trained on the train clips, so its per-machine calibration uses the 10 held-out
    val clips only (it would look too good on clips it was trained on).
  * Gearbox target domain (changed speed/load) is a stress test with the source-domain model.

Summaries (edge/summary.py) and autoencoder scores per clip are cached in data/cache/ (git-ignored).
"""

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from . import data, features, scoring, summary
from .config import MACHINE_TYPES, ROOT, RUNS
from .detectors import AE_COL, WIDTH, MachineModel
from .train import _code_hash, _git_rev

CACHE = ROOT / "data" / "cache"
SELECTION_IDS = ("id_00", "id_02", "section_00", "section_01")

CANDIDATES: dict[str, dict[str, Any]] = {
    "rms": {"names": ["rms"]},
    "spectral": {"names": ["spectral"]},
    "transient": {"names": ["transient"]},
    "mfcc": {"names": ["mfcc"]},
    "iforest": {"names": ["iforest"]},
    "ae": {"names": ["ae"]},
    "rms+spectral": {"names": ["rms", "spectral"]},
    "rms+mfcc": {"names": ["rms", "mfcc"]},
    "rms+transient": {"names": ["rms", "transient"]},
    "rms+ae": {"names": ["rms", "ae"]},
    "rms+spectral+transient": {"names": ["rms", "spectral", "transient"]},
    "classical": {"names": ["rms", "spectral", "transient", "mfcc", "iforest"]},
    "all": {"names": ["rms", "spectral", "transient", "mfcc", "iforest", "ae"]},
    "all/max": {"names": ["rms", "spectral", "transient", "mfcc", "iforest", "ae"], "fusion": "max"},
}

_MODELS: dict[str, Any] = {}


def _row(args: tuple[str, str, str]) -> np.ndarray:
    path, mtype, unit = args
    from .runtime import EdgeModel

    if mtype not in _MODELS:
        _MODELS[mtype] = EdgeModel(mtype)
    audio = features.load(path)
    r = np.full(WIDTH, np.nan)
    r[: summary.DIM] = summary.summarise(audio)
    r[AE_COL] = _MODELS[mtype].score_audio(audio, unit)["score"]
    return r


def rows_for(mtype: str, clips: list[data.Clip], workers: int) -> np.ndarray:
    key = hashlib.sha256("\n".join(str(c.path.relative_to(ROOT)) for c in clips).encode()).hexdigest()[:16]
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"summary_{mtype}_{key}.npy"
    if f.exists():
        return np.load(f)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(_row, [(str(c.path), mtype, c.unit) for c in clips], chunksize=8))
    x = np.vstack(rows)
    np.save(f, x)
    return x


def at_alarm(y: np.ndarray, risk: np.ndarray) -> dict[str, float]:
    alarm = risk >= 1.0
    tp, fp = int((alarm & (y == 1)).sum()), int((alarm & (y == 0)).sum())
    fn = int((~alarm & (y == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0,
        "false_alarm_rate": round(fp / max(int((y == 0).sum()), 1), 4),
    }


def evaluate_unit(x: np.ndarray, clips: list[data.Clip], unit: str, cond: str) -> dict[str, Any]:
    idx = {s: [i for i, c in enumerate(clips) if c.unit == unit and c.split == s] for s in ("train", "val")}
    test = [i for i, c in enumerate(clips) if c.unit == unit and c.split == "test" and c.condition == cond]
    y = np.array([clips[i].label for i in test])
    out: dict[str, Any] = {
        "n_fit": len(idx["train"]) + len(idx["val"]),
        "n_normal": int((y == 0).sum()),
        "n_anomalous": int(y.sum()),
    }
    for name, cfg in CANDIDATES.items():
        m = MachineModel(cfg["names"], cfg.get("fusion", "mean"))
        # classical detectors learn from train + val normals; the autoencoder is calibrated on val only
        m.fit(x[idx["train"] + idx["val"]], ae_rows=x[idx["val"]])
        risk = m.risk(x[test])
        out[name] = {
            "auc": round(scoring.auc(y, risk), 4),
            "pauc": round(scoring.pauc(y, risk), 4),
            **at_alarm(y, risk),
        }
    return out


def summarise_set(per_unit: dict[str, dict[str, Any]], units: list[str]) -> dict[str, dict[str, float]]:
    agg = {}
    for name in CANDIDATES:
        vals = [per_unit[u][name] for u in units]
        agg[name] = {k: round(float(np.mean([v[k] for v in vals])), 4) for k in vals[0]}
    return agg


def latest() -> Path | None:
    runs = sorted(RUNS.glob("compare_*.json"))
    return runs[-1] if runs else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", nargs="+", default=list(MACHINE_TYPES))
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args()
    t0 = time.time()
    per_unit: dict[str, dict[str, Any]] = {}
    stress: dict[str, dict[str, Any]] = {}
    for mtype in args.types:
        clips = data.clips_for(mtype)
        print(f"{mtype}: {len(clips)} clips", flush=True)
        x = rows_for(mtype, clips, args.workers)
        for unit in sorted({c.unit for c in clips}):
            per_unit[unit] = evaluate_unit(x, clips, unit, data.HEADLINE[mtype])
            if mtype == "gearbox":
                stress[unit] = evaluate_unit(x, clips, unit, "target")
    sel = [u for u in per_unit if u.split("/")[1] in SELECTION_IDS]
    held = [u for u in per_unit if u not in sel]
    sel_agg, held_agg = summarise_set(per_unit, sel), summarise_set(per_unit, held)
    chosen = max(CANDIDATES, key=lambda n: (sel_agg[n]["pauc"], sel_agg[n]["auc"]))
    record = {
        "run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "git_rev": _git_rev(),
        "code_sha256": _code_hash(),
        "protocol": {
            "selection_units": sel,
            "held_out_units": held,
            "choose_by": "mean pAUC over selection units, then AUC",
            "alarm": "risk >= 1 = 95th pct of the machine's own out-of-fold normal scores",
            "ae_calibration": "10 val clips per machine (the autoencoder was trained on the train clips)",
        },
        "candidates": CANDIDATES,
        "chosen": chosen,
        "selection": sel_agg,
        "held_out": held_agg,
        "per_unit": per_unit,
        "gearbox_target_domain": stress,
        "seconds": round(time.time() - t0, 1),
    }
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"compare_{record['run_id']}.json"
    out.write_text(json.dumps(record, indent=1))
    print(f"\nwrote {out}\nchosen on selection units: {chosen}")
    print(
        f"{'candidate':26s} {'sel AUC':>8s} {'sel pAUC':>8s} | {'held AUC':>8s} {'held pAUC':>9s} {'held F1':>7s} {'held FAR':>8s}"
    )
    for n in CANDIDATES:
        s, h = sel_agg[n], held_agg[n]
        print(
            f"{n:26s} {s['auc']:8.3f} {s['pauc']:8.3f} | {h['auc']:8.3f} {h['pauc']:9.3f} {h['f1']:7.3f} {h['false_alarm_rate']:8.3f}"
        )


if __name__ == "__main__":
    main()
