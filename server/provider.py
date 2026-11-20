"""The LLM provider: IBM Granite on a local Ollama server. No cloud model, no API key, no per-token bill.

    client = provider.make_client()      # None when DHADKAN_LLM=0 or Ollama / the model is not there
    resp = client.messages.create(model=..., system=..., tools=[...], messages=[...], max_tokens=...)

The client speaks the message shape the rest of the code already uses (content blocks of type text,
thinking and tool_use; tool_result blocks in the next user turn; stop_reason; usage.input_tokens and
usage.output_tokens), and translates it to Ollama's /api/chat. So the planner's bounded loop, the owner
agent, the daily digest, the second-look diagnosis and the sound labeller all run on Granite unchanged,
and the scripted mock clients in the tests keep working.

What maps to what:
    system (str or text blocks)           -> a system message
    tools [{name, description, input_schema}] -> Ollama function tools
    assistant tool_use blocks             -> assistant tool_calls
    user tool_result blocks               -> role "tool" messages (with the tool's name)
    output_config.format json_schema      -> Ollama structured output (format = the schema)
    image blocks                          -> a one-line note (the Granite chat model is text-only)
    output_config.effort "high"           -> think=True when DHADKAN_GRANITE_THINK=1 (off by default: slow on CPU)
    betas, fallbacks, cache_control, tool_choice -> ignored (Ollama has no such options)

Offline is normal: when Ollama does not answer, make_client() returns None and every caller runs its rules.

Settings (.env; defaults in brackets):
    OLLAMA_URL            [http://127.0.0.1:11434]
    DHADKAN_MODEL         [ibm/granite4.2:8b-q3_K_M]   the chat / tool-calling model
    DHADKAN_EMBED_MODEL   [granite-embedding]          the knowledge-base embedder (server/kb)
    DHADKAN_NUM_CTX       [8192]                       context window asked of Ollama
    DHADKAN_LLM_TIMEOUT_S [180]                        one call; a CPU-only laptop is slow
    DHADKAN_LLM=0         rules only, never call a model
"""

import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from server.notify import env

NAME = "granite"  # the engine label in decisions, traces and the UI
DEFAULT_MODEL = "ibm/granite4.2:8b-q3_K_M"
DEFAULT_EMBED = "granite-embedding"
_seen: dict[str, tuple[float, bool]] = {}  # url+model -> (checked at, available); re-checked every 30 s


