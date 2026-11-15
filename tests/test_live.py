"""Live server: API, the 2 Hz frame, the demo flow end to end, and the replay script."""

import asyncio
import itertools
import json

import pytest
from fastapi.testclient import TestClient

from edge.config import MODELS
from server import app as app_mod
from server import folk

pytestmark = pytest.mark.skipif(
    not (MODELS / "fan" / "model.tflite").exists(), reason="needs trained models (python -m edge.train)"
)

NOTE = "ब्लोअर में खट-खट की आवाज़ आ रही है"


@pytest.fixture()
def app(tmp_path):
    return app_mod.create_app(tmp_path / "node.db", tmp_path / "registry.db", stream=False)


@pytest.fixture()
def client(app):
    return TestClient(app)


def test_meta_and_empty_state(client):
    meta = client.get("/api/meta").json()
    assert len(meta["mel_hz"]) == 64 and meta["mel_hz"] == sorted(meta["mel_hz"])
    assert {f["id"] for f in meta["factories"]} == {"A", "B", "C"}
    s = client.get("/api/state").json()
    assert s["tick"] == -1 and s["clock"] is None and all(m["latest"] is None for m in s["machines"])


def test_step_frame_and_audio(app, client):
    assert client.post("/api/step", json={"n": 1}).json() == {"tick": 0}
    lv = app.state.live
    s = client.get("/api/state").json()
    assert all(m["latest"] and len(m["band_z"]) == 64 for m in s["machines"])
    assert {m["shape"] for m in s["machines"]} == {"pump", "motor", "loom", "mill"}
    f = lv.frame()
    assert f["type"] == "frame" and len(f["machines"]) == 12
    for m in f["machines"].values():
        assert len(m["spec"]) == 64 and all(0 <= v <= 1 for v in m["spec"])
        assert m["risk"] > 0 and -120 < m["energy"] < 0
    # each slice's window risk comes from that reading's own window errors
    a = lv.cache["A-F1"]
    assert len(a["risk"]) == app_mod.SLICES and len(a["energy"]) == app_mod.SLICES

    r = client.get("/api/audio/A-F1/live.wav")
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    ref = client.get("/api/audio/A-F1/normal.wav")
    assert ref.content[:4] == b"RIFF"
    # the normal reference is a held-out validation clip, never one the demo replays
    pool = [p.name for lab in lv.c.pools()["A-F1"].values() for p in lab]
    assert ref.headers["x-clip"] not in pool
    assert client.get("/api/audio/NOPE/live.wav").status_code == 404


def test_fault_to_route_to_reply(app, client):
    lv = app.state.live
    client.post("/api/step", json={"n": 3})
    client.post("/api/mode", json={"machine": "A-F1", "mode": "fault"})
    client.post("/api/step", json={"n": 3})
    lab = client.post("/api/label", json={"text": NOTE}).json()
    assert (lab["sound"], lab["fault"]) == ("khat-khat", "bearing")
    pub = client.post("/api/publish").json()
    assert pub["pending"] == 0 and len(pub["published"]) == 1
    checks = {c["machine"]: c for c in pub["published"][0]["checks"]}
    assert set(checks) == {"B-F1", "C-F1", "C-F2"}  # same type, other factories
    assert checks["B-F1"]["same_model"] and not checks["C-F1"]["same_model"]

    client.post("/api/mode", json={"machine": "B-F1", "mode": "onset"})
    for _ in range(8):
        client.post("/api/step", json={"n": 1})
        if any(e["kind"] == "early_match" for e in lv.events):
            break
    em = next(e for e in lv.events if e["kind"] == "early_match")
    assert em["machine"] == "B-F1" and em["source"] == "A-F1" and em["cosine"] > em["level"]

    out = client.post("/api/agent", json={"engine": "rules"}).json()
    assert out["run_id"].startswith("rules-")
    s = client.get("/api/state").json()
    legs = s["route"]["legs"]
    assert {"A-F1", "B-F1"} <= {leg["machine"] for leg in legs}
    assert all(leg["arrive"] < leg["leave"] for leg in legs)
    assert all(a["leave"] <= b["arrive"] for a, b in itertools.pairwise(legs))
    msgs = {m["machine"]: m for m in s["messages"]}
    assert msgs["B-F1"]["language"] == "mr" and msgs["B-F1"]["text_en"].startswith("Blower 1: sounds like")
    kinds = [e["kind"] for e in s["events"]]
    assert "route" in kinds and kinds.count("tool_call") == len(s["tool_calls"])

    r = client.post("/api/reply", json={"message_id": msgs["B-F1"]["id"], "reply": "हो"}).json()
    assert r == {"approved": True}
    assert lv.events[-1]["kind"] == "reply" and lv.events[-1]["approved"] is True
    assert client.post("/api/reply", json={"message_id": 999, "reply": "हो"}).status_code == 404


