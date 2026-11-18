"""What the per-machine pipeline costs per reading on THIS computer (not a phone, not an ESP32).

    python -m edge.bench   -> docs/runs/bench_<UTC>.json

Per 10 s reading at 16 kHz: quality check, summary (log-mel, MFCC, band statistics), and the risk
against a fitted per-machine model (the chosen detector set). Also the time to fit a machine's
baseline, peak Python memory (tracemalloc) for one reading, and what a machine's stored history costs
on disk per reading. A phone or ESP32 has not been measured; the record says so.
"""

import json
import platform
import sqlite3
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import numpy as np

from . import summary
from .baseline import BaselineStore
from .config import RUNS, SR
from .detectors import WIDTH, MachineModel, chosen
from .train import _code_hash, _git_rev


def main() -> None:
    rng = np.random.default_rng(0)
    audio = (0.05 * rng.standard_normal(SR * 10)).astype(np.float32)
    pick = chosen()
    rows = np.vstack([np.r_[summary.summarise(audio * (1 + 0.01 * i)), np.nan] for i in range(40)])

    def timed(fn: Callable[[], object], n: int) -> float:
        fn()
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - t0) / n * 1000

    model = MachineModel(pick["names"], pick["fusion"]).fit(rows)
    all_model = MachineModel(["rms", "spectral", "transient", "mfcc", "iforest"]).fit(rows)
    res = {
        "quality_ms": timed(lambda: summary.quality(audio), 20),
        "summary_ms": timed(lambda: summary.summarise(audio), 20),
        "risk_ms_chosen": timed(lambda: model.risk(rows[:1]), 200),
        "risk_ms_all_classical": timed(lambda: all_model.risk(rows[:1]), 50),
        "fit_baseline_ms_chosen_40_readings": timed(
            lambda: MachineModel(pick["names"], pick["fusion"]).fit(rows), 10
        ),
    }
    tracemalloc.start()
    summary.quality(audio)
    model.risk(np.r_[summary.summarise(audio), np.nan][None, :])
    res["peak_python_mib_one_reading"] = round(tracemalloc.get_traced_memory()[1] / 2**20, 2)
    tracemalloc.stop()
    with tempfile.TemporaryDirectory() as d:
        st = BaselineStore(Path(d) / "b.db", detectors=pick["names"])
        st.register("m", "x", learning_days=0, now=0)
        for i, r in enumerate(rows):
            st.observe("m", r, {"ok": True, "reasons": []}, ts=float(i), source="simulated")
        sqlite3.connect(Path(d) / "b.db").execute("VACUUM").close()
        res["disk_bytes_per_reading"] = int((Path(d) / "b.db").stat().st_size / len(rows))
    rec = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "git_rev": _git_rev(), "code_sha256": _code_hash(),
           "machine": {"platform": platform.platform(), "processor": platform.processor() or platform.machine(),
                       "python": platform.python_version()},
           "detectors": pick, "summary_width": WIDTH, "reading_seconds": 10,
           "results": {k: round(v, 3) if isinstance(v, float) else v for k, v in res.items()},
           "not_measured": "phone and ESP32 latency, memory and battery (no Android app; ESP32 firmware untested)"}  # fmt: skip
    out = RUNS / f"bench_{rec['run_id']}.json"
    out.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    print(out)
    print(json.dumps(rec["results"], indent=1))


if __name__ == "__main__":
    main()
