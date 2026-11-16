"""Train one autoencoder per machine type on NORMAL windows, then evaluate.

    python -m edge.train [--epochs 40] [--types pump fan gearbox]

Writes models/<type>/{model.keras, model.tflite, meta.json} and one evaluation
record runs/eval_<UTC timestamp>.json. Every number the dashboard shows is read
from the newest eval record; nothing is typed in by hand.
"""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import tensorflow as tf

from . import data, embed, features, model, scoring
from .config import (
    FRAMES,
    HOP,
    HOP_WIN_S,
    MACHINE_TYPES,
    MATCH_QUANTILE,
    MODELS,
    N_FFT,
    N_MELS,
    PAUC_MAX_FPR,
    ROOT,
    RUNS,
    SR,
    THRESHOLD_QUANTILE,
)

ONSET_MIX = (0.25, 0.5, 0.75, 1.0)


def _git_rev() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def _code_hash() -> str:
    h = hashlib.sha256()
    for f in sorted((ROOT / "edge").glob("*.py")):
        h.update(f.read_bytes())
    return h.hexdigest()


def _data_hash(clips: list[data.Clip]) -> str:
    h = hashlib.sha256()
    for c in sorted(clips, key=lambda c: str(c.path)):
        h.update(f"{c.path.relative_to(ROOT)}:{c.path.stat().st_size}:{c.split}\n".encode())
    return h.hexdigest()


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    y = [r["label"] for r in rows]
    s = [r["score"] for r in rows]
    n_norm, n_anom = y.count(0), y.count(1)
    if not n_norm or not n_anom:
        return None
    return {"auc": scoring.auc(y, s), "pauc": scoring.pauc(y, s), "pauc_raw": scoring.pauc_raw(y, s),
            "n_normal": n_norm, "n_anomalous": n_anom}  # fmt: skip


def _alarm_rates(rows: list[dict[str, Any]], thr_by_unit: dict[str, float]) -> dict[str, float | None]:
    """Hit and false-alarm rate when each machine uses its own alarm level."""
    fa = [r["score"] > thr_by_unit[r["unit"]] for r in rows if r["label"] == 0]
    hit = [r["score"] > thr_by_unit[r["unit"]] for r in rows if r["label"] == 1]
    return {
        "false_alarm_rate": float(np.mean(fa)) if fa else None,
        "detection_rate": float(np.mean(hit)) if hit else None,
    }