def url() -> str:
    return (env.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")


def chat_model() -> str:
    return env.get("DHADKAN_MODEL") or DEFAULT_MODEL


def embed_model() -> str:
    return env.get("DHADKAN_EMBED_MODEL") or DEFAULT_EMBED


def enabled() -> bool:
    return env.get("DHADKAN_LLM") != "0" and env.get("DHADKAN_PLANNER") != "0"


def _http(path: str, body: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url() + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def models() -> list[str]:
    """Models the Ollama server has pulled ([] when it does not answer)."""
    try:
        return [m["name"] for m in _http("/api/tags", timeout=2.0).get("models", [])]
    except (OSError, ValueError, urllib.error.URLError):
        return []


def has_model(name: str, pulled: list[str]) -> bool:
    return name in pulled or f"{name}:latest" in pulled


def available(model: str | None = None) -> bool:
    """Ollama answers and has `model` (the chat model by default). Cached for 30 s."""
    if not enabled():
        return False
    model = model or chat_model()
    key = url() + "|" + model
    hit = _seen.get(key)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    ok = has_model(model, models())
    _seen[key] = (time.monotonic(), ok)
    return ok


def status() -> dict[str, Any]:
    """For the UI and llm-check: what is configured and what is actually there."""
    pulled = models()
    return {"provider": "ollama", "url": url(), "model": chat_model(), "embed_model": embed_model(),
            "enabled": enabled(), "reachable": bool(pulled), "has_model": has_model(chat_model(), pulled),
            "has_embed_model": has_model(embed_model(), pulled), "pulled": pulled}  # fmt: skip


def make_client() -> "GraniteClient | None":
    """A Granite client when the model is reachable and DHADKAN_LLM is not 0; otherwise None (rules)."""
    return GraniteClient() if available() else None


def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Vectors for `texts` from the embedding model. Raises OSError when Ollama is down."""
    out = _http("/api/embed", {"model": model or embed_model(), "input": texts}, timeout=120.0)
    return out["embeddings"]


# ---------------------------------------------------------------------- translation
def _get(b: Any, k: str, default: Any = None) -> Any:
    return b.get(k, default) if isinstance(b, dict) else getattr(b, k, default)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(str(_get(b, "text", "")) for b in content or [] if _get(b, "type") == "text")


def to_ollama(system: Any, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sys_text = _text_of(system) if system else ""
    if sys_text:
        out.append({"role": "system", "content": sys_text})
    names: dict[str, str] = {}  # tool_use id -> tool name, for the tool messages that answer it
    for m in messages:
        role, content = m["role"], m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            calls = []
            for b in content or []:
                if _get(b, "type") == "tool_use":
                    names[str(_get(b, "id"))] = str(_get(b, "name"))
                    calls.append(
                        {"function": {"name": _get(b, "name"), "arguments": dict(_get(b, "input") or {})}}
                    )
            msg: dict[str, Any] = {"role": "assistant", "content": _text_of(content)}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        text = []
        for b in content or []:
            if _get(b, "type") == "tool_result":
                body = _get(b, "content")
                body = body if isinstance(body, str) else _text_of(body)
                if _get(b, "is_error"):
                    body = "ERROR: " + body
                out.append(
                    {"role": "tool", "content": body, "tool_name": names.get(str(_get(b, "tool_use_id")), "")}
                )
            elif _get(b, "type") == "text":
                text.append(str(_get(b, "text", "")))
            elif _get(b, "type") == "image":  # Granite here is text-only: say so rather than drop it silently
                text.append(
                    "[an image was attached here; this text-only model cannot see it, use the numbers]"
                )
        if text:
            out.append({"role": role, "content": "".join(text)})
    return out


def to_ollama_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in tools or []]  # fmt: skip


def from_ollama(raw: dict[str, Any], model: str) -> SimpleNamespace:
    msg = raw.get("message") or {}
    blocks: list[SimpleNamespace] = []
    if msg.get("thinking"):
        blocks.append(SimpleNamespace(type="thinking", thinking=msg["thinking"]))
    if msg.get("content"):
        blocks.append(SimpleNamespace(type="text", text=msg["content"]))
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        blocks.append(SimpleNamespace(type="tool_use", id="toolu_" + uuid.uuid4().hex[:16],
                                      name=fn.get("name", ""), input=args if isinstance(args, dict) else {}))  # fmt: skip
    has_tools = any(b.type == "tool_use" for b in blocks)
    stop = "tool_use" if has_tools else ("max_tokens" if raw.get("done_reason") == "length" else "end_turn")
    usage = SimpleNamespace(input_tokens=int(raw.get("prompt_eval_count") or 0),
                            output_tokens=int(raw.get("eval_count") or 0),
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)  # fmt: skip
    return SimpleNamespace(id="msg_" + uuid.uuid4().hex[:16], model=raw.get("model", model), content=blocks,
                           stop_reason=stop, usage=usage, role="assistant", type="message")  # fmt: skip


# ---------------------------------------------------------------------- the client
class _Batches:
    """Local stand-in for Message Batches: runs each request now, keeps the results by batch id."""

    def __init__(self, messages: "_Messages") -> None:
        self.m = messages
        self.results: dict[str, list[dict[str, Any]]] = {}

    def create(self, requests: list[dict[str, Any]]) -> SimpleNamespace:
        bid = "local_batch_" + uuid.uuid4().hex[:12]
        rows = []
        for r in requests:
            try:
                rows.append({"custom_id": r["custom_id"], "message": self.m.create(**r["params"])})
            except OSError as e:
                rows.append({"custom_id": r["custom_id"], "error": type(e).__name__})
        self.results[bid] = rows
        return SimpleNamespace(id=bid, processing_status="ended", request_counts={"total": len(rows)})


class _Messages:
    def __init__(self, client: "GraniteClient") -> None:
        self.c = client
        self.batches = _Batches(self)

    def create(self, *, messages: list[dict[str, Any]], model: str | None = None, system: Any = None,
               tools: list[dict[str, Any]] | None = None, max_tokens: int = 4000,
               output_config: dict[str, Any] | None = None, **_ignored: Any) -> SimpleNamespace:  # fmt: skip
        model = model or chat_model()
        oc = output_config or {}
        body: dict[str, Any] = {"model": model, "messages": to_ollama(system, messages), "stream": False,
                                "options": {"temperature": 0, "num_predict": int(max_tokens),
                                            "num_ctx": int(env.get("DHADKAN_NUM_CTX") or 8192)}}  # fmt: skip
        if tools:
            body["tools"] = to_ollama_tools(tools)
        fmt = oc.get("format") or {}
        if fmt.get("type") == "json_schema" and fmt.get("schema"):
            body["format"] = fmt["schema"]
        body["think"] = oc.get("effort") == "high" and env.get("DHADKAN_GRANITE_THINK") == "1"
        raw = _http("/api/chat", body, timeout=float(env.get("DHADKAN_LLM_TIMEOUT_S") or 180))
        self.c.calls += 1
        return from_ollama(raw, model)

    @contextmanager
    def stream(self, **kw: Any) -> Iterator[SimpleNamespace]:
        """Not streamed: one call, wrapped so `with client.messages.stream(...) as s: s.get_final_message()` works."""
        msg = self.create(**kw)
        yield SimpleNamespace(get_final_message=lambda: msg)


class GraniteClient:
    """Granite on Ollama in the client shape the code uses (messages.create, beta.messages.create, stream)."""

    provider = "ollama"

    def __init__(self) -> None:
        self.calls = 0
        self.messages = _Messages(self)
        self.beta = SimpleNamespace(messages=self.messages)
