import numpy as np
import pytest
from sklearn.metrics import roc_curve

from edge import scoring


def reference_pauc(y, s, max_fpr):
    fpr, tpr, _ = roc_curve(y, s)
    stop = np.searchsorted(fpr, max_fpr, side="right")
    x = np.append(fpr[:stop], max_fpr)
    t = np.append(tpr[:stop], np.interp(max_fpr, fpr, tpr))
    return np.trapezoid(t, x) / max_fpr


def test_window_errors_is_mse_per_window():
    x = np.zeros((3, 4, 2))
    x_hat = np.zeros((3, 4, 2))
    x_hat[1] = 1.0
    x_hat[2, 0, 0] = 2.0
    assert scoring.window_errors(x, x_hat).tolist() == [0.0, 1.0, 0.5]


def test_window_errors_accepts_channel_axis():
    x = np.ones((2, 4, 2, 1))
    assert scoring.window_errors(x, x * 0).tolist() == [1.0, 1.0]


def test_clip_score_is_mean():
    assert scoring.clip_score([1.0, 2.0, 6.0]) == 3.0


def test_threshold_is_quantile_of_normals():
    normals = np.arange(1, 101, dtype=float)
    assert scoring.threshold(normals, 0.95) == pytest.approx(95.05)


def test_perfect_separation():
    y = [0, 0, 0, 1, 1]
    s = [0.1, 0.2, 0.3, 0.8, 0.9]
    assert scoring.auc(y, s) == 1.0
    assert scoring.pauc(y, s) == pytest.approx(1.0)
    assert scoring.pauc_raw(y, s) == pytest.approx(1.0)


def test_inverted_scores():
    y = [0, 0, 1, 1]
    s = [0.9, 0.8, 0.2, 0.1]
    assert scoring.auc(y, s) == 0.0
    assert scoring.pauc_raw(y, s) == pytest.approx(0.0)
    assert scoring.pauc(y, s) < 0.5  # McClish: 0.5 is the diagonal; a fully inverted curve lands below it


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("max_fpr", [0.05, 0.1, 0.3])
def test_pauc_matches_reference(seed, max_fpr):
    rng = np.random.default_rng(seed)
    y = np.r_[np.zeros(60), np.ones(40)]
    s = np.r_[rng.normal(0, 1, 60), rng.normal(0.8, 1, 40)]
    assert scoring.pauc_raw(y, s, max_fpr) == pytest.approx(reference_pauc(y, s, max_fpr))


def test_pauc_with_ties():
    y = [0, 0, 0, 0, 1, 1, 1, 1]
    s = [1, 1, 2, 2, 2, 3, 3, 3]
    assert scoring.pauc_raw(y, s, 0.5) == pytest.approx(reference_pauc(y, s, 0.5))


def test_pauc_needs_both_classes():
    for fn in (scoring.pauc, scoring.pauc_raw):
        with pytest.raises(ValueError):
            fn([0, 0], [0.1, 0.2])


def test_risk_relative_to_own_alarm():
    assert scoring.risk(1.5, 1.0) == 1.5
    assert scoring.risk(0.5, 2.0) == 0.25


@pytest.mark.parametrize("seed", range(5))
def test_pauc_is_the_dcase_baseline_definition(seed):
    """pauc = sklearn's max_fpr pAUC (as the DCASE 2020 baseline code) = McClish transform of the raw area."""
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(seed)
    y = np.r_[np.zeros(60), np.ones(40)]
    s = np.r_[rng.normal(0, 1, 60), rng.normal(0.8, 1, 40)]
    f = 0.1
    assert scoring.pauc(y, s, f) == pytest.approx(roc_auc_score(y, s, max_fpr=f))
    raw_area = scoring.pauc_raw(y, s, f) * f
    mcclish = 0.5 * (1 + (raw_area - f * f / 2) / (f - f * f / 2))
    assert scoring.pauc(y, s, f) == pytest.approx(mcclish)
