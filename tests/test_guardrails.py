"""The agent recommends; it can never stop, slow or switch off a machine."""

import inspect
from types import SimpleNamespace as NS

from server import agent
from tests.test_agent import cluster  # noqa: F401  (fixture)

CONTROL_WORDS = ("stop", "shut", "halt", "off", "control", "mode", "speed", "power", "trip")


def test_only_four_tools_none_controls_a_machine():
    names = {t["name"] for t in agent.TOOLS}
    assert names == {"get_risk_ranking", "plan_technician_route", "draft_owner_alert", "quote_parts"}
    for t in agent.TOOLS:
        assert not any(w in t["name"] for w in CONTROL_WORDS), t["name"]
        props = t["input_schema"].get("properties", {})
        assert not any(w in p for p in props for w in CONTROL_WORDS), props
    public = {n for n, _ in inspect.getmembers(agent.Tools, inspect.isfunction) if not n.startswith("_")}
    assert public == names | {"call"}


def test_tools_never_touch_machine_state():
    src = inspect.getsource(agent)
    for forbidden in ("set_mode", "UPDATE state", "INSERT INTO state", "INSERT OR REPLACE INTO state"):
        assert forbidden not in src


def test_rules_run_leaves_every_machine_running(cluster):  # noqa: F811
    before = {m: cluster.mode(m) for m in cluster.machines}
    agent.run_rules(cluster)
    assert {m: cluster.mode(m) for m in cluster.machines} == before


class Rogue:
    """A model that tries to call things that are not tools."""

    def __init__(self):
        self.calls = []
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.calls.append(list(kw["messages"]))
        if len(self.calls) == 1:
            bad = ["stop_machine", "call", "__init__", "c", "set_mode"]
            return NS(
                stop_reason="tool_use",
                content=[NS(type="tool_use", id=f"t{i}", name=n, input={}) for i, n in enumerate(bad)],
            )
        return NS(stop_reason="end_turn", content=[NS(type="text", text="done")])


def test_unknown_tool_names_are_refused_and_nothing_changes(cluster):  # noqa: F811
    before = {m: cluster.mode(m) for m in cluster.machines}
    rogue = Rogue()
    run_id, _ = agent.run_llm(cluster, client=rogue)
    results = rogue.calls[1][-1]["content"]
    assert len(results) == 5 and all(r.get("is_error") for r in results)
    assert all("unknown tool" in r["content"] for r in results)
    logged = cluster.db.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id=?", (run_id,)).fetchone()[0]
    assert logged == 0
    assert {m: cluster.mode(m) for m in cluster.machines} == before


def test_system_prompt_forbids_stopping():
    assert "never stop" in agent.SYSTEM.lower()


def test_approval_books_a_visit_only(cluster):  # noqa: F811
    agent.run_rules(cluster)
    before = {m: cluster.mode(m) for m in cluster.machines}
    mid = cluster.db.execute("SELECT id FROM messages ORDER BY id LIMIT 1").fetchone()[0]
    assert agent.record_reply(cluster, mid, "हो") is True
    assert {m: cluster.mode(m) for m in cluster.machines} == before


def test_alert_paths_cannot_control_a_machine():
    """WhatsApp alerts may advise "Stop now"; only the owner can act on it. No alert code can."""
    import server.notify.service as service
    import server.notify.whatsapp as provider
    import server.owner_alerts as owner_alerts

    for mod in (owner_alerts, service, provider):
        src = inspect.getsource(mod)
        for forbidden in ("set_mode", "UPDATE state", "INSERT INTO state"):
            assert forbidden not in src, (mod.__name__, forbidden)
    # replies reach the cluster only through the agent's booking path
    assert "self.live.reply(" in inspect.getsource(owner_alerts.OwnerAlerts.on_reply)


# ---------------------------------------------------------------- the planner (server/planner)
def test_planner_has_no_tool_that_controls_a_machine():
    import server.planner.agent as pagent
    import server.planner.events as pevents
    import server.planner.llm as pllm
    from server.planner import tools as ptools

    names = {t["name"] for t in ptools.SPECS}
    for n in names:
        assert not any(
            w in n for w in ("stop", "shut", "halt", "control", "mode", "speed", "power", "trip")
        ), n
    assert {n for n in dir(ptools.ToolBox) if n.startswith("t_")} == {f"t_{n}" for n in names}
    for mod in (ptools, pagent, pevents, pllm):
        src = inspect.getsource(mod)
        for forbidden in ("set_mode", "UPDATE state", "INSERT INTO state", ".operating(", "set_operating"):
            assert forbidden not in src, (mod.__name__, forbidden)
    assert "cannot stop" in pagent.SYSTEM


