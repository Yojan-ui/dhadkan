"""Command centre: the cluster's readings go through the named agents to a verified repair, the state
survives a restart, paid actions need the owner role, and offline actions go out once."""

import pytest
from fastapi.testclient import TestClient

from server.app import create_app

LEARN = 23  # 7 learning days at 8 h per reading, plus a margin


def app_for(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path / "node.db", tmp_path / "reg.db", stream=False))


def step(c: TestClient, n: int = 1) -> None:
    for _ in range(n):
        assert c.post("/api/step", json={"n": 1}).status_code == 200


def machine(c: TestClient, mid: str) -> dict:
    return next(m for m in c.get("/api/cc/state").json()["machines"] if m["id"] == mid)


@pytest.fixture
def learned(tmp_path):
    c = app_for(tmp_path)
    step(c, LEARN)
    return c, tmp_path


def test_telemetry_is_measured_only(learned):
    c, _ = learned
    s = c.get("/api/cc/state").json()
    assert s["kpis"]["machines"] == 12 and s["kpis"]["factory_health"].endswith("machines normal")
    for m in s["machines"]:
        assert m["temperature_c"] is None and m["vibration"] is None  # no such sensors
        assert m["rpm_basis"] == "nameplate (assumption)"
        assert m["health"] != "learning" and m["last_ts"]
    assert "not measured" in s["performance"]["note"]


def test_blower_fault_runs_through_every_agent_to_a_verified_repair_and_survives_restart(learned):
    c, tmp = learned
    assert c.post("/api/cc/scenario", json={"machine": "C-F2", "mode": "fault"}).json()["simulated"]
    aid = None
    for _ in range(8):
        step(c)
        appr = [
            a
            for a in c.get("/api/cc/state").json()["approvals"]
            if a["case_id"].startswith("C-F2") and a["kind"] == "booking"
        ]
        if appr:
            aid = appr[0]["id"]
            break
    assert aid, "the blower's recorded fault should open a case that asks the owner"
    s = c.get("/api/cc/state").json()
    assert not [
        m for m in s["messages"] if m["kind"] == "book_job" and m["machine"] == "C-F2"
    ]  # nothing before yes
    rec = next(r for r in s["recommendations"] if r["machine"] == "C-F2")
    assert rec["evidence"] and rec["plan"]["window"] and "booking" in rec["approval_required"]
    assert c.post("/api/cc/decide", json={"approval_id": aid, "decision": "approved"}).json()["applied"]
    case = aid.split(":")[0]
    for _ in range(4):
        step(c)
        if [
            m
            for m in c.get("/api/cc/state").json()["messages"]
            if m["kind"] == "book_job" and m["machine"] == "C-F2"
        ]:
            break
    booked = [
        m
        for m in c.get("/api/cc/state").json()["messages"]
        if m["kind"] == "book_job" and m["machine"] == "C-F2"
    ]
    assert len(booked) == 1 and booked[0]["approval"] == aid
    assert c.post("/api/cc/repair_done", json={"case_id": case, "technician": booked[0]["technician"],
                                               "work": "bearing", "simulate_recovery": True}).status_code == 200  # fmt: skip
    step(c, 8)
    d = c.get("/api/cc/machine/C-F2").json()
    assert d["cases"][0]["outcome"] == "repair_fixed" and d["repairs"][0]["against_version"] == 1
    agents = {r["agent"] for r in c.get("/api/cc/state").json()["timeline"]} | {
        r["agent"] for r in d["agent_runs"]
    }
    assert {
        "observation",
        "intelligence",
        "memory",
        "planning",
        "technician",
        "execution",
        "verification",
    } <= agents
    runs_before = len(c.app.state.brain.activity.runs(10_000))
    # restart: a new server on the same files remembers everything
    c2 = app_for(tmp)
    d2 = c2.get("/api/cc/machine/C-F2").json()
    assert d2["cases"] == d["cases"] and d2["repairs"] == d["repairs"]
    assert len(c2.app.state.brain.activity.runs(10_000)) >= runs_before
    assert c2.app.state.brain.store.verify_audit()["ok"]


def test_offline_messages_wait_and_go_out_once(learned):
    c, _ = learned
    assert c.post("/api/link", json={"up": False}).status_code == 200
    c.post("/api/cc/scenario", json={"machine": "C-F2", "mode": "fault"})
    step(c, 4)
    s = c.get("/api/cc/state").json()
    assert s["kpis"]["link_up"] is False and s["queued"]
    sent_offline = len(s["messages"])
    c.post("/api/link", json={"up": True})
    step(c, 2)
    s = c.get("/api/cc/state").json()
    assert not s["queued"] and len(s["messages"]) > sent_offline
    idem = [m["idem"] for m in s["messages"]]
    assert len(idem) == len(set(idem))


def test_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("DHADKAN_OWNER_TOKEN", "own-123")
    monkeypatch.setenv("DHADKAN_TECH_TOKEN", "tech-123")
    monkeypatch.setenv("DHADKAN_VIEWER_TOKEN", "view-123")
    c = app_for(tmp_path)
    assert c.get("/api/cc/state").status_code == 401
    assert c.post("/api/cc/login", json={"token": "nope"}).status_code == 401
    view = {"Authorization": "Bearer view-123"}
    tech = {"Authorization": "Bearer tech-123"}
    assert c.get("/api/cc/state", headers=view).status_code == 200
    body = {"approval_id": "x:booking", "decision": "approved"}
    assert c.post("/api/cc/decide", json=body, headers=view).status_code == 403
    assert c.post("/api/cc/decide", json=body, headers=tech).status_code == 403
    assert c.post("/api/cc/decide", json=body, headers={"Authorization": "Bearer own-123"}).status_code == 404
    rep = {"case_id": "none", "technician": "T1", "work": "x", "simulate_recovery": False}
    assert c.post("/api/cc/repair_done", json=rep, headers=view).status_code == 403
    assert c.post("/api/cc/repair_done", json=rep, headers=tech).status_code == 404
    assert c.post("/api/cc/login", json={"token": "own-123"}).json()["role"] == "owner"
    assert c.get("/api/cc/me").json()["role"] == "owner"  # the session cookie works
    assert (
        c.post("/api/cc/scenario", json={"machine": "C-F2", "mode": "fault"}, headers=view).status_code == 403
    )


def test_session_cookies_cannot_be_forged_or_kept_past_expiry():
    from server import auth

    good = auth.session("owner", now=1000.0)
    assert auth.role_for_session(good, now=1000.0) == "owner"
    assert auth.role_for_session(good, now=1000.0 + auth.TTL_S + 1) is None
    _role, exp, sig = good.split(".")
    assert (
        auth.role_for_session(f"technician.{exp}.{sig}", now=1000.0) is None
    )  # role swapped, signature stale
    assert auth.role_for_session(f"owner.notanumber.{sig}") is None
    assert auth.role_for_session("garbage") is None


def test_state_reads_while_readings_are_written_concurrently(learned):
    import threading

    c, _ = learned
    errors = []

    def reader():
        for _ in range(25):
            r = c.get("/api/cc/state")
            if r.status_code != 200:
                errors.append(r.status_code)

    t = threading.Thread(target=reader)
    t.start()
    step(c, 6)
    t.join()
    assert errors == []
