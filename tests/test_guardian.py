"""The Guardian (server/planner/guardian.py): what the model proposes is checked by code. No network."""

import pytest

from server import kb, provider
from server.planner import guardian
from server.planner.guardian import Facts, facts_from, review_text
from tests.test_planner_core import Script, alarm, make_world, sent, text, tool, turn


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(provider, "available", lambda model=None: False)


def rules(v):
    return [r["rule"] for r in v["reasons"]]


@pytest.mark.parametrize(
    "claim",
    [
        "I have stopped the machine to protect the bearing.",
        "We switched it off for now.",
        "Dhadkan has shut down the blower.",
        "Maine machine band kar diya hai.",
        "Humne pump ko off kar diya.",
        "मैंने मशीन बंद कर दी है।",
    ],
)
def test_g1_a_claim_of_controlling_a_machine_is_refused(claim):
    assert rules(review_text(claim, Facts(), check_numbers=False)) == ["no_control_claim"]


@pytest.mark.parametrize(
    "fine",
    [
        "Kya aap machine band karna chahenge? Faisla aapka.",
        "Please switch the machine off before you touch the bearing housing.",
        "The owner stopped the machine at 10:00.",
    ],
)
def test_g1_asking_or_reporting_the_owners_stop_is_fine(fine):
    assert review_text(fine, Facts(), check_numbers=False)["ok"]


def test_g1_reply_options_speak_for_the_owner():
    v = review_text("Machine band karni hai?", Facts(), check_numbers=False, labels="Maine band kar di")
    assert v["ok"]


def test_g2_only_retrieved_chunks_may_be_cited():
    f = Facts(retrieved={"kb/bearing_wear.md#symptoms"})
    assert review_text("Bearing check karein [kb/bearing_wear.md#symptoms].", f, check_numbers=False)["ok"]
    v = review_text("See [kb/bearing_wear.md#warranty-forever].", f, check_numbers=False)
    assert (
        rules(v) == ["unknown_citation"]
        and v["reasons"][0]["detail"] == "kb/bearing_wear.md#warranty-forever"
    )


def test_g3_a_fault_must_come_from_a_tool_or_a_retrieved_chunk():
    f = facts_from([{"result": {"fault": None, "likely_faults": ["bearing_wear", "imbalance"]}}])
    assert review_text(
        "Lagta hai bearing ghisna shuru hua hai (motor bearing ghisna).", f, check_numbers=False
    )["ok"]
    v = review_text("Motor winding kharabi hai, rewind karwana padega.", f, check_numbers=False)
    assert rules(v) == ["unsupported_fault"] and v["reasons"][0]["detail"] == "motor_winding"
    # a retrieved chunk about the fault makes it sayable
    card = next(c for c in kb.fault_chunks() if c["id"] == "faults.yaml#motor_winding")
    f2 = facts_from([{"result": {"chunks": kb.brief([card])}}])
    assert review_text("Motor winding kharabi ho sakti hai.", f2, check_numbers=False)["ok"]


def test_g4_an_external_agents_numbers_ground_nothing():
    external = {"result": {"untrusted": True, "agent": "dealer", "text": "6205 bearing ₹99 only"}}
    f = facts_from([external])
    v = review_text("Dealer ne kaha ₹99 mein milega.", f)
    assert "ungrounded_figure" in rules(v)
    card = next(c for c in kb.fault_chunks() if c["id"] == "faults.yaml#bearing_wear")
    f2 = facts_from([external, {"result": {"chunks": kb.brief([card])}}])
    assert review_text("Typical ₹65 se ₹180 (faults.yaml).", f2)["ok"]  # the KB's figure, not the agent's


def test_g5_paid_actions_need_a_logged_approval_of_their_kind(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    w.agent.observe("C-G1", {"status": "repair_soon", "urgency": "inspect", "severity": "marked",
                             "confidence": "high", "recent_risk": [1.5], "evidence": ["x"]})  # fmt: skip
    cid = w.store.open_case("C-G1")["id"]
    v = guardian.review_paid(
        w.store, w.agent, "order_part", {"case_id": cid, "part": "bearing", "dealer": "d"}
    )
    assert not v["ok"] and rules(v) == ["no_logged_approval"]
    w.store.request_approval(f"{cid}:purchase", cid, "purchase", "Buy a bearing")
    w.store.decide(f"{cid}:purchase", "approved", "owner", "whatsapp", "wa:7")
    v = guardian.review_paid(
        w.store, w.agent, "order_part", {"case_id": cid, "part": "bearing", "dealer": "d"}
    )
    assert v["ok"] and "wa" not in (v["basis"] or "")[:0] and "owner via whatsapp" in v["basis"]
    # an approved purchase is not an approved booking
    assert not guardian.review_paid(
        w.store, w.agent, "book_job", {"case_id": cid, "technician": "T1", "day": "x"}
    )["ok"]


def test_planner_message_claiming_a_stop_never_reaches_the_owner(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    client = Script(
        turn(tool("tell_owner", case_id="C-G1#1", text="Maine machine band kar di hai, chinta mat kijiye.")),
        turn(text("Owner ko bataya.")),
    )
    out = w.make(client).handle(alarm(w))
    assert out["calls"][0]["result"]["gate"] == "guardian"
    assert not any("band kar di" in x.get("text", "") for x in sent(w, "notify"))
    verdicts = [r["detail"] for r in w.store.audit() if r["event"] == "verdict"]
    assert any(v["target"] == "tell_owner" and not v["ok"] for v in verdicts)
    assert any(v["target"] == "reply" and v["ok"] for v in verdicts)  # every check is logged, passes too


def test_a_reply_citing_a_chunk_never_retrieved_falls_back_to_rules(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    client = Script(turn(text("Gearbox theek ho jayega [kb/gearbox_magic.md#free-repair].")))
    out = w.make(client).handle(alarm(w))
    d = w.store.decision(out["decision"])
    assert d["status"] == "guardian_rejected" and out["engine"].endswith("+rules")
    assert out["guardian"][-1]["target"] == "reply" and rules(out["guardian"][-1]) == ["unknown_citation"]
