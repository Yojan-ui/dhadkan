"""The live server's alert path: digest -> WhatsApp alert with media -> signed webhook reply -> booking."""

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from edge.config import MODELS
from server import app as app_mod
from server import folk
from server.owner_alerts import OwnerAlerts

pytestmark = pytest.mark.skipif(not (MODELS / "fan" / "model.tflite").exists(), reason="needs trained models")
NOTE = "ब्लोअर में खट-खट की आवाज़ आ रही है"
OWNER_B, SUPERVISOR_B = "+91 90000 11111", "+91 90000 22222"


class Recorder:
    name = "whatsapp_meta"

    def __init__(self):
        self.sent = []

    def send(self, to, text, media, lang="en"):
        self.sent.append({"to": to, "text": text, "media": [m.mime for m in media], "lang": lang})
        return {"provider": self.name, "ids": [f"wamid.{len(self.sent)}"]}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("DHADKAN_PHONE_B", OWNER_B)
    mp.setenv("DHADKAN_SUPERVISOR_B", SUPERVISOR_B)
    mp.setenv("WA_APP_SECRET", "test-secret")
    mp.setenv("WA_VERIFY_TOKEN", "verify-me")
    mp.setattr(folk, "transcribe", lambda path, language=None, **k: (NOTE, language))
    d = tmp_path_factory.mktemp("alerts")
    app = app_mod.create_app(d / "node.db", d / "registry.db", stream=False)
    live = app.state.live
    rec = Recorder()
    live.alerts = OwnerAlerts(live, d / "node.db", providers=(rec, rec))
    asyncio.run(live.run_replay(tick_s=0, seed_days=2))
    yield app, TestClient(app), live, rec
    mp.undo()


def signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def wa_reply(phone: str, text: str) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
        {"from": phone.replace("+", "").replace(" ", ""), "id": "x", "type": "text", "text": {"body": text}}]}}]}]}  # fmt: skip


def test_digest_sends_a_whatsapp_alert_with_media(world):
    _, client, _live, rec = world
    to_b = [s for s in rec.sent if s["to"] == OWNER_B]
    assert to_b, "factory B's owner gets the B-F1 alert"
    a = to_b[0]
    assert a["lang"] == "mr" and "1 = मिस्त्री बोलवा" in a["text"] and a["text"].startswith("⚠ ")
    assert a["media"] == ["audio/ogg", "image/png"]
    alerts = client.get("/api/state").json()["alerts"]
    factory_a = [x for x in alerts if x["factory"] == "A"]
    assert factory_a and all(x["status"] == "no_contact" for x in factory_a)  # no number on file for A
    assert all(x["status"] == "sent" for x in alerts if x["factory"] == "B")


def test_diagnosis_endpoint(world):
    _, client, *_ = world
    d = client.get("/api/diagnosis/B-F1?lang=en").json()
    assert d["card"]["severity"]["level"] in ("watch", "repair_soon", "stop_now", "replace")
    assert d["text"].startswith("⚠ ")


def test_webhook_verification_and_signature(world):
    _, client, *_ = world
    ok = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"},
    )
    assert ok.status_code == 200 and ok.text == "42"
    assert (
        client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"},
        ).status_code
        == 403
    )
    raw = json.dumps(wa_reply(OWNER_B, "1")).encode()
    assert (
        client.post(
            "/webhooks/whatsapp", content=raw, headers={"Content-Type": "application/json"}
        ).status_code
        == 403
    )


def test_details_then_book_and_no_machine_is_touched(world):
    _, client, live, rec = world
    modes = {m: live.c.mode(m) for m in live.c.machines}
    raw, headers = signed(wa_reply(OWNER_B, "3"))
    assert (
        client.post("/webhooks/whatsapp", content=raw, headers=headers).json()["handled"][0]["choice"]
        == "details"
    )
    assert rec.sent[-1]["to"] == OWNER_B and "Repairable" in rec.sent[-1]["text"]
    msg = next(m for m in live.messages() if m["machine"] == "B-F1")
    assert msg["approved"] is None
    raw, headers = signed(wa_reply(OWNER_B, "1"))
    assert (
        client.post("/webhooks/whatsapp", content=raw, headers=headers).json()["handled"][0]["choice"]
        == "book"
    )
    assert next(m for m in live.messages() if m["machine"] == "B-F1")["approved"] == 1  # the visit is booked
    assert {m: live.c.mode(m) for m in live.c.machines} == modes  # and nothing else changed


def test_only_one_books(world):
    _, client, live, _ = world
    a_msg = next(m for m in live.messages() if m["machine"] == "A-F1")
    live.alerts.notifier.db.execute(
        "UPDATE alerts SET to_phone=? WHERE message_id=?", ("+91 90000 33333", a_msg["id"])
    )
    live.alerts.notifier.db.commit()
    for text in ("2", "kal dekhenge"):
        client.post("/api/alerts/reply", json={"phone": "+91 90000 33333", "text": text})
    assert next(m for m in live.messages() if m["machine"] == "A-F1")["approved"] != 1
