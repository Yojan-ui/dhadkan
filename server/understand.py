"""Understand: a Granite second look at an alert, only when needed, with the rules as fallback.

Granite (local Ollama, server/provider.py) reads the evidence (before/now spectrogram images, the 14-day trend, the machine and factory
profile, and server/faults.yaml) and names the likely fault with a confidence, the evidence it used,
a severity and a hand check the owner can do ("motor ka end garam hai?"). It does NOT produce prices,
parts, labour or warranty: those always come from faults.yaml through server/diagnosis.py, so no
number on the card is model-generated.

Model use:
- DHADKAN_MODEL (ibm/granite4.2:8b-q3_K_M by default) on the local Ollama; no prefill; no forced
  tool_choice. Structured output via output_config.format (JSON schema -> Ollama `format`); effort "high".
- the chat model is text-only: the spectrogram image goes as a one-line note, the trend and profile as text.
- stop_reason checked (max_tokens -> rules); the output is validated against the schema in code.
- every call logged with tokens and cost to data/llm/calls.jsonl (git-ignored); a local model is ₹0.

No local LLM, an error, or invalid output -> the rules diagnosis, marked "expert check pending".
"""

import base64
import json
import time
from pathlib import Path
from typing import Any

from . import costs, diagnosis, provider
from .config import FAULTS, ROOT

LOG = ROOT / "data" / "llm" / "calls.jsonl"
CONFIDENT = 0.6  # below this the card says "mechanic check needed" (assumption)
SEVERITY_WORDS = {  # the owner's words for the knowledge-base severities (spec: Hinglish labels)
    "ok": "Theek", "watch": "Dhyan do", "repair_soon": "Jaldi repair",
    "stop_now": "Band karne ka sochiye", "replace": "Badalna padega",
}  # fmt: skip

SYSTEM = (
    "You help a small Indian factory owner understand a machine alert. You see the machine's sound as "
    "two spectrograms side by side (before = its own normal, now = the latest reading), a 14-day trend of its anomaly "
    "score (1.0 = its alarm level), its profile, and the fault knowledge base below. Name the most likely "
    "fault from the knowledge base, or 'unknown' if the evidence does not support one. Be calibrated: a "
    "spectrogram rarely proves a fault, so give a confidence and say what you saw. Never invent prices, "
    "parts or warranty terms; those come from the knowledge base, not from you. Never recommend that "
    "anyone but the owner switch a machine off; the owner decides. Write the hand check in the owner's "
    "language as a short question a non-engineer can answer by touching or looking at the machine."
)


def schema() -> dict[str, Any]:
    faults = sorted(diagnosis.load()["faults"])
    return {
        "type": "object",
        "properties": {
            "fault": {"type": "string", "enum": [*faults, "unknown"]},
            "confidence": {"type": "number"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "severity": {"type": "string", "enum": list(SEVERITY_WORDS)},
            "hand_check": {"type": "string"},
            "mechanic_check_needed": {"type": "boolean"},
        },
        "required": ["fault", "confidence", "evidence", "severity", "hand_check", "mechanic_check_needed"],
        "additionalProperties": False,
    }


def build_request(
    pair_png: bytes,
    trend: list[float],
    machine: dict[str, Any],
    factory: dict[str, Any],
    lang: str,
) -> dict[str, Any]:
    """Keyword arguments for client.messages.stream(...). Stable blocks first."""
    kb = FAULTS.read_text(encoding="utf-8")
    profile = json.dumps(
        {k: factory.get(k) for k in ("name", "area", "language", "shifts", "off_days")}, ensure_ascii=False
    )

    def img(b: bytes) -> dict[str, Any]:
        data = base64.b64encode(b).decode()
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}

    return {
        "model": provider.chat_model(),
        "max_tokens": 4000,
        "output_config": {"effort": "high", "format": {"type": "json_schema", "schema": schema()}},
        "system": [
            {"type": "text", "text": SYSTEM},
            {"type": "text", "text": "Fault knowledge base (server/faults.yaml):\n" + kb},
            {"type": "text", "text": "Factory profile: " + profile},
        ],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": f"Machine: {json.dumps({k: machine.get(k) for k in ('name', 'type', 'motor_hp', 'nominal_rpm')}, ensure_ascii=False)}"},
            {"type": "text", "text": "Spectrograms, left = before (its own normal), right = now (latest reading); "
                                     "time across, pitch up, orange box = the band that changed most:"}, img(pair_png),
            {"type": "text", "text": f"14-day score trend, oldest first: {[round(x, 2) for x in trend]}. Owner language: {lang}."},
        ]}],
    }  # fmt: skip


def cost(usage: Any) -> dict[str, float]:
    """A local model: ₹0 unless DHADKAN_LOCAL_INR_PER_MTOK prices its electricity and hardware."""
    from .planner import llm

    inr = llm.cost_inr(provider.chat_model(), usage)
    rate = costs.load()["usd_to_inr"]
    return {"usd": round(inr / rate, 5), "inr": round(inr, 3), "usd_to_inr": rate}


def _log(row: dict[str, Any]) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def diagnose(request: dict[str, Any], client: Any = None, machine_id: str = "") -> dict[str, Any]:
    """Granite's reading, or {"source": "rules", "expert_check": "pending", "why": ...} on any failure."""
    if client is None:
        client = provider.make_client()
        if client is None:
            return {
                "source": "rules",
                "expert_check": "pending",
                "why": "no local LLM (Ollama not reachable, or DHADKAN_LLM=0)",
            }
    t0 = time.time()
    try:
        with client.messages.stream(**request) as stream:
            msg = stream.get_final_message()
    except Exception as e:  # network down, rate limit, bad request: the rules still work
        _log({"ts": t0, "machine": machine_id, "model": request["model"], "error": type(e).__name__})
        return {"source": "rules", "expert_check": "pending", "why": f"API error: {type(e).__name__}"}
    c = cost(msg.usage)
    row = {"ts": t0, "machine": machine_id, "model": request["model"], "served_by": getattr(msg, "model", None),
           "stop_reason": msg.stop_reason, "seconds": round(time.time() - t0, 2),
           "usage": {k: getattr(msg.usage, k, None) for k in ("input_tokens", "output_tokens",
                     "cache_read_input_tokens", "cache_creation_input_tokens")}, **c}  # fmt: skip
    _log(row)
    if msg.stop_reason in ("refusal", "max_tokens"):
        return {
            "source": "rules",
            "expert_check": "pending",
            "why": f"stop_reason {msg.stop_reason}",
            "cost": c,
        }
    try:
        text = next(b.text for b in msg.content if getattr(b, "type", None) == "text")
        out = json.loads(text)
        assert set(schema()["required"]) <= set(out)
    except Exception:
        return {
            "source": "rules",
            "expert_check": "pending",
            "why": "output did not match the schema",
            "cost": c,
        }
    unsure = out["fault"] == "unknown" or float(out["confidence"]) < CONFIDENT or out["mechanic_check_needed"]
    return {**out, "source": provider.NAME, "mechanic_check_needed": bool(unsure), "cost": c,
            "severity_words": SEVERITY_WORDS[out["severity"]]}  # fmt: skip


def calls_summary(path: Path = LOG) -> dict[str, Any]:
    """Tokens and rupees per call so far (for RESULTS.md: cost per alert)."""
    if not path.exists():
        return {"calls": 0}
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    ok = [r for r in rows if "inr" in r]
    return {"calls": len(rows), "errors": len(rows) - len(ok),
            "inr_per_call_median": sorted(r["inr"] for r in ok)[len(ok) // 2] if ok else None}  # fmt: skip
