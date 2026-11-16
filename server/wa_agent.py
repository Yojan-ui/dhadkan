"""Dhadkan's WhatsApp agent: Claude Opus 5.5 with five tools, on the user's base loop (run_agent).

Handlers run against the live replay (NASA IMS on "Loom 4, Shed A"), server/faults.yaml and
server/factory/contacts.yaml. Guardrails, in code:
  * send_whatsapp refuses a text with a number that no tool returned (no invented prices or parts),
    an owner message over 700 characters, or one without the "1 / 2 / 3" options line. The model gets the
    refusal back as the tool result and can rewrite.
  * Nothing here books, buys or stops anything: booking happens only on the owner's "1"
    (server/factory/flow.py), and there is no stop tool.
  * API failure, no key or a refusal -> the same message filled from faults.yaml (owner_template).
Every tool step is logged (flow.log) and shown in the Digital Twin's Messages timeline.
"""

import json
import os
import re
from typing import Any

from .notify import env

MODEL = env.get("CLAUDE_MODEL") or os.getenv("CLAUDE_MODEL") or "claude-opus-5-5"
OPTIONS = "1 Mechanic bulao / 2 Baad mein yaad dilao / 3 Poori detail"
OPTIONS_MR = "1 Mistri bolva / 2 Nantar aathvan kara / 3 Purna mahiti"
OWNER_LIMIT = 700

SYSTEM = """You are Dhadkan, a maintenance agent for small Indian factories.
Use ONLY facts from tools and faults.yaml. Never invent prices or parts.
Write to owners in their language, spoken style, short, no jargon.
Any action that costs money or stops a machine needs the owner's '1' first.
If unsure, say so."""

TOOLS = [
    {"name": "get_machine_health", "description": "Current score, trend and days-to-failure for a machine",
     "input_schema": {"type": "object", "properties": {"machine_id": {"type": "string"}}, "required": ["machine_id"]}},
    {"name": "diagnose_fault", "description": "Match features to faults.yaml; returns fault, cause, fix, part, price range, warranty",
     "input_schema": {"type": "object", "properties": {"machine_id": {"type": "string"}}, "required": ["machine_id"]}},
    {"name": "find_mechanic", "description": "Owner's own mechanic and free slots",
     "input_schema": {"type": "object", "properties": {"factory_id": {"type": "string"}}, "required": ["factory_id"]}},
    {"name": "check_part_stock", "description": "Local dealer stock and price for a part",
     "input_schema": {"type": "object", "properties": {"part": {"type": "string"}}, "required": ["part"]}},
    {"name": "send_whatsapp", "description": "Send WhatsApp to a role (owner/supervisor/mechanic/dealer)",
     "input_schema": {"type": "object", "properties": {"role": {"type": "string"}, "text": {"type": "string"}},
                      "required": ["role", "text"]}},
]  # fmt: skip


def run_agent(task: str, handlers: dict[str, Any], client: Any = None, max_turns: int = 10) -> str:
    """The user's base loop, plus a turn cap and a refusal check."""
    from anthropic import Anthropic

    client = client or Anthropic(api_key=env.get("ANTHROPIC_API_KEY"))
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    for _ in range(max_turns):
        resp = client.messages.create(
            model=MODEL, max_tokens=2000, system=SYSTEM, tools=TOOLS, messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "refusal":
            raise RuntimeError("refusal")
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                try:
                    out = handlers[b.name](**b.input)
                except Exception as e:  # a bad argument goes back to the model, not up the stack
                    out = {"error": f"{type(e).__name__}: {e}"}
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str)})  # fmt: skip
        messages.append({"role": "user", "content": results})
    return ""


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