def test_publish_needs_label_and_replay_locks_actions(app, client):
    client.post("/api/step", json={"n": 1})
    assert client.post("/api/publish").status_code == 400
    app.state.live.replay["on"] = True
    assert client.post("/api/step", json={"n": 1}).status_code == 409
    assert client.post("/api/mode", json={"machine": "A-F1", "mode": "fault"}).status_code == 409
    assert client.post("/api/mode", json={"machine": "A-F1", "mode": "explode"}).status_code == 409
    app.state.live.replay["on"] = False
    assert client.post("/api/mode", json={"machine": "A-F1", "mode": "explode"}).status_code == 400
    assert client.post("/api/agent", json={"engine": "gpt"}).status_code == 400


def test_websocket_sends_state_then_frames(tmp_path):
    app = app_mod.create_app(tmp_path / "n.db", tmp_path / "r.db", stream=True)
    with TestClient(app) as c:
        c.post("/api/step", json={"n": 1})
        with c.websocket_connect("/ws") as ws:
            first = json.loads(ws.receive_text())
            assert first["type"] == "state" and first["state"]["tick"] == 0
            seen = {json.loads(ws.receive_text())["type"] for _ in range(3)}
            assert "frame" in seen
    # nothing in the stream is raw audio: a frame is 64 numbers per machine plus two scalars
    f = app.state.live.frame()
    assert all(set(m) == {"risk", "energy", "spec"} for m in f["machines"].values())


def test_replay_script(tmp_path, monkeypatch):
    monkeypatch.setattr(folk, "transcribe", lambda path, language=None, **k: (NOTE, language))
    lv = app_mod.Live(tmp_path / "n.db", tmp_path / "r.db")
    asyncio.run(lv.run_replay(tick_s=0))
    kinds = [e["kind"] for e in lv.events]
    fault = next(e for e in lv.events if e["kind"] == "mode" and e["machine"] == "A-F1")
    assert fault["mode"] == "fault"
    readings_before = [e for e in lv.events[: lv.events.index(fault)] if e["kind"] == "reading"]
    assert len(readings_before) == app_mod.REPLAY_FAULT_AT  # 0:40 at 10 s per reading
    assert kinds.index("label") < kinds.index("published") < kinds.index("digest")
    assert lv.replay["phase"] == "awaiting owner" and lv.replay["on"] is False
    onset = next(e for e in lv.events if e["kind"] == "mode" and e["machine"] == "B-F1")
    assert onset["synthetic"] is True


def test_fingerprint_waits_in_outbox_while_shed_is_offline(app, client):
    lv = app.state.live
    client.post("/api/step", json={"n": 2})
    client.post("/api/mode", json={"machine": "A-F1", "mode": "fault"})
    client.post("/api/step", json={"n": 3})
    client.post("/api/label", json={"text": NOTE})
    client.post("/api/link", json={"up": False})
    pub = client.post("/api/publish").json()
    assert pub["published"] == [] and pub["pending"] == 1
    assert lv.c.reg.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0] == 0
    assert client.get("/api/state").json()["link"] == {"up": False, "pending": 1}
    back = client.post("/api/link", json={"up": True}).json()
    assert back["pending"] == 0 and back["published"][0]["machine"] == "A-F1"
    assert [e["kind"] for e in lv.events].count("published") == 1
    assert lv.c.reg.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0] == 1
