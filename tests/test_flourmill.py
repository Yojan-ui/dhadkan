"""The flour-mill demonstration end to end (recorded MIMII DUE gearbox readings, demo people, simulated clock)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import flourmill, mill_api


def test_full_story_detects_waits_for_owner_books_and_verifies(tmp_path):
    d = flourmill.FlourMillDemo(tmp_path, auto_owner=True)
    st = d.run()
    tl = st["timeline"]
    assert [b["version"] for b in st["baseline"]] == [1] and st["baseline"][0][
        "reason"
    ] == "learning_complete"
    learning = [r for r in tl if r["phase"] == "learning"]
    assert all(r["risk"] is None for r in learning)  # no alarms while learning
    normal = [r for r in tl if r["phase"] == "normal" and r["risk"] is not None]
    assert sum(r["risk"] >= 1 for r in normal) <= 1
    assert max(r["risk"] for r in tl if r["phase"] == "fault") > 2
    assert st["case"]["outcome"] == "repair_fixed"
    rep = st["repairs"][0]
    assert rep["against_version"] == 1 and rep["before_median"] > 1 > rep["after_median"]
    offline = [r for r in tl if not r["online"]]
    assert offline and max(r["events_waiting"] for r in offline) > 0
    assert tl[-1]["events_waiting"] == 0 and st["sync"]["cloud_events"] > 0
    booked = [m for m in st["messages"] if m["kind"] == "book_job"]
    assert len(booked) == 1
    from datetime import datetime

    assert (
        datetime.fromtimestamp(rep["done"]).date().isoformat() >= booked[0]["day"]
    )  # repaired on/after the visit day
    assert not [a for a in st["approvals"] if a["status"] == "requested"]  # nothing left asking after closing
    assert st["audit_ok"] is True
    assert "RECORDED" in st["provenance"]["readings"] and "DEMO DATA" in st["provenance"]["people_and_stock"]


def test_without_the_owner_nothing_is_booked_and_the_story_waits(tmp_path):
    d = flourmill.FlourMillDemo(tmp_path, auto_owner=False)
    st = d.run()
    assert st["waiting_for"] and not st["done"]
    assert not [m for m in st["messages"] if m["kind"] == "book_job"]
    assert st["repairs"] == []
    aid = next(a["id"] for a in st["approvals"] if a["kind"] == "booking")
    st = d.decide(aid, "rejected") and d.state()
    assert st["declined"] and st["done"]
    assert not [m for m in st["messages"] if m["kind"] == "book_job"]


def test_mill_api_drives_the_same_story(tmp_path):
    app = FastAPI()
    app.include_router(mill_api.router(tmp_path))
    c = TestClient(app)
    s = c.get("/api/mill/state").json()
    assert s["i"] == 0 and s["machine"]["id"] == "chakki-1"
    s = c.post("/api/mill/step", json={"n": 60}).json()
    assert s["waiting_for"]
    aid = next(a["id"] for a in s["approvals"] if a["kind"] == "booking" and a["status"] == "requested")
    assert c.post("/api/mill/decide", json={"approval_id": aid, "decision": "maybe"}).status_code == 422
    s = c.post("/api/mill/decide", json={"approval_id": aid, "decision": "approved"}).json()
    s = c.post("/api/mill/step", json={"n": 60}).json()
    assert s["case"]["outcome"] == "repair_fixed"
    s = c.post("/api/mill/network", json={"online": False}).json()
    assert s["sync"]["network"] == "offline"
    assert c.get("/api/mill/audio/before.wav").headers["content-type"] == "audio/wav"
    assert c.post("/api/mill/reset").json()["i"] == 0
