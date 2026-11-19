"""The live recorder's server side (server/live_feed.py, /api/ingest/live/*): live clips go through the brain's
pipeline, readings are idempotent on device:seq, and quick learning is labelled as the demo setting it is.

The clips here are synthetic (a fan-like hum; the "fault" adds a strong tone and broadband noise): they test
the plumbing, not detection quality. The measured stage trial is scripts/live_trial.py on a real fan."""

import base64
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from edge.baseline import BaselineStore
from edge.config import MODELS, SR
from server import app as app_mod
from server.live_feed import QUICK_CLIPS, QUICK_LABEL

pytestmark = pytest.mark.skipif(
    not (MODELS / "fan" / "model.tflite").exists(), reason="needs trained models (python -m edge.train)"
)
MID = "A-F1"  # Blower 1: the table fan on stage stands in for it


def clip(rng: np.random.Generator, fault: bool = False, seconds: float = 10.0) -> str:
    t = np.arange(int(seconds * SR)) / SR
    x = sum(
        a * np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f, a in ((50, 0.2), (100, 0.1), (150, 0.05))
    )
    x = x + rng.normal(0, 0.02, t.size)
    if fault:  # a rubbing / unbalanced machine: a strong tone and a much higher noise floor
        x = x + 0.25 * np.sin(2 * np.pi * 2900 * t) + rng.normal(0, 0.08, t.size)
    pcm = (np.clip(x, -1, 1) * 32767 * 0.8).astype("<i2")
    return base64.b64encode(pcm.tobytes()).decode()


def reading(
    c: TestClient,
    seq: int,
    b64: str,
    ts: float,
    mid: str = MID,
    source: str = "live",
    device: str = "phone-1",
):
    return c.post("/api/ingest/reading", json={"device": device, "seq": seq, "machine": mid, "kind": "audio",
                                                "sr": SR, "samples_b64": b64, "source": source, "ts": ts})  # fmt: skip


@pytest.fixture()
def world(tmp_path):
    app = app_mod.create_app(tmp_path / "node.db", tmp_path / "registry.db", stream=False)
    return app, TestClient(app)


def learn(c: TestClient, rng: np.random.Generator, t0: float, n: int = QUICK_CLIPS) -> float:
    for i in range(n):
        assert reading(c, i, clip(rng), t0 + 10 * i).status_code == 200
    return t0 + 10 * n


def test_quick_learn_is_labelled_and_learns_from_live_clips_only(world):
    app, c = world
    brain = app.state.brain
    st = c.post("/api/ingest/live/start", json={"machine": MID, "device": "phone-1", "quick": True}).json()
    assert st["live"] and st["quick"] and st["label"] == QUICK_LABEL == "short learning period, demo"
    assert st["state"] == "learning" and st["progress"] == {"clips": 0, "target": QUICK_CLIPS}
    rng = np.random.default_rng(1)
    t0 = time.time()
    mid_way = None
    for i in range(QUICK_CLIPS):
        out = reading(c, i, clip(rng), t0 + 10 * i).json()
        if i == 5:
            mid_way = out
    assert mid_way["state"] == "learning" and mid_way["progress"]["clips"] == 6
    st = c.get(f"/api/ingest/live/{MID}").json()
    assert st["state"] == "monitoring" and st["readings"] == QUICK_CLIPS
    v = brain.base.versions(MID)
    assert len(v) == 1 and v[0]["n_rows"] == QUICK_CLIPS  # v1 from live clips only
    assert {h["source"] for h in brain.base.history(MID)} == {
        "live"
    }  # earlier readings archived, not mixed in
    events = [e for e in app.state.live.events if e["kind"] in ("live_started", "live_learned")]
    assert [e["label"] for e in events] == [QUICK_LABEL, QUICK_LABEL]
    twin = next(m for m in c.get("/api/state").json()["machines"] if m["id"] == MID)
    assert (
        twin["live"]["label"] == QUICK_LABEL and twin["mode"] == "live" and twin["latest"]["mode"] == "live"
    )
    row = next(r for r in brain.machine_rows() if r["id"] == MID)
    assert row["live"]["quick"] and row["source"] == "live"


