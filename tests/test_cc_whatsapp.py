"""The command centre's agent on WhatsApp: its booking question reaches the owner's phone, the owner's "1" books
the factory's own mechanic, and nothing is sent unless switched on (never to the made-up directory numbers).
"""

import base64
import hashlib
import hmac

import pytest

from server import cc_whatsapp
from server import whatsapp as wa

from .test_command_centre import LEARN, app_for, step

OWNER_C, MECH_C, SUP_C = "+91 90000 33333", "+91 90000 44444", "+91 90000 55555"


@pytest.fixture
def learned(tmp_path):
    c = app_for(tmp_path)
    step(c, LEARN)
    return c, tmp_path


def _on(monkeypatch) -> list[dict]:
    sent: list[dict] = []
    for k, v in {"DHADKAN_CC_WHATSAPP": "1", "DHADKAN_PHONE_C": OWNER_C, "DHADKAN_MECHANIC_C": MECH_C,
                 "DHADKAN_SUPERVISOR_C": SUP_C, "TWILIO_SID": "AC1", "TWILIO_TOKEN": "tok",
                 "TWILIO_WA_FROM": "whatsapp:+14155238886"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)

    def fake(to, text, media_urls=None):
        sent.append({"to": wa.wa(to), "text": text})
        return {"sid": f"SM{len(sent)}", "status": "queued"}

    monkeypatch.setattr(wa, "send", fake)
    return sent


def _ask(c) -> str | None:
    assert c.post("/api/cc/scenario", json={"machine": "C-F2", "mode": "fault"}).json()["simulated"]
    for _ in range(8):
        step(c)
        for a in c.get("/api/cc/state").json()["approvals"]:
            if a["case_id"].startswith("C-F2") and a["kind"] == "booking":
                return a["id"]
    return None


def test_parse_replies():
    assert cc_whatsapp.parse("1") == ("approved", None)
    assert cc_whatsapp.parse("Haan 7c2a") == ("approved", "7C2A")
    assert cc_whatsapp.parse("2, later") == ("rejected", None)
    assert cc_whatsapp.parse("what is this?") == (None, None)


def test_booking_question_goes_to_the_owner_and_1_books_the_mechanic(learned, monkeypatch):
    c, _ = learned
    sent = _on(monkeypatch)
    aid = _ask(c)
    assert aid, "the blower's recorded fault should ask the owner"
    ask = [m for m in sent if m["to"] == wa.wa(OWNER_C) and "Reply 1 to book" in m["text"]]
    ref = cc_whatsapp.ref_for(aid)
    assert ask and f"(ref {ref})" in ask[-1]["text"] and "Blower" in ask[-1]["text"]
    assert not [m for m in sent if m["to"] == wa.wa(MECH_C)], "no mechanic before the owner's 1"

    brain = c.app.state.brain
    assert brain.wa.inbound(OWNER_C, "what?") is None  # not an answer: left for the other flow
    got = brain.wa.inbound(OWNER_C, "1", sid="SMin1")
    assert got and got["decision"] == "approved"
    a = brain.store.approval(aid)
    assert a["status"] == "approved" and a["channel"] == "whatsapp" and a["ref"] == "whatsapp:SMin1"
    step(c, 2)
    job = [m for m in sent if m["to"] == wa.wa(MECH_C)]
    assert job and "Dhadkan job" in job[0]["text"] and aid in job[0]["text"]
    assert any(m["to"] == wa.wa(OWNER_C) and "Booked" in m["text"] for m in sent)
    s = c.get("/api/cc/state").json()["whatsapp"]
    assert s["mode"] == "dry_run" and all("33333" not in (m["phone"] or "") for m in s["messages"])  # masked
    assert brain.wa.inbound(OWNER_C, "1") is None  # already decided: nothing left to answer


def test_twilio_webhook_routes_a_signed_reply_to_the_agent(learned, monkeypatch):
    c, _ = learned
    _on(monkeypatch)
    aid = _ask(c)
    assert aid
    form = {"From": wa.wa(OWNER_C), "Body": "2", "MessageSid": "SMx", "NumMedia": "0"}
    url = "http://testserver/whatsapp/twilio"
    sig = base64.b64encode(hmac.new(b"tok", (url + "".join(k + form[k] for k in sorted(form))).encode(),
                                    hashlib.sha1).digest()).decode()  # fmt: skip
    assert c.post("/whatsapp/twilio", data=form).status_code == 403  # unsigned
    r = c.post("/whatsapp/twilio", data=form, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200
    assert c.app.state.brain.store.approval(aid)["status"] == "rejected"


def test_off_by_default_and_capped(learned, monkeypatch):
    c, _ = learned
    sent = _on(monkeypatch)
    monkeypatch.delenv("DHADKAN_CC_WHATSAPP")
    assert _ask(c)
    assert sent == []
    brain = c.app.state.brain
    assert brain.wa.mode() == "off" and {m["status"] for m in brain.wa.messages()} == {"off"}
    monkeypatch.setenv("DHADKAN_CC_WHATSAPP", "1")
    for i in range(cc_whatsapp.CAP + 2):
        brain.wa.send(
            "notify", {"to": "owner", "text": f"t{i}", "machine": "C-F2", "idem": "C-F2#9:notify:x"}
        )
    assert len(sent) == cc_whatsapp.CAP
    assert brain.wa.messages(1)[0]["status"] == "held_rate_cap"
