"""Warranty / insurance from the machine's own papers (server/coverage.py), offline and with a fake Claude."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from server import coverage as cv
from server.config import DATA
from server.notify import service, whatsapp

ON = date(2026, 11, 19)
LOOM = {"name": "Loom 1", "name_local": "लूम 1", "type": "gearbox", "motor_hp": 2}
PUMP = {"name": "Pump 1", "name_local": "पंप 1", "type": "pump", "motor_hp": 5}
LANGS = ("en", "hi", "mr", "hi_latn", "gu")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DHADKAN_SAMPLE_DOCS", raising=False)
    return cv.DocStore(tmp_path / "machine_docs")


def test_rule_reader_gets_the_warranty_card_fields():
    text = (DATA / "sample_docs" / "A-G1" / "warranty_card.txt").read_text(encoding="utf-8")
    f = cv.read_rules(text, "warranty_card.txt")
    assert f["kind"] == "warranty"
    assert f["provider"] == "Shree Ganesh Looms Pvt. Ltd."
    assert f["number"] == "SGL/WC/25/0817"
    assert f["serial_no"] == "SGL56-2025-04471"  # the machine's, not the motor's (first one wins)
    assert (f["start"], f["end"]) == ("2025-08-12", "2027-08-11")  # 24 months from purchase
    assert any("bearings" in c for c in f["covers"])
    assert any("V-belts" in x for x in f["excludes"])
    assert (
        f["claim_within_hours"] == 7 * 24
    )  # "Report a problem within 7 days", not the engineer's 72 h visit
    assert f["contact"]["phone"].startswith("1800-000-4471")
    assert len(f["claim_steps"]) == 4


def test_rule_reader_gets_the_policy_fields():
    text = (DATA / "sample_docs" / "A-G1" / "insurance_policy.txt").read_text(encoding="utf-8")
    f = cv.read_rules(text, "insurance_policy.txt")
    assert f["kind"] == "insurance"
    assert (f["start"], f["end"]) == ("2026-04-01", "2027-03-31")
    assert f["deductible_inr"] == 5000 and f["max_claim_inr"] == 180000
    assert f["claim_within_hours"] == 48
    assert f["contact"]["email"] == "claims@suraksha-gi.example"


@pytest.mark.parametrize(
    ("raw", "iso"),
    [("12-08-2025", "2025-08-12"), ("12/08/2025", "2025-08-12"), ("12 Aug 2025", "2025-08-12"),
     ("2025-08-12", "2025-08-12"), ("12th August 2025", "2025-08-12"), ("soon", None)],
)  # fmt: skip
def test_dates_are_read_day_first(raw, iso):
    assert cv.parse_date(raw) == iso


def test_bearing_wear_on_loom_1_is_claimable_under_warranty_and_says_dont_open(store):
    cov = cv.check(store, "A-G1", LOOM, "bearing_wear", [2000, 2400], on=ON)
    assert cov["verdict"] == "claimable"
    assert cov["best"]["kind"] == "warranty" and cov["best"]["why"] == "Motor bearings and gearbox bearings"
    assert cov["saves_inr"] == [2000, 2400]  # a warranty pays the whole repair
    assert cov["dont_open"]  # "void if opened by an unauthorised person"
    # the policy excludes wear and tear, so it is not the paper to claim on...
    ins = next(j for j in cov["judgements"] if j["kind"] == "insurance")
    assert ins["status"] == "excluded" and "wear and tear" in ins["why"]
    # ...but if the owner waits and the motor burns, that breakdown is the insurer's
    assert cov["if_ignored"]["kind"] == "insurance" and cov["if_ignored"]["deductible_inr"] == 5000


def test_specific_exclusion_beats_everything(store):
    cov = cv.check(store, "A-G1", LOOM, "belt_slip", [300, 900], on=ON)
    assert cov["verdict"] == "not_covered" and "V-belts" in cov["best"]["why"]


def test_a_defect_only_cover_is_a_check_and_the_policy_pays_the_burnt_winding(store):
    cov = cv.check(store, "A-G1", LOOM, "motor_winding", [2500, 6500], on=ON)
    by_kind = {j["kind"]: j["status"] for j in cov["judgements"]}
    assert by_kind["warranty"] == "conditional"  # "winding failure due to a manufacturing defect"
    assert cov["verdict"] == "claimable" and cov["best"]["kind"] == "insurance"
    assert cov["saves_inr"] == [0, 1500]  # above the Rs 5,000 deductible only
    assert "up to ₹1,500" in cv.line(cov, "en")


def test_below_the_deductible_says_repair_now(store):
    cov = cv.check(store, "B-G1", LOOM, "motor_winding", [2000, 4000], on=ON)
    assert cov["below_deductible"]
    assert "won't pay" in cv.line(cov, "en")
    assert "4 = " not in cv.alert_text(cov, "en")


def test_expired_and_missing_papers(store):
    cov = cv.check(store, "A-P1", PUMP, "bearing_wear", [500, 900], on=ON)
    assert cov["verdict"] == "not_covered" and cov["best"]["status"] == "expired"
    assert "04-06-2025" in cv.line(cov, "en")
    none = cv.check(store, "A-F1", {"name": "Blower 1", "type": "fan"}, "bearing_wear", None, on=ON)
    assert none["verdict"] == "no_docs" and "upload" in cv.line(none, "en")
    early = cv.check(store, "A-G1", LOOM, "motor_winding", None, on=date(2026, 1, 1))
    ins = next(j for j in early["judgements"] if j["kind"] == "insurance")
    assert ins["status"] == "not_started"


@pytest.mark.parametrize("lang", LANGS)
def test_owner_words_in_every_alert_language(store, lang):
    cov = cv.check(store, "A-G1", LOOM, "bearing_wear", [2000, 2400], on=ON)
    words = cv.alert_text(cov, lang)
    assert words.startswith("✅") and "1800-000-4471" in words and "₹5,000" in words
    assert "4 =" in words  # reply 4 = claim help
    if lang in ("hi", "mr", "gu"):
        assert not any(w in words for w in ("covers this", "likely claimable"))  # not English


def test_claim_help_has_steps_contact_and_a_letter(store):
    cov = cv.check(store, "A-G1", LOOM, "bearing_wear", [2000, 2400], on=ON)
    text = cv.claim_help(
        cov, LOOM, "hi", "motor bearing wear", "sound rising for 3 days.", "Sunil Shinde", "Shinde Works"
    )
    assert text.startswith("लूम 1 के लिए क्लेम मदद:")
    assert "Call the service helpline" in text and "service@sgl-looms.example" in text
    assert "Warranty card no.: SGL/WC/25/0817" in text and "serial no. SGL56-2025-04471" in text
    assert "has not been opened" in text and text.rstrip().endswith("Sunil Shinde, Shinde Works")


def test_samples_seed_once_dedupe_and_delete(store):
    assert len(store.list("A-G1")) == 0
    cv.check(store, "A-G1", LOOM, None, on=ON)
    docs = store.list("A-G1")
    assert {d["kind"] for d in docs} == {"warranty", "insurance", "invoice"}
    assert all(d["read_by"] == "rules" and d["sample"] for d in docs)
    again = store.add("A-G1", LOOM, (DATA / "sample_docs/A-G1/invoice.txt").read_bytes(), "copy.txt")
    assert again["id"] in {d["id"] for d in docs}  # same bytes: the same paper
    w = next(d for d in docs if d["kind"] == "warranty")
    assert store.delete("A-G1", w["id"]) and store.file("A-G1", w["id"]) is None
    cv.check(store, "A-G1", LOOM, "bearing_wear", None, on=ON)  # a deleted sample does not come back
    assert "warranty" not in {d["kind"] for d in store.list("A-G1")}
    with pytest.raises(ValueError):
        store.add("A-G1", LOOM, b"MZ...", "virus.exe")


def test_no_sample_docs_when_turned_off(store, monkeypatch):
    monkeypatch.setenv("DHADKAN_SAMPLE_DOCS", "0")
    assert cv.check(store, "A-G1", LOOM, "bearing_wear", None, on=ON)["verdict"] == "no_docs"


def test_unknown_fault_lists_papers_in_force(store):
    cov = cv.check(store, "A-G1", LOOM, None, on=ON)
    assert cov["verdict"] == "unknown_fault"
    assert "till 11-08-2027" in cv.line(cov, "en") and "तक" in cv.line(cov, "hi")


# ---------------------------------------------------------------- Claude reads the paper (fake client)
class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, out=None, stop="end_turn", fail=False):
        self.out, self.stop, self.fail, self.requests = out, stop, fail, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kw):
        self.requests.append(kw)
        if self.fail:
            raise ConnectionError("offline")
        usage = SimpleNamespace(
            input_tokens=1200, output_tokens=300, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        content = [SimpleNamespace(type="text", text=json.dumps(self.out))]
        return FakeStream(
            SimpleNamespace(stop_reason=self.stop, content=content, usage=usage, model=kw["model"])
        )


PHOTO_FIELDS = {
    "kind": "warranty", "provider": "Kisan Motors", "number": "KM-77", "serial_no": "K-1", "machine": "2 HP motor",
    "start": "2026-02-01", "end": "2027-01-31", "covers": ["Motor bearings"], "excludes": [], "conditions": [],
    "claim_steps": ["Call us"], "deductible_inr": None, "max_claim_inr": None, "claim_within_hours": None,
    "contact": {"name": None, "phone": "+91 90000 00077", "email": None},
    "quotes": [{"field": "end", "quote": "Valid till 31/01/2027"}],
}  # fmt: skip


def test_claude_reads_a_photo_with_a_schema_and_a_document_block(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "LOG", tmp_path / "calls.jsonl")
    fake = FakeClient(PHOTO_FIELDS)
    got = cv.read_document(b"\xff\xd8 jpeg", "card.jpg", LOOM, client=fake)
    assert got["read_by"] == "claude" and got["fields"]["end"] == "2027-01-31"
    req = fake.requests[0]
    assert req["model"] == "claude-fable-5-2" and req["fallbacks"] == "default"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["messages"][0]["content"][0]["type"] == "image"
    pdf = cv.build_extract_request(b"%PDF-1.4", "application/pdf", "p.pdf", LOOM)
    assert pdf["messages"][0]["content"][0]["source"]["media_type"] == "application/pdf"


def test_offline_or_refusal_falls_back_to_the_rules(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "LOG", tmp_path / "calls.jsonl")
    text = (DATA / "sample_docs" / "B-G1" / "insurance_policy.txt").read_bytes()
    assert cv.read_document(text, "p.txt", LOOM, client=FakeClient(fail=True))["read_by"] == "rules"
    assert (
        cv.read_document(text, "p.txt", LOOM, client=FakeClient(PHOTO_FIELDS, stop="refusal"))["read_by"]
        == "rules"
    )
    photo = cv.read_document(b"\xff\xd8 jpeg", "card.jpg", LOOM, client=FakeClient(fail=True))
    assert photo["read_by"] == "none" and "ANTHROPIC_API_KEY" in photo["warnings"][0]


def test_without_a_key_no_call_is_made(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cv.read_claude(cv.build_extract_request(b"x", "text/plain", "a.txt", LOOM)) is None


# ---------------------------------------------------------------- WhatsApp: "4" and a photo of the card
def test_reply_4_means_claim_help():
    assert service.parse_choice("4") == "claim" and service.parse_choice("क्लेम") == "claim"
    assert service.parse_choice("3") == "details"


def test_incoming_photo_or_pdf_is_a_paper():
    meta = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919000011111", "id": "wamid.1", "type": "document",
         "document": {"id": "M1", "mime_type": "application/pdf", "filename": "policy.pdf", "caption": "Loom 1"}}]}}]}]}  # fmt: skip
    got = whatsapp.parse_meta(meta)
    assert got[0]["paper"] == {
        "id": "M1",
        "mime": "application/pdf",
        "filename": "policy.pdf",
        "caption": "Loom 1",
    }
    tw = whatsapp.parse_twilio({"From": "whatsapp:+919000011111", "NumMedia": "1", "MediaUrl0": "https://x/y",
                                "MediaContentType0": "image/jpeg", "Body": ""})  # fmt: skip
    assert tw[0]["paper"]["mime"] == "image/jpeg" and tw[0]["text"] is None


# ---------------------------------------------------------------- the API (no recordings needed)
def test_docs_api_upload_list_claim_delete(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server import app as app_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = app_mod.create_app(tmp_path / "node.db", tmp_path / "registry.db", stream=False)
    c = TestClient(app)
    r = c.get("/api/docs/A-G1?lang=hi").json()
    assert (
        len(r["documents"]) == 3 and r["coverage"]["verdict"] == "unknown_fault" and "चालू कागज़" in r["text"]
    )
    body = (DATA / "sample_docs" / "B-G1" / "insurance_policy.txt").read_bytes()
    up = c.post(
        "/api/docs/A-F1", files={"file": ("policy.txt", body, "text/plain")}, data={"kind": "insurance"}
    )
    assert up.status_code == 200 and up.json()["document"]["kind"] == "insurance"
    doc_id = up.json()["document"]["id"]
    assert c.get(f"/api/docs/A-F1/{doc_id}/file").content == body
    assert (
        c.post("/api/docs/A-F1", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code
        == 400
    )
    assert c.get("/api/claim/A-G1?lang=en").json()["text"].startswith("Claim help for Loom 1")
    assert c.delete(f"/api/docs/A-F1/{doc_id}").json() == {"deleted": doc_id}
    assert c.get("/api/docs/NOPE").status_code == 404


# ---------------------------------------------------------------- wired into the alert (no recordings needed)
class Recorder:
    name = "whatsapp_meta"

    def __init__(self):
        self.sent = []

    def send(self, to, text, media, lang="en", **kw):
        self.sent.append({"to": to, "text": text, "lang": lang})
        return {"provider": self.name, "ids": [f"wamid.{len(self.sent)}"]}


@pytest.fixture
def alerts(tmp_path, monkeypatch):
    """A live server whose Loom 1 card is a real diagnosis card (bearing wear, repair soon)."""
    from server import app as app_mod
    from server import diagnosis
    from server.owner_alerts import OwnerAlerts

    for k in ("ANTHROPIC_API_KEY", "WA_TOKEN", "TWILIO_ACCOUNT_SID", "DHADKAN_SAMPLE_DOCS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cv, "today_ist", lambda: ON)
    app = app_mod.create_app(tmp_path / "node.db", tmp_path / "registry.db", stream=False)
    live = app.state.live
    rec = Recorder()
    live.alerts = OwnerAlerts(live, tmp_path / "node.db", providers=(rec, rec))

    def card(self, mid, lang=None):
        m = live.c.machines[mid]
        row = {"id": mid, "type": m["type"], "status": "alarm", "known_fault": None,
               "forecast": {}, "days_at_fault": 0}  # fmt: skip
        out = diagnosis.card(row, m["factory"], "bearing_wear", m.get("motor_hp", 2), 8, lang or "hi")
        out["days_rising"] = 3
        out["coverage"] = self.coverage(mid, out)
        return out

    monkeypatch.setattr(OwnerAlerts, "card", card)
    monkeypatch.setattr(OwnerAlerts, "media", lambda self, mid: [])
    return live, rec


def test_alert_text_carries_the_warranty_line_in_the_owners_language(alerts):
    live, _ = alerts
    hi = live.alerts.text("A-G1", "hi")
    assert "लूम 1" in hi and "✅ वारंटी (Shree Ganesh Looms Pvt. Ltd., 11-08-2027 तक)" in hi
    assert "न खुलवाएँ" in hi and "4 = क्लेम मदद" in hi
    assert len(hi) < 900  # fits the Twilio trial template slot
    assert "Not covered" not in live.alerts.text("A-G1", "en")
    assert "📄" in live.alerts.text("A-F1", "mr")  # Blower 1 has no papers: ask for them
    assert "वारंटी" in live.alerts.details("A-G1", "hi").splitlines()[-1]


def test_owner_replies_4_and_gets_claim_help_then_1_still_books(alerts):
    live, rec = alerts
    phone = "+91 90000 11111"
    sent = live.alerts.send_for("A-G1", to=phone, lang="hi")
    assert sent["status"] in ("sent", "queued", "held", "digest") or sent
    got = live.alerts.on_reply(phone, "4")
    assert got["choice"] == "claim"
    claim = rec.sent[-1]["text"]
    assert claim.startswith("लूम 1 के लिए क्लेम मदद") and "Sunil Shinde, Shinde Engineering Works" in claim
    assert live.alerts.decide("A-G1", "claim")["text"].startswith("लूम 1")


def test_agent_tool_answers_from_the_papers(alerts):
    from server import owner_agent as oa

    live, _ = alerts
    t = oa.AgentTools(live, "run-test", "ta")
    cov = t.call("check_warranty_insurance", {"machine_id": "A-G1"})
    assert cov["coverage"]["verdict"] == "claimable" and cov["owner_words"].startswith("✅ Warranty")
    docs = t.call("read_machine_documents", {"machine_id": "A-G1"})
    assert {d["kind"] for d in docs["documents"]} == {"warranty", "insurance", "invoice"}
    assert "surveyor visits" in " ".join(d["text"] for d in docs["documents"])  # the agent can quote it


def test_a_whatsapp_photo_is_filed_on_the_alerted_machine(alerts, monkeypatch, tmp_path):
    live, rec = alerts
    monkeypatch.setattr(cv, "LOG", tmp_path / "calls.jsonl")
    phone = "+91 90000 11111"
    live.alerts.send_for("A-F1", to=phone, lang="hi")
    body = (DATA / "sample_docs" / "B-G1" / "insurance_policy.txt").read_bytes()
    got = live.alerts.on_paper(phone, body, {"mime": "text/plain", "filename": "policy.txt", "caption": None})
    assert got["machine"] == "A-F1" and got["kind"] == "insurance"
    assert rec.sent[-1]["text"].startswith("ब्लोअर 1 का बीमा कागज़ सेव कर लिया।")
