"""The planner: events once, a scripted mock LLM choosing tools, the gate, grounding, bounds, routing, tools.

No network: the "LLM" is a script of responses (tests/test_planner_core.Script). Every test runs the real
planner, tool box, maintenance agent, store and approval gate.
"""

from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from edge.baseline import BaselineStore
from server import provider
from server.maint import agent as ag
from server.maint.agent import ApprovalRequired
from server.maint.store import Store
from server.maint.tools import Network, Transport
from server.planner import grounding, llm
from server.planner.agent import Planner, preferences
from server.planner.events import EventBus
from server.planner.tools import SPECS, Env, ToolBox

T0 = datetime(2026, 11, 16, 9, 0).timestamp()  # a Monday
H = 3600.0
FACTORIES = {"C": {"id": "C", "name": "Ansari Agro Processing", "language": "hi_latn", "xy_km": [24, -6],
                   "power_cuts": [["14:00", "16:00"]]}}  # fmt: skip
HEALTH = {"status": "repair_soon", "urgency": "inspect", "severity": "marked", "confidence": "high",
          "recent_risk": [1.4, 1.6, 1.9], "evidence": ["3 of the last 3 readings above its alarm level"],
          "time_to_threshold": {"status": "estimate", "low": 2.0, "high": 6.0}}  # fmt: skip


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


def tool(name, **inp):
    return NS(type="tool_use", id=f"tu_{name}_{len(inp)}", name=name, input=inp)


def text(s):
    return NS(type="text", text=s)


def turn(*blocks, stop=None, inp=1000, out=200):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    return NS(stop_reason=stop, content=list(blocks), usage=NS(input_tokens=inp, output_tokens=out))


class Script:
    """A mock LLM client (the shape server/provider.GraniteClient has): scripted turns, every request recorded."""

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []
        self.messages = NS(create=self._plain)
        self.beta = NS(messages=NS(create=self._beta))

    def _next(self, endpoint, kw):
        self.calls.append({"endpoint": endpoint, **kw, "messages": list(kw["messages"])})
        return self.turns.pop(0) if self.turns else turn(text("ok"))

    def _plain(self, **kw):
        return self._next("plain", kw)

    def _beta(self, **kw):
        return self._next("beta", kw)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def make_world(tmp_path, monkeypatch):
    for k in ("DHADKAN_PLANNER_MAX_TURNS", "DHADKAN_PLANNER_MAX_TOKENS", "DHADKAN_PLANNER_CASE_CAP_INR",
              "DHADKAN_MODEL_ROUTINE", "DHADKAN_MODEL_PLAN", "DHADKAN_MODEL_DIAGNOSE"):  # fmt: skip
        monkeypatch.delenv(k, raising=False)
    clock = Clock()
    b = BaselineStore(tmp_path / "edge.db", detectors=["rms"])
    b.register("C-G1", "gearbox", learning_days=3, now=T0 - 5 * 86400)
    s = Store(tmp_path / "maint.db", clock=clock)
    s.add_machine("C-G1", "Flour mill 1", "C", "flour_mill", "gearbox", 10, "mill")
    s.add_machine("C-F1", "Blower 1", "C", "flour_mill", "fan", 3, "blower")
    net = Network(online=True)
    tr = Transport(network=net)
    agent = ag.MaintenanceAgent(s, b, ag.Tools(transport=tr, factories=FACTORIES), clock=clock)
    bus = EventBus(s)
    agent.emit_hook = bus.emit
    health = {"C-G1": dict(HEALTH)}

    def make(client=None, **kw):
        p = Planner(s, agent, bus, client=client, factories=FACTORIES, health_of=health.get,
                    route_cfg={"start_xy": [3, 2], "hours": 7, "kmph": 22}, **kw)  # fmt: skip
        agent.driver = "planner" if client else "rules"
        return p

    return NS(
        store=s, agent=agent, bus=bus, transport=tr, net=net, clock=clock, health=health, make=make, base=b
    )


def alarm(w, idem="alarm:C-G1:1"):
    return w.bus.emit(
        "alarm", machine="C-G1", factory="C", idem=idem, payload={"to": "inspect"}, source="brain"
    )


ASK = {"question": "Flour mill 1 ki awaaz badli hai. Ramesh ko bulaun?",
           "options": [{"label": "Haan, Ramesh ko bulao", "means": "approve_booking", "technician": "T1", "day": "2026-11-17"},
                    {"label": "Baad mein", "means": "later", "technician": None, "day": None}]}  # fmt: skip


