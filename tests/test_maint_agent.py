"""Maintenance agent: persistent multi-step workflow, approval gate, recovery, idempotency, sync."""

from datetime import datetime

import pytest

from edge.baseline import BaselineStore
from server.maint import agent as ag
from server.maint.store import Store
from server.maint.sync import Cloud, SyncClient
from server.maint.tools import Network, Transport
from tests.test_baseline import OK, fault_row, normal_rows

T0 = datetime(2026, 11, 16, 9, 0).timestamp()  # a Monday
H = 3600.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def health(urgency: str, severity: str = "marked", risk=(2.5, 2.6, 2.8)) -> dict:
    return {"urgency": urgency, "severity": severity, "recent_risk": list(risk), "confidence": "high",
            "evidence": ["3 of the last 3 readings at or above its alarm level"]}  # fmt: skip


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    b = BaselineStore(tmp_path / "edge.db", detectors=["rms", "spectral"])
    b.register("chakki-1", "gearbox", learning_days=3, now=T0 - 5 * 86400)
    for i, r in enumerate(normal_rows(60)):
        b.observe("chakki-1", r, OK, ts=T0 - 5 * 86400 + i * 1.5 * 3600, source="simulated")
    for i in range(6):  # the anomaly that opened the case
        b.observe("chakki-1", fault_row(1.0, 500 + i), OK, ts=T0 - 6 * H + i * H, source="simulated")
    s = Store(tmp_path / "maint.db", clock=clock)
    s.add_machine("chakki-1", "Chakki 1", "Demo flour mill", "flour_mill", "gearbox", 10, "mill")
    net = Network(online=True)
    tr = Transport(network=net)
    a = ag.MaintenanceAgent(s, b, ag.Tools(transport=tr), clock=clock)
    return {"agent": a, "store": s, "base": b, "clock": clock, "transport": tr, "net": net, "tmp": tmp_path}


def statuses(s: Store, cid: str) -> dict[str, str]:
    return {st["name"]: st["status"] for st in s.steps(cid)}


def test_full_workflow_detect_plan_approve_book_repair_verify(env):
    a, s, b, clock, tr = env["agent"], env["store"], env["base"], env["clock"], env["transport"]
    case = a.observe("chakki-1", health("inspect"))
    assert case["plan"] == ag.PLAN_FIX and b.machine("chakki-1")["hold"] == 1
    a.tick()
    st = statuses(s, case["id"])
    assert st["await_approval"] == "waiting" and st["book_job"] == "pending"
    assert not [x for x in tr.sent if x["kind"] == "book_job"]  # nothing booked before the owner says yes
    appr = s.approval(f"{case['id']}:booking")
    assert appr["status"] == "requested" and appr["amount_low"] > 0 and "typical" in appr["summary"]
    a.decide(appr["id"], "approved", by="owner", channel="whatsapp", ref="wamid.1")
    a.tick()
    booked = [x for x in tr.sent if x["kind"] == "book_job"]
    assert len(booked) == 1 and booked[0]["approval_ref"] == "wamid.1"
    clock.t += 26 * H
    a.repair_done(case["id"], technician=booked[0]["technician"], work="replaced bearing")
    a.tick()
    assert statuses(s, case["id"])["verify_repair"] == "waiting"  # needs readings after the repair
    for i, r in enumerate(normal_rows(6, seed=77)):
        b.observe("chakki-1", r, OK, ts=clock.t + (i + 1) * 600, source="simulated")
    clock.t += 2 * H
    a.tick()
    c = s.case(case["id"])
    assert c["outcome"] == "repair_fixed" and c["closed"]
    rep = s.repairs("chakki-1")[0]
    assert rep["against_version"] == 1 and rep["after_median"] < 1 < rep["before_median"]
    assert b.machine("chakki-1")["hold"] == 0
    assert s.verify_audit()["ok"]
    events = [e["event"] for e in s.audit(case["id"])]
    assert events[0] == "case_opened" and "decision" in events and "case_closed" in events


def test_paid_actions_refuse_to_run_without_approval(env):
    a, s, tr = env["agent"], env["store"], env["transport"]
    case = a.observe("chakki-1", health("inspect"))
    for kind in ("book_job", "order_part"):
        with pytest.raises(ag.ApprovalRequired):
            a.act(case["id"], kind, "x", {"technician": "T1"})
    assert tr.sent == []
    assert any(e["event"] == "blocked_without_approval" for e in s.audit(case["id"]))
    s.request_approval("other:booking", "other-case", "booking", "x")
    s.decide("other:booking", "approved", "owner", "app", "r")
    with pytest.raises(ag.ApprovalRequired):  # an approval for another case does not count
        a.act(case["id"], "book_job", "x", {"technician": "T1"})


