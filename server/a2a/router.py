"""Dhadkan as an A2A agent: its card and its JSON-RPC endpoint. Read-only skills; an inbound agent can never act.

    GET  /.well-known/agent-card.json  (and /.well-known/agent.json)
    POST /a2a                          JSON-RPC 2.0 message/send

Skills:
  maintenance_knowledge  a question -> the matching knowledge-base chunks (server/kb), with their ids and sources
  machine_status         the cluster's machines, status and risk (needs `Authorization: Bearer <DHADKAN_A2A_TOKEN>`;
                         off when no token is set)

An inbound message is data: it is never given to the planner as an instruction, never creates an event, an
approval, a booking or an order, and every call is logged (event a2a_inbound).
"""

import hmac
from typing import Any

from fastapi import APIRouter, Request

from server import kb
from server.notify import env

from . import protocol

SKILLS = [
    protocol.skill("maintenance_knowledge", "Maintenance knowledge",
                   "Symptoms, safe hand checks, repairs and typical rupee ranges (with sources) for motor, pump, fan and "
                   "gearbox faults common in small Indian factories.",
                   ["maintenance", "knowledge", "india"], ["Pump sounds like gravel, what should I check?"]),
    protocol.skill("machine_status", "Machine status",
                   "Status and risk of the machines Dhadkan listens to. Needs a bearer token.",
                   ["monitoring"], ["Which machines need attention?"]),
]  # fmt: skip


def _authorised(request: Request) -> bool:
    token = env.get("DHADKAN_A2A_TOKEN")
    got = request.headers.get("authorization", "")
    return bool(token) and hmac.compare_digest(got, f"Bearer {token}")


def router(live: Any = None) -> APIRouter:
    r = APIRouter()

    def card(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return protocol.card("Dhadkan maintenance agent",
                             "Listens to small-factory machines and advises on maintenance. Read-only over A2A: it "
                             "never stops a machine and never spends money for anyone.", f"{base}/a2a", SKILLS)  # fmt: skip

    r.add_api_route("/.well-known/agent-card.json", card, methods=["GET"])
    r.add_api_route("/.well-known/agent.json", card, methods=["GET"])

    @r.post("/a2a")
    async def rpc(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            return protocol.error(None, protocol.PARSE_ERROR, "invalid JSON")
        rid = body.get("id") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            return protocol.error(rid, protocol.INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        if body["method"] != "message/send":
            return protocol.error(rid, protocol.METHOD_NOT_FOUND, f"method {body['method']!r} not supported")
        msg = (body.get("params") or {}).get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("parts"), list):
            return protocol.error(rid, protocol.INVALID_PARAMS, "params.message with parts is required")
        text, data = protocol.split(msg["parts"])
        skill = str(
            data.get("skill") or ("machine_status" if "status" in text.lower() else "maintenance_knowledge")
        )
        if live is not None:
            live.event("a2a_inbound", skill=skill, chars=len(text))
        if skill == "machine_status":
            if not _authorised(request):
                return protocol.error(rid, protocol.UNAUTHORIZED, "machine_status needs a valid bearer token")
            rows = [{"machine": x["machine_id"], "status": x.get("status"), "risk": round(x.get("risk") or 0, 2)}
                    for x in (live.ranking() if live is not None else [])]  # fmt: skip
            need = [x["machine"] for x in rows if x["status"] in ("alarm", "early_match")]
            words = f"{len(rows)} machines; need attention: {', '.join(need) or 'none'}."
            return protocol.result(rid, protocol.message(words, {"machines": rows}, role="agent"))
        hits = kb.search(text or str(data.get("query") or ""), k=3, fault=data.get("fault"),
                         machine_type=data.get("machine_type"))  # fmt: skip
        if not hits:
            return protocol.result(
                rid, protocol.message("No matching maintenance knowledge.", {"chunks": []}, role="agent")
            )
        top = hits[0]
        words = f"{top['title']}: {top['text'][:500]} [{top['id']}]"
        return protocol.result(rid, protocol.message(words, {"chunks": kb.brief(hits, 600)}, role="agent"))

    return r
