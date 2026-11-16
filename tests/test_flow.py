"""Real WhatsApp flow (server/factory/flow.py, server/wa_agent.py): who gets what per stage, the owner's 1,
mechanic + dealer, the repair recording, reminders and the Twilio webhook. Nothing is sent (dry run)."""

import base64
import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from server import wa_agent
from server.factory import flow as flow_mod
from tests.conftest import ims_world, needs_ims

OWNER = "+91 98765 00007"
URL = "https://dhadkan.example"


def twilio_sig(token: str, url: str, form: dict[str, str]) -> str:
    s = url + "".join(k + form[k] for k in sorted(form))
    return base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode()


def post(client, form: dict[str, str]):
    return client.post("/whatsapp/twilio", content=urlencode(form), headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Twilio-Signature": twilio_sig("tw-token", URL + "/whatsapp/twilio", form)})  # fmt: skip


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from server import prove

    mp = pytest.MonkeyPatch()
    d = tmp_path_factory.mktemp("flow")
    mp.setenv("OWNER_PHONE", "whatsapp:" + OWNER.replace(" ", ""))
    mp.setenv("PUBLIC_BASE_URL", URL)
    mp.setattr(prove, "KEYS", d / "keys")
    mp.setattr(prove, "LABELS", d / "labels.jsonl")
    w = ims_world(d, mp)  # clears every live credential; the webhook's signing token is set after it
    mp.setenv("TWILIO_TOKEN", "tw-token")
    yield w
    mp.undo()


def out_msgs(live, role=None):
    return [
        r
        for r in live.flow.timeline("A-G1")
        if r["direction"] == "out" and (role is None or r["role"] == role)
    ]


@needs_ims
def test_contacts_default_to_the_owner_phone(world):
    _, _, live, _ = world
    for role in ("owner", "supervisor", "mechanic", "dealer"):
        assert live.flow.contact(role)["phone"] == "whatsapp:+919876500007"


@needs_ims
def test_one_message_per_stage_to_the_right_roles(world):
    _, _, live, _ = world
    assert live.c.machines["A-G1"]["name"] == "Loom 4"
    stages = [
        r["stage"] for r in live.c.db.execute("SELECT stage FROM wa_stages WHERE machine='A-G1' ORDER BY ts")
    ]
    assert stages and set(stages) <= set(flow_mod.ORDER) and len(stages) == len(set(stages))
    for st in stages:
        roles = [m["role"] for m in out_msgs(live) if m["stage"] == st and m["kind"] in ("agent", "template")]
        assert sorted(roles) == sorted(flow_mod.ROLES_FOR[st]), st  # one per role per stage
    owner = [m for m in out_msgs(live, "owner") if m["stage"] in ("repair_soon", "stop_now", "replace")]
    assert owner and all(
        len(m["text"]) <= 700 and m["text"].rstrip().endswith(wa_agent.OPTIONS) for m in owner
    )
    assert all("Loom 4" in m["text"] and "Shed A" in m["text"] for m in owner)


@needs_ims
def test_owner_1_brings_mechanic_and_dealer_then_repair_is_verified(world, monkeypatch):
    from server import whatsapp

    _, client, live, _ = world
    phone = "whatsapp:+919876500007"
    assert post(client, {"From": phone, "Body": "1"}).status_code == 200
    assert any(
        m["approved"] == 1 for m in live.messages() if m["machine"] == "A-G1"
    )  # booked by the owner's 1
    mech = out_msgs(live, "mechanic")[-1]["text"]
    deal = out_msgs(live, "dealer")[-1]["text"]
    assert (
        "6205" in mech
        and "bearing puller" in mech
        and "Bhosari MIDC" in mech
        and "Slot:" in mech
        and "1 bhejein" in mech
    )
    assert "6205" in deal and "reserve" in deal
    post(client, {"From": phone, "Body": "1"})  # the mechanic accepts (latest open question)
    post(client, {"From": phone, "Body": "1"})  # the dealer reserves
    assert "Shukriya" in out_msgs(live, "dealer")[-1]["text"]
    post(client, {"From": phone, "Body": "done"})  # the mechanic reports the repair
    assert "60 second" in out_msgs(live, "owner")[-1]["text"]
    monkeypatch.setattr(whatsapp, "download", lambda url: b"OggS-recording")
    post(
        client,
        {
            "From": phone,
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/m",
            "MediaContentType0": "audio/ogg",
        },
    )
    assert out_msgs(live, "owner")[-1]["text"].startswith("[Owner] Theek ho gaya")


@needs_ims
def test_bad_signature_is_refused(world):
    _, client, *_ = world
    r = client.post("/whatsapp/twilio", content="From=x&Body=1", headers={
        "Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": "nope"})  # fmt: skip
    assert r.status_code == 403


@needs_ims
def test_reminder_after_no_reply(world, monkeypatch):
    _, _, live, _ = world
    live.flow.send("A-G1", "owner", "[Owner] test decision " + wa_agent.OPTIONS, stage="repair_soon")
    monkeypatch.setattr(flow_mod, "REMIND_AFTER_S", -1)
    assert live.flow.tick() and "Yaad dila rahe hain" in out_msgs(live, "owner")[-1]["text"]
    assert live.flow.tick() == []  # only one reminder


def test_agent_refuses_invented_numbers_and_templates_fill_the_gap():
    sent: list[tuple[str, str, str]] = []

    class FakeFlow:
        def contact(self, role):
            return {"language": "hi_latn"}

        def display(self, mid):
            return {"name": "Loom 4", "shed": "Shed A"}

        def health(self, mid):
            return {"time_left": "2-5 days to fault level", "summary": "alarm"}

        def diagnosis(self, mid):
            return {"fault": "bearing_wear", "fix_now_inr_incl_labour": [315, 680], "summary": "bearing_wear"}

        def log(self, *a, **k):
            pass

        def send(self, mid, role, text, stage=None, by="flow"):
            sent.append((role, text, by))
            return {"status": "dry_run"}

        def template(self, mid, role, stage):
            return f"[{role}] template"

    calls = []
    bad = "[Owner] Loom 4: bearing. Abhi ₹999 lagenge. " + wa_agent.OPTIONS
    good = "[Owner] Loom 4: bearing ghis raha hai, 2-5 din. Abhi ₹315-680. " + wa_agent.OPTIONS
    turns = [
        SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", id="a", name="diagnose_fault", input={"machine_id": "A-G1"}),
                                                         SimpleNamespace(type="tool_use", id="h", name="get_machine_health", input={"machine_id": "A-G1"})]),
        SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", id="b", name="send_whatsapp", input={"role": "owner", "text": bad})]),
        SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", id="c", name="send_whatsapp", input={"role": "owner", "text": good})]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")]),
    ]  # fmt: skip

    def create(**kw):
        calls.append(kw)
        return turns[len(calls) - 1]

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    out = wa_agent.compose(FakeFlow(), "A-G1", "repair_soon", {"owner", "supervisor"}, client=client)
    assert calls[0]["model"] == "claude-opus-5-5" and "thinking" not in calls[0]
    assert out["owner"] == good and ("owner", good, "agent") in sent  # the ₹999 text was refused, never sent
    assert not any(t == bad for _, t, _ in sent)
    assert ("supervisor", "[supervisor] template", "template") in sent  # Claude skipped it: the template went


