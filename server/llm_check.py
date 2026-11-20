"""Is the local LLM answering? Provider, model, latency, and one real tool call through the planner's loop.

    python -m server.llm_check          (.\\dev.ps1 llm-check  /  make llm-check)

Exit code 0 only when Granite answered through Ollama *and* called the tool with a valid argument, and the
tool's result came back into the reply. Offline it prints the exact `ollama pull` to run and exits 1;
the server still works then (every LLM path falls back to its rules).
"""

import sys
import time
from typing import Any

from server import provider
from server.planner import llm

TOOLS = [{
    "name": "get_machine_health",
    "description": "Health of one machine: status and risk score (1.0 = its alarm level).",
    "input_schema": {"type": "object", "properties": {"machine_id": {"type": "string"}},
                     "required": ["machine_id"], "additionalProperties": False},
}]  # fmt: skip
SYSTEM = "You are a maintenance assistant for a small factory. Use the tools; never guess a number."
CANNED = {"A-G1": {"status": "alarm", "risk": 1.37, "note": "CHECK DATA, not a live reading"}}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # the Windows console codepage cannot print ₹ or Devanagari
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    st = provider.status()
    print(f"provider : {st['provider']} at {st['url']}")
    print(f"model    : {st['model']}  (pulled: {st['has_model']})")
    print(f"embedder : {st['embed_model']}  (pulled: {st['has_embed_model']})")
    if not st["enabled"]:
        print("DHADKAN_LLM=0 (or DHADKAN_PLANNER=0): the LLM is switched off; rules only.")
        return 1
    if not st["reachable"]:
        print("Ollama is not answering. Start it (the Ollama app, or `ollama serve`), then run:")
        print(f"  ollama pull {st['model']}\n  ollama pull {st['embed_model']}")
        return 1
    missing = [
        m for m, ok in ((st["model"], st["has_model"]), (st["embed_model"], st["has_embed_model"])) if not ok
    ]
    for m in missing:
        print(f"missing  : run  ollama pull {m}")
    if not st["has_model"]:
        return 1
    client = provider.make_client()
    assert client is not None
    seen: list[dict[str, Any]] = []

    def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        seen.append({"tool": name, "input": args})
        if name != "get_machine_health" or args.get("machine_id") not in CANNED:
            return {"error": f"unknown machine {args.get('machine_id')!r}"}
        return CANNED[args["machine_id"]]

    t0 = time.perf_counter()
    msgs = [
        {
            "role": "user",
            "content": "How is machine A-G1 right now? Check with the tool, then answer in one line.",
        }
    ]
    res = llm.run_loop(
        client, st["model"], "low", SYSTEM, TOOLS, msgs, dispatch, max_turns=3, max_tokens=6000
    )
    secs = time.perf_counter() - t0
    ok_call = any(c["tool"] == "get_machine_health" and c["input"].get("machine_id") == "A-G1" for c in seen)
    grounded = "1.37" in res.reply or "alarm" in res.reply.lower()
    print(f"latency  : {secs:.1f} s for {res.turns} model call(s), {res.input_tokens}+{res.output_tokens} tokens, "
          f"₹{res.cost_inr:g}")  # fmt: skip
    print(f"tool call: {seen or 'none'}")
    print(f"reply    : {res.reply[:300]}")
    good = ok_call and bool(res.reply) and grounded
    print("RESULT   : OK, Granite answered through Ollama with a tool call" if good
          else "RESULT   : FAILED (no valid tool call, or the reply ignored the tool's result)")  # fmt: skip
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
