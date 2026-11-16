"""WhatsApp payloads and webhooks, SMS fallback, offline queue, replies, escalation, no-stop guardrail."""

import base64
import hashlib
import hmac
import io
import json
import sqlite3

import httpx
import numpy as np
import pytest
import soundfile as sf

from server.notify import media
from server.notify import whatsapp as wa
from server.notify.service import ESCALATE_AFTER_S, Notifier, parse_choice

TO = "+91 98765-43210"


# ---------------------------------------------------------------- payloads
def test_text_and_template_payloads():
    p = wa.text_payload(TO, "hello")
    assert p == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": "919876543210",
                 "type": "text", "text": {"preview_url": False, "body": "hello"}}  # fmt: skip
    t = wa.template_payload(TO, "dhadkan_alert", "hi", ["बेयरिंग"])
    assert t["type"] == "template" and t["template"]["language"] == {"code": "hi"}
    assert t["template"]["components"][0]["parameters"] == [{"type": "text", "text": "बेयरिंग"}]
    assert wa.media_payload(TO, "audio", "MID")["audio"] == {"id": "MID"}
    with pytest.raises(ValueError):
        wa.text_payload("12345", "x")


def meta_client(calls, status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "nope"}})
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": f"media{len(calls)}"})
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(calls)}"}]})

    return httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer T"})


def test_meta_sends_text_then_uploaded_media(tmp_path):
    clip, png = tmp_path / "c.ogg", tmp_path / "s.png"
    clip.write_bytes(b"OggS...")
    png.write_bytes(b"\x89PNG...")
    calls: list[httpx.Request] = []
    m = wa.MetaCloud("T", "12345", "v21.0", client=meta_client(calls))
    res = m.send(TO, "body", [wa.Media(clip, "audio/ogg"), wa.Media(png, "image/png", "before | now")])
    paths = [c.url.path for c in calls]
    assert paths == ["/v21.0/12345/messages", "/v21.0/12345/media", "/v21.0/12345/messages",
                     "/v21.0/12345/media", "/v21.0/12345/messages"]  # fmt: skip
    assert json.loads(calls[0].content)["text"]["body"] == "body"
    assert json.loads(calls[2].content)["audio"] == {"id": "media2"}
    assert json.loads(calls[4].content)["image"] == {"id": "media4", "caption": "before | now"}
    assert res["provider"] == "whatsapp_meta" and len(res["ids"]) == 3
    assert all(c.headers["Authorization"] == "Bearer T" for c in calls)


@pytest.mark.parametrize("status, error", [(500, ConnectionError), (429, ConnectionError), (400, ValueError)])
def test_meta_errors(status, error):
    m = wa.MetaCloud("T", "1", client=meta_client([], status))
    with pytest.raises(error):
        m.send(TO, "x", [])


