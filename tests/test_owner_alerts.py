"""The live server's alert path on real data: Demo mode replays the NASA IMS bearing failure as Loom 1;
its alarm -> diagnosis card -> WhatsApp alert with media -> signed webhook reply -> booking."""

import hashlib
import hmac
import json

import pytest

from tests.conftest import OWNER_A, ims_world, needs_ims

pytestmark = needs_ims
DEMO_TO = "+91 90000 44444"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    yield ims_world(tmp_path_factory.mktemp("alerts"), mp)
    mp.undo()


def signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def wa_reply(phone: str, text: str) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
        {"from": phone.replace("+", "").replace(" ", ""), "id": "x", "type": "text", "text": {"body": text}}]}}]}]}  # fmt: skip


def test_the_real_bearing_failure_sends_a_whatsapp_alert_with_media(world):
    _, client, live, rec = world
    assert live.demo["alerted"] and live.demo["phase"].startswith("bearing failed")
    to_a = [s for s in rec.sent if s["to"] == OWNER_A]
    loom = [s for s in to_a if "लूम 1" in s["text"]]
    assert loom, "factory A's owner gets the Loom 1 alert"
    a = loom[0]
    assert a["lang"] == "hi" and "1 = मिस्त्री बुलाओ" in a["text"] and a["text"].startswith("⚠ ")
    assert "बेयरिंग" in a["text"]  # the classifier's bearing fault maps to the bearing-wear card
    assert a["media"] == ["audio/ogg", "image/png"]
    alerts = client.get("/api/state").json()["alerts"]
    assert all(
        x["status"] == "no_contact" for x in alerts if x["factory"] in ("B", "C")
    )  # no numbers on file
    assert any(x["machine"] == "A-G1" and x["status"] == "sent" for x in alerts)


def test_diagnosis_endpoint(world):
    _, client, *_ = world
    d = client.get("/api/diagnosis/A-G1?lang=en").json()
    assert d["card"]["fault"] == "bearing_wear" and d["card"]["named_by"] == "classifier"
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
    raw = json.dumps(wa_reply(OWNER_A, "1")).encode()
    assert (
        client.post(
            "/webhooks/whatsapp", content=raw, headers={"Content-Type": "application/json"}
        ).status_code
        == 403
    )


def test_unclear_reply_does_not_book(world):
    _, client, live, _ = world
    client.post("/api/alerts/reply", json={"phone": OWNER_A, "text": "kal dekhenge"})
    assert next(m for m in live.messages() if m["machine"] == "A-G1")["approved"] != 1


def test_details_then_book_and_no_machine_is_touched(world):
    _, client, live, rec = world
    modes = {m: live.c.mode(m) for m in live.c.machines}
    raw, headers = signed(wa_reply(OWNER_A, "3"))
    handled = client.post("/webhooks/whatsapp", content=raw, headers=headers).json()["handled"]
    assert handled[0]["choice"] == "details"
    assert (
        rec.sent[-1]["to"] == OWNER_A and "मरम्मत हो सकती है:" in rec.sent[-1]["text"]
    )  # labels in the owner's language
    assert next(m for m in live.messages() if m["machine"] == "A-G1")["approved"] is None
    raw, headers = signed(wa_reply(OWNER_A, "1"))
    assert (
        client.post("/webhooks/whatsapp", content=raw, headers=headers).json()["handled"][0]["choice"]
        == "book"
    )
    assert next(m for m in live.messages() if m["machine"] == "A-G1")["approved"] == 1  # the visit is booked
    assert {m: live.c.mode(m) for m in live.c.machines} == modes  # and nothing else changed


def test_demo_number_only_hears_about_the_demo_machine(tmp_path):
    """The judge's number is attached to Loom 1 only: other machines' (false) alarms never reach it."""
    mp = pytest.MonkeyPatch()
    try:
        _, _, live, rec = ims_world(tmp_path, mp, to=DEMO_TO, lang="en")
        to_judge = [a for a in live.alerts.notifier.recent(50) if a["to_phone"] == DEMO_TO]
        assert to_judge and {a["machine"] for a in to_judge} == {"A-G1"}
        assert all(a["lang"] == "en" for a in to_judge)
        assert all(s["to"] != DEMO_TO or "Loom 1" in s["text"] for s in rec.sent)
        live.reset()  # a new replay must not show the previous one's alerts
        assert live.alerts.notifier.recent(50) == []
    finally:
        mp.undo()


def test_demo_endpoint_accepts_every_owner_language():
    """The Demo mode form offers every owner language the knowledge base has; the API must accept them all."""
    from server import diagnosis
    from server.app import DemoIn

    for lang in diagnosis.LANGS:
        assert DemoIn(on=True, to=None, lang=lang).lang == lang


def test_voice_note_becomes_a_weak_label_on_the_alerted_machine(world, monkeypatch):
    from server import folk

    _, client, live, rec = world
    monkeypatch.setattr(rec, "download", lambda audio: b"OggS-fake", raising=False)
    monkeypatch.setattr(
        folk, "transcribe", lambda path, language=None, **k: ("मोटर में खट-खट की आवाज़ है", "hi")
    )
    body = {"entry": [{"changes": [{"value": {"messages": [{"from": OWNER_A.replace("+", "").replace(" ", ""),
            "id": "v1", "type": "audio", "audio": {"id": "M1", "mime_type": "audio/ogg"}}]}}]}]}  # fmt: skip
    raw, headers = signed(body)
    handled = client.post("/webhooks/whatsapp", content=raw, headers=headers).json()["handled"]
    assert handled and handled[0]["machine"] == "A-G1" and handled[0]["label"]["sound"] == "khat-khat"
    assert any(e["kind"] == "owner_voice" for e in live.events)
