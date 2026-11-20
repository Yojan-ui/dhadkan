"""Voice notes (server/voice.py) and voice in the owner's loop. A fake engine stands in for the TTS models,
so nothing heavy loads; the real engines are measured by scripts/tts_eval.py (docs/runs/tts_*.json)."""

import json
from pathlib import Path

import numpy as np
import pytest

from server import voice

SR = 16000


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    """A 'TTS' that makes 0.1 s of tone per character, and remembers what it was asked to say."""
    said: list[str] = []

    def say(text):
        said.append(text)
        n = int(0.1 * SR * max(1, len(text) // 10))
        return (0.1 * np.sin(np.arange(n) / 8)).astype(np.float32), SR

    monkeypatch.setenv("DHADKAN_VOICE", "1")
    monkeypatch.setattr(voice, "CACHE", tmp_path / "cache")
    for e in ("fake", "mms-fake"):
        monkeypatch.setitem(voice._engines, e, say)
    for lg in ("HI", "MR", "GU", "EN", "TA"):
        monkeypatch.setenv(f"DHADKAN_TTS_{lg}", "fake")
    return said


ALERT_HI = ("जल्दी रिपेयर · लूम 1 – मोटर का बेयरिंग घिस रहा है। अभी ठीक हो सकता है: बेयरिंग 6205, ~₹315-680, 1-2 घंटा। "
            "जवाब दें: 1 = मिस्त्री बुलाओ, 2 = बाद में, 3 = पूरी जानकारी। (अनुमानित दाम और वारंटी, डीलर से पक्का करें।)")  # fmt: skip


def test_the_spoken_form_reads_money_and_ranges_as_people_say_them():
    assert voice.spoken("~₹315-680, 1-2 घंटा", "hi") == "लगभग 315 से 680 रुपये , 1 से 2 घंटा"
    assert voice.spoken("~₹1,315-2,680", "en") == "about 1315 to 2680 rupees"


def test_mms_gets_numbers_as_words_and_piper_keeps_digits():
    assert voice.speakable("315 रुपये", "mr", "mms-mr") == "तीन एक पाच रुपये"
    assert voice.speakable("6205", "gu", "mms-gu") == "છ બે શૂન્ય પાંચ"
    assert (
        voice.speakable("156960 rupees", "en", "mms-en")
        == "one lakh fifty six thousand nine hundred sixty rupees"
    )
    assert voice.speakable("315 रुपये", "hi", "piper-hi-priyamvada") == "315 रुपये"


def test_a_voice_note_is_made_from_the_template_text_and_cached(fake):
    v = voice.note("alert_repair", {"machine": "A-G1", "text": ALERT_HI}, "hi", ALERT_HI)
    assert v["path"] and not v["cached"] and v["engine"] == "fake" and v["seconds"] <= voice.MAX_SECONDS
    assert Path(v["path"]).read_bytes()[:4] == b"OggS"  # OGG / Opus: what WhatsApp plays as a voice note
    assert (
        "मोटर का बेयरिंग घिस रहा है" in v["spoken"] and "डीलर से पक्का करें" in v["spoken"]
    )  # template + disclaimer
    n = len(fake)
    again = voice.note("alert_repair", {"machine": "A-G1", "text": ALERT_HI}, "hi", ALERT_HI)
    assert again["cached"] and len(fake) == n  # a repeated alert is a file read
    other = voice.note("alert_repair", {"machine": "A-G1", "text": ALERT_HI}, "mr", ALERT_HI)
    assert other["path"] != v["path"]  # cached per (template, values, language, engine)


def test_the_cap_keeps_what_is_wrong_and_the_disclaimer_and_drops_the_reply_options_first():
    parts = [{"text": "first", "seconds": 10, "reply": False, "disclaimer": False},
             {"text": "price", "seconds": 10, "reply": False, "disclaimer": False},
             {"text": "reply 1 2 3", "seconds": 8, "reply": True, "disclaimer": False},
             {"text": "typical, confirm with dealer", "seconds": 6, "reply": False, "disclaimer": True}]  # fmt: skip
    kept = [p["text"] for p in voice.keep_within(parts, 30)]
    assert kept == ["first", "price", "typical, confirm with dealer"]
    kept = [p["text"] for p in voice.keep_within(parts, 18)]
    assert kept == ["first", "typical, confirm with dealer"]


def test_hi_latn_hears_hindi_and_voice_can_be_switched_off(fake, monkeypatch):
    assert voice.engine_for("hi_latn") == voice.engine_for("hi")
    v = voice.note("alert_repair", {"text": ALERT_HI}, "hi_latn", ALERT_HI)
    assert v["lang"] == "hi"
    monkeypatch.setenv("DHADKAN_VOICE", "0")
    assert voice.note("alert_repair", {"text": "x"}, "hi", "x") is None


def test_a_broken_engine_never_blocks_the_alert(fake, monkeypatch):
    def broken(text):
        raise RuntimeError("model missing")

    monkeypatch.setitem(voice._engines, "fake", broken)
    v = voice.note("alert_repair", {"text": ALERT_HI}, "hi", ALERT_HI)
    assert v["path"] is None and "model missing" in v["error"]


# ---------------------------------------------------------------- in the owner's loop
@pytest.fixture()
def world(tmp_path, monkeypatch, fake):
    from tests.conftest import ims_world

    return ims_world(tmp_path, monkeypatch)


def test_owner_alerts_and_the_digest_carry_a_voice_note(world):
    _app, _c, live, rec = world
    alerts = [x for x in rec.sent if "audio/ogg" in x["media"]]
    assert alerts, rec.sent[:2]
    row = live.alerts.notifier.recent(20)
    voiced = [a for a in row if any(m.get("voice") for m in a["media"])]
    assert voiced and all(m["mime"] == "audio/ogg" for a in voiced for m in a["media"] if m.get("voice"))
    # the digest: a watch-level alert waits for it, then goes out with its own voice note
    a = voiced[0]
    live.alerts.notifier.send(machine=a["machine"], factory=a["factory"], severity="watch", to=a["to_phone"],
                              lang=a["lang"], body="Loom 1: dhyan do.", digest=True)  # fmt: skip
    now = live.c.now().replace(hour=23, minute=0)
    ids = live.alerts.digest(now)
    d = live.alerts.notifier.alert(ids[0])
    assert d["kind"] == "digest" and any(m.get("voice") for m in d["media"])


def test_the_suno_button_reads_the_alert_aloud(world):
    _app, c, _live, _rec = world
    r = c.get("/api/voice/machine/A-G1.ogg?lang=hi")
    assert r.status_code == 200 and r.content[:4] == b"OggS" and r.headers["x-voice-engine"] == "fake"
    assert c.get("/api/voice/note/../../etc.ogg").status_code == 404


def test_a_question_by_voice_is_answered_as_text_and_a_template_voice(world, monkeypatch):
    _app, _c, live, _rec = world
    from server import folk
    from tests.conftest import OWNER_A

    monkeypatch.setattr(folk, "transcribe", lambda path, language=None, **k: ("Loom 1 kaisa hai?", "hi"))
    got = live.alerts.on_audio(OWNER_A, b"RIFF....", "audio/wav")
    assert got["question"] == "Loom 1 kaisa hai?" and got["reply"] and got["voice"]
    sent = live.alerts.notifier.alert(got["alert"])
    assert sent["kind"] == "chat" and any(m.get("voice") for m in sent["media"])
    ev = [e for e in live.events if e["kind"] == "owner_voice_question"][-1]
    assert ev["reply_source"] in ("granite", "template")
    # a spoken command: the commands answer it (and band karo still calls no tool)
    monkeypatch.setattr(folk, "transcribe", lambda path, language=None, **k: ("machine band karo", "hi"))
    stop = live.alerts.on_audio(OWNER_A, b"RIFF....", "audio/wav")
    assert "faisla" in stop["reply"] or "फ़ैसला" in stop["reply"]


# ---------------------------------------------------------------- Granite replies: language and grounding
def test_a_granite_reply_in_the_wrong_language_gives_way_to_the_template(tmp_path, monkeypatch):
    from tests.test_planner_core import Script, make_world, text, turn

    w = make_world(tmp_path, monkeypatch)
    template = {"reply": "Flour mill 1 की हालत ठीक है।"}
    p = w.make(Script(turn(text("The machine looks fine."))), rules_reply=lambda ev: template)
    out = p.handle(w.bus.emit("owner_reply", machine="C-G1", factory="C", idem="chat:1",
                              payload={"text": "फ्लोर मिल 1 कैसी है?", "channel": "chat", "lang": "hi"}))  # fmt: skip
    assert out["reply"] == template["reply"] and out["engine"].endswith("+template")
    src = [r["detail"] for r in w.store.audit() if r["event"] == "reply_source"][-1]
    assert src["source"] == "template" and "wrong language" in src["why"] and src["lang_got"] == "en"
    # in the owner's language and grounded: Granite's reply is used
    p2 = w.make(Script(turn(text("फ्लोर मिल 1 की आवाज़ सामान्य है।"))), rules_reply=lambda ev: template)
    out = p2.handle(w.bus.emit("owner_reply", machine="C-G1", factory="C", idem="chat:2",
                               payload={"text": "फ्लोर मिल 1 कैसी है?", "channel": "chat", "lang": "hi"}))  # fmt: skip
    assert out["reply"] == "फ्लोर मिल 1 की आवाज़ सामान्य है।"
    assert [r["detail"]["source"] for r in w.store.audit() if r["event"] == "reply_source"][-1] == "granite"
    # switched off: always the template
    monkeypatch.setenv("DHADKAN_GRANITE_REPLIES", "0")
    p3 = w.make(Script(turn(text("फ्लोर मिल 1 की आवाज़ सामान्य है।"))), rules_reply=lambda ev: template)
    out = p3.handle(w.bus.emit("owner_reply", machine="C-G1", factory="C", idem="chat:3",
                               payload={"text": "फ्लोर मिल 1 कैसी है?", "channel": "chat", "lang": "hi"}))  # fmt: skip
    assert out["reply"] == template["reply"]


def test_the_evaluation_record_exists_and_names_a_pick_per_language():
    runs = sorted(Path("docs/runs").glob("tts_*.json"))
    assert runs
    rec = json.loads(runs[-1].read_text(encoding="utf-8"))
    assert {"hi", "mr", "gu", "en"} <= set(rec["picked"]) and rec["picked"]["hi"]["engine"] == voice.CHOICE[
        "hi"
    ]
    assert rec["engines"]["ai4bharat-indicf5"]["status"].startswith("not evaluated")
