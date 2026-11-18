"""Days to fault level, rupees at risk, and the route that uses them."""

import json

import numpy as np
import pytest

from server import costs, forecast, route

T = (np.arange(18) / 3).tolist()  # 6 days, 3 readings a day


def series(slope, noise=0.03, seed=1, start=0.75):
    rng = np.random.default_rng(seed)
    return (start + slope * np.asarray(T) + rng.normal(0, noise, len(T))).tolist()


def test_recovers_a_known_trend_inside_its_range():
    est = forecast.estimate(T, series(0.06), level=1.28)
    true = (1.28 - 0.75) / 0.06 - T[-1]
    assert est["status"] == "estimate" and est["low"] <= true <= est["high"]
    assert est["low"] <= est["mid"] <= est["high"]
    assert [b["lo"] <= b["fit"] <= b["hi"] for b in est["band"]].count(True) == len(est["band"])


def test_flat_noise_gives_no_estimate_almost_always():
    hits = [
        forecast.estimate(T, series(0.0, 0.05, seed), 1.28)["status"] == "estimate" for seed in range(300)
    ]
    assert np.mean(hits) < 0.02


def test_no_estimate_where_faults_score_below_the_alarm_level():
    assert forecast.estimate(T, series(0.06), level=0.9)["status"] == "not_separable"


def test_too_few_readings_and_already_at_fault_level():
    assert forecast.estimate(T[:4], series(0.06)[:4], 1.28)["status"] == "too_few"
    est = forecast.estimate(T, [1.4] * len(T), 1.28)
    assert est["status"] == "at_fault_level" and forecast.p_before(est, 0.1) == 1.0


def test_probability_grows_with_time():
    est = forecast.estimate(T, series(0.06), 1.28)
    ps = [forecast.p_before(est, d) for d in (0.5, 2, 4, 8)]
    assert ps == sorted(ps) and ps[0] < 0.5 < ps[-1]
    assert forecast.p_before({"status": "no_trend"}, 30) == 0.0


def test_fault_levels_come_from_the_evaluation_record():
    import json

    ev = json.loads(sorted(forecast.RUNS.glob("eval_*.json"))[-1].read_text(encoding="utf-8"))
    lv = forecast.fault_levels()
    for rep in ev["types"].values():
        for unit, thr in rep["thresholds"].items():
            rows = [
                r
                for r in rep["clip_scores"]
                if r["unit"] == unit and r["condition"] == rep["headline_condition"]
            ]
            bad = sorted(r["score"] / thr for r in rows if r["label"] == 1)
            assert lv[unit]["n"] == len(bad) > 0, unit
            assert lv[unit]["level"] == pytest.approx(float(np.median(bad))), unit


def test_breakdown_cost_is_all_assumptions():
    c = costs.breakdown("B", "fan", "bearing", service_h=0.75)
    a = costs.load()
    per_h = a["output_per_hour_inr"]["B"] + a["workers_idle"]["fan"] * a["wage_per_hour_inr"]
    markup = round(costs.parts_cost("fan", "bearing") * a["emergency_part_markup"])
    assert c["breakdown_cost_inr"] == round(a["unplanned_downtime_hours"]["fan"] * per_h + markup)
    assert c["saved_inr"] == round((a["unplanned_downtime_hours"]["fan"] - 0.75) * per_h + markup)
    unknown = costs.breakdown("B", "fan", None, 0.75)
    assert unknown["part_markup_inr"] == 0 and unknown["parts_known"] is False


def test_route_maximises_value_when_given():
    stops = [
        {"id": "near", "xy_km": [1, 0], "service_h": 3, "risk": 1.5, "value": 100},
        {"id": "far", "xy_km": [30, 0], "service_h": 3, "risk": 1.0, "value": 90000},
    ]
    assert route.plan(stops, [0, 0], hours=7, kmph=20)["order"] == ["far"]
    no_value = [{k: v for k, v in s.items() if k != "value"} for s in stops]
    assert route.plan(no_value, [0, 0], hours=7, kmph=20)["order"] == ["near"]


@pytest.mark.parametrize("hours", [0.5])
def test_route_with_no_time_visits_nobody(hours):
    stops = [{"id": "x", "xy_km": [10, 0], "service_h": 1, "risk": 2, "value": 5}]
    assert route.plan(stops, [0, 0], hours=hours, kmph=20)["order"] == []


def test_calibrated_range_widens_by_measured_ratios():
    """An IMS-style unit: the range is mid x the 10th-90th percentile of the given error ratios."""
    ratios = [-0.5, -0.3, 0.0, 0.2, 0.4, 0.6]
    forecast.register("test/ims", level=50.0, window_days=1.0, log=True, ratios_log10=ratios)
    try:
        hist = [{"tick": k, "risk": 10 ** (0.02 * k)} for k in range(60)]  # log score rising steadily
        est = forecast.from_history(hist, tick_hours=1.0, unit="test/ims")
        assert est["status"] == "estimate" and est["calibrated_on"].startswith("NASA IMS")
        f = 10 ** np.asarray(ratios)
        assert est["low"] == pytest.approx(est["mid"] * np.percentile(f, 10), abs=0.06)
        assert est["high"] == pytest.approx(est["mid"] * np.percentile(f, 90), abs=0.06)
        assert est["raw_low"] is not None and len(est["crossings"]) == len(ratios)
    finally:
        forecast.EXTRA.pop("test/ims", None)


def test_ims_calibration_helpers():
    from edge import ims

    rows = [{"status": "estimate", "mid": 2.0, "remaining": 4.0}, {"status": "no_trend", "remaining": 1.0}]
    assert ims.log_ratios(rows) == [pytest.approx(np.log10(2))]
    out = ims.calibrate([dict(rows[0])], (-0.3, 0.35))[0]
    assert out["cal_low"] == pytest.approx(2 * 10**-0.3, abs=1e-3) and out["cal_inside"] is True


def test_demo_pack_matches_the_real_ims_replay():
    """The committed pack is the real IMS data Demo mode plays, checked against data/raw when present."""
    from edge import ims
    from edge.config import RAW

    pack = np.load(ims.PACK)
    meta = json.loads((ims.MODELS / "ims_replay.json").read_text(encoding="utf-8"))
    idx = pack["snapshot_index"]
    assert idx[0] >= int(len(meta["t_days"]) * ims.BASELINE_FRAC)  # replay starts after the learning window
    assert np.allclose(pack["score"], np.asarray(meta["score"])[idx])
    assert np.allclose(pack["classifier_p"].sum(axis=1), 1.0, atol=1e-3)
    if (RAW / "ims" / f"{meta['test']}_{meta['bearing']}.npy").exists():
        t, x = ims.load(meta["test"], meta["bearing"])
        assert np.allclose(pack["t_days"], t[idx]) and np.array_equal(pack["snap"], x[idx].astype(np.float16))
