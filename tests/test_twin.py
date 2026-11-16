"""Digital twin state: a stopped machine and a lost sensor are different, neither streams a fake live
reading, and every simulated state is labelled."""

from fastapi.testclient import TestClient

from server.app import create_app


def client(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path / "node.db", tmp_path / "reg.db", stream=False))


def machine(c: TestClient, mid: str) -> dict:
    return next(m for m in c.get("/api/state").json()["machines"] if m["id"] == mid)


def test_running_machine_is_live_with_no_simulated_flag(tmp_path):
    c = client(tmp_path)
    c.post("/api/step", json={"n": 1})
    op = machine(c, "A-P1")["operating"]
    assert op == {**op, "running": True, "sensor": "live", "ticks_since": 0, "simulated": False}


def test_stopped_is_confirmed_and_lost_sensor_is_unknown(tmp_path):
    c = client(tmp_path)
    c.post("/api/step", json={"n": 1})
    assert c.post("/api/machine/A-P1/operating", json={"run": "stopped"}).json()["simulated"] is True
    c.post("/api/machine/A-P2/operating", json={"sensor": "lost"})
    before = machine(c, "A-P1")["latest"]["tick"]
    c.post("/api/step", json={"n": 2})
    stopped, lost, other = machine(c, "A-P1"), machine(c, "A-P2"), machine(c, "A-F1")
    assert stopped["operating"]["running"] is False and stopped["operating"]["sensor"] == "stale"
    assert lost["operating"]["running"] is None and lost["operating"]["sensor"] == "lost"
    assert stopped["latest"]["tick"] == before  # no new reading was made up
    assert stopped["operating"]["ticks_since"] == 2 and stopped["operating"]["simulated"]
    assert other["operating"]["sensor"] == "live"
    frame = c.app.state.live.frame()["machines"]
    assert "A-P1" not in frame and "A-P2" not in frame and "A-F1" in frame  # no fake live stream


def test_restore_resumes_readings_and_bad_values_are_refused(tmp_path):
    c = client(tmp_path)
    c.post("/api/step", json={"n": 1})
    c.post("/api/machine/A-P1/operating", json={"run": "stopped", "sensor": "lost"})
    c.post("/api/step", json={"n": 1})
    assert (
        c.post("/api/machine/A-P1/operating", json={"run": "running", "sensor": "ok"}).json()["simulated"]
        is False
    )
    c.post("/api/step", json={"n": 1})
    m = machine(c, "A-P1")
    assert m["operating"]["running"] is True and m["operating"]["sensor"] == "live"
    assert not m["operating"]["simulated"]
    assert c.post("/api/machine/A-P1/operating", json={"run": "flying"}).status_code == 422
    assert c.post("/api/machine/NOPE/operating", json={"run": "stopped"}).status_code == 404
    assert c.post("/api/machine/A-P1/operating", json={"power": "off"}).status_code == 422


def test_clock_moves_even_when_every_machine_is_quiet(tmp_path):
    c = client(tmp_path)
    c.post("/api/step", json={"n": 1})
    ids = [m["id"] for m in c.get("/api/state").json()["machines"]]
    for mid in ids:
        c.post(f"/api/machine/{mid}/operating", json={"run": "stopped"})
    t0 = c.get("/api/state").json()["tick"]
    c.post("/api/step", json={"n": 2})
    assert c.get("/api/state").json()["tick"] == t0 + 2
