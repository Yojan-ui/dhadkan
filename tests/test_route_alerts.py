import itertools
import math

from server import alerts, route


def stops():
    return [
        {"id": "A1", "xy_km": [0, 0], "service_h": 1.0, "risk": 1.5},
        {"id": "A2", "xy_km": [0, 0], "service_h": 1.0, "risk": 1.1},
        {"id": "B1", "xy_km": [10, 0], "service_h": 1.0, "risk": 2.0},
        {"id": "C1", "xy_km": [0, 30], "service_h": 1.0, "risk": 1.0},
    ]


def test_route_visits_all_when_time_allows():
    p = route.plan(stops(), [0, 0], hours=20, kmph=10)
    assert sorted(p["order"]) == ["A1", "A2", "B1", "C1"] and p["skipped"] == []


def test_route_is_shortest_order():
    p = route.plan(stops(), [0, 0], hours=20, kmph=10)
    xy = {s["id"]: s["xy_km"] for s in stops()}

    def length(order):
        pts = [[0, 0]] + [xy[i] for i in order] + [[0, 0]]
        return sum(math.dist(a, b) for a, b in itertools.pairwise(pts))

    assert math.isclose(length(p["order"]), min(length(o) for o in itertools.permutations(xy)))


def test_route_keeps_highest_risk_within_hours():
    # 3 h: B1 alone = 2 h drive + 1 h; A1+A2 = 2 h. Best risk fitting: A1+A2 (2.6) vs B1 (2.0)
    p = route.plan(stops(), [0, 0], hours=3, kmph=10)
    assert sorted(p["order"]) == ["A1", "A2"]
    assert "C1" in p["skipped"] and p["hours"] <= 3


def test_route_empty():
    assert route.plan([], [0, 0], 7, 20)["order"] == []


STOP_WORDS = ["बंद", "रोक", "थांब", "stop", "switch off", "shut"]


def test_alerts_never_say_stop():
    for lang in ("hi", "mr", "en"):
        for kind in ("rising", "match"):
            for fault in (None, "bearing", "imbalance", "loose_belt", "clog"):
                t = alerts.draft(lang, kind, "Pump 1", 3, fault=fault, days=3, area="Bhosari")
                assert not any(w in t.lower() for w in STOP_WORDS), t


def test_alert_reads_like_a_mechanic():
    t = alerts.draft("en", "rising", "Loom 4", 3, fault="bearing", days=3, tech="Ramesh")
    assert (
        t
        == "Loom 4: bearing sound rising for 3 days. Check before Thursday. Send Ramesh on Thursday? Reply YES / NO"
    )
    hi = alerts.draft("hi", "rising", "पंप 1", 3, fault="bearing", days=3, tech="रमेश")
    assert hi.startswith("पंप 1: बेयरिंग की आवाज़ 3 दिन से बढ़ रही है। गुरुवार से पहले")
    mr = alerts.draft("mr", "rising", "पंप 1", 3, fault="bearing", days=3, tech="रमेश")
    assert mr == (
        "पंप 1: बेअरिंगचा आवाज 3 दिवसांपासून वाढतोय. गुरुवारच्या आधी तपासून घ्या."
        " रमेशला गुरुवारी पाठवू का? उत्तर द्या: हो / नाही"
    )


def test_parse_reply():
    for yes in ("हाँ", "हो", "haan ji", "YES", "ho, pathva"):
        assert alerts.parse_reply(yes) is True, yes
    for no in ("ना", "नाही", "नको", "nahi", "No"):
        assert alerts.parse_reply(no) is False, no
    assert alerts.parse_reply("kal dekhte hain") is None
    assert alerts.parse_reply("haan nahi") is None
