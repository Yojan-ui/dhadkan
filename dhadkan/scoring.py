"""Reconstruction-error scoring, thresholds and the DCASE metrics.

Pure numpy so it is easy to test and identical on the phone side.
"""
import numpy as np
from sklearn.metrics import roc_auc_score

from .config import PAUC_MAX_FPR, THRESHOLD_QUANTILE


def window_errors(x, x_hat):
    """Mean squared reconstruction error per window. x: (n, mels, frames[,1])."""
    x = x.reshape(len(x), -1)
    x_hat = x_hat.reshape(len(x_hat), -1)
    return ((x - x_hat) ** 2).mean(axis=1)


def clip_score(win_errors):
    """A clip (one reading) is scored by the mean of its window errors."""
    return float(np.mean(win_errors))


def threshold(normal_scores, q=THRESHOLD_QUANTILE):
    """Alarm level from held-out normal scores of one machine."""
    return float(np.quantile(np.asarray(normal_scores), q))


def auc(y_true, scores):
    return float(roc_auc_score(y_true, scores))


def pauc(y_true, scores, max_fpr=PAUC_MAX_FPR):
    """Partial AUC over FPR in [0, max_fpr], normalised to [0, 1] by max_fpr.

    This is the DCASE 2020-2021 definition (not sklearn's McClish-corrected one).
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
        t_end = np.interp(max_fpr, fpr[stop - 1:stop + 1], tpr[stop - 1:stop + 1])
        x, y_ = np.append(x, max_fpr), np.append(y_, t_end)
    return float(np.trapezoid(y_, x) / max_fpr)


def risk(score, thr):
    """Score relative to the machine's own alarm level. 1.0 = at the alarm."""
    return float(score / thr) if thr > 0 else float("inf")