def test_a_resent_clip_is_counted_once(world):
    _, c = world
    c.post("/api/ingest/live/start", json={"machine": MID, "device": "phone-1", "quick": True})
    rng = np.random.default_rng(2)
    b64, ts = clip(rng), time.time()
    first = reading(c, 7, b64, ts).json()
    again = reading(c, 7, b64, ts).json()  # the phone did not get the reply and sent it again
    assert not first["duplicate"] and again["duplicate"] and again["id"] == first["id"]
    assert c.get(f"/api/ingest/live/{MID}").json()["readings"] == 1
    other = reading(c, 7, b64, ts, device="phone-2").json()  # the same seq from another device is new
    assert not other["duplicate"]


def test_live_readings_need_a_started_recorder_and_the_live_label(world):
    _, c = world
    rng = np.random.default_rng(3)
    assert reading(c, 0, clip(rng), time.time()).status_code == 409
    c.post("/api/ingest/live/start", json={"machine": MID, "device": "phone-1", "quick": True})
    assert reading(c, 0, clip(rng), time.time(), source="simulated").status_code == 422
    assert (
        c.post("/api/ingest/live/start", json={"machine": "nope", "device": "d", "quick": True}).status_code
        == 404
    )


def test_a_fault_after_learning_raises_the_risk_and_goes_to_the_planner(world, monkeypatch):
    monkeypatch.setenv("DHADKAN_PHONE_A", "+91 90000 11111")
    app, c = world
    brain = app.state.brain
    c.post("/api/ingest/live/start", json={"machine": MID, "device": "phone-1", "quick": True})
    rng = np.random.default_rng(4)
    t = learn(c, rng, time.time())
    normal = [reading(c, 100 + i, clip(rng), t + 10 * i).json()["risk"] for i in range(3)]
    t += 30
    fault = [reading(c, 200 + i, clip(rng, fault=True), t + 10 * i).json() for i in range(4)]
    assert max(normal) < 1.0 and all(f["risk"] >= 2.0 for f in fault)
    assert fault[-1]["health"]["urgency"] in ("inspect", "maintenance", "urgent_owner_attention")
    types = [e["type"] for e in brain.store.events()]
    assert "alarm" in types and "health_change" in types  # the planner's events
    assert brain.store.open_case(MID) is not None
    # the stream: the live clip's slices are in the frame cache
    frame = app.state.live.frame()
    assert MID in frame["machines"] and len(frame["machines"][MID]["spec"]) == 64


def test_messages_about_a_live_machine_go_through_the_real_notifier(world, monkeypatch):
    monkeypatch.setenv("DHADKAN_PHONE_A", "+91 90000 11111")
    app, c = world
    feed = app.state.brain.feed
    assert feed.send("notify", {"machine": MID, "text": "x", "idem": "abc"})["dry_run"]  # not live: internal
    c.post("/api/ingest/live/start", json={"machine": MID, "device": "phone-1", "quick": True})
    out = feed.send("notify", {"machine": MID, "text": "Blower 1 ki awaaz badli hai. 1 = Haan, 2 = Baad mein",
                               "idem": "q1"})  # fmt: skip
    assert out["id"].startswith("alert:")
    rows = app.state.live.alerts.notifier.recent(5)
    assert (
        rows[0]["machine"] == MID
        and rows[0]["kind"] == "planner"
        and rows[0]["to_phone"] == "+91 90000 11111"
    )


def test_simulator_steps_do_not_feed_a_live_machine(world):
    app, c = world
    c.post("/api/ingest/live/start", json={"machine": MID, "device": "phone-1", "quick": True})
    c.post("/api/step", json={"n": 1})
    assert app.state.brain.base.history(MID) == []  # nothing simulated reached it


