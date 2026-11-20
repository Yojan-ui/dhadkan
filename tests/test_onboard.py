"""Per-factory setup (spec section 6): presets, profile questions, WhatsApp onboarding, baseline clips."""

import json
import sqlite3

import pytest

from server import diagnosis, onboard

PHONE = "+91 98765 00001"


@pytest.fixture
def ob(tmp_path):
    return onboard.Onboarding(sqlite3.connect(tmp_path / "o.db"), root=tmp_path / "factories")


def test_presets_cover_the_four_factory_types_and_every_language():
    p = onboard.presets()
    assert set(p["presets"]) == {"powerloom", "flour_mill", "pump_set", "machine_shop"}
    faults = set(diagnosis.load()["faults"])
    for preset in p["presets"].values():
        assert set(preset["name"]) == set(diagnosis.LANGS)
        for m in preset["machines"]:
            assert (
                set(m["faults"]) <= faults
                and set(m["name"]) == set(diagnosis.LANGS)
                and set(m["parts"]) == set(diagnosis.LANGS)
            )
    assert p["district_language"]["surat"] == "gu"
    # every question exists in every owner language (the language menu itself is multilingual)
    for step in onboard.STEPS:
        if step != "language":
            assert set(onboard.Q[step]) == set(diagnosis.LANGS), step


def test_namaste_to_learning_in_roman_hindi(ob, tmp_path):
    assert ob.handle(PHONE, "kya haal") is None  # not onboarding, not a greeting: ignored
    assert "Choose your language" in ob.handle(PHONE, "Namaste")
    assert "shehar" in ob.handle(PHONE, "5")
    assert "Kaisi factory" in ob.handle(PHONE, "Bhiwandi")
    assert "Kitne Loom" in ob.handle(PHONE, "1")  # powerloom
    assert "Maaf kijiye" in ob.handle(PHONE, "bahut")  # unclear: asked again
    answers = ["6", "2", "Sunday", "14-16", "Oct-Dec", "0", "60000", "12", "0", "+91 98765 00002", "0"]
    for a in answers:
        q = ob.handle(PHONE, a)
    assert "Loom 1 ke paas" in q and "30 second" in q
    for _ in range(6):
        q = ob.clip(PHONE, b"OggS", ".ogg")
    assert "din 1/7" in q and not ob.active(PHONE)
    prof = json.loads((tmp_path / "factories" / "919876500001.json").read_text(encoding="utf-8"))
    assert prof["language"] == "hi_latn" and prof["district_default_language"] == "hi_latn"
    assert (
        prof["shifts"] == 2
        and prof["power_cuts"] == [["14:00", "16:00"]]
        and prof["output_per_day_inr"] == 60000
    )
    assert prof["contacts"] == {"supervisor": None, "mechanic": "+919876500002", "dealer": None}
    assert len(prof["machines"]) == 6 and all(
        m["learning_day"] == 1 and m["model"] == "gearbox" for m in prof["machines"]
    )
    assert (tmp_path / "factories" / "919876500001" / "loom-1_baseline.ogg").exists()


def test_flour_mill_asks_for_each_machine_kind(ob):
    ob.handle(PHONE, "नमस्ते")
    ob.handle(PHONE, "1")  # Hindi
    ob.handle(PHONE, "Kanpur")
    assert "चक्की" in ob.handle(PHONE, "2")
    assert "ब्लोअर" in ob.handle(PHONE, "1")  # one chakki, then: how many blowers
    assert "शिफ्ट" in ob.handle(PHONE, "1")


@pytest.mark.skipif(
    not (onboard.ROOT / "edge" / "models" / "ims_demo_pack.npz").exists(), reason="needs models"
)
def test_webhook_onboards_a_new_number_without_touching_alerts(tmp_path, monkeypatch):
    import hashlib
    import hmac

    from tests.conftest import ims_world

    mp = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(onboard, "FACTORIES", tmp_path / "f")
        _, client, live, rec = ims_world(tmp_path, mp)
        live.alerts.onboard.root = tmp_path / "f"
        body = {"entry": [{"changes": [{"value": {"messages": [{"from": "919876500009", "id": "n1", "type": "text",
                "text": {"body": "Namaste"}}]}}]}]}  # fmt: skip
        raw = json.dumps(body).encode()
        sig = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        out = client.post(
            "/webhooks/whatsapp",
            content=raw,
            headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
        )
        assert out.json()["handled"][0]["onboarding"] and "Choose your language" in rec.sent[-1]["text"]
        assert live.alerts.onboard.active("919876500009")
    finally:
        mp.undo()
