"""Every planner evaluation scenario (server/planner/evaluate.py) with the scripted mock LLM: no network.

Each must pass its own check, with no approval-gate violation and an intact audit chain."""

import pytest

from server.planner import evaluate as ev


@pytest.mark.parametrize("sc", ev.SCENARIOS, ids=[s["name"] for s in ev.SCENARIOS])
def test_scenario(sc):
    r = ev.run_one(sc, live=False)
    assert r["error"] is None, r["error"]
    assert r["ok"], r["note"]
    assert r["gate_violations"] == 0 and r["audit_ok"]
    assert (
        r["language_ok"] == r["owner_messages"]
    ), f"{r['owner_messages'] - r['language_ok']} messages off-language"


def test_summary_metrics_are_computed():
    rows = [
        ev.run_one(s, live=False)
        for s in ev.SCENARIOS
        if s["name"] in ("approve_then_book", "invented_price")
    ]
    s = ev.summarise(rows)
    assert s["gate_violations"] == 0 and s["grounding_rejections"] >= 2
    assert s["tokens_per_alarm"] > 0 and s["inr_per_alarm"] > 0 and s["latency_ms_p50"] is not None
    assert len(ev.SCENARIOS) >= 30
