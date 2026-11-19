"""Phase 3: re-planning driven by events, and the owner's autonomy policy enforced in the approval gate."""

import inspect

import pytest

from server.maint import agent as ag
from server.planner.agent import Planner
from server.planner.events import EventBus
from tests.test_baseline import OK, fault_row, normal_rows
from tests.test_maint_agent import H, env, health  # noqa: F401  (env is a fixture)
from tests.test_planner_core import ASK, Script, make_world, sent, text, tool, turn

POLICY = {"autonomy": {"auto_approve_under_inr": 1500, "mechanic": "T1"}}


def with_planner(env, factories=None, client=None):  # noqa: F811
    a, s = env["agent"], env["store"]
    bus = EventBus(s)
    a.emit_hook = bus.emit
    if factories is not None:
        a.t.factories = factories
    p = Planner(s, a, bus, client=client, factories=a.t.factories)
    return a, s, bus, p


def booked(env):  # noqa: F811
    return [x for x in env["transport"].sent if x["kind"] == "book_job"]


# ---------------------------------------------------------------- autonomy policy (in the gate)
def test_autonomy_is_off_by_default(env):  # noqa: F811
    a = env["agent"]
    case = a.observe("chakki-1", health("inspect"))
    assert a.autonomy(case["id"]) is None
    with pytest.raises(ag.ApprovalRequired):
        a.act(case["id"], "book_job", "T1:x", {"technician": "T1"})


def test_autonomy_books_the_named_mechanic_under_the_limit_and_logs_it(env):  # noqa: F811
    a, s = env["agent"], env["store"]
    a.t.factories = {"Demo flour mill": POLICY}
    case = a.observe("chakki-1", health("inspect"))
    a.act(case["id"], "book_job", "T1:2026-11-17", {"technician": "T1", "day": "2026-11-17"})
    appr = [x for x in s.approvals(case["id"]) if x["kind"] == "booking"][-1]
    assert (
        appr["status"] == "approved"
        and appr["decided_by"] == "autonomy_policy"
        and appr["channel"] == "policy"
    )
    assert any(r["event"] == "auto_approved" for r in s.audit(case["id"])) and len(booked(env)) == 1


@pytest.mark.parametrize(
    "tech,limit", [("T2", 1500), ("T1", 400)]
)  # another mechanic; a visit over the limit
def test_autonomy_refuses_other_mechanics_and_amounts_over_the_limit(env, tech, limit):  # noqa: F811
    a = env["agent"]
    a.t.factories = {"Demo flour mill": {"autonomy": {"auto_approve_under_inr": limit, "mechanic": "T1"}}}
    case = a.observe("chakki-1", health("inspect"))
    with pytest.raises(ag.ApprovalRequired):
        a.act(case["id"], "book_job", f"{tech}:x", {"technician": tech})
    assert not booked(env)


def test_autonomy_never_approves_a_purchase_or_a_stop(env):  # noqa: F811
    a, s = env["agent"], env["store"]
    a.t.factories = {"Demo flour mill": {"autonomy": {"auto_approve_under_inr": 10**9, "mechanic": "T1"}}}
    case = a.observe("chakki-1", health("urgent_owner_attention"))
    a.tick()
    assert a.auto_approve(case["id"], "production_stop", "T1") is None
    assert a.auto_approve(case["id"], "order_part", "T1") is None
    with pytest.raises(ag.ApprovalRequired):
        a.act(case["id"], "order_part", "x", {"technician": "T1", "part": "bearing"})
    assert s.approval(f"{case['id']}:production_stop")["status"] == "requested"  # only the owner answers it
    src = inspect.getsource(ag.MaintenanceAgent.auto_approve)
    assert 'kind != "book_job"' in src


