"""Quiet hours, power cuts, the daily digest, templates and reply buttons (spec section 3)."""

from datetime import datetime

import pytest

from server import diagnosis
from server.notify import policy, service
from server.notify import whatsapp as wa

PROFILE = {"quiet_hours": ["21:00", "07:00"], "power_cuts": [["14:00", "16:00"]], "digest_at": "19:00"}


@pytest.mark.parametrize(
    "sev,now,want",
    [
        ("stop_now", "2026-11-16 23:30", ("send", None)),  # stop-level always goes, even at night
        ("replace", "2026-11-16 15:00", ("send", None)),
        ("repair_soon", "2026-11-16 23:30", ("hold", "2026-11-17 07:00")),  # quiet hours (wraps midnight)
        ("repair_soon", "2026-11-16 14:30", ("hold", "2026-11-16 16:00")),  # power cut
        ("repair_soon", "2026-11-16 10:00", ("send", None)),
        ("watch", "2026-11-16 10:00", ("digest", "2026-11-16 19:00")),
        ("watch", "2026-11-16 20:00", ("digest", "2026-11-17 19:00")),
    ],
)
def test_policy(sev, now, want):
    got = policy.decide(sev, datetime.fromisoformat(now), PROFILE)
    assert (got[0], got[1] and got[1].strftime("%Y-%m-%d %H:%M")) == want


def test_every_owner_language_has_buttons_digest_head_and_templates():
    for lang in diagnosis.LANGS:
        assert len(policy.BUTTONS[lang]) == 3 and all(len(b) <= 20 for b in policy.BUTTONS[lang])
        assert lang in policy.DIGEST_HEAD and lang in wa.META_LANG
    assert set(policy.TEMPLATES.values()) | {policy.DIGEST_TEMPLATE} == {
        "alert_watch", "alert_repair", "alert_stop", "daily_digest"}  # fmt: skip


def test_buttons_round_trip_as_choices():
    p = wa.interactive_payload("+91 98765 43210", "body", policy.BUTTONS["hi_latn"])
    ids = [b["reply"]["id"] for b in p["interactive"]["action"]["buttons"]]
    assert (
        ids == ["1", "2", "3"]
        and p["interactive"]["action"]["buttons"][0]["reply"]["title"] == "Mechanic bulao"
    )
    assert [service.parse_choice(i) for i in ids] == ["book", "later", "details"]
    for lang, titles in policy.BUTTONS.items():  # a typed title means the same as tapping it
        assert [service.parse_choice(t) for t in titles] == ["book", "later", "details"], lang
    reply = {"entry": [{"changes": [{"value": {"messages": [{"from": "91", "id": "x", "type": "interactive",
             "interactive": {"button_reply": {"id": "1", "title": "Mechanic bulao"}}}]}}]}]}  # fmt: skip
    assert wa.parse_meta(reply)[0]["text"] == "1"


def test_meta_uses_templates_only_when_enabled(monkeypatch):
    sent = []

    class Http:
        def post(self, url, json=None, **k):
            sent.append(json)
            return type(
                "R",
                (),
                {
                    "status_code": 200,
                    "json": lambda s: {"messages": [{"id": "w"}]},
                    "raise_for_status": lambda s: None,
                },
            )()

    m = wa.MetaCloud("t", "1", client=Http())
    monkeypatch.setattr(wa.env, "get", lambda k, d=None: None)
    m.send("+91 98765 43210", "hello", [], lang="gu", template="alert_repair", buttons=policy.BUTTONS["gu"])
    assert sent[-1]["type"] == "interactive"
    monkeypatch.setattr(wa.env, "get", lambda k, d=None: "1" if k == "WA_TEMPLATES" else None)
    m.send("+91 98765 43210", "hello", [], lang="gu", template="alert_repair", buttons=policy.BUTTONS["gu"])
    assert sent[-1]["type"] == "template" and sent[-1]["template"]["name"] == "alert_repair"
    assert sent[-1]["template"]["language"]["code"] == "gu"


def test_gujarati_alert():
    m = {"id": "A-G1", "type": "gearbox", "status": "alarm", "days_at_fault": 0,
         "forecast": {"status": "estimate", "low": 3, "mid": 5, "high": 9, "crossings": [3, 5, 9]}}  # fmt: skip
    c = diagnosis.card(m, "A", "bearing_wear", 2, 1.5, "gu")
    text = diagnosis.message(c, "Loom 1", 3, "gu")
    assert "મોટરનું બેરિંગ ઘસાઈ રહ્યું છે" in text and "1 = મિસ્ત્રી બોલાવો" in text


def test_held_alert_is_released_and_watch_goes_to_the_digest(tmp_path):
    import sqlite3

    class Rec:
        name = "whatsapp_meta"

        def __init__(self):
            self.sent = []

        def send(self, to, text, media, lang="en", **kw):
            self.sent.append({"to": to, "text": text, **kw})
            return {"provider": self.name, "ids": ["w"]}

    rec = Rec()
    n = service.Notifier(sqlite3.connect(tmp_path / "a.db"), rec, rec, tmp_path / "o.db")
    n.db.row_factory = sqlite3.Row
    t_end = datetime.fromisoformat("2026-11-17 07:00").timestamp()
    held = n.send(machine="A-P1", factory="A", severity="repair_soon", to="+91 1", lang="hi", body="b",
                  template="alert_repair", hold_until=t_end)  # fmt: skip
    assert held["status"] == "held" and rec.sent == []
    assert n.release(t_end - 60) == [] and n.release(t_end) == [held["id"]]
    assert rec.sent[-1]["template"] == "alert_repair" and rec.sent[-1]["buttons"] == policy.BUTTONS["hi"]
    w = n.send(machine="A-P2", factory="A", severity="watch", to="+91 1", lang="hi", body="w", digest=True)
    assert w["status"] == "digest" and [a["id"] for a in n.pending_digest()] == [w["id"]]
