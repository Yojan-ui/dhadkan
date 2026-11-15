"""Explain an alert by sound: which band changed, by how much, in plain words, and a 3 s before/now pair.

The band is the run of mel bands whose reconstruction residual sits furthest
above this machine's own normal (band_z from the phone model). The percentage
is measured on the audio: mean mel power in that band now vs the machine's
earliest normal reading. The words come from server/sound_words.json.
"""

import json
from functools import cache
from typing import Any

import numpy as np

from edge import features
from edge.config import HOP, SR

from .config import SOUND_WORDS

Z_MIN = 2.0  # a band counts as changed above 2 std of its own normal residual
CLIP_S = 3.0


@cache
def words() -> dict[str, Any]:
    return json.loads(SOUND_WORDS.read_text(encoding="utf-8"))


def changed_band(band_z: list[float]) -> tuple[int, int] | None:
    """(first, last) mel band of the contiguous run with the largest summed z above Z_MIN."""
    best, run, best_sum, s = None, None, 0.0, 0.0
    for i, z in enumerate([*band_z, -np.inf]):
        if z > Z_MIN:
            run = (run[0], i) if run else (i, i)
            s += z
            continue
        if run and s > best_sum:
            best, best_sum = run, s
        run, s = None, 0.0
    return best


def _band_power(audio: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """Linear mel power in bands lo..hi, per frame."""
    return (10.0 ** features.log_mel(audio)[lo : hi + 1]).sum(axis=0)


def pitch_word(hz: float) -> str:
    return next(p["word"] for p in words()["pitch"] if hz < p["below_hz"])


def explain(now: np.ndarray, before: np.ndarray, band_z: list[float], mel_hz: list[float]) -> dict[str, Any]:
    band = changed_band(band_z)
    if band is None:
        return {"status": "no_clear_band"}
    lo, hi = band
    p_now, p_before = _band_power(now, lo, hi), _band_power(before, lo, hi)
    change = float(p_now.mean() / p_before.mean() - 1.0)
    centre = float(np.sqrt(mel_hz[lo] * mel_hz[hi]))
    word = pitch_word(centre)
    louder = change > 0
    hint = words()["louder"][word] if louder else words()["quieter"]
    # the 3 s of "now" where the band is strongest; the same stretch of "before"
    n = int(CLIP_S * SR / HOP)
    sums = np.convolve(p_now, np.ones(n), "valid") if len(p_now) > n else np.array([p_now.sum()])
    start_s = float(int(np.argmax(sums)) * HOP / SR)
    return {
        "status": "ok",
        "lo_band": lo,
        "hi_band": hi,
        "lo_hz": round(mel_hz[lo]),
        "hi_hz": round(mel_hz[hi]),
        "max_z": round(float(max(band_z[lo : hi + 1])), 1),
        "change_pct": round(change * 100),
        "word": word,
        "hint": hint,
        "start_s": round(start_s, 2),
        "clip_s": CLIP_S,
    }


def cut(audio: np.ndarray, start_s: float, clip_s: float = CLIP_S) -> np.ndarray:
    a = int(start_s * SR)
    return audio[a : a + int(clip_s * SR)]