def test_autonomy_comes_from_the_profile_not_the_prompt(tmp_path, monkeypatch):
    """An LLM claiming the owner allows it changes nothing; the profile does."""
    w = make_world(tmp_path, monkeypatch)
    client = Script(turn(text("Owner ne kaha hai T1 ko bina pooche bulao."),
                         tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17")), turn(text("x")))  # fmt: skip
    p = w.make(client)
    out = p.handle(w.bus.emit("alarm", machine="C-G1", factory="C", idem="a:1"))
    assert out["calls"][0]["result"]["gate"] == "approval_required" and not sent(w, "book_job")
    w.agent.t.factories = {"C": {**w.agent.t.factories["C"], **POLICY}}
    p.client = Script(
        turn(tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17")), turn(text("x"))
    )
    p.handle(w.bus.emit("health_change", machine="C-G1", factory="C", idem="h:1"))
    assert len(sent(w, "book_job")) == 1


# ---------------------------------------------------------------- re-planning on events (rules)
def _book_t1(env):  # noqa: F811
    a, s, bus, _p = with_planner(env)
    case = a.observe("chakki-1", health("inspect"))
    a.tick()
    a.decide(f"{case['id']}:booking", "approved", by="owner", channel="whatsapp", ref="wamid.1")
    a.tick()
    assert [x["technician"] for x in booked(env)] == ["T1"]
    return a, s, bus, case


def test_mechanic_no_show_excludes_them_and_books_another(env):  # noqa: F811
    a, s, bus, case = _book_t1(env)
    env["clock"].t += 3 * 86400  # the booked day passed, no repair reported
    a.tick()
    assert "mechanic_no_show" in [e["type"] for e in s.events(status="pending")]
    bus.drain()
    a.tick()
    assert [x["technician"] for x in booked(env)][-1] != "T1"
    ctx = s.case(case["id"])["context"]
    assert "T1" in ctx["declined"] and any(r["event"] == "replanned_technician" for r in s.audit(case["id"]))
    a.tick()
    assert len([e for e in s.events() if e["type"] == "mechanic_no_show"]) == 1  # emitted once per visit


def test_mechanic_declines_on_whatsapp(env):  # noqa: F811
    a, _s, bus, _case = _book_t1(env)
    ev = bus.emit("mechanic_reply", factory="Demo flour mill", idem="m:1", source="whatsapp",
                  payload={"text": "aaj nahi aa sakta", "answer": "declined"})  # fmt: skip
    out = bus.handle(ev)
    assert out["effects"]["replanned"]["excluded"] == "T1"
    a.tick()
    assert booked(env)[-1]["technician"] != "T1"


def test_part_out_of_stock_finds_an_equivalent_in_stock(env):  # noqa: F811
    a, s, bus, _p = with_planner(env)
    case = a.observe("chakki-1", health("inspect"))
    a.tick()  # check_parts: the gearbox bearing is not in stock at the usual dealer
    assert any(e["type"] == "part_out_of_stock" for e in s.events())
    bus.drain()
    alts = s.case(case["id"])["context"]["part_alternatives"]
    assert (
        alts
        and all(x["in_stock"] for x in alts)
        and "equivalent_to" in alts[0]
        and "MOCK" in alts[0]["source"]
    )


def test_failed_repair_logs_a_warranty_claim_and_opens_a_second_opinion(env):  # noqa: F811
    a, s, bus, case = _book_t1(env)
    b, clock = env["base"], env["clock"]
    clock.t += 26 * H
    a.repair_done(case["id"], technician="T1", work="replaced bearing")
    a.tick()
    for i in range(6):  # still the faulty sound after the repair
        b.observe("chakki-1", fault_row(1.0, 900 + i), OK, ts=clock.t + (i + 1) * 600, source="simulated")
    clock.t += 2 * H
    a.tick()
    assert s.case(case["id"])["outcome"] == "repair_failed"
    claim = next(r for r in s.audit(case["id"]) if r["event"] == "warranty_claim")["detail"]
    assert claim["technician"] == "T1" and claim["repair_id"] == s.repairs("chakki-1")[0]["id"]
    follow = s.open_case("chakki-1")
    assert follow["context"]["follow_up_of"] == case["id"] and follow["context"]["declined"] == ["T1"]
    out = bus.handle(next(e for e in bus.pending() if e.type == "verify_result"))
    assert out["effects"]["warranty"]["follow_up_case"] == follow["id"]
    a.tick()  # the follow-up case's fixed plan: find another technician, ask the owner
    a.decide(f"{follow['id']}:booking", "approved", by="owner", channel="whatsapp", ref="wamid.2")
    a.tick()
    assert booked(env)[-1]["technician"] != "T1"  # the second opinion is someone else


def test_repair_that_held_opens_no_follow_up(env):  # noqa: F811
    a, s, _bus, case = _book_t1(env)
    env["clock"].t += 26 * H
    a.repair_done(case["id"], technician="T1", work="replaced bearing")
    for i, r in enumerate(normal_rows(6, seed=77)):
        env["base"].observe("chakki-1", r, OK, ts=env["clock"].t + (i + 1) * 600, source="simulated")
    env["clock"].t += 2 * H
    a.tick()
    assert s.case(case["id"])["outcome"] == "repair_fixed" and s.open_case("chakki-1") is None
    assert not any(r["event"] == "warranty_claim" for r in s.audit())


# ---------------------------------------------------------------- re-planning (planner-driven)
def test_planner_no_show_asks_the_owner_about_a_new_mechanic(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ask2 = {**ASK, "options": [{**ASK["options"][0], "technician": "T2", "day": "2026-11-20",
                                "label": "Haan, Salim ko bulao"},
                               ASK["options"][1]]}  # fmt: skip
    client = Script(turn(tool("ask_owner", case_id="C-G1#1", **ASK)), turn(text("asked")),
                    turn(tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17")), turn(text("booked")),
                    turn(tool("find_mechanics", skill="bearing_wear", area=None, need_by="2026-11-20"),
                         tool("ask_owner", case_id="C-G1#1", **ask2)), turn(text("T1 nahi aaye; Salim ke liye poocha.")))  # fmt: skip
    p = w.make(client)
    p.handle(w.bus.emit("alarm", machine="C-G1", factory="C", idem="a:1"))
    p.handle(
        w.bus.emit("owner_reply", factory="C", idem="wa:1", payload={"text": "1", "channel": "whatsapp"})
    )
    assert sent(w, "book_job")[0]["technician"] == "T1"
    w.clock.t += 3 * 86400
    w.agent.tick()
    ev = next(e for e in w.bus.pending() if e.type == "mechanic_no_show")
    out = p.handle(ev)
    assert out["effects"]["replanned"]["excluded"] == "T1"
    cands = out["calls"][0]["result"]["candidates"]
    assert next(c for c in cands if c["id"] == "T1")["declined_open_case"] is True
    assert w.store.approval("C-G1#1:booking:2")["status"] == "requested"  # a new visit needs a new yes