def test_twilio_whatsapp_form_and_public_media_only(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(201, json={"sid": "SM1"})

    t = wa.TwilioWhatsApp(
        "AC1", "tok", "whatsapp:+14155238886", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    t.send(TO, "body", [wa.Media(tmp_path / "x.ogg", "audio/ogg", public_url="https://h/x.ogg"),
                        wa.Media(tmp_path / "y.png", "image/png")])  # fmt: skip
    form = httpx.QueryParams(seen[0].content.decode())
    assert seen[0].url.path == "/2010-04-01/Accounts/AC1/Messages.json"
    assert form["To"] == "whatsapp:+919876543210" and form["From"] == "whatsapp:+14155238886"
    assert form.get_list("MediaUrl") == ["https://h/x.ogg"]  # Twilio can only fetch public media


# ---------------------------------------------------------------- webhooks
def test_meta_signature():
    body = b'{"object":"whatsapp_business_account"}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert wa.verify_meta_signature("secret", body, good)
    assert not wa.verify_meta_signature("secret", body + b" ", good)
    assert not wa.verify_meta_signature("secret", body, None)


def test_twilio_signature():
    url, params = "https://h.example/webhooks/twilio", {"From": "whatsapp:+919876543210", "Body": "1"}
    s = url + "Body1Fromwhatsapp:+919876543210"
    good = base64.b64encode(hmac.new(b"tok", s.encode(), hashlib.sha1).digest()).decode()
    assert wa.verify_twilio_signature("tok", url, params, good)
    assert not wa.verify_twilio_signature("tok", url, {**params, "Body": "2"}, good)


def test_parse_incoming():
    meta = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919876543210", "id": "a", "type": "text", "text": {"body": "1"}},
        {"from": "919876543210", "id": "b", "type": "button", "button": {"text": "बाद में"}},
        {"from": "919876543210", "id": "c", "type": "interactive", "interactive": {"button_reply": {"title": "3"}}},
        {"from": "919876543210", "id": "d", "type": "image"},
    ]}}]}]}  # fmt: skip
    assert [m["text"] for m in wa.parse_meta(meta)] == ["1", "बाद में", "3"]
    assert (
        wa.parse_twilio({"From": "whatsapp:+919876543210", "Body": "2", "MessageSid": "SM"})[0]["from"]
        == "+919876543210"
    )


@pytest.mark.parametrize(
    "text, choice",
    [("1", "book"), ("१", "book"), ("हाँ", "book"), ("2", "later"), ("बाद में", "later"), ("3", "details"),
     ("1 2", None), ("dekhte hain", None)],
)  # fmt: skip
def test_parse_choice(text, choice):
    assert parse_choice(text) == choice


# ---------------------------------------------------------------- service
class Flaky:
    """A provider whose network can be switched off."""

    def __init__(self, name):
        self.name, self.up, self.sent = name, True, []

    def send(self, to, text, media_, lang="en", **kw):
        if not self.up:
            raise ConnectionError("no network")
        self.sent.append({"to": to, "text": text, "media": len(media_)})
        return {"provider": self.name, "ids": [f"{self.name}-{len(self.sent)}"]}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def svc(tmp_path):
    db = sqlite3.connect(tmp_path / "node.db")
    db.row_factory = sqlite3.Row
    w, s, clock = Flaky("whatsapp_meta"), Flaky("sms_twilio"), Clock()
    return Notifier(db, w, s, tmp_path / "out.db", clock), w, s, clock


def send(n, **kw):
    base = {
        "machine": "A-G1",
        "factory": "A",
        "severity": "repair_soon",
        "to": TO,
        "lang": "hi",
        "body": "Loom 1 ...",
        "media": [],
    }
    return n.send(**{**base, **kw})


def test_whatsapp_first(svc):
    n, w, s, _ = svc
    a = send(n)
    assert a["status"] == "sent" and a["channel"] == "whatsapp_meta" and len(w.sent) == 1 and not s.sent


def test_falls_back_to_sms_when_whatsapp_is_down(svc):
    n, w, s, _ = svc
    w.up = False
    a = send(n)
    assert a["status"] == "sent" and a["channel"] == "sms_twilio" and s.sent[0]["media"] == 0


def test_queues_offline_and_sends_when_back(svc):
    n, w, s, clock = svc
    w.up = s.up = False
    a = send(n)
    assert a["status"] == "queued" and n.outbox.pending()
    w.up = True
    clock.t += 60
    n.retry()
    assert n.alert(a["id"])["status"] == "sent" and len(w.sent) == 1 and not n.outbox.pending()


def test_no_contact_is_logged_not_sent(svc):
    n, w, s, _ = svc
    a = send(n, to=None)
    assert a["status"] == "no_contact" and not w.sent and not s.sent


def test_dry_run_without_keys(tmp_path):
    db = sqlite3.connect(tmp_path / "n.db")
    db.row_factory = sqlite3.Row
    dry = wa.DryRun()
    n = Notifier(db, dry, wa.DryRun(), tmp_path / "o.db")
    a = send(n)
    assert a["status"] == "dry_run" and dry.sent[0]["to"] == TO