def sent(w, kind):
    return [x for x in w.transport.sent if x["kind"] == kind]


# ---------------------------------------------------------------- events
def test_same_event_key_is_stored_and_handled_once(world):
    p = world.make()
    assert alarm(world) is not None and alarm(world) is None  # a retried webhook / a restart
    assert world.bus.drain() == 1 and world.bus.drain() == 0
    assert len(world.store.decisions()) == 1 and p.llm_on is False


def test_interrupted_event_and_decision_resume_after_restart(world):
    world.make()
    ev = alarm(world)
    world.store.set_event(ev.idem, status="running")  # crashed mid-turn
    did = world.store.start_decision(ev.idem, "alarm", None, "C-G1", "granite", "m")
    bus2 = EventBus(world.store)
    Planner(world.store, world.agent, bus2, factories=FACTORIES, health_of=world.health.get)
    assert world.store.decision(did)["status"] == "interrupted"
    assert bus2.drain() == 1 and world.store.event(ev.idem)["status"] == "handled"


def test_unknown_event_type_is_refused(world):
    with pytest.raises(ValueError):
        world.bus.emit("stop_machine", machine="C-G1")


# ---------------------------------------------------------------- rules without a key
def test_without_a_key_the_fixed_plan_runs_and_no_llm_is_called(world):
    p = world.make()
    case = world.agent.observe("C-G1", HEALTH)
    assert case["plan"] == ag.PLAN_FIX  # the deterministic plan, as before the planner
    out = p.handle(alarm(world))
    assert out["engine"] == "rules" and "no local LLM" in out["reason"]


def test_llm_error_falls_back_to_rules_and_hands_the_case_over(world):
    class Down(Script):
        def _plain(self, **kw):
            raise ConnectionError("offline")

    p = world.make(Down())
    out = p.handle(alarm(world))
    case = world.store.open_case("C-G1")
    assert out["engine"] == "rules" and "LLM error" in out["reason"]
    assert case["context"]["driver"] == "rules" and "find_technician" in case["plan"]


