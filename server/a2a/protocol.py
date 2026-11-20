"""The A2A wire format, the subset Dhadkan speaks: JSON-RPC 2.0 `message/send` with text and data parts.

An agent publishes a card at /.well-known/agent-card.json (A2A 0.3; /.well-known/agent.json for 0.2) naming
its skills and its JSON-RPC URL. A request carries a Message (role, parts, messageId); the answer is either a
Message or a Task whose status.message and artifacts hold parts. Parts are {"kind": "text", "text"} or
{"kind": "data", "data"}; older agents write "type" instead of "kind", which is accepted too.
"""

import uuid
from typing import Any

PROTOCOL_VERSION = "0.3.0"
CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")
# JSON-RPC error codes (the spec's, plus A2A's own)
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
UNAUTHORIZED = -32001


def message(text: str, data: dict[str, Any] | None = None, role: str = "user") -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"kind": "text", "text": text}]
    if data:
        parts.append({"kind": "data", "data": data})
    return {"kind": "message", "role": role, "messageId": uuid.uuid4().hex, "parts": parts}


def request(msg: dict[str, Any], rid: str | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid or uuid.uuid4().hex[:12], "method": "message/send",
            "params": {"message": msg}}  # fmt: skip


def result(rid: Any, res: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": res}


def error(rid: Any, code: int, msg: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def _parts(obj: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((obj or {}).get("parts") or [])


def parts_of(res: dict[str, Any]) -> list[dict[str, Any]]:
    """All parts of an answer: a Message's, or a Task's status message and artifacts."""
    if res.get("kind") == "task" or "status" in res or "artifacts" in res:
        out = _parts((res.get("status") or {}).get("message"))
        for a in res.get("artifacts") or []:
            out += _parts(a)
        return out
    return _parts(res)


def split(parts: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """(all text joined, all data parts merged)."""
    texts, data = [], {}
    for p in parts:
        kind = p.get("kind") or p.get("type")
        if kind == "text" and isinstance(p.get("text"), str):
            texts.append(p["text"])
        elif kind == "data" and isinstance(p.get("data"), dict):
            data.update(p["data"])
    return "\n".join(texts).strip(), data


def card(name: str, description: str, url: str, skills: list[dict[str, Any]], version: str = "1.0.0",
         provider: str = "Dhadkan") -> dict[str, Any]:  # fmt: skip
    return {"protocolVersion": PROTOCOL_VERSION, "name": name, "description": description, "url": url,
            "preferredTransport": "JSONRPC", "version": version, "provider": {"organization": provider},
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "application/json"], "skills": skills}  # fmt: skip


def skill(sid: str, name: str, description: str, tags: list[str], examples: list[str]) -> dict[str, Any]:
    return {"id": sid, "name": name, "description": description, "tags": tags, "examples": examples}
