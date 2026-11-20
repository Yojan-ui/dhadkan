"""Granite second look (server/understand.py): request shape, fallbacks, cost. No network."""

import json
from types import SimpleNamespace

import pytest

from server import provider, understand


def req():
    return understand.build_request(b"\x89PNG", [0.8, 0.9, 1.2], {"name": "Loom 1", "type": "gearbox"},
                                    {"name": "Shinde", "language": "hi"}, "hi_latn")  # fmt: skip


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, msg=None, error=None):
        self.calls, self.msg, self.error = [], msg, error
        self.messages = SimpleNamespace(stream=self.stream)

    def stream(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return FakeStream(self.msg)


def message(payload, stop="end_turn"):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=3000,
                            cache_creation_input_tokens=0)  # fmt: skip
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, usage=usage,
                           model=provider.chat_model())  # fmt: skip


GOOD = {"fault": "bearing_wear", "confidence": 0.8, "evidence": ["high band up"], "severity": "repair_soon",
        "hand_check": "Motor ka end garam hai?", "mechanic_check_needed": False}  # fmt: skip


@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setattr(understand, "LOG", tmp_path / "calls.jsonl")


def test_request_follows_the_spec():
    r = req()
    assert r["model"] == provider.chat_model() and "thinking" not in r and "tool_choice" not in r
    assert "betas" not in r and "fallbacks" not in r
    assert r["output_config"]["effort"] == "high" and r["output_config"]["format"]["type"] == "json_schema"
    assert "faults.yaml" in r["system"][1]["text"]  # the fault knowledge base goes in as text
    assert r["messages"][-1]["role"] == "user"  # no prefill
    assert set(r["output_config"]["format"]["schema"]["properties"]["fault"]["enum"]) >= {
        "bearing_wear",
        "unknown",
    }


def test_good_answer_is_used_and_costed():
    c = FakeClient(message(GOOD))
    out = understand.diagnose(req(), client=c, machine_id="A-G1")
    assert out["source"] == "granite" and out["fault"] == "bearing_wear" and not out["mechanic_check_needed"]
    assert out["severity_words"] == "Jaldi repair"
    assert out["cost"]["inr"] == 0 and out["cost"]["usd"] == 0  # a local model: no per-token bill
    row = json.loads(understand.LOG.read_text(encoding="utf-8").splitlines()[0])
    assert row["machine"] == "A-G1" and row["stop_reason"] == "end_turn" and "inr" in row


def test_unsure_answer_asks_for_a_mechanic():
    out = understand.diagnose(req(), client=FakeClient(message({**GOOD, "confidence": 0.3})))
    assert out["source"] == "granite" and out["mechanic_check_needed"]


@pytest.mark.parametrize("msg,why", [(message(GOOD, stop="refusal"), "refusal"), (message(GOOD, stop="max_tokens"), "max_tokens"),
                                      (message("not json"), "schema")])  # fmt: skip
def test_bad_stops_and_bad_output_fall_back_to_rules(msg, why):
    out = understand.diagnose(req(), client=FakeClient(msg))
    assert out["source"] == "rules" and out["expert_check"] == "pending" and why in out["why"]


def test_api_error_or_no_key_falls_back_to_rules(monkeypatch):
    out = understand.diagnose(req(), client=FakeClient(error=TimeoutError()))
    assert out["source"] == "rules" and "TimeoutError" in out["why"]
    monkeypatch.setattr(provider, "make_client", lambda: None)  # Ollama down
    assert understand.diagnose(req())["why"].startswith("no local LLM")
