"""Granite (local Ollama) vs the rules on labelled bearing faults (needs Ollama with the model pulled; costs ₹0).

    python -m scripts.eval_understand [--limit N]   -> docs/runs/understand_<UTC>.json

Cases: every CWRU recording (data/raw/cwru, 48 = 44 faults + 4 normal), labels from CWRU's own file
list. Truth: a fault recording should be named bearing_wear; a normal one should not be named.
- rules: the CWRU-trained classifier, retrained WITHOUT the case's load (leave-one-load-out, so it never
  scores a recording from a load it trained on), mapped to faults.yaml by server/diagnosis.fault_for.
- llm: server/understand.py on a before|now spectrogram (before = CWRU normal at the same load, now =
  the case), vibration resampled to 16 kHz, no trend (CWRU has no time series).
Reports accuracy, how often Granite asked for a mechanic, and tokens / INR per call. Nothing is written
without a local LLM.
"""

import argparse
import json
import time
from typing import Any

import numpy as np
from scipy import signal

from edge import faulttype
from edge.config import RUNS
from server import diagnosis, provider, understand
from server.notify import media


def to16k(x: np.ndarray) -> np.ndarray:
    y = signal.resample_poly(x, 4, 3)[: 10 * 16_000]  # 12 kHz -> 16 kHz, at most 10 s
    return (y / (np.max(np.abs(y)) + 1e-9) * 0.5).astype(np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the first N cases (cost control)")
    args = ap.parse_args()
    if not provider.available():
        raise SystemExit(
            "needs Ollama running with " + provider.chat_model() + "; nothing run, nothing written"
        )
    rows = faulttype.load_cwru()
    X, y, meta = faulttype.dataset(rows)
    loads = np.array([m["load"] for m in meta])
    files = np.array([m["file"] for m in meta])
    normal_at = {r["load"]: r["x"] for r in rows if r["label"] == "normal"}
    cases = rows[: args.limit] if args.limit else rows
    out: list[dict[str, Any]] = []
    for r in cases:
        clf = faulttype.model().fit(X[loads != r["load"]], y[loads != r["load"]])
        sel = files == r["file"]
        pred = faulttype.CLASSES[int(np.argmax(clf.predict_proba(X[sel]).mean(axis=0)))]
        rules_fault = diagnosis.fault_for(classifier=pred) if pred != "normal" else None
        pair = media.spectrogram_png(to16k(normal_at[r["load"]]), to16k(r["x"]))
        req = understand.build_request(
            pair, [], {"name": "test-rig motor", "type": "motor"}, {"name": "CWRU"}, "en"
        )
        got = understand.diagnose(req, machine_id=f"cwru:{r['file']}")
        truth = "bearing_wear" if r["label"] != "normal" else None
        llm_fault = (
            got.get("fault") if got.get("source") == provider.NAME and got.get("fault") != "unknown" else None
        )
        out.append({"file": r["file"], "label": r["label"], "load": r["load"], "truth": truth,
                    "rules": rules_fault, "rules_ok": rules_fault == truth, "llm_source": got.get("source"),
                    "llm": llm_fault, "llm_ok": llm_fault == truth if got.get("source") == provider.NAME else None,
                    "mechanic_check_needed": got.get("mechanic_check_needed"), "confidence": got.get("confidence"),
                    "cost": got.get("cost")})  # fmt: skip
        print(out[-1]["file"], out[-1]["truth"], "rules", rules_fault, "llm", llm_fault)
    done = [o for o in out if o["llm_ok"] is not None]
    rec = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "model": provider.chat_model(), "cases": len(out),
           "rules_accuracy": float(np.mean([o["rules_ok"] for o in out])),
           "llm_answered": len(done), "llm_accuracy": float(np.mean([o["llm_ok"] for o in done])) if done else None,
           "llm_mechanic_check_share": float(np.mean([bool(o["mechanic_check_needed"]) for o in done])) if done else None,
           "inr_per_call_median": float(np.median([o["cost"]["inr"] for o in out if o["cost"]])) if any(o["cost"] for o in out) else None,
           "rows": out}  # fmt: skip
    RUNS.mkdir(parents=True, exist_ok=True)
    path = RUNS / f"understand_{rec['run_id']}.json"
    path.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    print(path, {k: rec[k] for k in ("cases", "rules_accuracy", "llm_answered", "llm_accuracy")})


if __name__ == "__main__":
    main()
