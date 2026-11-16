"""The owner's agent (observe -> diagnose -> plan -> ask -> act -> verify) and the Digital Twin summary."""

from types import SimpleNamespace

import pytest

from server import owner_agent as oa
from tests.conftest import ims_world, needs_ims


def test_inr_lakh_crore_and_ist():
    assert oa.inr(156960) == "₹1,56,960 (1.57 lakh)"
    assert oa.inr(950) == "₹950" and oa.inr(23450000) == "₹2,34,50,000 (2.35 crore)"
    from datetime import datetime

    assert oa.ist(datetime(2026, 11, 16, 14, 30)).endswith("02:30 PM IST")


@pytest.mark.parametrize(
    "text,lang",
    [("Loom 4 ki awaaz kyun badal gayi?", "hi_latn"), ("लूम 4 की आवाज़ क्यों बदल गई?", "hi"),
     ("लूम 4 चा आवाज का बदलला आहे?", "mr"), ("લૂમ 4 નો અવાજ કેમ બદલાયો?", "gu"),
     ("லூம் 4 ஒலி ஏன் மாறியது?", "ta"), ("ಲೂಮ್ 4 ಶಬ್ದ ಏಕೆ ಬದಲಾಯಿತು?", "kn"),
     ("లూమ్ 4 శబ్దం ఎందుకు మారింది?", "te"), ("Why did loom 4 change its sound?", "en")],
)  # fmt: skip
def test_language_detection(text, lang):
    assert oa.detect_lang(text) == lang


def test_tools_are_strict_and_none_can_book_or_stop():
    names = {t["name"] for t in oa.TOOLS}
    assert names == {"get_machine_health", "get_spectrogram", "diagnose_fault", "find_local_mechanic", "check_part_stock",
                     "estimate_cost_inr", "schedule_repair", "send_whatsapp", "verify_repair", "log_warranty_claim"}  # fmt: skip
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in oa.TOOLS)
    assert not any(w in n for n in names for w in ("stop", "shutdown", "book", "approve", "control"))
    assert not hasattr(oa.AgentTools, "send_job")  # booking happens only on the owner's reply (server/fix.py)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    yield ims_world(tmp_path_factory.mktemp("agent"), mp)
    mp.undo()


@needs_ims
def test_owner_asks_in_hinglish_and_agent_asks_back_without_booking(world):
    _, client, live, _ = world
    out = client.post(
        "/api/agent/chat", json={"text": "Loom 1 ki awaaz kyun badal gayi?", "factory": "A"}
    ).json()
    assert out["lang"] == "hi_latn" and out["engine"] == "rules" and out["machine"] == "A-G1"
    steps = [s["step"] for s in out["steps"]]
    assert (
        steps[:2] == ["observe", "observe"]
        and "diagnose" in steps
        and "plan" in steps
        and steps[-1] == "ask owner"
    )
    assert "1 = Haan, 2 = Nahi" in out["reply"] and "₹" in out["reply"]
    assert all(m["approved"] != 1 for m in live.messages() if m["machine"] == "A-G1")  # nothing booked
    assert [c for c in out["checklist"] if c["done"]] and not next(
        c for c in out["checklist"] if c["step"] == "verify"
    )["done"]
    v = oa.AgentTools(live, "t", "en").verify_repair("A-G1")
    assert v["status"] == "no approved repair"  # verify needs the owner's yes first


@needs_ims
def test_tamil_question_gets_a_tamil_reply(world):
    _, client, *_ = world
    out = client.post("/api/agent/chat", json={"text": "லூம் 1 ஒலி ஏன் மாறியது?", "factory": "A"}).json()
    assert out["lang"] == "ta" and (
        "ஆம்" in out["reply"] or "இயல்பாக" in out["reply"] or "உறுதியாக" in out["reply"]
    )


@needs_ims
def test_twin_summary_is_real_data(world):
    _, client, live, _ = world
    t = client.get("/api/twin").json()
    assert t["chips"]["total"] == len(live.c.machines) and t["focus"] == "A-G1"
    assert t["shifts"] and all(s["max"] >= s["mean"] >= 0 for s in t["shifts"])
    assert t["trend"] == [round(h["risk"], 3) for h in live.c.history("A-G1", 42)]
    assert t["ranking"][0]["id"] == "A-G1" and 0 <= t["health_pct"] <= 100


def test_claude_loop_uses_the_spec_settings():
    """A fake client: one tool call, then a final answer. Model, tools, betas and effort as specified."""

    class FakeLive:
        def __init__(self):
            self.c = SimpleNamespace(machines={"A-G1": {"name": "Loom 1", "type": "gearbox", "factory": "A"}},
                                     now=lambda: __import__("datetime").datetime(2026, 11, 16, 10))  # fmt: skip

        def ranking(self):
            return [
                {
                    "machine_id": "A-G1",
                    "status": "alarm",
                    "risk": 2.0,
                    "days_rising": 2,
                    "days_to_fault_level": {},
                }
            ]

        def event(self, *a, **k):
            pass

    calls = []
    tool_turn = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", id="t1", name="get_machine_health", input={"machine_id": "A-G1"})])  # fmt: skip
    final = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Loom 1: bearing ghis raha hai.")])  # fmt: skip

    def create(**kw):
        calls.append(kw)
        return tool_turn if len(calls) == 1 else final

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    out = oa.run_claude(FakeLive(), "Loom 1 kaisa hai?", "A", "hi_latn", client)
    assert (
        out["engine"] == "claude"
        and out["reply"].startswith("Loom 1")
        and out["steps"][0]["tool"] == "get_machine_health"
    )
    k = calls[0]
    assert k["model"] == "claude-fable-5-2" and "thinking" not in k and "tool_choice" not in k
    assert k["betas"] == ["server-side-fallback-2026-07-01"] and k["fallbacks"] == "default"
    turns = [m for m in calls[1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)]
    assert turns and turns[0]["content"][0]["type"] == "tool_result"  # the tool result went back to the model


@needs_ims
def test_agent_acts_on_the_alarm_then_verifies_after_the_owner_says_yes(tmp_path):
    from server import prove

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(prove, "KEYS", tmp_path / "keys")
        mp.setattr(prove, "LABELS", tmp_path / "labels.jsonl")
        _, client, live, _ = ims_world(tmp_path, mp)
        first = live.last_agent  # run by itself at the first alarm (no owner message)
        assert first and first["machine"] == "A-G1" and first["steps"][-1]["step"] == "ask owner"
        assert all(m["approved"] != 1 for m in live.messages() if m["machine"] == "A-G1")
        client.post("/api/owner/decide", json={"machine": "A-G1", "choice": "book"})  # the owner: Haan
        out = client.post("/api/agent/chat", json={"text": "Loom 1 theek hua?", "factory": "A"}).json()
        steps = [s["step"] for s in out["steps"]]
        assert "act" in steps and "verify" in steps and prove.records(live, "A-G1")
        assert all(c["done"] for c in out["checklist"] if c["step"] in ("act", "verify"))
    finally:
        mp.undo()
