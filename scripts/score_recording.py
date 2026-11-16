"""Score real field recordings of one machine (a phone near the machine, any length; split into 10 s readings).

    python -m scripts.score_recording --type gearbox --baseline loom4_mon.m4a loom4_tue.m4a --new loom4_fri.m4a

--baseline: recordings made while the machine was known to be fine (the learning period). They set this
machine's own alarm level (95th percentile of their readings, as in edge/adapt.py) and its loudness
normal. --new: the recordings to check. --type picks the trained listening model (pump, fan, valve,
slider or gearbox) closest to the machine; a field machine is not one of the training units, so only
its own baseline makes the scores meaningful.

Writes runs/field/<UTC>.json (git-ignored: field audio and results stay on this computer; only numbers
are written, never audio) and prints, per new reading: autoencoder risk (1.0 = alarm), loudness z (the
RMS baseline from edge/listen_eval.py) and the noisy-shed flag. Record what you heard and what the
mechanic found in docs/FIELD_NOTES.md.
"""

import argparse
import json
import time
from pathlib import Path
from typing import Any

import librosa
import numpy as np

from edge import adapt, features
from edge.config import MODELS, ROOT, SR
from edge.runtime import EdgeModel

OUT = ROOT / "runs" / "field"
READING_S = 10


def readings(path: Path) -> list[np.ndarray]:
    audio, _ = librosa.load(path, sr=SR, mono=True)
    n = READING_S * SR
    return [audio[i : i + n] for i in range(0, len(audio) - n + 1, n)]


def score(model: EdgeModel, audio: np.ndarray) -> float:
    unit = next(iter(model.meta["units"]))  # any unit: we only use the raw score, not its threshold
    return float(model.score_audio(audio, unit)["score"])


def band_db(audio: np.ndarray) -> np.ndarray:
    return 10 * features.log_mel(audio).mean(axis=1)  # log10 power -> dB per mel band


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--type",
        required=True,
        choices=sorted(p.name for p in MODELS.iterdir() if (p / "model.tflite").exists()),
    )
    ap.add_argument("--baseline", nargs="+", type=Path, required=True)
    ap.add_argument("--new", nargs="+", type=Path, required=True)
    args = ap.parse_args()
    model = EdgeModel(args.type)
    base = [r for p in args.baseline for r in readings(p)]
    if len(base) < 5:
        raise SystemExit(f"need at least 5 baseline readings of {READING_S} s, got {len(base)}")
    b_scores = [score(model, r) for r in base]
    thr = float(np.quantile(b_scores, adapt.ALARM_QUANTILE))
    b_rms = np.log10([np.sqrt(np.mean(r**2)) + 1e-12 for r in base])
    med, mad = float(np.median(b_rms)), float(np.median(np.abs(b_rms - np.median(b_rms)))) + 1e-9
    b_band = np.mean([band_db(r) for r in base], axis=0)
    rows: list[dict[str, Any]] = []
    for p in args.new:
        for k, r in enumerate(readings(p)):
            rows.append({"file": p.name, "reading": k, "risk": round(score(model, r) / thr, 3),
                         "loudness_z": round(float((np.log10(np.sqrt(np.mean(r**2)) + 1e-12) - med) / mad), 2),
                         "noisy_shed": adapt.noisy_shed(band_db(r) - b_band)})  # fmt: skip
            print(rows[-1])
    rec = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "model": args.type,
           "baseline_files": [p.name for p in args.baseline], "baseline_readings": len(base),
           "alarm_level_score": thr, "rows": rows,
           "note": "field recordings; numbers only, no audio stored here"}  # fmt: skip
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{rec['run_id']}.json"
    out.write_text(json.dumps(rec, indent=1))
    print(out)


if __name__ == "__main__":
    main()