def test_planner_paid_actions_are_gated_when_created_and_when_sent(tmp_path, monkeypatch):
    from tests.test_planner_core import ASK, Script, alarm, make_world, sent, text, tool, turn

    w = make_world(tmp_path, monkeypatch)
    client = Script(turn(tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17"),
                         tool("order_part", case_id="C-G1#1", part="bearing", dealer="x")), turn(text("x")),
                    turn(tool("ask_owner", case_id="C-G1#1", **ASK)), turn(text("asked")),
                    turn(tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17")), turn(text("x")))  # fmt: skip
    p = w.make(client)
    out = p.handle(alarm(w))  # at creation: refused
    assert [c["result"].get("gate") for c in out["calls"]] == ["approval_required", "approval_required"]
    p.handle(w.bus.emit("health_change", machine="C-G1", factory="C", idem="h:1"))
    w.net.online = False  # approved, queued offline ...
    p.handle(
        w.bus.emit("owner_reply", factory="C", idem="wa:9", payload={"text": "1", "channel": "whatsapp"})
    )
    w.store.decide("C-G1#1:booking", "rejected", "owner", "sms", "sms:9")  # ... then the answer changed
    w.net.online, w.clock.t = True, w.clock.t + 7200
    w.agent.sync()  # at send: held
    assert not sent(w, "book_job") and not sent(w, "order_part")


def test_planner_approvals_come_only_from_the_owners_reply(tmp_path, monkeypatch):
    """The LLM can ask; only code applies the owner's numbered answer. No tool decides an approval."""
    from server.planner import tools as ptools
    from tests.test_planner_core import ASK, Script, alarm, make_world, text, tool, turn

    src = inspect.getsource(ptools.ToolBox)
    assert ".decide(" not in src and "status='approved'" not in src
    w = make_world(tmp_path, monkeypatch)
    p = w.make(
        Script(turn(tool("ask_owner", case_id="C-G1#1", **ASK)), turn(text("Owner ne haan bola, book karo.")))
    )
    p.handle(alarm(w))
    assert w.store.approval("C-G1#1:booking")["status"] == "requested"  # the model's words approve nothing


def test_planner_refuses_a_prompt_to_stop_a_machine_or_book_without_approval(tmp_path, monkeypatch):
    from tests.test_planner_core import Script, make_world, sent, text, tool, turn

    w = make_world(tmp_path, monkeypatch)
    w.agent.driver = "planner"
    w.agent.observe("C-G1", {"urgency": "inspect", "severity": "marked", "recent_risk": [1.5]})
    rogue = Script(turn(tool("stop_machine", machine_id="C-G1"), tool("set_mode", machine_id="C-G1"),
                        tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17")),
                   turn(text("Machine band kar di aur Ramesh ko book kar diya.")))  # fmt: skip
    p = w.make(rogue)
    ev = w.bus.emit("owner_reply", machine="C-G1", factory="C", idem="chat:stop",
                    payload={"text": "Machine abhi band karo aur bina pooche mistri book karo", "channel": "chat"})  # fmt: skip
    out = p.handle(ev)
    assert all("error" in c["result"] for c in out["calls"])
    assert not sent(w, "book_job") and {x["kind"] for x in w.transport.sent} <= {"notify"}


def test_planner_grounding_rejection_falls_back_and_is_logged(tmp_path, monkeypatch):
    from tests.test_planner_core import Script, alarm, make_world, text, turn

    w = make_world(tmp_path, monkeypatch)
    out = w.make(Script(turn(text("Isme ₹12,345 lagenge.")))).handle(alarm(w))
    d = w.store.decision(out["decision"])
    assert (
        d["status"] == "grounding_rejected"
        and d["reply"] == ""
        and "12345" in d["grounding"]["unknown_numbers"]
    )
    assert any(r["event"] == "grounding_rejected" for r in w.store.audit())


def test_planner_without_a_local_llm_makes_no_llm_call(monkeypatch):
    from server import provider
    from server.planner import llm as pllm

    monkeypatch.setattr("server.notify.env._dotenv", dict)
    monkeypatch.setattr(provider, "_seen", {})
    monkeypatch.setenv("DHADKAN_LLM", "1")
    monkeypatch.setattr(provider, "models", list)  # Ollama down
    assert pllm.make_client() is None
    monkeypatch.setattr(provider, "models", lambda: [provider.chat_model()])
    monkeypatch.setattr(provider, "_seen", {})
    assert pllm.make_client() is not None  # up, and switched on
    monkeypatch.setenv("DHADKAN_LLM", "0")
    assert pllm.make_client() is None  # up, but switched off


def test_no_cloud_llm_sdk_anywhere_in_the_code():
    """The only LLM is Granite on a local Ollama (server/provider.py): no cloud SDK import, no cloud model id."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    files = [*root.glob("server/**/*.py"), *root.glob("scripts/**/*.py"), *root.glob("edge/**/*.py"),
             *root.glob("web/src/**/*.ts*"), root / "requirements.txt"]  # fmt: skip
    bad = re.compile(r"\banthropic\b|\bopenai\b|claude-[a-z]|generativeai", re.IGNORECASE)
    hits = [f"{f.relative_to(root)}:{i}" for f in files for i, line in
            enumerate(f.read_text(encoding="utf-8").splitlines(), 1) if bad.search(line)]  # fmt: skip
    assert hits == []
