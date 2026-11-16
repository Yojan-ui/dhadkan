"""Health engine and time-to-threshold: states, urgency, confidence, and never a failure date."""

from datetime import datetime

import numpy as np

from edge import rul
from server import health

NOW = datetime(2026, 11, 16, 10, 0)


def rows(risks, **kw):
    return [{"risk": r, "running": 1, "excluded": None, "after_gap": 0, **kw} for r in risks]


def test_learning_and_off_states():
    assert health.assess("m", rows([None]), now=NOW).status == "learning"
    off = [{"risk": None, "running": 0, "excluded": "not_running", "after_gap": 0}]
    assert health.assess("m", off, now=NOW).status == "not_running"
    assert health.assess("m", [], now=NOW).status == "no_signal"


def test_one_crossing_means_measure_again_not_alarm():
    h = health.assess("m", rows([0.3, 0.4, 0.2, 0.5, 1.3]), now=NOW)
    assert h.urgency == "measure_again" and h.severity == "none"


def test_persistent_mild_is_monitor_marked_is_inspect_severe_is_maintenance():
    assert health.assess("m", rows([0.5, 1.2, 1.1, 1.3]), now=NOW).urgency == "monitor"
    assert health.assess("m", rows([0.5, 2.5, 2.2, 2.8]), now=NOW).urgency == "inspect"
    h = health.assess("m", rows([0.5, 5.0, 4.5, 6.0]), now=NOW)
    assert h.urgency == "maintenance" and h.status == "repair_soon"


def test_short_time_to_threshold_with_severe_change_is_urgent_for_the_owner():
    est = {"status": "estimate", "low": 0.5, "mid": 1.0, "high": 3.0, "meaning": rul.MEANING}
    h = health.assess("m", rows([4.5, 5.0, 6.0]), now=NOW, rul=est)
    assert h.urgency == "urgent_owner_attention" and h.next_inspection == "2026-11-16"
    assert any("maintenance threshold" in e for e in h.evidence)
    assert "not a failure date" in " ".join(h.evidence)


def test_noisy_shed_and_broken_readings_lower_confidence_and_ask_for_another_reading():
    h = health.assess("m", rows([0.5, 2.2, 2.4, 2.5]), now=NOW, noisy_shed=True)
    assert h.confidence == "low" and h.urgency == "measure_again"
    bad = rows([0.4]) + [{"risk": None, "running": 0, "excluded": "quality:clipped", "after_gap": 0}] * 3
    h2 = health.assess("m", bad + rows([1.2, 1.3, 1.1]), now=NOW)
    assert h2.confidence == "low"


def test_restart_and_small_baseline_are_named_reasons():
    h = health.assess("m", rows([1.5, 1.6]) + rows([1.7], after_gap=1), now=NOW, baseline_rows=12)
    assert any("restart" in r for r in h.confidence_reasons)
    assert any("12 readings" in r for r in h.confidence_reasons)


def test_explanation_in_plain_words_with_frequencies():
    ex = {"loudness_db": 4.2, "loudness_z": 3.1, "bands": [{"band": 44, "z": 6.0}, {"band": 10, "z": -4.0}]}
    lines = health.explain_text(ex)
    assert lines[0].startswith("Louder than its learned normal by 4.2 dB")
    assert "Hz" in lines[1] and "Weaker" in lines[2]


def test_every_result_says_anomaly_is_not_a_confirmed_fault():
    h = health.assess("m", rows([3.0, 3.0, 3.0]), now=NOW)
    assert "Only an inspection confirms" in h.note


# ---------------------------------------------------------------- time to threshold


def ramp(n=60, days=3.0, start=0.8, end=3.0, seed=0):
    rng = np.random.default_rng(seed)
    t = np.linspace(0, days, n)
    r = np.exp(np.linspace(np.log(start), np.log(end), n)) * np.exp(rng.normal(0, 0.03, n))
    return t, r


def test_rising_trend_gives_a_range_to_the_threshold():
    t, r = ramp()
    e = rul.estimate(t, r, threshold=6.0, window_days=2.0)
    assert e["status"] == "estimate" and e["target"] == "maintenance_threshold"
    assert 0 < e["low"] <= e["mid"] <= e["high"]
    assert e["meaning"] == rul.MEANING


def test_too_few_readings_after_a_gap_is_insufficient_data():
    t, r = ramp()
    t2 = np.concatenate([t, [t[-1] + 2.0, t[-1] + 2.01]])
    r2 = np.concatenate([r, [3.1, 3.2]])
    e = rul.estimate(t2, r2, threshold=6.0, window_days=2.0, gap_days=0.5)
    assert e["status"] == "insufficient_data" and "gap" in e["reason"]


def test_repair_event_starts_a_new_segment_and_unusable_readings_are_dropped():
    t, r = ramp()
    e = rul.estimate(t, r, threshold=6.0, window_days=2.0, events=[t[-3]])
    assert e["status"] == "insufficient_data"
    valid = np.ones(len(t), bool)
    valid[-20:] = False
    e2 = rul.estimate(t, r, threshold=6.0, window_days=2.0, valid=valid)
    assert e2["n_unusable"] == 20


def test_flat_signal_has_no_trend_and_at_threshold_is_zero():
    t = np.linspace(0, 3, 60)
    flat = 0.8 * np.exp(np.random.default_rng(1).normal(0, 0.05, 60))
    assert rul.estimate(t, flat, threshold=6.0)["status"] == "no_trend"
    e = rul.estimate(t, np.full(60, 7.0), threshold=6.0)
    assert e["status"] == "at_threshold" and e["mid"] == 0.0


def test_calibrated_interval_uses_measured_error_ratios():
    t, r = ramp()
    e = rul.estimate(t, r, threshold=6.0, window_days=2.0, ratios_log10=[-0.5, 0.0, 0.5])
    assert e["calibrated"] and e["low"] < e["mid"] < e["high"]