def test_the_agent_has_no_way_to_stop_a_machine(env):
    a = env["agent"]
    names = [n for n in dir(a) if "stop" in n.lower() or "shutdown" in n.lower() or "switch" in n.lower()]
    assert names == []
    assert not {"stop", "stop_machine", "power_off"} & set(ag.GATED)
    case = a.observe("chakki-1", health("urgent_owner_attention", "severe", (5, 6, 7)))
    a.tick()
    stop_q = env["store"].approval(f"{case['id']}:production_stop")
    assert stop_q["status"] == "requested" and "your decision" in stop_q["summary"]


def test_offline_actions_queue_and_go_out_once_when_back(env):
    a, s, tr, net, clock = env["agent"], env["store"], env["transport"], env["net"], env["clock"]
    net.online = False
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    assert tr.sent == [] and s.queued_actions()
    assert statuses(s, case["id"])["request_approval"] == "waiting"
    for _ in range(3):  # retries while offline do not duplicate anything
        clock.t += 2 * H
        a.tick()
    net.online = True
    clock.t += 2 * H
    a.tick()
    idems = [x["idem"] for x in tr.sent]
    assert len(idems) == len(set(idems)) and any("approval_booking" in i for i in idems)
    assert statuses(s, case["id"])["await_approval"] == "waiting"


def test_provider_failures_retry_with_backoff(env):
    a, s, tr, clock = env["agent"], env["store"], env["transport"], env["clock"]
    tr.fail_next = 2
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    act = s.action(f"{case['id']}:notify:approval_booking")
    assert act["status"] == "queued" and act["attempts"] == 1 and act["next_try"] > clock.t
    a.tick()  # not due yet: no new attempt
    assert s.action(act["idem"])["attempts"] == 1
    clock.t += 10 * H
    a.tick()
    clock.t += 10 * H
    a.tick()
    assert s.action(act["idem"])["status"] == "done"
    assert sum(x["idem"] == act["idem"] for x in tr.sent) == 1


def test_unavailable_technician_falls_back_to_the_next_one(env):
    a, s, tr = env["agent"], env["store"], env["transport"]
    tr.decline = {"T1"}
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    a.decide(f"{case['id']}:booking", "approved", "owner", "whatsapp", "w1")
    a.tick()
    a.tick()
    booked = [x for x in tr.sent if x["kind"] == "book_job"]
    assert [x["technician"] for x in booked] == ["T2"]
    assert any(e["event"] == "technician_unavailable" for e in s.audit(case["id"]))


def test_no_technician_at_all_tells_the_owner_and_keeps_trying(env):
    a, s, tr = env["agent"], env["store"], env["transport"]
    a.t.technicians.people = []
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    st = next(x for x in s.steps(case["id"]) if x["name"] == "find_technician")
    assert st["status"] == "retry" and st["next_try"] > env["clock"].t
    assert any("No mechanic" in x["text"] for x in tr.sent if x["kind"] == "notify")


def test_missing_part_is_reported_as_an_order_and_prices_are_cited(env):
    a, s = env["agent"], env["store"]
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    res = {x["name"]: x["result"] for x in s.steps(case["id"])}
    bearing = next(c for c in res["check_parts"]["candidates"] if c["fault"] == "bearing_wear")
    assert bearing["order_needed"] is True and "MOCK" in bearing["stock"][0]["source"]
    assert all("price_inr" not in r for r in bearing["stock"])  # mock prices are never used
    for c in res["estimate_cost"]["if_found"]:
        assert c["price_source"].startswith("https://") and c["low_inr"] <= c["high_inr"]
    tech = res["find_technician"]
    assert "DEMO DATA" in tech["source"]


def test_interrupted_workflow_resumes_without_duplicates(env):
    a, s, b, tr, clock = env["agent"], env["store"], env["base"], env["transport"], env["clock"]
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    a.decide(f"{case['id']}:booking", "approved", "owner", "app", "tap-1")
    book = next(x for x in s.steps(case["id"]) if x["name"] == "book_job")
    a.act(case["id"], "book_job", "T1:2026-11-16", {"technician": "T1"})  # sent, then the node crashed
    s.set_step(case["id"], book["idx"], status="running")
    a2 = ag.MaintenanceAgent(Store(env["tmp"] / "maint.db", clock=clock), b, a.t, clock=clock)  # restart
    a2.tick()
    assert sum(x["kind"] == "book_job" for x in tr.sent) == 1
    assert statuses(a2.s, case["id"])["book_job"] == "done"


def test_repeat_observations_one_case_escalation_and_resolution(env):
    a, s = env["agent"], env["store"]
    c1 = a.observe("chakki-1", health("monitor", "mild", (1.1, 1.2, 1.1)))
    c2 = a.observe("chakki-1", health("monitor", "mild", (1.1, 1.2, 1.1)))
    assert c1["id"] == c2["id"] and len(s.cases("chakki-1")) == 1
    a.tick()
    up = a.observe("chakki-1", health("inspect"))
    assert up["urgency"] == "inspect" and "book_job" in up["plan"]
    m = a.observe("chakki-1", health("measure_again", "none", (0.3, 0.2, 1.1)))
    for _ in range(3):
        m = a.observe("chakki-1", health("none", "none", (0.2, 0.3, 0.1)))
    assert m["closed"] is None  # a case with a repair plan does not close on its own
    c3 = s.create_case("chakki-1#x", "chakki-1", "monitor", "mild", ag.PLAN_MONITOR, {"history": []})
    assert c3["state"] == "open"


