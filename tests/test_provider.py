"""server/provider.py: the Granite-on-Ollama client. Translation both ways, offline fallback. No network."""

import json
from types import SimpleNamespace as NS

import pytest

from server import provider

TOOLS = [{"name": "get_machine_health", "description": "health", "strict": True,
          "input_schema": {"type": "object", "properties": {"machine_id": {"type": "string"}}}}]  # fmt: skip


@pytest.fixture()
def http(monkeypatch):
    """Records every Ollama request and answers with the queued raw responses."""
    sent, queue = [], []

    def fake(path, body=None, timeout=5.0):
        sent.append((path, body))
        if path == "/api/tags":
            return {"models": [{"name": provider.chat_model()}, {"name": provider.embed_model() + ":latest"}]}
        return queue.pop(0)

    monkeypatch.setattr(provider, "_http", fake)
    monkeypatch.setattr(provider, "_seen", {})
    monkeypatch.setenv("DHADKAN_LLM", "1")
    return NS(sent=sent, queue=queue)


def test_tool_call_round_trip(http):
    http.queue += [
        {"model": "g", "message": {"role": "assistant", "content": "",
                                   "tool_calls": [{"function": {"name": "get_machine_health",
                                                                "arguments": {"machine_id": "A-G1"}}}]},
         "done_reason": "stop", "prompt_eval_count": 120, "eval_count": 9},
        {"model": "g", "message": {"role": "assistant", "content": "A-G1 is in alarm."}, "done_reason": "stop"},
    ]  # fmt: skip
    c = provider.make_client()
    assert c is not None
    msgs = [{"role": "user", "content": "How is A-G1?"}]
    r = c.messages.create(system=[{"type": "text", "text": "Be brief."}], tools=TOOLS, messages=msgs,
                          max_tokens=300, betas=["x"], fallbacks="default", output_config={"effort": "low"})  # fmt: skip
    assert r.stop_reason == "tool_use" and r.usage.input_tokens == 120 and r.usage.output_tokens == 9
    use = r.content[0]
    assert use.type == "tool_use" and use.name == "get_machine_health" and use.input == {"machine_id": "A-G1"}
    body = http.sent[-1][1]
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}
    assert body["tools"][0]["function"]["name"] == "get_machine_health" and "betas" not in body
    assert body["options"]["num_predict"] == 300 and body["think"] is False
    msgs += [{"role": "assistant", "content": r.content},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id,
                                           "content": json.dumps({"status": "alarm"}), "is_error": False}]}]  # fmt: skip
    r2 = c.beta.messages.create(tools=TOOLS, messages=msgs, max_tokens=100)
    assert r2.stop_reason == "end_turn" and r2.content[0].text == "A-G1 is in alarm."
    sent = http.sent[-1][1]["messages"]
    assert sent[1]["tool_calls"][0]["function"] == {
        "name": "get_machine_health",
        "arguments": {"machine_id": "A-G1"},
    }
    assert sent[2] == {"role": "tool", "content": '{"status": "alarm"}', "tool_name": "get_machine_health"}


def test_json_schema_output_images_and_stream(http):
    http.queue.append({"message": {"content": '{"fault": "bearing_wear"}'}, "done_reason": "stop"})
    c = provider.GraniteClient()
    schema = {"type": "object", "properties": {"fault": {"type": "string"}}}
    msgs = [{"role": "user", "content": [{"type": "text", "text": "Look:"},
                                         {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]}]  # fmt: skip
    with c.messages.stream(
        messages=msgs, output_config={"format": {"type": "json_schema", "schema": schema}}
    ) as s:
        msg = s.get_final_message()
    assert json.loads(msg.content[0].text) == {"fault": "bearing_wear"}
    body = http.sent[-1][1]
    assert body["format"] == schema
    assert "text-only model" in body["messages"][0]["content"]  # the image is named, not silently dropped


def test_length_stop_and_string_arguments(http):
    http.queue.append({"message": {"content": "partial", "tool_calls": [
        {"function": {"name": "t", "arguments": '{"a": 1}'}}]}, "done_reason": "length"})  # fmt: skip
    r = provider.GraniteClient().messages.create(messages=[{"role": "user", "content": "x"}])
    assert r.stop_reason == "tool_use" and r.content[-1].input == {"a": 1}
    http.queue.append({"message": {"content": "cut"}, "done_reason": "length"})
    assert provider.GraniteClient().messages.create(
        messages=[{"role": "user", "content": "x"}]
    ).stop_reason == ("max_tokens")


def test_offline_or_switched_off_means_no_client(monkeypatch):
    monkeypatch.setattr(provider, "_seen", {})
    monkeypatch.setenv("DHADKAN_LLM", "1")
    monkeypatch.setattr(provider, "models", list)  # Ollama not answering
    assert provider.make_client() is None and not provider.status()["reachable"]
    monkeypatch.setattr(provider, "models", lambda: [provider.chat_model()])
    monkeypatch.setattr(provider, "_seen", {})
    assert provider.make_client() is not None
    monkeypatch.setenv("DHADKAN_LLM", "0")
    assert provider.make_client() is None


def test_local_batch_runs_every_request(http):
    http.queue += [
        {"message": {"content": '{"message": "hi"}'}},
        {"message": {"content": '{"message": "yo"}'}},
    ]
    c = provider.GraniteClient()
    reqs = [
        {"custom_id": f"d{i}", "params": {"messages": [{"role": "user", "content": "x"}]}} for i in range(2)
    ]
    b = c.messages.batches.create(requests=reqs)
    rows = c.messages.batches.results[b.id]
    assert [r["custom_id"] for r in rows] == ["d0", "d1"] and rows[1]["message"].content[
        0
    ].text == '{"message": "yo"}'
