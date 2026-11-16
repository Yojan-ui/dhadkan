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
        self.beta = NS(messages=NS(create=self.create))

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
    run_id, _ = agent.run_claude(cluster, client=rogue)
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