# ---------------------------------------------------------------- the LLM decides, code gates
def test_alarm_ask_then_owner_says_1_then_book(world):
    client = Script(
        turn(
            text("Pehle dekhta hoon."),
            tool("get_machine_health", machine_id="C-G1"),
            tool("find_mechanics", skill="bearing_wear", area=None, need_by="2026-11-18"),
        ),
        turn(tool("propose_slots", machine_id="C-G1", duration_h=1.5, prefer_day=None)),
        turn(tool("ask_owner", case_id="C-G1#1", **ASK)),
        turn(text("Owner se Ramesh ke liye poocha; jawab ka intezaar.")),
        # the owner's "1" (a new event): the code applied it; the planner books
        turn(tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17")),
        turn(tool("tell_owner", case_id="C-G1#1", text="Ramesh ko 2026-11-17 ke liye bula liya hai.")),
        turn(text("Booked T1 for 2026-11-17 after the owner's approval.")),
    )
    p = world.make(client)
    first = p.handle(alarm(world))
    case = world.store.case("C-G1#1")
    assert case["plan"] == ag.PLAN_PLANNER and first["engine"] == "granite"
    assert [c["tool"] for c in first["calls"]] == [
        "get_machine_health",
        "find_mechanics",
        "propose_slots",
        "ask_owner",
    ]
    appr = world.store.approval("C-G1#1:booking")
    assert appr["status"] == "requested" and not sent(world, "book_job")
    ask = [x for x in sent(world, "notify") if "1 = Haan" in x["text"]]
    assert len(ask) == 1 and "2 = Baad mein" in ask[0]["text"]
    reply = world.bus.emit(
        "owner_reply", factory="C", idem="wa:1", payload={"text": "1", "channel": "whatsapp"}
    )
    second = p.handle(reply)
    assert second["effects"]["answer"]["means"] == "approve_booking"
    assert world.store.approval("C-G1#1:booking")["decided_by"] == "owner"
    booked = sent(world, "book_job")
    assert len(booked) == 1 and booked[0]["technician"] == "T1" and booked[0]["approval_ref"] == "wa:1"
    d = world.store.decision(second["decision"])
    assert d["input_tokens"] > 0 and d["cost_inr"] == 0 and d["model"] == provider.chat_model()  # local: ₹0
    assert world.store.verify_audit()["ok"]


def test_book_without_approval_is_refused_by_the_gate(world):
    client = Script(turn(tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17")),
                    turn(text("Could not book.")))  # fmt: skip
    p = world.make(client)
    out = p.handle(alarm(world))
    # two independent layers: the Guardian refuses before the tool runs ...
    assert out["calls"][0]["result"]["gate"] == "guardian" and not sent(world, "book_job")
    v = [
        r["detail"]
        for r in world.store.audit("C-G1#1")
        if r["event"] == "verdict" and r["detail"]["target"] == "book_job"
    ]
    assert v and not v[0]["ok"] and v[0]["reasons"][0]["rule"] == "no_logged_approval"
    # ... and the tool's own gate still refuses when called past the Guardian
    box = ToolBox(Env(store=world.store, agent=world.agent, factory="C"))
    with pytest.raises(ApprovalRequired):
        box.t_book_job("C-G1#1", "T1", "2026-11-17")
    assert any(r["event"] == "blocked_without_approval" for r in world.store.audit("C-G1#1"))


def test_booking_a_different_visit_than_the_owner_approved_is_refused(world):
    client = Script(turn(tool("ask_owner", case_id="C-G1#1", **ASK)), turn(text("asked")),
                    turn(tool("book_job", case_id="C-G1#1", technician="T2", day="2026-11-17")), turn(text("x")))  # fmt: skip
    p = world.make(client)
    p.handle(alarm(world))
    p.handle(
        world.bus.emit("owner_reply", factory="C", idem="wa:1", payload={"text": "1", "channel": "whatsapp"})
    )
    assert not sent(world, "book_job")  # T1 was approved, not T2


def test_a_send_time_recheck_holds_a_queued_booking_whose_approval_changed(world):
    client = Script(turn(tool("ask_owner", case_id="C-G1#1", **ASK)), turn(text("asked")),
                    turn(tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17")), turn(text("x")))  # fmt: skip
    p = world.make(client)
    p.handle(alarm(world))
    world.net.online = False
    p.handle(
        world.bus.emit("owner_reply", factory="C", idem="wa:1", payload={"text": "1", "channel": "whatsapp"})
    )
    assert world.store.action("C-G1#1:book_job:T1:2026-11-17")["status"] == "queued"
    world.store.decide("C-G1#1:booking", "rejected", "owner", "sms", "sms:2")  # a conflicting answer
    world.net.online = True
    world.clock.t += 2 * H  # past the queued action's retry time
    world.agent.sync()
    assert (
        not sent(world, "book_job")
        and world.store.action("C-G1#1:book_job:T1:2026-11-17")["status"] == "held"
    )


def test_a_chat_question_is_never_read_as_an_approval(world):
    client = Script(
        turn(tool("ask_owner", case_id="C-G1#1", **ASK)), turn(text("asked")), turn(text("Theek hai."))
    )
    p = world.make(client)
    p.handle(alarm(world))
    chat = world.bus.emit("owner_reply", machine="C-G1", factory="C", idem="chat:1",
                          payload={"text": "Flour mill 1 kaisa hai?", "channel": "chat"})  # fmt: skip
    out = p.handle(chat)
    assert "answer" not in out["effects"] and world.store.approval("C-G1#1:booking")["status"] == "requested"


def test_baad_mein_rejects_the_booking_question(world):
    client = Script(
        turn(tool("ask_owner", case_id="C-G1#1", **ASK)), turn(text("asked")), turn(text("Theek hai."))
    )
    p = world.make(client)
    p.handle(alarm(world))
    out = p.handle(world.bus.emit("owner_reply", factory="C", idem="wa:2",
                                  payload={"text": "baad mein", "channel": "whatsapp"}))  # fmt: skip
    assert out["effects"]["answer"]["means"] == "later"
    assert world.store.approval("C-G1#1:booking")["status"] == "rejected"


def test_stop_is_only_a_question_and_its_answer_is_only_logged(world):
    ask = {"question": "Machine band karni hai ya nahi, aapka faisla.",
               "options": [{"label": "Maine band kar di", "means": "stop_noted", "technician": None, "day": None},
                        {"label": "Chalne do", "means": "later", "technician": None, "day": None}]}  # fmt: skip
    client = Script(
        turn(tool("ask_owner", case_id="C-G1#1", **ask)), turn(text("asked")), turn(text("noted"))
    )
    p = world.make(client)
    p.handle(alarm(world))
    p.handle(
        world.bus.emit("owner_reply", factory="C", idem="wa:3", payload={"text": "1", "channel": "whatsapp"})
    )
    assert world.store.approval("C-G1#1:production_stop")["status"] == "approved"
    assert {x["kind"] for x in world.transport.sent} == {"notify"}  # no action of any other kind exists


def test_unknown_tools_and_other_factories_are_refused(world):
    world.store.add_machine("A-G1", "Loom 1", "A", "flour_mill", "gearbox", 5, "mill")
    client = Script(turn(tool("stop_machine", machine_id="C-G1"), tool("get_machine_health", machine_id="A-G1"),
                         tool("get_machine_health", machine_id="C-G1", extra=1)), turn(text("done")))  # fmt: skip
    p = world.make(client)
    out = p.handle(alarm(world))
    errs = [c["result"]["error"] for c in out["calls"]]
    assert "unknown tool" in errs[0] and "not in this factory" in errs[1] and "unexpected" in errs[2]
    results = client.calls[1]["messages"][-1]["content"]
    assert all(r.get("is_error") for r in results)


# ---------------------------------------------------------------- grounding
def test_grounding_rejects_invented_numbers_in_the_reply_and_in_messages(world):
    client = Script(
        turn(tool("tell_owner", case_id="C-G1#1", text="Repair mein ₹4,999 lagenge.")),
        turn(text("Bearing ₹7,450 mein badal jayega, part 6206-ZZ.")),
    )
    p = world.make(client)
    out = p.handle(alarm(world))
    assert "grounding" in out["calls"][0]["result"]["error"] and not sent(world, "notify")
    d = world.store.decision(out["decision"])
    assert d["status"] == "grounding_rejected" and "7450" in d["grounding"]["unknown_numbers"]
    assert "6206-ZZ" in d["grounding"]["unknown_parts"]
    assert sum(r["event"] == "grounding_rejected" for r in world.store.audit()) == 2


def test_grounding_accepts_tool_numbers_in_indian_formats():
    src = [{"fix_now_inr": [156960, 160000], "p_breakdown": 0.42, "part": "Ball bearing 6205-2RS"}]
    assert grounding.check("₹1,56,960 (1.57 lakh), 42% chance, 6205-2RS", src)["ok"]
    assert grounding.check("₹१,५६,९६०", src)["ok"]  # Devanagari digits
    assert not grounding.check("₹1,57,000", src)["ok"]  # a coarser rounding is a new number
    assert grounding.check("Loom 1: 1 = Haan, 2 = Nahi", [], ["Loom 1"])["ok"]


# ---------------------------------------------------------------- bounds and routing
def test_turn_limit_and_token_budget(world, monkeypatch):
    monkeypatch.setenv("DHADKAN_PLANNER_MAX_TURNS", "3")
    loop = [turn(tool("get_machine_health", machine_id="C-G1")) for _ in range(10)]
    p = world.make(Script(*loop))
    out = p.handle(alarm(world))
    assert out["stop"] == "turns" and out["turns"] == 3
    monkeypatch.setenv("DHADKAN_PLANNER_MAX_TOKENS", "1500")
    p.client = Script(*[turn(tool("get_machine_health", machine_id="C-G1"), inp=1000) for _ in range(10)])
    out = p.handle(world.bus.emit("health_change", machine="C-G1", factory="C", idem="h:2"))
    assert out["stop"] == "tokens" and out["turns"] == 2


def test_per_case_rupee_cap_hands_the_case_to_rules(world, monkeypatch):
    monkeypatch.setenv("DHADKAN_PLANNER_CASE_CAP_INR", "1")
    monkeypatch.setenv("DHADKAN_LOCAL_INR_PER_MTOK", "100")  # price the local model so the cap can bite
    monkeypatch.setenv("DHADKAN_PLANNER_MAX_TOKENS", "10000000")
    big = [turn(tool("get_machine_health", machine_id="C-G1"), inp=200_000, out=20_000) for _ in range(5)]
    p = world.make(Script(*big))
    out = p.handle(alarm(world))
    assert out["stop"] == "cost" and world.store.llm_cost_inr("C-G1#1") >= 1
    assert world.store.case("C-G1#1")["context"]["driver"] == "rules"
    again = p.handle(world.bus.emit("health_change", machine="C-G1", factory="C", idem="h:3"))
    assert again["engine"] == "rules"


def test_model_routing_by_event(world):
    c = Script()
    p = world.make(c)
    p.handle(alarm(world))
    assert c.calls[0]["model"] == provider.chat_model() and c.calls[0]["endpoint"] == "plain"
    assert "fallbacks" not in c.calls[0] and "betas" not in c.calls[0]
    assert c.calls[0]["output_config"] == {"effort": "medium"} and "tool_choice" not in c.calls[0]
    assert all(t["strict"] for t in c.calls[0]["tools"])
    p.handle(world.bus.emit("daily_tick", idem="day:1"))
    assert c.calls[1]["output_config"] == {"effort": "low"} and c.calls[1]["endpoint"] == "plain"
    p.handle(
        world.bus.emit(
            "owner_voice_note", machine="C-G1", factory="C", idem="v:1", payload={"text": "khat khat"}
        )
    )
    assert c.calls[2]["model"] == provider.chat_model() and c.calls[2]["output_config"] == {"effort": "high"}


def test_models_come_from_env(world, monkeypatch):
    monkeypatch.setenv("DHADKAN_MODEL_PLAN", "granite4:small-h")
    c = Script()
    world.make(c).handle(alarm(world))
    assert c.calls[0]["model"] == "granite4:small-h"


def test_cost_in_rupees_from_usage(monkeypatch):
    u = NS(input_tokens=1_000_000, output_tokens=0)
    assert llm.cost_inr(provider.chat_model(), u) == 0  # a local model costs nothing per token
    monkeypatch.setenv("DHADKAN_LOCAL_INR_PER_MTOK", "3")
    assert llm.cost_inr(provider.chat_model(), u) == 3.0
    monkeypatch.setitem(llm.PRICES, "priced-model", (2.0, 10.0))
    assert llm.cost_inr("priced-model", u) == round(2.0 * llm.usd_to_inr(), 4)


# ---------------------------------------------------------------- memory and preferences
def test_preferences_from_the_owners_words():
    assert preferences("Ravivar ko bhejo, budget 2,000 rupaye se zyada nahi") == [
        "budget limit ₹2,000 for a repair", "prefers Sunday visits"]  # fmt: skip


def test_remember_and_recall(world):
    client = Script(turn(tool("remember", scope="owner:C", note="prefers Sunday visits"),
                         tool("remember", scope="owner:A", note="x is wrong"), tool("recall", machine_id="C-G1")),
                    turn(text("noted")))  # fmt: skip
    out = world.make(client).handle(alarm(world))
    assert out["calls"][2]["result"]["owner_preferences"] == ["prefers Sunday visits"]
    assert "must be this factory" in out["calls"][1]["result"]["error"]


# ---------------------------------------------------------------- the decision tools
def tb(world, **kw):
    from server.planner.tools import Env

    world.agent.driver = "planner"
    if world.store.open_case("C-G1") is None:
        world.agent.observe("C-G1", HEALTH)
    return ToolBox(Env(store=world.store, agent=world.agent, factories=FACTORIES, health_of=world.health.get,
                       factory="C", lang="hi_latn", route_cfg={"start_xy": [3, 2], "hours": 7, "kmph": 22}, **kw))  # fmt: skip


def test_find_mechanics_returns_candidates_with_reasons(world):
    world.store.add_repair(machine="C-G1", case_id="old", technician="T2", work="bearing", done=T0 - 30 * 86400,
                           outcome="fixed", before_median=2, after_median=0.6, against_version=1)  # fmt: skip
    out = tb(world).dispatch(
        "find_mechanics", {"skill": "bearing_wear", "area": None, "need_by": "2026-11-19"}
    )
    ids = [c["id"] for c in out["candidates"]]
    assert len(ids) >= 3 and ids[0] == "T2"  # fixed this machine before
    c = out["candidates"][0]
    assert c["free_days"] and c["km"] is not None and c["past_here"]["fixed"] == 1 and "DEMO" in out["source"]


def test_propose_slots_prices_production_and_sees_the_order_book(world):
    out = tb(world).dispatch("propose_slots", {"machine_id": "C-G1", "duration_h": 2, "prefer_day": None})
    assert out["slots"] and all("production_lost_inr" in s and "order_clashes" in s for s in out["slots"])
    assert any(s["power_cut_overlap_h"] > 0 or s["production_lost_inr"] == 0 for s in out["slots"])
    assert "DEMO" in out["basis"]


def test_check_parts_lists_dealers_and_equivalents(world):
    out = tb(world).dispatch("check_parts", {"fault": "bearing_wear", "machine_type": "gearbox"})
    assert len(out["dealers"]) >= 3 and out["any_in_stock"] and out["equivalents"]
    assert all("price_inr" not in d for d in out["dealers"]) and all(
        "MOCK" in d["source"] for d in out["dealers"]
    )


def test_compare_options_uses_the_rul_range(world):
    out = tb(world).dispatch("compare_options", {"case_id": "C-G1#1", "options": [
        {"label": "now", "wait_days": 0}, {"label": "a week", "wait_days": 7}]})  # fmt: skip
    now, week = out["options"]
    assert now["p_threshold_within_wait"] == 0.0 and week["p_threshold_within_wait"] == 1.0
    assert week["expected_inr"] > now["expected_inr"] and "ASSUMPTIONS" in out["basis"]


def test_triage_orders_cases_for_one_mechanic(world):
    world.agent.observe("C-F1", {**HEALTH, "urgency": "maintenance"})
    out = tb(world).dispatch("triage", {"factory": "C"})
    assert out["priority"][0]["machine"] == "C-F1"  # the more urgent case first
    assert {o["machine"] for o in out["order"]} == {"C-F1", "C-G1"}  # both fit one mechanic's day


def test_measurement_request_in_the_owners_language_and_rescore(world):
    box = tb(world)
    out = box.dispatch("request_measurement", {"machine_id": "C-G1", "instruction": "", "kind": "audio"})
    assert out["text"].startswith(
        "Phone ko Flour mill 1 ki motor ke paas rakh ke 10 sec ki awaaz record karo"
    )
    assert sent(world, "notify")
    p = world.make(score_audio=lambda m, path: 1.8)
    got = p.handle(world.bus.emit("owner_voice_note", machine="C-G1", factory="C", idem="v:2",
                                  payload={"text": "", "audio_path": "x.ogg"}))  # fmt: skip
    m = got["effects"]["measurement"]
    assert m["risk_vs_alarm"] == 1.8 and m["fault_confidence"] == "higher"
    assert world.store.case("C-G1#1")["context"]["measurements"][-1]["status"] == "received"


def test_hand_check_reply(world):
    box = tb(world)
    box.dispatch("request_measurement", {"machine_id": "C-G1", "instruction": "", "kind": "hand_check"})
    out = world.make().handle(world.bus.emit("owner_reply", machine="C-G1", factory="C", idem="wa:h",
                                             payload={"text": "1", "channel": "whatsapp"}))  # fmt: skip
    assert out["effects"]["measurement"]["result"] == "hot"


def test_offline_ask_is_queued_and_sent_once_when_the_network_is_back(world):
    box = tb(world)
    world.net.online = False
    world.agent.sync()
    out = box.dispatch("ask_owner", {"case_id": "C-G1#1", **ASK})
    assert out["send"] == "queued" and not sent(world, "notify")
    world.net.online = True
    world.clock.t += 2 * H
    world.agent.sync()
    assert len(sent(world, "notify")) == 1
    assert [e["type"] for e in world.store.events(status="pending")] == ["network_restored"]


def test_every_tool_is_strict_and_none_controls_a_machine():
    for t in SPECS:
        assert t["strict"] and t["input_schema"]["additionalProperties"] is False
        assert not any(
            w in t["name"] for w in ("stop", "shut", "halt", "control", "speed", "power", "approve")
        )


def test_routine_events_go_to_rules_on_a_local_model_but_alarms_do_not(world, monkeypatch):
    class Local(Script):
        provider = "ollama"

    c = Local()
    p = world.make(c)
    out = p.handle(world.bus.emit("daily_tick", idem="day:9"))
    assert out["engine"] == "rules" and "routine event" in out["reason"] and not c.calls
    p.handle(alarm(world))
    assert c.calls  # the alarm still goes to the model
    monkeypatch.setenv("DHADKAN_LLM_SKIP_EVENTS", "none")
    p.handle(world.bus.emit("daily_tick", idem="day:10"))
    assert len(c.calls) >= 2