def test_reply_matches_the_latest_alert_for_that_number(svc):
    n, *_ = svc
    send(n, machine="A-P1")
    second = send(n, machine="A-G1")
    got = n.reply("919876543210", "1")
    assert got["alert"]["id"] == second["id"] and got["choice"] == "book"
    assert n.reply("+1 555 000 0000", "1") is None


def test_stop_now_escalates_once_after_two_hours(svc):
    n, w, _, clock = svc
    send(n, severity="stop_now", machine="A-G1")
    send(n, severity="repair_soon", machine="A-P1")  # not urgent: never escalated
    contact = lambda f: ("+91 90000 99999", "hi")  # noqa: E731
    assert n.escalate(contact, lambda r: "escalated: " + r["body"]) == []
    clock.t += ESCALATE_AFTER_S + 1
    made = n.escalate(contact, lambda r: "escalated: " + r["body"])
    assert len(made) == 1 and w.sent[-1]["to"] == "+91 90000 99999"
    assert n.escalate(contact, lambda r: "x") == []  # once
    # the supervisor's answer counts for the original alert too
    n.reply("+91 90000 99999", "1")
    first = n.recent()[-1]
    assert first["choice"] == "book"


def test_answered_stop_now_is_not_escalated(svc):
    n, _, _, clock = svc
    send(n, severity="stop_now")
    n.reply(TO, "2")
    clock.t += ESCALATE_AFTER_S + 1
    assert n.escalate(lambda f: (TO, "hi"), lambda r: "x") == []


# ---------------------------------------------------------------- media
def test_media_formats():
    rng = np.random.default_rng(0)
    before = rng.normal(0, 0.05, 160_000).astype(np.float32)
    now = before + 0.05 * np.sin(np.arange(160_000) * 2 * np.pi * 3000 / 16_000).astype(np.float32)
    clip = media.before_now_clip(before, now)
    assert clip[:4] == b"OggS"
    audio, sr = sf.read(io.BytesIO(clip))
    assert abs(len(audio) / sr - media.CLIP_S) < 0.1
    png = media.spectrogram_png(before, now, hot=(40, 45))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    short = media.before_now_clip(before[:16_000], now[:16_000])  # a 1 s snapshot is looped to fill 5 s
    assert abs(len(sf.read(io.BytesIO(short))[0]) / 16_000 - media.CLIP_S) < 0.1


def test_dry_run_switch_overrides_keys(monkeypatch):
    monkeypatch.setenv("WA_TOKEN", "t")
    monkeypatch.setenv("WA_PHONE_NUMBER_ID", "1")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "s")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "a")
    monkeypatch.setenv("TWILIO_SMS_FROM", "+1")
    assert wa.from_env()[0].name == "whatsapp_meta"
    monkeypatch.setenv("DHADKAN_DRY_RUN", "1")
    w, sms = wa.from_env()
    assert w.name == sms.name == "dry_run"


def test_voice_notes_are_parsed_from_both_providers():
    meta = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919876543210", "id": "w1", "type": "audio", "audio": {"id": "MEDIA1", "mime_type": "audio/ogg; codecs=opus"}}]}}]}]}  # fmt: skip
    got = wa.parse_meta(meta)
    assert got == [
        {
            "from": "919876543210",
            "text": None,
            "id": "w1",
            "audio": {"id": "MEDIA1", "mime": "audio/ogg; codecs=opus"},
        }
    ]
    form = {"From": "whatsapp:+919876543210", "Body": "", "MessageSid": "SM1", "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/m/1", "MediaContentType0": "audio/ogg"}  # fmt: skip
    t = wa.parse_twilio(form)[0]
    assert t["text"] is None and t["audio"] == {"url": "https://api.twilio.com/m/1", "mime": "audio/ogg"}