@needs_ims
def test_any_message_gets_the_current_alert_as_twiml_and_1_books(world, monkeypatch):
    from server import whatsapp

    for k, v in {"TWILIO_SID": "AC1", "TWILIO_WA_FROM": "whatsapp:+17372508034"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DHADKAN_DRY_RUN", "0")
    monkeypatch.setattr(
        whatsapp, "_client", lambda *a: pytest.fail("a reply must go back as TwiML, not the API")
    )
    whatsapp._pending["whatsapp:+919876500007"] = ["stale queued message"]
    r = post(world[1], {"From": "whatsapp:+919876500007", "Body": "hi"})
    assert r.status_code == 200 and "stale" not in r.text
    assert "Loom 4 (Shed A)" in r.text and wa_agent.OPTIONS in r.text and r.text.count("<Message>") == 1
    r = post(world[1], {"From": "whatsapp:+919876500007", "Body": "1"})
    assert (
        "[Mechanic]" in r.text and "[Dealer]" in r.text
    )  # booked: mechanic + dealer go back in the same reply


@needs_ims
def test_owner_whatsapp_matches_the_phone_screen(world):
    _, client, live, _ = world
    live.flow.on_reading("A-G1", background=False)  # catch up if the fault was named after the stage message
    card = client.get("/api/diagnosis/A-G1?lang=hi_latn").json()["card"]
    what = (card.get("what") or card.get("unknown"))["hi_latn"]
    msg = [
        m
        for m in out_msgs(live, "owner")
        if m["kind"] in ("agent", "template", "update")
        and m["stage"] in ("repair_soon", "stop_now", "replace")
    ][-1]
    last = msg["text"]
    from server import diagnosis as kb

    # the label of the stage the message is about, from the same faults.yaml labels the phone screen shows
    assert what in last and kb.load()["severity"]["labels"][msg["stage"]]["hi_latn"] in last
    # the rupee figure is live on the phone and a snapshot in WhatsApp (sent once per stage); same line, same source
    assert not live.flow.risk_line("A-G1") or "khatre mein (2 din)" in last


@needs_ims
def test_phone_screen_gets_the_label_whatsapp_alerted_at(world):
    _, client, live, _ = world
    from server import diagnosis as kb

    top = max(
        (r[0] for r in live.c.db.execute("SELECT stage FROM wa_stages WHERE machine='A-G1'")),
        key=flow_mod.ORDER.index,
    )
    got = client.get("/api/diagnosis/A-G1?lang=hi_latn").json()["alerted"]
    assert got == {"level": top, "label": kb.load()["severity"]["labels"][top]["hi_latn"]}
