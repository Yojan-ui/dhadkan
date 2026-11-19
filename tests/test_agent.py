from types import SimpleNamespace as NS

import pytest

from server import agent, registry
from server.sim import Cluster
from tests.conftest import needs_recordings

CFG = {
    "start": "2026-11-09T06:00:00",
    "tick_hours": 8,
    "learning_days": 0,  # these tests are about the status logic, not the learning period
    "road_kmph": 20,
    "technician": {"name": "Ramesh (mistri)", "name_local": "रमेश", "xy_km": [0, 0], "hours_per_day": 7},
    "service_hours": {"pump": 1.0, "fan": 0.75, "gearbox": 1.5},
    "factories": [
        {
            "id": "A",
            "name": "A Works",
            "area": "Bhosari",
            "area_local": "भोसरी",
            "xy_km": [0, 0],
            "owner": "S",
            "language": "mr",
            "phone": "1",
        },
        {
            "id": "B",
            "name": "B Works",
            "area": "Chakan",
            "xy_km": [10, 0],
            "owner": "R",
            "language": "hi",
            "phone": "2",
        },
    ],
    "machines": [
        {
            "id": "A-P1",
            "factory": "A",
            "name": "Pump 1",
            "name_local": "पंप 1",
            "type": "pump",
            "source": "pump/id_00",
        },
        {
            "id": "B-P2",
            "factory": "B",
            "name": "Pump 2",
            "name_local": "पंप 2",
            "type": "pump",
            "source": "pump/id_00",
        },
    ],
}


@pytest.fixture
def cluster(tmp_path):
    c = Cluster(tmp_path / "n.db", tmp_path / "r.db", cluster=CFG)
    fp = registry.publish(c.reg, "pump", "bearing", "खडखड", "A", "A-P1", [1.0] + [0.0] * 63)
    # A-P1 rising over 3 days to alarm; B-P2 below alarm but matching A's fingerprint.
    for tick in range(9):
        ts = c.now(tick).isoformat(timespec="minutes")
        for mid, risk, cos in (("A-P1", 0.6 + 0.12 * tick, None), ("B-P2", 0.7, 0.9)):
            c.db.execute(
                "INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tick,
                    ts,
                    mid,
                    "x.wav",
                    "normal",
                    0,
                    risk,
                    1.0,
                    risk,
                    cos,
                    0.5 if cos else None,
                    fp if cos else None,
                    "[]",
                ),
            )
    c.db.commit()
    return c


def test_ranking_statuses(cluster):
    t = agent.Tools(cluster, "t", "test")
    r = {x["machine_id"]: x for x in t.get_risk_ranking()}
    assert r["A-P1"]["status"] == "alarm" and r["A-P1"]["days_rising"] == 3
    assert r["B-P2"]["status"] == "early_match"
    assert r["B-P2"]["registry"]["fault"] == "bearing"


def test_rules_run_logs_every_tool_call(cluster):
    run_id, _note = agent.run_rules(cluster)
    tools = [
        r["tool"]
        for r in cluster.db.execute("SELECT tool FROM tool_calls WHERE run_id=? ORDER BY id", (run_id,))
    ]
    assert tools[:2] == ["get_risk_ranking", "plan_technician_route"]
    assert tools.count("draft_owner_alert") == 2
    msgs = [r["body"] for r in cluster.db.execute("SELECT body FROM messages")]
    assert any(m.startswith("पंप 1: बेअरिंगचा आवाज 3 दिवसांपासून वाढतोय") for m in msgs)
    assert any("भोसरी की एक मशीन में बेयरिंग ख़राब होने से पहले" in m for m in msgs)


def test_reply_books_visit_only(cluster):
    agent.run_rules(cluster)
    mid = cluster.db.execute("SELECT id FROM messages LIMIT 1").fetchone()[0]
    assert agent.record_reply(cluster, mid, "हो") is True
    row = cluster.db.execute("SELECT approved, reply FROM messages WHERE id=?", (mid,)).fetchone()
    assert tuple(row) == (1, "हो")


class FakeClaude:
    """Asks for the ranking, then drafts one alert, then finishes."""

    def __init__(self):
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        n = len(self.calls)
        if n == 1:
            return NS(
                stop_reason="tool_use",
                content=[NS(type="tool_use", id="t1", name="get_risk_ranking", input={})],
            )
        if n == 2:
            return NS(
                stop_reason="tool_use",
                content=[
                    NS(
                        type="tool_use",
                        id="t2",
                        name="draft_owner_alert",
                        input={"machine_id": "A-P1", "kind": "rising", "visit_day": "Tuesday"},
                    )
                ],
            )
        return NS(stop_reason="end_turn", content=[NS(type="text", text="Tuesday: A-P1")])


def test_claude_loop_with_fake_client(cluster):
    fake = FakeClaude()
    run_id, note = agent.run_claude(cluster, client=fake)
    assert note == "Tuesday: A-P1"
    assert fake.calls[0]["model"] == "claude-fable-5-2" and "thinking" not in fake.calls[0]
    assert fake.calls[0]["output_config"] == {"effort": "low"} and "tool_choice" not in fake.calls[0]
    assert {t["name"] for t in fake.calls[0]["tools"]} == {t["name"] for t in agent.TOOLS}
    # tool results went back to the model in one user turn each
    last = fake.calls[-1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["tool_use_id"] == "t2"
    logged = cluster.db.execute("SELECT actor, tool FROM tool_calls WHERE run_id=?", (run_id,)).fetchall()
    assert [tuple(r) for r in logged] == [("claude", "get_risk_ranking"), ("claude", "draft_owner_alert")]


def test_no_repeat_question_while_open(cluster):
    agent.run_rules(cluster)
    n = cluster.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    agent.run_rules(cluster)
    assert cluster.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == n


def test_single_stray_match_is_not_early(cluster):
    cluster.db.execute("UPDATE readings SET match_cos=0.1 WHERE machine='B-P2' AND tick IN (7, 8)")
    cluster.db.commit()
    r = {x["machine_id"]: x for x in agent.Tools(cluster, "t", "t").get_risk_ranking()}
    assert r["B-P2"]["status"] == "ok"


@needs_recordings
def test_new_machine_learns_before_it_can_alarm(tmp_path):
    """A machine raises no alarm and gets no rupees at risk while it is still learning its normal."""
    import copy

    from server import agent

    c = Cluster(tmp_path / "l.db", tmp_path / "lr.db", cluster={**copy.deepcopy(CFG), "learning_days": 7})
    c.step()
    rows = agent.Tools(c, "view", "test").get_risk_ranking()
    assert rows and all(r["status"] == "learning" and r["rupees_at_risk"] == 0 for r in rows)
    assert rows[0]["learning"] == {"day": 1, "of": 7}