class TypeModel:
    """Keras model + normalisation for one machine type, used during training."""

    def __init__(self, ae: tf.keras.Model, mu: np.ndarray, sd: np.ndarray) -> None:
        self.ae, self.mu, self.sd = ae, mu, sd

    def norm(self, wins: np.ndarray) -> np.ndarray:
        return ((wins - self.mu) / self.sd)[..., None].astype(np.float32)

    def run(self, audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        x = self.norm(features.windows(audio))
        return x, self.ae.predict(x, verbose=0, batch_size=256)


def train_type(mtype: str, epochs: int) -> dict[str, Any]:
    clips = data.clips_for(mtype)
    if not clips:
        raise SystemExit(f"no clips for {mtype}; run python -m data.download first")
    print(f"\n== {mtype}: {len(clips)} clips", flush=True)
    audio = {c.path: features.load(c.path) for c in clips}

    train = np.concatenate([features.windows(audio[c.path]) for c in clips if c.split == "train"])
    val = np.concatenate([features.windows(audio[c.path]) for c in clips if c.split == "val"])
    mu = train.mean(axis=(0, 2))[:, None]
    sd = train.std(axis=(0, 2))[:, None] + 1e-6

    tf.keras.utils.set_random_seed(0)
    ae = model.build()
    tm = TypeModel(ae, mu, sd)
    hist = ae.fit(
        tm.norm(train),
        tm.norm(train),
        validation_data=(tm.norm(val), tm.norm(val)),
        epochs=epochs,
        batch_size=64,
        verbose=2,
        callbacks=[tf.keras.callbacks.EarlyStopping(patience=5, restore_best_weights=True)],
    )

    # Score every clip once.
    rows, prof = [], {}
    for c in clips:
        x, x_hat = tm.run(audio[c.path])
        errs = scoring.window_errors(x, x_hat)
        prof[c.path] = embed.band_profile(x, x_hat)
        rows.append(
            {
                "file": c.path.name,
                "unit": c.unit,
                "split": c.split,
                "label": c.label,
                "condition": c.condition,
                "score": scoring.clip_score(errs),
                "window_auc_input": errs.tolist(),
            }
        )

    units = sorted({c.unit for c in clips})
    by_path = {c.path: r for c, r in zip(clips, rows, strict=False)}
    meta_units, thr = {}, {}
    for u in units:
        val_clips = [c for c in clips if c.unit == u and c.split == "val"]
        thr[u] = scoring.threshold([by_path[c.path]["score"] for c in val_clips])
        p_mu, p_sd = embed.normal_stats(np.concatenate([prof[c.path] for c in val_clips]))
        val_emb = [embed.fingerprint(embed.embed(prof[c.path], p_mu, p_sd)) for c in val_clips]
        meta_units[u] = {
            "threshold": thr[u],
            "profile_mean": p_mu.tolist(),
            "profile_std": p_sd.tolist(),
            "val_embeddings": np.array(val_emb).tolist(),
        }

    # Headline: in-domain test set. Failure panel: noisy shed / changed speed.
    main_cond = "source" if mtype == "gearbox" else "6dB"
    test = [r for r in rows if r["split"] == "test"]
    report = {"headline_condition": main_cond, "per_unit": {}, "conditions": {}}
    for u in units:
        report["per_unit"][u] = _metrics([r for r in test if r["unit"] == u and r["condition"] == main_cond])
    for cond in sorted({r["condition"] for r in test}):
        sub = [r for r in test if r["condition"] == cond]
        m = _metrics(sub)
        if m:
            m.update(_alarm_rates(sub, thr))
            # window-level AUC, so the 1-second view is not hidden behind clip averaging
            yw = [r["label"] for r in sub for _ in r["window_auc_input"]]
            sw = [e for r in sub for e in r["window_auc_input"]]
            m["window_auc"] = scoring.auc(yw, sw)
        report["conditions"][cond] = m
    report["overall"] = report["conditions"][main_cond]

    # Registry check: does a published fingerprint flag the same fault earlier elsewhere?
    report["registry_transfer"] = registry_transfer(clips, audio, tm, meta_units, thr)

    # Export for the phone and check the .tflite agrees with Keras.
    d = MODELS / mtype
    d.mkdir(parents=True, exist_ok=True)
    ae.save(d / "model.keras")
    tfl = model.to_tflite(ae)
    (d / "model.tflite").write_bytes(tfl)
    meta = {"mtype": mtype, "mel_mean": mu[:, 0].tolist(), "mel_std": sd[:, 0].tolist(), "units": meta_units}
    (d / "meta.json").write_text(json.dumps(meta))
    report["tflite"] = tflite_check(d, clips, audio, rows)
    report["training"] = {
        "epochs_run": len(hist.history["loss"]),
        "final_loss": float(hist.history["loss"][-1]),
        "best_val_loss": float(min(hist.history["val_loss"])),
        "train_windows": len(train),
        "params": int(ae.count_params()),
    }
    report["data_sha256"] = _data_hash(clips)
    report["thresholds"] = thr
    for r in rows:
        r.pop("window_auc_input")
    report["clip_scores"] = rows
    return report


def registry_transfer(
    clips: list[data.Clip],
    audio: dict[Path, np.ndarray],
    tm: "TypeModel",
    meta_units: dict[str, dict[str, Any]],
    thr: dict[str, float],
) -> list[dict[str, Any]]:
    """A confirmed fault on machine A is published; machine B is re-scored against it.

    Two cases per pair:
      same model:  B is another unit of the same recorded machine (held-out clips,
                   disjoint from the 4 clips used to build the fingerprint)
      other unit:  B is a different recorded unit of the same machine type
    B's fault onset is SYNTHETIC: B's abnormal clip mixed into its normal clip at
    strength `mix` (power-preserving, see features.onset_mix). For each strength
    we report how often B is flagged by its own alarm level, by a registry match,
    or by either, plus the registry false-flag rate on B's untouched normal clips.
    """

    def test(u: str, label: int) -> list[data.Clip]:
        return [
            c
            for c in clips
            if c.unit == u and c.split == "test" and c.label == label and c.condition in ("6dB", "source")
        ]

    def clip_emb(x_audio: np.ndarray, u: str) -> tuple[np.ndarray, float]:
        x, x_hat = tm.run(x_audio)
        mu_, sd_ = np.array(meta_units[u]["profile_mean"]), np.array(meta_units[u]["profile_std"])
        e = embed.fingerprint(embed.embed(embed.band_profile(x, x_hat), mu_, sd_))
        return e, scoring.clip_score(scoring.window_errors(x, x_hat))

    units = sorted({c.unit for c in clips})
    out = []
    for a in units:
        a_anom = test(a, 1)
        if len(a_anom) < 8:
            continue
        # The owner confirmed the first 4 anomalous readings of machine A.
        fp = embed.fingerprint([clip_emb(audio[c.path], a)[0] for c in a_anom[:4]])
        for b in units:
            b_norm = test(b, 0)
            b_anom = a_anom[4:] if a == b else test(b, 1)
            level = float(
                np.quantile(embed.cosine(fp, np.array(meta_units[b]["val_embeddings"])), MATCH_QUANTILE)
            )
            res = {
                "from": a,
                "to": b,
                "case": "same model" if a == b else "other unit",
                "match_level": level,
                "by_mix": {},
            }
            res["registry_false_flag_rate"] = float(
                np.mean([embed.cosine(clip_emb(audio[c.path], b)[0], fp)[0] > level for c in b_norm])
            )
            n = min(len(b_norm), len(b_anom))
            for mix in ONSET_MIX:
                by_thr, by_reg = [], []
                for cn, ca in zip(b_norm[:n], b_anom[:n], strict=False):
                    e, s = clip_emb(features.onset_mix(audio[cn.path], audio[ca.path], mix), b)
                    by_thr.append(s > thr[b])
                    by_reg.append(embed.cosine(e, fp)[0] > level)
                res["by_mix"][str(mix)] = {
                    "n": n,
                    "alarm": float(np.mean(by_thr)),
                    "registry": float(np.mean(by_reg)),
                    "either": float(np.mean(np.logical_or(by_thr, by_reg))),
                }
            out.append(res)
    return out


def tflite_check(
    d: Path, clips: list[data.Clip], audio: dict[Path, np.ndarray], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Re-score a few test clips through the exported .tflite file."""
    from .runtime import EdgeModel

    em = EdgeModel(d.name, d.parent)
    sample = [(c, r) for c, r in zip(clips, rows, strict=False) if c.split == "test"][::7][:12]
    diffs = [abs(em.score_audio(audio[c.path], c.unit)["score"] - r["score"]) / r["score"] for c, r in sample]
    return {
        "size_bytes": (d / "model.tflite").stat().st_size,
        "clips_checked": len(sample),
        "max_rel_score_diff_vs_keras": float(max(diffs)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--types", nargs="+", default=list(MACHINE_TYPES))
    args = ap.parse_args()

    t0 = time.time()
    record = {
        "run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "git_rev": _git_rev(),
        "code_sha256": _code_hash(),
        "frontend": {
            "sr": SR,
            "n_fft": N_FFT,
            "hop": HOP,
            "n_mels": N_MELS,
            "frames": FRAMES,
            "window_hop_s": HOP_WIN_S,
            "log": "log10(mel_power + 1e-10)",
        },
        "assumptions": {
            "threshold_quantile": THRESHOLD_QUANTILE,
            "match_quantile": MATCH_QUANTILE,
            "pauc_max_fpr": PAUC_MAX_FPR,
            "pauc_definition": "sklearn roc_auc_score(max_fpr) = McClish, as the DCASE 2020 baseline code",
            "onset_mix": list(ONSET_MIX),
        },
        "types": {},
    }
    for mtype in args.types:
        record["types"][mtype] = train_type(mtype, args.epochs)
    record["seconds"] = round(time.time() - t0, 1)

    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"eval_{record['run_id']}.json"
    out.write_text(json.dumps(record, indent=1))
    (MODELS / "frontend.json").write_text(json.dumps(record["frontend"], indent=1))

    print(f"\nwrote {out}")
    for mtype, rep in record["types"].items():
        o = rep["overall"]
        print(
            f"  {mtype:8s} AUC {o['auc']:.3f}  pAUC {o['pauc']:.3f}  "
            f"(n={o['n_normal']}+{o['n_anomalous']}, {rep['headline_condition']})"
        )


if __name__ == "__main__":
    main()