def test_a_new_machine_registered_from_the_recorder_survives_a_restart(tmp_path):
    app = app_mod.create_app(tmp_path / "node.db", tmp_path / "registry.db", stream=False)
    c = TestClient(app)
    spec = c.post(
        "/api/ingest/live/machine", json={"factory": "A", "name": "Table fan", "mtype": "fan", "hp": 0.1}
    ).json()
    assert spec["id"] == "A-L1" and spec["source"] == "live" and spec["shape"] == "motor"
    assert (
        c.post("/api/ingest/live/machine", json={"factory": "Z", "name": "x", "mtype": "fan"}).status_code
        == 422
    )
    c.post("/api/ingest/live/start", json={"machine": "A-L1", "device": "laptop", "quick": True})
    assert (
        reading(c, 0, clip(np.random.default_rng(5)), time.time(), mid="A-L1", device="laptop").status_code
        == 200
    )
    ids = {m["id"] for m in c.get("/api/ingest/live").json()["machines"]}
    assert "A-L1" in ids and "A-L1" in {m["id"] for m in c.get("/api/state").json()["machines"]}
    app2 = app_mod.create_app(tmp_path / "node.db", tmp_path / "registry.db", stream=False)
    c2 = TestClient(app2)
    st = c2.get("/api/ingest/live/A-L1").json()
    assert st["live"] and st["readings"] == 1


def test_baseline_restart_archives_and_learns_again(tmp_path):
    s = BaselineStore(tmp_path / "e.db", detectors=["rms"])
    s.register("m", "fan", learning_days=1e-9, now=0)
    rng = np.random.default_rng(6)
    from edge.detectors import WIDTH

    for i in range(12):
        s.observe(
            "m", rng.normal(0, 1, WIDTH), {"ok": True}, ts=float(i), source="simulated", reading_id=f"s{i}"
        )
    assert s.versions("m")
    archive = s.restart("m", learning_days=1e-9, now=100.0)
    assert s.history("m") == [] and s.versions("m") == [] and s.machine("m")["state"] == "learning"
    assert len(s.history(archive)) == 12 and s.versions(archive)  # archived, not deleted


def test_the_twin_ranks_a_live_machine_on_its_live_readings(world):
    _, c = world
    c.post("/api/ingest/live/start", json={"machine": MID, "device": "phone-1", "quick": True})
    rng = np.random.default_rng(7)
    t = learn(c, rng, time.time())
    reading(c, 300, clip(rng, fault=True), t)
    tw = c.get(f"/api/twin?focus={MID}").json()
    row = next(r for r in tw["ranking"] if r["id"] == MID)
    assert row["live"] and row["risk"] >= 1.0 and tw["trend"] and tw["trend"][-1] == row["risk"]
    assert sum(1 for r in tw["ranking"] if r["id"] == MID) == 1  # not also ranked from the simulator


def test_trial_measurement_counts_false_alarms_and_detection_delay():
    from scripts import live_trial

    t0 = 1_000_000.0
    # one stray clip over 1 in normal running, then the fault
    risks = [0.3, 0.5, 1.2, 0.4, 0.6, 0.5, 0.4, 3.1, 3.5, 2.8, 4.0]
    hist = [{"id": i, "ts": t0 + 10 * i, "risk": r, "running": 1, "excluded": None, "after_gap": 0, "quality": "{}"}
            for i, r in enumerate(risks)]  # fmt: skip
    on = t0 + 70  # the coin went on at the 8th clip's start
    res = live_trial.compute(hist, [{"label": "fault_on", "ts": on}], "A-F1", baseline_rows=12)
    assert res["normal"]["n"] == 7 and res["normal"]["at_or_above_alarm"] == 1
    assert res["normal"]["false_alarms"] == 0  # one stray clip is not an alarm (2 of 3 needed)
    assert res["fault"]["n"] == 4 and res["detection_delay_s"]["first_clip_over_alarm"] == 10.0
    assert res["detection_delay_s"]["alarm_raised"] == 20.0 and res["detected"]
    assert res["score_jump"]["difference"] > 2.5 and res["score_jump"]["normal_median"] == 0.5
    with pytest.raises(ValueError):
        live_trial.compute(hist, [], "A-F1")  # no mark: nothing measured


def test_a_phone_clock_behind_the_server_still_finishes_learning(world):
    """Learning ends after the clean clips, counted by the server, not when the phone's clock says so."""
    app, c = world
    c.post("/api/ingest/live/start", json={"machine": MID, "device": "phone-1", "quick": True})
    rng = np.random.default_rng(8)
    learn(c, rng, time.time() - 300)  # every clip stamped 5 minutes before the recorder started
    st = c.get(f"/api/ingest/live/{MID}").json()
    assert st["state"] == "monitoring"
    v = app.state.brain.base.versions(MID)[0]
    assert v["n_rows"] == QUICK_CLIPS and v["note"] == QUICK_LABEL
