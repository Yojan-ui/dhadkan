"""Learning period, drift-safe adaptation and the noisy-shed flag (edge/adapt.py)."""

import numpy as np

from edge import adapt


def test_learning_progress_counts_days():
    assert adapt.learning_progress(3.2, 7) == {"day": 4, "of": 7}
    assert adapt.learning_progress(7.0, 7) is None


def test_no_alarm_while_learning_then_level_from_learned_readings():
    b = adapt.Baseline(learning_days=3)
    rng = np.random.default_rng(0)
    for k in range(30):  # 10 readings a day for 3 days
        out = b.update(float(rng.normal(1.0, 0.05)), days_recorded=k / 10)
        assert out["state"] == "learning" and out["risk"] is None
    out = b.update(1.0, days_recorded=3.0)
    assert out["state"] == "monitoring" and b.threshold0 == np.quantile(b.learned, 0.95)


def test_slow_fault_is_never_learned_as_normal():
    b = adapt.Baseline(learning_days=1)
    for k in range(20):
        b.update(1.0 + 0.01 * (k % 5), days_recorded=k / 20)
    thr0 = b.update(1.0, days_recorded=1.0) and b.threshold0
    for k in range(400):  # a fault that grows by 0.5% per reading
        b.update(thr0 * (0.9 + 0.005 * k), days_recorded=1 + k / 20)
    assert all(s < adapt.FREEZE_AT * b.threshold for s in b.recent)
    assert b.threshold <= thr0 * (1 + adapt.MAX_DRIFT)


def test_genuine_drift_moves_the_level_but_within_bounds():
    b = adapt.Baseline(learning_days=1)
    for k in range(20):
        b.update(1.0, days_recorded=k / 20)
    b.update(1.0, days_recorded=1.0)
    for k in range(300):  # the machine settles quieter
        b.update(0.5, days_recorded=1 + k / 20)
    assert b.threshold == b.threshold0 * (1 - adapt.MAX_DRIFT)


def test_noisy_shed_is_broadband_not_a_band():
    assert adapt.noisy_shed([4.0 + 0.2 * np.sin(i) for i in range(64)])  # everything louder by ~4 dB
    one_band = [0.0] * 64
    one_band[40:44] = [9.0, 10.0, 9.0, 8.0]
    assert not adapt.noisy_shed(one_band)  # a fault in a few bands
    assert not adapt.noisy_shed([0.5] * 64)  # unchanged
