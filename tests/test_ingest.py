"""Device ingest: register, learn, detect; resends counted once; source labels kept; bad input refused."""

import base64

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import ingest
from tools.sim_device import payload, vibration

T0 = 1_795_000_000.0


def client(tmp_path) -> TestClient:
    app = FastAPI()
    app.include_router(ingest.router(tmp_path / "ingest.db"))
    return TestClient(app)


def test_simulated_vibration_node_learns_then_flags_a_growing_fault(tmp_path):
    c = client(tmp_path)
    assert (
        c.post(
            "/api/ingest/register", json={"machine": "m", "mtype": "motor", "learning_days": 1, "ts": T0}
        ).status_code
        == 200
    )
    outs = []
    for seq in range(60):
        p = payload("esp-1", seq, "m", vibration(seq, fault_after=40), 1000, "vibration", ts=T0 + seq * 3600)
        outs.append(c.post("/api/ingest/reading", json=p).json())
    assert outs[0]["state"] == "learning" and outs[30]["state"] == "monitoring"
    assert np.mean([o["risk"] >= 1 for o in outs[25:40]]) < 0.3
    assert all(o["risk"] >= 1 for o in outs[50:])
    assert {o["source"] for o in outs} == {"simulated"}
    hist = c.get("/api/ingest/m").json()
    assert (
        hist["readings"][-1]["source"] == "simulated" and hist["versions"][0]["reason"] == "learning_complete"
    )


def test_resend_after_lost_reply_is_not_counted_twice(tmp_path):
    c = client(tmp_path)
    c.post("/api/ingest/register", json={"machine": "m", "mtype": "motor", "ts": T0})
    p = payload("esp-1", 7, "m", vibration(7, None), 1000, "vibration", ts=T0 + 10)
    first = c.post("/api/ingest/reading", json=p).json()
    again = c.post("/api/ingest/reading", json=p).json()
    assert again["duplicate"] is True and again["id"] == first["id"]
    assert len(c.get("/api/ingest/m").json()["readings"]) == 1


def test_bad_input_is_refused(tmp_path):
    c = client(tmp_path)
    p = payload("esp-1", 1, "nope", vibration(1, None), 1000, "vibration")
    assert c.post("/api/ingest/reading", json=p).status_code == 404
    c.post("/api/ingest/register", json={"machine": "m", "mtype": "motor"})
    assert (
        c.post("/api/ingest/reading", json={**p, "machine": "m", "samples_b64": "not base64!"}).status_code
        == 422
    )
    assert (
        c.post("/api/ingest/reading", json={**p, "machine": "m", "source": "field-validated"}).status_code
        == 422
    )
    long = base64.b64encode(np.zeros(1000 * 31, "<i2").tobytes()).decode()
    assert c.post("/api/ingest/reading", json={**p, "machine": "m", "samples_b64": long}).status_code == 413
    assert c.post("/api/ingest/reading", json={**p, "machine": "m", "kind": "temperature"}).status_code == 422


def test_clipped_reading_is_stored_but_not_scored(tmp_path):
    c = client(tmp_path)
    c.post("/api/ingest/register", json={"machine": "m", "mtype": "motor", "ts": T0})
    x = np.ones(2000)
    out = c.post("/api/ingest/reading", json=payload("esp-1", 1, "m", x, 1000, "vibration", ts=T0 + 1)).json()
    assert out["state"] == "unusable" and "clipped" in out["quality"]["reasons"]
