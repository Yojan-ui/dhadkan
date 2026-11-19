"""The planner's LLM layer: which model for which event, what a call costs, and the bounded tool loop.

The model is IBM Granite on a local Ollama server (server/provider.py): no API key, no cloud call, no
per-token bill. Offline (Ollama not running, or the model not pulled) make_client() returns None and the
planner runs its rules.

Model routing (.env; every tier defaults to DHADKAN_MODEL, ibm/granite4.2:8b-q3_K_M):
    DHADKAN_MODEL_ROUTINE   effort low     owner acks, daily tick, network back
    DHADKAN_MODEL_PLAN      effort medium  alarms, mechanic and parts events
    DHADKAN_MODEL_DIAGNOSE  effort high    unsure diagnosis, voice notes, failed repairs
A bigger local model for one tier (e.g. a Granite 4 "small" for diagnose) is one line in .env.
`effort` high turns on Granite's thinking when DHADKAN_GRANITE_THINK=1.

Bounds, per event (one planner turn): DHADKAN_PLANNER_MAX_TURNS [6] model calls, DHADKAN_PLANNER_MAX_TOKENS
[40000] input + output tokens; per case: DHADKAN_PLANNER_CASE_CAP_INR [50] rupees of LLM cost. A local
model costs ₹0 per token unless DHADKAN_LOCAL_INR_PER_MTOK prices its electricity and hardware; PRICES
below is kept for a priced model id set in .env.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server import provider
from server.maint.tools import COSTS
from server.notify import env

TIERS = {
    "routine": ("DHADKAN_MODEL_ROUTINE", "low"),
    "plan": ("DHADKAN_MODEL_PLAN", "medium"),
    "diagnose": ("DHADKAN_MODEL_DIAGNOSE", "high"),
}
EVENT_TIER = {"alarm": "plan", "health_change": "plan", "owner_reply": "routine", "owner_voice_note": "diagnose",
              "mechanic_reply": "plan", "mechanic_no_show": "plan", "part_out_of_stock": "plan",
              "network_restored": "routine", "repair_done": "plan", "verify_result": "plan",
              "daily_tick": "routine"}  # fmt: skip
# USD per million tokens (input, output) for a priced model id; local models are not listed (₹0).
PRICES: dict[str, tuple[float, float]] = {}


def setting(name: str, default: float) -> float:
    try:
        return float(env.get(name) or default)
    except ValueError:
        return default


def tier_for(event_type: str, payload: dict[str, Any] | None = None) -> str:
    """The event's tier; an unsure diagnosis, a failed repair or a chat question that names a problem
    moves up a tier."""
    p = payload or {}
    if p.get("unsure") or p.get("outcome") == "failed":
        return "diagnose"
    if event_type == "owner_reply" and p.get("channel") == "chat" and not p.get("choice"):
        return "plan"  # a question in the owner's words, not a 1/2 button press
    return EVENT_TIER.get(event_type, "plan")


def model_for(tier: str) -> tuple[str, str]:
    var, effort = TIERS[tier]
    return env.get(var) or provider.chat_model(), effort


def usd_to_inr() -> float:
    return float(json.loads(COSTS.read_text(encoding="utf-8"))["usd_to_inr"])


def cost_inr(model: str, usage: Any) -> float:
    """Rupees for one response's usage: PRICES for a priced model, DHADKAN_LOCAL_INR_PER_MTOK (default 0)
    for a local one."""
    if usage is None:
        return 0.0
    i, o = tokens(usage)
    if model in PRICES:
        pin, pout = PRICES[model]
        return round((i * pin + o * pout) / 1e6 * usd_to_inr(), 4)
    return round((i + o) * setting("DHADKAN_LOCAL_INR_PER_MTOK", 0) / 1e6, 4)


def tokens(usage: Any) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    inp = sum(int(getattr(usage, k, 0) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                        "cache_creation_input_tokens"))  # fmt: skip
    return inp, int(getattr(usage, "output_tokens", 0) or 0)


def request(client: Any, model: str, effort: str, system: str, tools: list[dict[str, Any]],
            messages: list[dict[str, Any]], max_tokens: int = 4000) -> Any:  # fmt: skip
    return client.messages.create(model=model, max_tokens=max_tokens, tools=tools, messages=messages,
                                  system=system, output_config={"effort": effort})  # fmt: skip


@dataclass
class LoopResult:
    reply: str = ""
    reasoning: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_inr: float = 0.0
    turns: int = 0
    stop: str = "end"  # end | turns | tokens | cost | refusal
    latency_ms: int = 0
    model: str = ""


Dispatch = Callable[[str, dict[str, Any]], dict[str, Any]]


def run_loop(client: Any, model: str, effort: str, system: str, tools: list[dict[str, Any]],
             messages: list[dict[str, Any]], dispatch: Dispatch, *, max_turns: int = 6,
             max_tokens: int = 40000, cost_cap_inr: float | None = None, spent_inr: float = 0.0) -> LoopResult:  # fmt: skip
    """Call the model, run the tools it chose (through `dispatch`), repeat until it stops or a bound is hit.

    All tool results of one turn go back in one user message; a failed tool comes back as is_error.
    Thinking blocks are passed back unchanged (the assistant content is appended as returned)."""
    out = LoopResult(model=model)
    t0 = time.perf_counter()
    for _ in range(max_turns):
        if out.input_tokens + out.output_tokens >= max_tokens:
            out.stop = "tokens"
            break
        if cost_cap_inr is not None and spent_inr + out.cost_inr >= cost_cap_inr:
            out.stop = "cost"
            break
        resp = request(client, model, effort, system, tools, messages,
                       max_tokens=max(1000, min(8000, max_tokens - out.input_tokens - out.output_tokens)))  # fmt: skip
        out.turns += 1
        usage = getattr(resp, "usage", None)
        i, o = tokens(usage)
        out.input_tokens += i
        out.output_tokens += o
        out.cost_inr = round(out.cost_inr + cost_inr(model, usage), 4)
        if resp.stop_reason == "refusal":
            out.stop = "refusal"
            break
        content = list(resp.content)
        messages.append({"role": "assistant", "content": content})
        texts = [getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text"]
        thoughts = [getattr(b, "thinking", "") for b in content if getattr(b, "type", None) == "thinking"]
        out.reasoning += [t for t in thoughts + texts if t]
        uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
        if not uses or resp.stop_reason != "tool_use":
            out.reply = "".join(texts).strip()
            out.stop = "end"
            break
        results = []
        for u in uses:
            args = u.input if isinstance(u.input, dict) else {}
            res = dispatch(u.name, dict(args))
            out.calls.append({"tool": u.name, "input": args, "result": res, "ok": "error" not in res})
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": u.id,
                                     "content": json.dumps(res, ensure_ascii=False, default=str)}  # fmt: skip
            if "error" in res:
                block["is_error"] = True
            results.append(block)
        messages.append({"role": "user", "content": results})
    else:
        out.stop = "turns"
    out.latency_ms = round((time.perf_counter() - t0) * 1000)
    return out


def make_client() -> Any:
    """Granite on the local Ollama when it answers and DHADKAN_LLM is not 0; otherwise None (rules)."""
    return provider.make_client()
