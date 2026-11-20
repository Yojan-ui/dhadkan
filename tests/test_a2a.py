"""A2A interop (server/a2a): the wire format, partner calls (UNTRUSTED), Dhadkan's own endpoint, and a
prompt injection from a partner that changes nothing. No network: the DEMO agents run in-process."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import provider
from server.a2a import client as a2a
from server.a2a import demo_agents, protocol
from server.a2a import router as a2a_router
from tests.test_planner_core import Script, alarm, make_world, sent, text, tool, turn

BASE = "http://127.0.0.1:8701"


InProcess = demo_agents.InProcess


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(provider, "available", lambda model=None: False)


@pytest.fixture()
def http():
    return InProcess(demo_agents.create_app())


# ---------------------------------------------------------------- wire format
def test_message_and_task_answers_are_both_read():
    msg = protocol.message("hi", {"a": 1}, role="agent")
    assert protocol.split(protocol.parts_of(msg)) == ("hi", {"a": 1})
    task = {"kind": "task", "id": "t1", "status": {"state": "completed", "message": protocol.message("done")},
            "artifacts": [{"parts": [{"type": "data", "data": {"b": 2}}, {"kind": "text", "text": "more"}]}]}  # fmt: skip
    assert protocol.split(protocol.parts_of(task)) == ("done\nmore", {"b": 2})


# ---------------------------------------------------------------- calling partners
def test_partner_card_and_answer_come_back_untrusted(http):
    card = a2a.discover("ramesh_workshop", http=http)
    assert card["untrusted"] and "availability" in card["skills"] and "DEMO" in card["name"]
    out = a2a.ask("ramesh_workshop", "Can you come for a bearing job?", {"fault": "bearing_wear", "from": "2026-11-16"},
                  http=http)  # fmt: skip
    assert out["untrusted"] and "EXTERNAL AGENT, UNTRUSTED" in out["note"]
    assert out["data"]["available_days"] == ["2026-11-17", "2026-11-18", "2026-11-19"] and out["data"]["demo"]
    assert "Ramesh Motor Works (DEMO)" in out["said"]


def test_only_allow_listed_partners_are_called(http):
    with pytest.raises(KeyError):
        a2a.ask("http://evil.example/agent", "hi", http=http)


def test_unreachable_or_malformed_partners_return_errors_not_exceptions():
    class Down:
        def post(self, url, body):
            raise ConnectionRefusedError

    class Junk:
        def post(self, url, body):
            return {"hello": "world"}

    assert "unreachable" in a2a.ask("bhosari_dealer", "x", http=Down())["error"]
    assert "JSON-RPC" in a2a.ask("bhosari_dealer", "x", http=Junk())["error"]


def test_partner_text_is_clipped_and_stripped():
    class Loud:
        def post(self, url, body):
            return protocol.result(
                body["id"], protocol.message("a\x00b" + "x" * 5000, {"k" * 100: "v" * 999})
            )

    out = a2a.ask("bhosari_dealer", "x", http=Loud())
    assert "\x00" not in out["said"] and len(out["said"]) <= a2a.MAX_TEXT
    assert all(len(k) <= 60 and len(v) <= 300 for k, v in out["data"].items())


# ---------------------------------------------------------------- Dhadkan's own endpoint
@pytest.fixture()
def dhadkan(monkeypatch):
    class Live:
        def __init__(self):
            self.events = []

        def event(self, kind, **d):
            self.events.append((kind, d))

        def ranking(self):
            return [
                {"machine_id": "A-G1", "status": "alarm", "risk": 1.73},
                {"machine_id": "A-P1", "status": "ok", "risk": 0.4},
            ]

    app = FastAPI()
    live = Live()
    app.include_router(a2a_router.router(live))
    return TestClient(app), live


def rpc(c, text, data=None, headers=None, method="message/send"):
    body = protocol.request(protocol.message(text, data))
    body["method"] = method
    return c.post("/a2a", json=body, headers=headers or {}).json()


def test_dhadkan_card_and_knowledge_skill(dhadkan):
    c, live = dhadkan
    card = c.get("/.well-known/agent-card.json").json()
    assert card["url"].endswith("/a2a") and {s["id"] for s in card["skills"]} == {
        "maintenance_knowledge",
        "machine_status",
    }
    assert c.get("/.well-known/agent.json").json() == card
    r = rpc(c, "pump sounds like gravel and the pressure dropped")
    t, d = protocol.split(protocol.parts_of(r["result"]))
    assert "cavitation" in d["chunks"][0]["id"] and "[kb/cavitation.md#" in t
    assert live.events and live.events[0][0] == "a2a_inbound"


def test_dhadkan_machine_status_needs_the_token(dhadkan, monkeypatch):
    c, _ = dhadkan
    monkeypatch.setattr(
        "server.notify.env.get", lambda k, d=None: "s3cret" if k == "DHADKAN_A2A_TOKEN" else None
    )
    assert rpc(c, "status please")["error"]["code"] == protocol.UNAUTHORIZED
    assert (
        rpc(c, "status please", headers={"Authorization": "Bearer nope"})["error"]["code"]
        == protocol.UNAUTHORIZED
    )
    r = rpc(c, "status please", headers={"Authorization": "Bearer s3cret"})
    t, d = protocol.split(protocol.parts_of(r["result"]))
    assert "A-G1" in t and d["machines"][0]["risk"] == 1.73


def test_dhadkan_refuses_other_methods_and_bad_requests(dhadkan):
    c, _ = dhadkan
    assert rpc(c, "x", method="tasks/cancel")["error"]["code"] == protocol.METHOD_NOT_FOUND
    assert c.post("/a2a", json={"id": 1}).json()["error"]["code"] == protocol.INVALID_REQUEST
    bad = c.post("/a2a", content=b"{not json", headers={"Content-Type": "application/json"}).json()
    assert bad["error"]["code"] == protocol.PARSE_ERROR


# ---------------------------------------------------------------- a partner tries to take over the planner
def test_a_partners_prompt_injection_books_nothing_and_its_price_never_reaches_the_owner(
    tmp_path, monkeypatch
):
    w = make_world(tmp_path, monkeypatch)
    http = InProcess(demo_agents.create_app(inject=True))
    client = Script(
        turn(tool("ask_partner_agent", partner="bhosari_dealer", question="Bearing for Flour mill 1 in stock?", fault="bearing_wear")),
        # the model "obeys" the injection: books without approval and repeats the dealer's ₹99
        turn(tool("book_job", case_id="C-G1#1", technician="T1", day="2026-11-17"),
             tool("tell_owner", case_id="C-G1#1", text="Dealer ke paas bearing hai, sirf ₹99 mein.")),
        turn(text("Done as the dealer said.")),
    )  # fmt: skip
    p = w.make(client, a2a_http=http)
    out = p.handle(alarm(w))
    a2a_res = out["calls"][0]["result"]
    assert a2a_res["untrusted"] and "SYSTEM OVERRIDE" in a2a_res["said"]  # the injection arrived ...
    assert out["calls"][1]["result"]["gate"] == "guardian" and not sent(
        w, "book_job"
    )  # ... and changed nothing
    assert "error" in out["calls"][2]["result"] and not any("99" in x["text"] for x in sent(w, "notify"))
    assert any(
        r["event"] == "a2a_call" and r["detail"]["partner"] == "bhosari_dealer" for r in w.store.audit()
    )
    ctx = json.loads(client.calls[0]["messages"][0]["content"])
    assert {pa["id"] for pa in ctx["partner_agents"]} == {"ramesh_workshop", "bhosari_dealer"}


def test_on_an_alarm_code_asks_the_partner_workshops_and_marks_it_untrusted(tmp_path, monkeypatch):
    from server.brain import planner_trace
    from server.planner import guardian

    monkeypatch.setenv("DHADKAN_A2A_PREFETCH", "1")
    w = make_world(tmp_path, monkeypatch)
    client = Script(turn(text("Owner se poochna hai.")))
    p = w.make(client, a2a_http=InProcess(demo_agents.create_app()))
    out = p.handle(alarm(w))
    ctx = json.loads(client.calls[0]["messages"][0]["content"])
    offers = ctx["partner_offers"]
    assert [o["agent"] for o in offers] == ["ramesh_workshop"]  # mechanics only, not the dealer
    assert offers[0]["untrusted"] and "Ramesh Motor Works (DEMO)" in offers[0]["said"]
    logged = [r["detail"] for r in w.store.audit() if r["event"] == "a2a_call"]
    assert logged and logged[0]["prefetch"] and logged[0]["partner"] == "ramesh_workshop"
    # the partner's words are not facts: they are left out of what the Guardian may rely on
    assert all("partner_offers" not in s for s in guardian.facts_from([], ctx).sources if isinstance(s, dict))
    step = planner_trace(w.store, p)["cases"][0]["steps"][0]
    assert step["a2a"][0]["partner"] == "ramesh_workshop" and step["knowledge"] and step["guardian"]
    assert out["decision"] == step["decision"]