def test_watch_case_closes_after_normal_streak(env):
    a, _s, b = env["agent"], env["store"], env["base"]
    a.observe("chakki-1", health("measure_again", "none", (0.3, 0.2, 1.2)))
    a.tick()
    for _ in range(3):
        last = a.observe("chakki-1", health("none", "none", (0.2, 0.3, 0.1)))
    assert last["outcome"] == "resolved_without_action" and b.machine("chakki-1")["hold"] == 0


def test_owner_declines_then_the_agent_watches(env):
    a, s, tr = env["agent"], env["store"], env["transport"]
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    a.decide(f"{case['id']}:booking", "rejected", "owner", "whatsapp", "w2")
    a.tick()
    st = statuses(s, case["id"])
    assert st["book_job"] == "skipped" and st["await_change"] == "waiting"
    assert not [x for x in tr.sent if x["kind"] == "book_job"]


def test_urgent_unanswered_goes_to_the_supervisor_once(env):
    a, _s, tr, clock = env["agent"], env["store"], env["transport"], env["clock"]
    a.observe("chakki-1", health("urgent_owner_attention", "severe", (5, 6, 7)))
    a.tick()
    clock.t += 3 * H
    a.tick()
    a.tick()
    sup = [x for x in tr.sent if x.get("to") == "supervisor"]
    assert len(sup) == 1


def test_sync_offline_then_online_idempotent_and_conflict_asks_again(env):
    a, s, tr, clock = env["agent"], env["store"], env["transport"], env["clock"]
    cloud = Cloud()
    up = {"on": False}

    def send(op, payload):
        if not up["on"]:
            raise ConnectionError("no network")
        return cloud.handle(op, payload)

    sync = SyncClient(s, a, send)
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    r = sync.run()
    assert r["online"] is False and r["events_waiting"] > 0
    aid = f"{case['id']}:booking"
    a.decide(aid, "approved", "owner", "app", "tap-7")  # owner taps Approve on the shed screen
    cloud.add_decision("d1", "factory-node", aid, "rejected", "owner", "whatsapp", "wamid.9", clock.t)
    up["on"] = True
    r = sync.run()
    assert r["online"] is True and r["pushed"] > 0 and r["events_waiting"] == 0 and r["decisions"] == 1
    assert s.approval(aid)["status"] == "conflict"
    assert sync.run()["pushed"] >= 0 and cloud.count("factory-node") == len(s.audit())
    a.tick()
    assert s.approval(aid)["status"] == "requested"  # asked again
    assert any(x.get("text", "").startswith("Two different answers") for x in tr.sent)
    assert not [x for x in tr.sent if x["kind"] == "book_job"]


def test_cloud_refuses_a_changed_audit_row_and_the_node_detects_tampering(env):
    a, s = env["agent"], env["store"]
    a.observe("chakki-1", health("inspect"))
    a.tick()
    cloud = Cloud()
    rows = s.audit()
    assert cloud.push("n", rows)["accepted"] == len(rows)
    assert cloud.push("n", rows)["duplicates"] == len(rows)
    bad = [dict(rows[0], hash="0" * 64)]
    assert cloud.push("n", bad)["error"] == "chain_mismatch"
    s.db.execute("UPDATE audit SET detail='{}' WHERE seq=2")
    s.db.commit()
    assert s.verify_audit() == {"ok": False, "broken_at": 2}


def test_memory_recurrence_and_failed_technician_is_not_sent_again(env):
    a, s, clock = env["agent"], env["store"], env["clock"]
    s.add_repair(machine="chakki-1", case_id="old", technician="T1", work="bearing", done=clock.t - 10 * 86400,
                 outcome="failed", before_median=3.0, after_median=2.9, against_version=1)  # fmt: skip
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    res = {x["name"]: x["result"] for x in s.steps(case["id"])}
    assert res["recall"]["recurrence_after_repair"] and "may not have held" in res["recall"]["note"]
    assert res["find_technician"]["technician"] != "T1" and "T1" in res["find_technician"]["excluded"]


def test_late_approval_moves_the_visit_to_a_day_that_has_not_passed(env):
    a, s, tr, clock = env["agent"], env["store"], env["transport"], env["clock"]
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    clock.t += 3 * 86400  # the owner answers three days later
    a.decide(f"{case['id']}:booking", "approved", "owner", "whatsapp", "late")
    a.tick()
    a.tick()
    booked = [x for x in tr.sent if x["kind"] == "book_job"]
    assert len(booked) == 1 and booked[0]["day"] >= datetime.fromtimestamp(clock.t).date().isoformat()
    assert any(e["event"] == "replanned_visit_day" for e in s.audit(case["id"]))
