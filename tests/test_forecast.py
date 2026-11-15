"""Days to fault level, rupees at risk, and the route that uses them."""

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
    lv = forecast.fault_levels()
    assert lv["fan/id_00"]["n"] == 20 and lv["fan/id_00"]["level"] > 1.0
    assert lv["gearbox/section_00"]["level"] < 1.0  # the model can't separate these faults: no estimate


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