class Handlers:
    """Tool handlers bound to the live replay; facts seen so far feed the number check."""

    def __init__(self, flow: Any, machine: str, stage: str, allowed_roles: set[str]) -> None:
        self.flow, self.machine, self.stage, self.roles = flow, machine, stage, allowed_roles
        self.facts: list[str] = [machine, flow.display(machine)["name"], "1 2 3 4 60 700 24"]
        self.sent: dict[str, str] = {}

    def _log(self, tool: str, out: Any) -> dict[str, Any]:
        self.facts.append(json.dumps(out, ensure_ascii=False, default=str))
        self.flow.log(self.machine, "agent", "agent", None, f"{tool}: {summary(out)}", kind="tool")
        return out

    def get_machine_health(self, machine_id: str) -> dict[str, Any]:
        return self._log("get_machine_health", self.flow.health(machine_id))

    def diagnose_fault(self, machine_id: str) -> dict[str, Any]:
        return self._log("diagnose_fault", self.flow.diagnosis(machine_id))

    def find_mechanic(self, factory_id: str) -> dict[str, Any]:
        return self._log("find_mechanic", self.flow.mechanic(self.machine))

    def check_part_stock(self, part: str) -> dict[str, Any]:
        return self._log("check_part_stock", self.flow.stock(self.machine, part))

    def send_whatsapp(self, role: str, text: str) -> dict[str, Any]:
        problem = check(role, text, " ".join(self.facts), self.roles)
        if problem:
            self.flow.log(
                self.machine, "agent", "agent", None, f"send_whatsapp refused: {problem}", kind="tool"
            )
            return {"sent": False, "error": problem}
        if role in self.sent:
            return {"sent": False, "error": f"already sent to {role} for this stage"}
        res = self.flow.send(self.machine, role, text, stage=self.stage, by="agent")
        self.sent[role] = text
        return {"sent": True, "status": res["status"]}


def check(role: str, text: str, facts: str, roles: set[str]) -> str | None:
    if role not in roles:
        return f"role {role} is not due a message at this stage (allowed: {sorted(roles)})"
    if role == "owner":
        if len(text) > OWNER_LIMIT:
            return f"owner message is {len(text)} characters; keep it under {OWNER_LIMIT}"
        if not re.search(r"\b1\b.*\b2\b.*\b3\b", text, re.S):
            return f"owner message must end with the options: {OPTIONS}"
    extra = numbers_in(text) - numbers_in(facts)
    if extra:
        return f"numbers not from any tool: {sorted(extra)}; use only numbers the tools returned"
    return None


def summary(out: Any) -> str:
    if isinstance(out, dict):
        if "summary" in out:
            return str(out["summary"])
        return ", ".join(f"{k}={v}" for k, v in list(out.items())[:4])[:160]
    return str(out)[:160]


def compose(flow: Any, machine: str, stage: str, roles: set[str], client: Any = None) -> dict[str, str]:
    """Claude writes and sends this stage's messages to `roles`; whatever it did not send, the templates do."""
    h = Handlers(flow, machine, stage, roles)
    handlers = {t["name"]: getattr(h, t["name"]) for t in TOOLS}
    lang = flow.contact("owner")["language"]
    d = flow.display(machine)
    task = (f"Machine {machine} ({d['name']}, {d['shed']}) has reached stage '{stage}'. Look at its health and "
            f"diagnosis, then send WhatsApp messages to these roles: {sorted(roles)}. The owner's message: in "
            f"{'Marathi' if lang == 'mr' else 'Hindi (Roman script, Hinglish spoken style)' if lang == 'hi_latn' else 'Hindi'}, "
            f"under {OWNER_LIMIT} characters: what is wrong, time left, cost now vs if it breaks, the cause or misuse "
            f"to avoid, warranty, then end with exactly: {OPTIONS_MR if lang == 'mr' else OPTIONS}. "
            f"{'If it is beyond repair, say so plainly with the repair-now vs breakdown cost. ' if stage == 'replace' else ''}"
            "The supervisor's message: what to do this shift, in one or two lines; the owner decides on stopping. "
            "Start each message with the role in brackets, e.g. [Owner].")  # fmt: skip
    sent: dict[str, str] = {}
    if env.get("ANTHROPIC_API_KEY") or client is not None:
        try:
            run_agent(task, handlers, client=client)
            sent = dict(h.sent)
        except Exception as e:  # API down, rate limit, refusal: the templates still go out
            flow.log(
                machine,
                "agent",
                "agent",
                None,
                f"Claude unavailable ({type(e).__name__}); template used",
                kind="tool",
            )
    for role in sorted(roles - set(sent)):
        text = flow.template(machine, role, stage)
        flow.send(machine, role, text, stage=stage, by="template")
        sent[role] = text
    return sent
