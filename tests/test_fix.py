"""Fix (spec section 4): the mechanic's job only after the owner's logged approval; parts forecast; dealer batch."""

import pytest

from server import fix, provider
from tests.conftest import OWNER_A, ims_world, needs_ims

MECHANIC_A = "+91 90000 33333"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("DHADKAN_MECHANIC_A", MECHANIC_A)
    yield ims_world(tmp_path_factory.mktemp("fix"), mp)
    mp.undo()


@needs_ims
def test_no_job_without_the_owners_logged_approval(world):
    _, _, live, rec = world
    a = next(x for x in live.alerts.notifier.recent(50) if x["machine"] == "A-G1" and x["kind"] == "alert")
    with pytest.raises(fix.ApprovalMissing):
        fix.send_job(live.alerts, a)
    assert not any(s["to"] == MECHANIC_A for s in rec.sent)


@needs_ims
def test_mechanic_bulao_sends_the_job_with_part_time_and_stock(world):
    _, client, live, rec = world
    client.post("/api/alerts/reply", json={"phone": OWNER_A, "text": "Mechanic bulao"})
    job = [s for s in rec.sent if s["to"] == MECHANIC_A]
    assert len(job) == 1
    assert "6205" in job[0]["text"] and "डेमो डेटा" in job[0]["text"]  # part to bring; stock labelled mock
    jobs = [a for a in live.alerts.notifier.recent(50) if a["kind"] == "job"]
    assert jobs and jobs[0]["machine"] == "A-G1" and jobs[0]["status"] == "sent"


@needs_ims
def test_parts_forecast_counts_machines_from_real_forecasts(world):
    _, client, *_ = world
    out = client.get("/api/forecast/parts?days=14").json()
    for e in out["rows"]:
        assert 0 <= e["likely"] <= e["may"] == len(e["machines"])
    assert "mock" in out["stock_note"]


def test_dealer_batch_requests_and_numbers_check():
    rows = [
        {"part": "ball bearing 6205", "area": "Bhosari", "may": 3, "likely": 1, "machines": ["a", "b", "c"]}
    ]
    reqs = fix.dealer_batch_requests(rows, {"bhosari-bearing": "hi_latn"})
    p = reqs[0]["params"]
    assert p["model"] == provider.chat_model() and "fallbacks" not in p and "thinking" not in p
    assert p["output_config"]["effort"] == "low" and "1-3 machines" in p["messages"][0]["content"]
    facts = fix.forecast_text(rows)
    assert fix.numbers_kept("6205 bearing: 1-3 machines in 14 days", facts)
    assert not fix.numbers_kept("6205 bearing: 9 needed", facts)  # an invented number is caught

    class Batches:
        def create(self, requests):
            self.got = requests
            return type("B", (), {"id": "msgbatch_1"})()

    client = type("C", (), {"messages": type("M", (), {"batches": Batches()})()})()
    assert fix.submit_dealer_batch(rows, {"d": "en"}, client=client) == "msgbatch_1"
