"""What runs on the phone: the .tflite autoencoder plus thresholds per machine.

The dashboard scores every replayed clip through this exact file, so the
numbers on screen come from the same model that would ship to Android.
"""

import json
from pathlib import Path
from typing import Any

import numpy as np
from ai_edge_litert.interpreter import Interpreter
from numpy.typing import ArrayLike

from . import embed, features, scoring
from .config import MATCH_QUANTILE, MODELS


class EdgeModel:
    def __init__(self, mtype: str, models_dir: Path = MODELS) -> None:
        d = models_dir / mtype
        self.mtype = mtype
        self.meta = json.loads((d / "meta.json").read_text())
        self.mu = np.array(self.meta["mel_mean"], dtype=np.float32)[:, None]
        self.sd = np.array(self.meta["mel_std"], dtype=np.float32)[:, None]
        self.interp = Interpreter(model_path=str(d / "model.tflite"), num_threads=4)
        self._n = None

    def _run(self, x: np.ndarray) -> np.ndarray:
        if self._n != len(x):
            inp = self.interp.get_input_details()[0]["index"]
            self.interp.resize_tensor_input(inp, x.shape)
            self.interp.allocate_tensors()
            self._n = len(x)
        self.interp.set_tensor(self.interp.get_input_details()[0]["index"], x)
        self.interp.invoke()
        return self.interp.get_tensor(self.interp.get_output_details()[0]["index"])

    def normalise(self, wins: np.ndarray) -> np.ndarray:
        return ((wins - self.mu) / self.sd)[..., None].astype(np.float32)

    def unit(self, unit: str) -> dict[str, Any]:
        return self.meta["units"][unit]

    def score_audio(self, audio: np.ndarray, unit: str) -> dict[str, Any]:
        """One reading. Returns score, risk vs this machine's alarm, embedding."""
        u = self.unit(unit)
        x = self.normalise(features.windows(audio))
        x_hat = self._run(x)
        errs = scoring.window_errors(x, x_hat)
        score = scoring.clip_score(errs)
        prof = embed.band_profile(x, x_hat)
        p_mu, p_sd = np.array(u["profile_mean"]), np.array(u["profile_std"])
        win_emb = embed.embed(prof, p_mu, p_sd)
        clip_emb = embed.fingerprint(win_emb)
        return {
            "score": score,
            "threshold": u["threshold"],
            "risk": scoring.risk(score, u["threshold"]),
            "window_errors": errs.tolist(),
            "embedding": clip_emb.tolist(),
            # per mel band: how far the residual sits from this machine's normal, in std units
            "band_z": ((prof - p_mu) / p_sd).mean(axis=0).tolist(),
        }

    def match_level(self, unit: str, fingerprint: ArrayLike) -> float:
        """Similarity this machine reached with `fingerprint` while healthy.

        A registry match is only a flag when the current reading resembles the
        published fault more than any of the machine's own held-out normal clips.
        """
        normals = np.array(self.unit(unit)["val_embeddings"])
        return float(np.quantile(embed.cosine(fingerprint, normals), MATCH_QUANTILE))
