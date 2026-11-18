"""Two checks on the listening model that RESULTS.md needs: is it better than a loudness threshold, and
what does it cost to run.

    python -m edge.listen_eval   -> docs/runs/listen_<UTC>.json

1. RMS-threshold baseline, the simplest thing a factory could do with a phone: score each clip by how
   far its loudness (log10 RMS) sits from the machine's own normal (median and MAD of its normal
   training + validation clips, two-sided, so both louder and quieter count). AUC/pAUC per machine on
   exactly the test clips the autoencoder is scored on. pAUC is the DCASE baseline definition.
2. Edge cost of the exported .tflite on this computer: file size, ms per 1.06 s window (1 and 4
   threads), ms per 10 s reading end to end (log-mel + model). Measured on a laptop CPU, not a phone.
   Battery cannot be measured without the Android app; it is recorded as null with that reason.
"""

import json
import platform
import time
from typing import Any

import numpy as np

from . import data, features, scoring
from .config import MODELS, RUNS


def log_rms(path: Any) -> float:
    a = features.load(path)
    return float(np.log10(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-12))


def rms_baseline(mtype: str) -> dict[str, Any]:
    clips = data.clips_for(mtype)
    cond = data.HEADLINE[mtype]
    out: dict[str, Any] = {}
    for unit in sorted({c.unit for c in clips}):
        ref = [log_rms(c.path) for c in clips if c.unit == unit and c.split in ("train", "val")]
        test = [c for c in clips if c.unit == unit and c.split == "test" and c.condition == cond]
        if len(ref) < 5 or not test or len({c.label for c in test}) < 2:
            out[unit] = None
            continue
        med = float(np.median(ref))
        mad = float(np.median(np.abs(np.asarray(ref) - med))) + 1e-9
        y = [c.label for c in test]
        s = [abs(log_rms(c.path) - med) / mad for c in test]
        out[unit] = {"auc": scoring.auc(y, s), "pauc": scoring.pauc(y, s), "n_normal": y.count(0),
                     "n_anomalous": y.count(1), "n_reference": len(ref)}  # fmt: skip
    return out


def edge_cost(mtype: str, reps: int = 30) -> dict[str, Any]:
    from ai_edge_litert.interpreter import Interpreter  # same runtime as edge/runtime.py

    path = MODELS / mtype / "model.tflite"
    clip = next(c for c in data.clips_for(mtype) if c.split == "test")
    audio = features.load(clip.path)
    res: dict[str, Any] = {"size_bytes": path.stat().st_size}
    for threads in (1, 4):
        it = Interpreter(model_path=str(path), num_threads=threads)
        inp, outp = it.get_input_details()[0], it.get_output_details()[0]
        wins = features.windows(audio)
        x1 = ((wins[:1] - wins.mean()) / (wins.std() + 1e-6))[..., None].astype(np.float32)
        it.resize_tensor_input(inp["index"], x1.shape)
        it.allocate_tensors()
        times = []
        for _ in range(reps):
            t0 = time.perf_counter()
            it.set_tensor(inp["index"], x1)
            it.invoke()
            it.get_tensor(outp["index"])
            times.append((time.perf_counter() - t0) * 1000)
        res[f"ms_per_window_{threads}_threads"] = round(float(np.median(times)), 3)
    from .runtime import EdgeModel

    em = EdgeModel(mtype)
    unit = clip.unit
    em.score_audio(audio, unit)  # warm-up
    t = []
    for _ in range(10):
        t0 = time.perf_counter()
        em.score_audio(audio, unit)
        t.append((time.perf_counter() - t0) * 1000)
    res["ms_per_10s_reading_4_threads"] = round(float(np.median(t)), 1)
    res["windows_per_reading"] = len(features.windows(audio))
    return res


def main() -> None:
    rec: dict[str, Any] = {
        "run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "device": {
            "machine": platform.machine(),
            "processor": platform.processor(),
            "system": platform.system(),
            "python": platform.python_version(),
        },
        "battery": None,
        "battery_reason": "not measurable without the Android app (not built); laptop CPU timings only",
        "rms_baseline": {},
        "edge": {},
    }
    for mtype in ("fan", "pump", "slider", "valve", "gearbox"):
        if (MODELS / mtype / "model.tflite").exists():
            rec["rms_baseline"][mtype] = rms_baseline(mtype)
            rec["edge"][mtype] = edge_cost(mtype)
            print(mtype, rec["edge"][mtype])
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"listen_{rec['run_id']}.json"
    out.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
