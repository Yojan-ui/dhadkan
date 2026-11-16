"""Name the likely bearing fault once the anomaly model has flagged a machine (vibration sensor needed).

Trained ONLY on CWRU (Case Western Reserve University bearing data, 12 kHz drive-end accelerometer):
normal, inner race, outer race (load zone), ball. Labels come from CWRU's own file listing
(data/raw/cwru/index.json). The CWRU normal baseline is recorded at 48 kHz (we measured: its
shaft-speed peak only matches the logged rpm at 48 kHz) and is resampled to 12 kHz like the rest.

Features are made to transfer between bearings: envelope-spectrum peaks at the bearing's own defect
frequencies (outer race, inner race, ball, 3 harmonics each) as multiples of shaft speed, plus
kurtosis and crest factor. So a model trained on CWRU's SKF 6205 can be applied to another bearing
whose geometry is known (e.g. the IMS rig's Rexnord ZA-2115). It does NOT apply to microphone audio.

    python -m edge.faulttype     -> edge/models/faulttype.joblib + docs/runs/faulttype_<UTC>.json
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import scipy.io as sio
from scipy import signal, stats
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import confusion_matrix

from .config import MODELS, RAW, RUNS

FS = 12_000
WIN_S, HOP_S = 1.0, 0.5
BAND = (1_500, 5_000)  # Hz, the resonance band the envelope is taken from
CLASSES = ("normal", "inner_race", "outer_race", "ball")


@dataclass(frozen=True)
class Bearing:
    """Defect frequencies as multiples of shaft speed."""

    name: str
    bpfo: float
    bpfi: float
    ball: float  # ball / rolling-element defect, 2 x ball spin frequency

    @classmethod
    def from_geometry(cls, name: str, n: int, d: float, pitch: float, contact_deg: float) -> "Bearing":
        r = d / pitch * np.cos(np.radians(contact_deg))
        return cls(name, n / 2 * (1 - r), n / 2 * (1 + r), pitch / d * (1 - r**2))


# CWRU drive end: SKF 6205-2RS JEM, orders from CWRU's bearing-information page.
CWRU_6205 = Bearing("SKF 6205-2RS (CWRU drive end)", bpfo=3.5848, bpfi=5.4152, ball=4.7135)


def windows(x: np.ndarray, fs: int = FS) -> np.ndarray:
    n, hop = int(WIN_S * fs), int(HOP_S * fs)
    starts = range(0, max(1, len(x) - n + 1), hop)
    return np.stack([x[s : s + n] for s in starts if s + n <= len(x)])


_SOS: dict[int, np.ndarray] = {}


def features(win: np.ndarray, fs: int, shaft_hz: float, bearing: Bearing) -> np.ndarray:
    """One window -> 11 features (9 envelope-order peaks, log kurtosis, crest factor)."""
    if fs not in _SOS:
        _SOS[fs] = signal.butter(4, BAND, btype="bandpass", fs=fs, output="sos")
    env = np.abs(signal.hilbert(signal.sosfiltfilt(_SOS[fs], win)))
    env -= env.mean()
    spec = np.abs(np.fft.rfft(env * np.hanning(len(env))))
    freqs = np.fft.rfftfreq(len(env), 1 / fs)
    floor = np.median(spec[(freqs > 10) & (freqs < 600)]) + 1e-12
    out = []
    for order in (bearing.bpfo, bearing.bpfi, bearing.ball):
        for h in (1, 2, 3):
            f0 = h * order * shaft_hz
            band = (freqs > f0 * 0.97) & (freqs < f0 * 1.03)
            out.append(np.log(spec[band].max() / floor) if band.any() else 0.0)
    out.append(np.log(stats.kurtosis(win, fisher=False) + 1e-9))
    out.append(np.max(np.abs(win)) / (np.sqrt(np.mean(win**2)) + 1e-12))
    return np.array(out, dtype=np.float32)


def load_cwru() -> list[dict[str, Any]]:
    """Every CWRU recording as {label, load, size, x (12 kHz), rpm}."""
    rows = []
    for r in json.loads((RAW / "cwru" / "index.json").read_text()):
        m = sio.loadmat(RAW / "cwru" / r["file"])
        keys = [k for k in m if not k.startswith("__")]
        x = next(m[k] for k in keys if k.endswith("_DE_time")).ravel().astype(np.float64)
        rpm = next((float(m[k].ravel()[0]) for k in keys if k.endswith("RPM")), None)
        rpm = (
            rpm or {0: 1797, 1: 1772, 2: 1750, 3: 1730}[r["load_hp"]]
        )  # CWRU's table, when the file lacks it
        if r["fault"] == "normal":  # recorded at 48 kHz
            x = signal.resample_poly(x, 1, 4)
        rows.append(
            {
                "label": r["fault"],
                "load": r["load_hp"],
                "size": r["size_in"],
                "file": r["file"],
                "x": x,
                "rpm": rpm,
            }
        )
    return rows


def dataset(
    rows: list[dict[str, Any]], bearing: Bearing = CWRU_6205
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    X, y, meta = [], [], []
    for r in rows:
        for w in windows(r["x"]):
            X.append(features(w, FS, r["rpm"] / 60, bearing))
            y.append(CLASSES.index(r["label"]))
            meta.append({"load": r["load"], "size": r["size"], "file": r["file"]})
    return np.stack(X), np.array(y), meta


def model() -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=300, min_samples_leaf=2, class_weight="balanced", random_state=0
    )


def _eval(X: np.ndarray, y: np.ndarray, train: np.ndarray, test: np.ndarray) -> dict[str, Any]:
    clf = model().fit(X[train], y[train])
    pred = clf.predict(X[test])
    cm = confusion_matrix(y[test], pred, labels=range(len(CLASSES)))
    per = {c: (float(cm[i, i] / cm[i].sum()) if cm[i].sum() else None) for i, c in enumerate(CLASSES)}
    return {"accuracy": float((pred == y[test]).mean()), "per_class_recall": per, "n_test": int(test.sum()),
            "confusion": cm.tolist()}  # fmt: skip


def evaluate(X: np.ndarray, y: np.ndarray, meta: list[dict[str, Any]]) -> dict[str, Any]:
    loads = np.array([m["load"] for m in meta])
    sizes = np.array([m["size"] if m["size"] is not None else -1 for m in meta])
    by_load = {f"held_out_load_{h}hp": _eval(X, y, loads != h, loads == h) for h in sorted(set(loads))}
    # the hardest case: a fault size never seen in training (0.028" only exists for inner race and ball)
    unseen = sizes == 0.028
    by_size = _eval(X, y, ~unseen & (sizes != -1) | (y == 0), unseen)
    accs = [v["accuracy"] for v in by_load.values()]
    return {"leave_one_load_out": by_load, "mean_accuracy_leave_one_load_out": float(np.mean(accs)),
            "unseen_fault_size_0p028in": by_size}  # fmt: skip


def predict(x: np.ndarray, fs: int, shaft_hz: float, bearing: Bearing, clf: Any = None) -> dict[str, Any]:
    """A vibration recording -> class probabilities (averaged over 1 s windows)."""
    clf = clf or load()
    if fs != FS:
        g = np.gcd(int(fs), FS)
        x = signal.resample_poly(x, FS // g, int(fs) // g)
    F = np.stack([features(w, FS, shaft_hz, bearing) for w in windows(x)])
    p = clf.predict_proba(F).mean(axis=0)
    return {
        "class": CLASSES[int(np.argmax(p))],
        "p": {c: round(float(v), 3) for c, v in zip(CLASSES, p, strict=True)},
    }


def load(path: Path = MODELS / "faulttype.joblib") -> Any:
    return joblib.load(path)


def main() -> None:
    rows = load_cwru()
    X, y, meta = dataset(rows)
    record = {
        "run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "data": "CWRU 12 kHz drive-end: normal (48 kHz, resampled), inner race, outer race @6:00, ball; "
        "sizes 0.007-0.028 in; loads 0-3 HP",
        "bearing": CWRU_6205.__dict__,
        "windows": {c: int((y == i).sum()) for i, c in enumerate(CLASSES)},
        "recordings": len(rows),
        "features": "envelope peaks at BPFO/BPFI/ball x1-3 (band 1.5-5 kHz), log kurtosis, crest factor",
        "eval": evaluate(X, y, meta),
    }
    clf = model().fit(X, y)
    joblib.dump(clf, MODELS / "faulttype.joblib")
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"faulttype_{record['run_id']}.json"
    out.write_text(json.dumps(record, indent=1))
    print(out)
    ev = record["eval"]
    print("leave-one-load-out:", {k: round(v["accuracy"], 3) for k, v in ev["leave_one_load_out"].items()})
    print("unseen fault size 0.028 in:", round(ev["unseen_fault_size_0p028in"]["accuracy"], 3))


if __name__ == "__main__":
    main()
