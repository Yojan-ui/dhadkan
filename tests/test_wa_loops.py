"""WhatsApp loops (server/wa_loops.py and the planner's investigation): the owner's measurements, the
mechanic's job, the owner's commands, all through the same path the webhook takes (/api/wa/simulate posts
into it). Dry-run provider, synthetic fan clips (they test the plumbing, not detection on a real fan)."""

import base64
import io
import json
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from edge.config import MODELS, SR
from server import app as app_mod
from server.planner.tools import Env, ToolBox
from tests.test_live_recorder import clip, learn, reading

pytestmark = pytest.mark.skipif(
    not (MODELS / "fan" / "model.tflite").exists(), reason="needs trained models (python -m edge.train)"
)
MID = "A-F1"
PNG = base64.b64encode(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                                     "0000000a49444154789c6300010000050001")).decode()  # fmt: skip


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DHADKAN_DRY_RUN", "1")  # nothing leaves the machine; demo numbers (+0 ...) are used
    for k in ("DHADKAN_PHONE_A", "DHADKAN_MECHANIC_A", "DHADKAN_AGENT_WHATSAPP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("server.wa_loops.PHOTOS", tmp_path / "photos")
    monkeypatch.setattr("server.owner_alerts.ALERT_DIR", tmp_path / "alerts")
    app = app_mod.create_app(tmp_path / "node.db", tmp_path / "registry.db", stream=False)
    c = TestClient(app)
    c.post("/api/ingest/live/start", json={"machine": MID, "device": "phone-1", "quick": True})
    return app, c


def say(c, role, text=None, **media):
    r = c.post("/api/wa/simulate", json={"role": role, "factory": "A", "text": text, **media})
    assert r.status_code == 200, r.text
    return r.json()


def thread(c, who):
    return [m["text"] for m in c.get("/api/wa/phones?factory=A").json()[f"{who}_thread"]]


def wav_b64(b64_pcm: str) -> str:
    pcm = np.frombuffer(base64.b64decode(b64_pcm), dtype="<i2").astype(np.float32) / 32768
    buf = io.BytesIO()
    sf.write(buf, pcm, SR, format="WAV")
    return base64.b64encode(buf.getvalue()).decode()


def open_case(app, c, rng):
    """Learn the fan's normal from live clips, then fault clips: the brain opens a case on its own."""
    t = learn(c, rng, time.time() - 600)
    for i in range(4):
        reading(c, 500 + i, clip(rng, fault=True), t + 10 * i)
    case = app.state.brain.store.open_case(MID)
    assert case is not None
    return case, t + 40


def toolbox(app):
    b = app.state.brain
    return ToolBox(Env(store=b.store, agent=b.agent, factory="A", lang="hi_latn", health_of=b.health.get))


# ---------------------------------------------------------------- investigation: asked -> answered -> confidence
def test_hand_check_answer_updates_the_confidence_and_reaches_the_planner(world):
    app, c = world
    brain = app.state.brain
    case, _ = open_case(app, c, np.random.default_rng(1))
    toolbox(app).t_request_measurement(MID, "", "hand_check")
    asked = thread(c, "owner")[-1]
    assert "garam hai? 1 = haan, 2 = nahi" in asked and "Blower 1" in asked
    say(c, "owner", "1")
    cur = brain.store.case(case["id"])
    m = cur["context"]["measurements"][-1]
    assert m["status"] == "received" and m["result"] == "hot"
    from server.planner.tools import CONFIDENCE_FROM, update_confidence

    start = CONFIDENCE_FROM[brain.health[MID]["confidence"]]  # from the health assessment (low: quick learn)
    assert m["confidence_before"] == start and m["confidence_after"] == update_confidence(start, "up")
    assert m["confidence_after"] == cur["context"]["fault_confidence"] > start
    assert update_confidence(0.55, "up") == 0.82  # the rule: 60% of the way to 1
    inv = [e for e in brain.store.events() if e["type"] == "investigation_result"]
    assert (
        inv
        and inv[-1]["status"] == "handled"
        and inv[-1]["payload"]["confidence_after"] == m["confidence_after"]
    )
    step = next(
        s
        for cs in brain.planner_trace(case["id"])["cases"]
        for s in cs["steps"]
        if s["event"] == "investigation_result"
    )
    assert step["investigation"]["answered"] == "hot" and step["investigation"]["confidence_before"] == start


def test_a_recording_sent_back_is_scored_like_a_reading(world):
    app, c = world
    brain = app.state.brain
    rng = np.random.default_rng(2)
    case, _ = open_case(app, c, rng)
    toolbox(app).t_request_measurement(MID, "", "audio")
    assert "10 sec ki awaaz record karo" in thread(c, "owner")[-1]
    say(c, "owner", audio_b64=wav_b64(clip(rng, fault=True)), mime="audio/wav")
    m = brain.store.case(case["id"])["context"]["measurements"][-1]
    assert m["status"] == "received" and m["risk"] >= 1.0 and m["confidence_after"] > m["confidence_before"]


def test_a_photo_is_stored_and_attached_to_the_case(world):
    app, c = world
    brain = app.state.brain
    case, _ = open_case(app, c, np.random.default_rng(3))
    toolbox(app).t_request_measurement(MID, "", "photo")
    assert "belt ki ek saaf photo bhejo" in thread(c, "owner")[-1]
    say(c, "owner", "yeh rahi", image_b64=PNG, mime="image/png")
    cur = brain.store.case(case["id"])
    assert (
        cur["context"]["photos"][-1]["by"] == "owner"
        and cur["context"]["measurements"][-1]["status"] == "received"
    )
    # no image model: the confidence is left as it was
    assert (
        cur["context"]["measurements"][-1]["confidence_after"]
        == cur["context"]["measurements"][-1]["confidence_before"]
    )


# ---------------------------------------------------------------- the mechanic's job
def approve_and_book(app):
    brain = app.state.brain
    case = brain.store.open_case(MID)
    with brain.lock:
        brain.agent.tick()
    aid = f"{case['id']}:booking"
    assert brain.store.approval(aid)["status"] == "requested"
    assert brain.desk.jobs(case["id"]) == []  # nothing goes to a mechanic before the owner's yes
    brain.decide(aid, "approved", by="owner", channel="whatsapp")
    with brain.lock:
        brain.agent.tick()
    return case


def test_the_whole_loop_from_approval_to_a_signed_record(world):
    app, c = world
    brain = app.state.brain
    rng = np.random.default_rng(4)
    open_case(app, c, rng)
    case = approve_and_book(app)
    job = brain.desk.jobs(case["id"])[-1]
    text = thread(c, "mechanic")[-1]
    assert (
        job["status"] == "sent"
        and "Blower 1" in text
        and "1 = Aaunga, 2 = Nahi aa sakta, 3 = Doosra samay" in text
    )
    assert "Saath laayein" in text and "Samay:" in text and "Bhosari" in text  # part, slot, address
    say(c, "mechanic", "1")
    assert brain.desk.jobs(case["id"])[-1]["status"] == "accepted"
    say(c, "mechanic", "raste mein hoon, 20 min")
    assert any(
        ("raste mein" in x or "रास्ते में" in x) and "20 min" in x for x in thread(c, "owner")
    )  # owner A: Hindi
    refused = say(c, "mechanic", "ho gaya: bearing badla")
    assert refused["handled"][0]["refused"] and "repair" not in brain.store.case(case["id"])["context"]
    say(c, "mechanic", image_b64=PNG, mime="image/png")
    done = say(c, "mechanic", "ho gaya: bearing badla, grease kiya")["handled"][0]
    assert done["repair_done"] and not done["simulated_recovery"]  # a live machine: real readings decide
    assert brain.store.case(case["id"])["context"]["repair"]["work"].startswith("bearing badla")
    for i in range(7):  # the owner re-records the repaired fan: normal clips again
        reading(c, 900 + i, clip(rng), time.time() + 10 * i)
    with brain.lock:
        brain.agent.tick()
    rec = [r for r in brain.store.audit(case["id"]) if r["event"] == "repair_record_signed"]
    assert rec, [r["event"] for r in brain.store.audit(case["id"])][-12:]
    stored = app.state.live.c.db.execute("SELECT record FROM repair_records ORDER BY id DESC").fetchone()[
        "record"
    ]
    assert (
        '"against_baseline_version": 1' in stored
        and job["case_id"] in stored
        and '"old_part_photo_sha256": "' in stored
    )
    v = json.loads(stored)["verification"]  # fault readings before the repair, normal ones after
    assert v["outcome"] == "fixed" and v["before_median_risk"] >= 1.0 > v["after_median_risk"]


def test_a_mechanic_declining_re_plans_without_asking_the_owner_again(world):
    app, c = world
    brain = app.state.brain
    open_case(app, c, np.random.default_rng(5))
    case = approve_and_book(app)
    first = brain.desk.jobs(case["id"])[-1]["technician"]
    say(c, "mechanic", "2")
    with brain.lock:
        brain.agent.tick()
    jobs = brain.desk.jobs(case["id"])
    assert (
        jobs[0]["status"] == "declined" and jobs[-1]["technician"] != first and jobs[-1]["status"] == "sent"
    )
    assert brain.store.approval(f"{case['id']}:booking")["status"] == "approved"  # same cost: not asked again
    assert first in brain.store.case(case["id"])["context"]["declined"]


def test_a_dearer_replacement_needs_the_owner_again(world, monkeypatch):
    app, c = world
    brain = app.state.brain
    open_case(app, c, np.random.default_rng(6))
    case = approve_and_book(app)
    first = brain.desk.jobs(case["id"])[-1]["technician"]
    for p in brain.agent.t.technicians.people:  # every other mechanic charges more than the approved visit
        if p["id"] != first:
            monkeypatch.setitem(p, "rate_inr_per_hour", 5000)
    say(c, "mechanic", "2")
    with brain.lock:
        brain.agent.tick()
    a = brain.store.approval(f"{case['id']}:booking")
    assert a["status"] == "requested" and a["note"] == "reopened: cost changed"
    assert len(brain.desk.jobs(case["id"])) == 1  # no new job until the owner says yes again


def test_no_answer_in_time_is_a_no_show_and_re_plans(world, monkeypatch):
    app, c = world
    brain = app.state.brain
    open_case(app, c, np.random.default_rng(7))
    case = approve_and_book(app)
    first = brain.desk.jobs(case["id"])[-1]["technician"]
    later = time.time() + 5 * 3600
    monkeypatch.setattr(brain.desk, "now", lambda: later)
    with brain.lock:
        assert brain.desk.tick() == [case["id"]]
        brain.bus.drain()
        brain.agent.tick()
    jobs = brain.desk.jobs(case["id"])
    assert jobs[0]["status"] == "no_reply" and jobs[-1]["technician"] != first
    assert any(e["type"] == "mechanic_no_show" for e in brain.store.events())


def test_another_time_keeps_the_mechanic_on_a_different_day(world):
    app, c = world
    brain = app.state.brain
    open_case(app, c, np.random.default_rng(8))
    case = approve_and_book(app)
    first = brain.desk.jobs(case["id"])[-1]
    say(c, "mechanic", "3")
    with brain.lock:
        brain.agent.tick()
    jobs = brain.desk.jobs(case["id"])
    assert jobs[0]["status"] == "another_time" and jobs[-1]["status"] == "sent"
    assert (jobs[-1]["technician"], jobs[-1]["day"]) != (first["technician"], first["day"])


# ---------------------------------------------------------------- the owner's commands
def test_owner_commands_answer_from_stored_facts_and_band_karo_calls_no_tool(world):
    app, c = world
    brain = app.state.brain
    open_case(app, c, np.random.default_rng(9))
    case = approve_and_book(app)
    st = say(c, "owner", "status")["handled"][0]
    assert st["command"] == "status" and "Blower 1" in st["reply"]
    cost = say(c, "owner", "kitna kharcha?")["handled"][0]
    assert cost["command"] == "cost" and "Rs" in cost["reply"]
    when = say(c, "owner", "mistri kab aayega?")["handled"][0]
    assert when["command"] == "mechanic" and brain.desk.jobs(case["id"])[-1]["day"] in when["reply"]
    sent_before = len(brain.transport.sent)
    stop = say(c, "owner", "machine band karo")["handled"][0]
    assert stop["command"] == "stop" and stop["tool_called"] is None
    assert "sirf aapka faisla" in stop["reply"] and len(brain.transport.sent) == sent_before
    assert any(r["event"] == "stop_requested_by_owner" for r in brain.store.audit())
    assert thread(c, "owner")[-1] == stop["reply"]


def test_simulated_phones_are_refused_on_a_real_provider(world, monkeypatch):
    app, c = world
    monkeypatch.setattr(type(app.state.live.alerts), "provider", property(lambda self: "twilio"))
    assert c.post("/api/wa/simulate", json={"role": "owner", "text": "status"}).status_code == 409


def test_photo_and_voice_messages_parse_for_both_providers():
    from server.notify import whatsapp

    meta = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919", "id": "w1", "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg", "caption": "belt"}}]}}]}]}  # fmt: skip
    got = whatsapp.parse_meta(meta)[0]
    assert got["image"] == {"id": "m1", "mime": "image/jpeg"} and got["text"] == "belt"
    tw = whatsapp.parse_twilio({"From": "whatsapp:+919", "Body": "purana part", "NumMedia": "1",
                                "MediaContentType0": "image/jpeg", "MediaUrl0": "https://x/1"})[0]  # fmt: skip
    assert tw["image"]["url"] == "https://x/1" and tw["text"] == "purana part"
