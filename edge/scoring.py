"""Reconstruction-error scoring, thresholds and the DCASE metrics.

Pure numpy so it is easy to test and identical on the phone side.
"""

import numpy as np
from numpy.typing import ArrayLike
from sklearn.metrics import roc_auc_score

from .config import PAUC_MAX_FPR, THRESHOLD_QUANTILE


def window_errors(x: np.ndarray, x_hat: np.ndarray) -> np.ndarray:
    """Mean squared reconstruction error per window. x: (n, mels, frames[,1])."""
    x = x.reshape(len(x), -1)
    x_hat = x_hat.reshape(len(x_hat), -1)
    return ((x - x_hat) ** 2).mean(axis=1)


def clip_score(win_errors: ArrayLike) -> float:
    """A clip (one reading) is scored by the mean of its window errors."""
    return float(np.mean(win_errors))


def threshold(normal_scores: ArrayLike, q: float = THRESHOLD_QUANTILE) -> float:
    """Alarm level from held-out normal scores of one machine."""
    return float(np.quantile(np.asarray(normal_scores), q))


def auc(y_true: ArrayLike, scores: ArrayLike) -> float:
    return float(roc_auc_score(y_true, scores))


def pauc(y_true: ArrayLike, scores: ArrayLike, max_fpr: float = PAUC_MAX_FPR) -> float:
    """pAUC exactly as the official DCASE 2020 Task 2 baseline computes it, so it compares with the
    published baseline numbers: `metrics.roc_auc_score(y_true, y_pred, max_fpr=param["max_fpr"])` in
    01_test.py of github.com/y-kawagu/dcase2020_task2_baseline. That is sklearn's McClish-standardised
    partial AUC: 0.5 = chance, 1.0 = perfect. The raw area is `pauc_raw`.
    """
    y = np.asarray(y_true).astype(bool)
    if y.all() or not y.any():
        raise ValueError("need both normal and anomalous scores")
    return float(roc_auc_score(y, np.asarray(scores, dtype=float), max_fpr=max_fpr))


def pauc_raw(y_true: ArrayLike, scores: ArrayLike, max_fpr: float = PAUC_MAX_FPR) -> float:
    """Raw partial AUC over FPR in [0, max_fpr], divided by max_fpr (chance = max_fpr / 2).

    The formula on the DCASE task page; NOT what the baseline code reports (see `pauc`).
    """
    y = np.asarray(y_true).astype(bool)
    s = np.asarray(scores, dtype=float)
    neg, pos = s[~y], s[y]
    if len(neg) == 0 or len(pos) == 0:
        raise ValueError("need both normal and anomalous scores")
    # ROC points at every distinct threshold, highest first.
    thr = np.unique(s)[::-1]
    fpr = np.array([0.0] + [(neg >= t).mean() for t in thr])
    tpr = np.array([0.0] + [(pos >= t).mean() for t in thr])
    # Clip the curve at max_fpr with linear interpolation.
    stop = np.searchsorted(fpr, max_fpr, side="right")
    x, y_ = fpr[:stop], tpr[:stop]
    if stop < len(fpr) and x[-1] < max_fpr:
        t_end = np.interp(max_fpr, fpr[stop - 1 : stop + 1], tpr[stop - 1 : stop + 1])
        x, y_ = np.append(x, max_fpr), np.append(y_, t_end)
    return float(np.trapezoid(y_, x) / max_fpr)


def risk(score: float, thr: float) -> float:
    """Score relative to the machine's own alarm level. 1.0 = at the alarm."""
    return float(score / thr) if thr > 0 else float("inf")
