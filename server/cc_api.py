"""HTTP routes for the command centre (web: the default page). Reads need any role; decisions need the
owner; repair reports need the technician or the owner (server/auth.py)."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import auth
from .brain import Brain


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=256)


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str
    decision: str = Field(pattern="^(approved|rejected)$")


class RepairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    technician: str = Field(min_length=1, max_length=64)
    work: str = Field(min_length=1, max_length=300)
    simulate_recovery: bool = False


class ScenarioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine: str
    mode: str = Field(pattern="^(fault|normal)$")


def router(brain: Brain) -> APIRouter:
    r = APIRouter(prefix="/api/cc")

    @r.post("/login")
    def login(body: LoginIn, response: Response) -> dict[str, Any]:
        role = auth.role_for_token(body.token)
        if role is None:
            raise HTTPException(401, "unknown token")
        response.set_cookie(
            auth.COOKIE, auth.session(role), httponly=True, samesite="strict", max_age=auth.TTL_S
        )
        return {"role": role}

    @r.post("/logout")
    def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(auth.COOKIE)
        return {"ok": True}

    @r.get("/me")
    def me(request: Request) -> dict[str, Any]:
        return auth.current(request)

    @r.get("/state")
    def state(request: Request) -> dict[str, Any]:
        auth.current(request)
        return brain.state()

    @r.get("/machine/{mid}")
    def machine(mid: str, request: Request) -> dict[str, Any]:
        auth.current(request)
        if mid not in brain.c.machines:
            raise HTTPException(404, "unknown machine")
        return brain.machine_detail(mid)

    @r.get("/planner")
    def planner(request: Request, case: str | None = None, machine: str | None = None) -> dict[str, Any]:
        """The planner's trace: per case, event -> reasoning -> tool calls -> approvals -> outcome."""
        auth.current(request)
        return brain.planner_trace(case, machine, limit=20)

    @r.get("/audit")
    def audit(request: Request, since: int = 0) -> dict[str, Any]:
        auth.current(request)
        with brain.lock:
            rows = brain.store.audit(since_seq=since)[-300:]
            return {"rows": rows, "ok": brain.store.verify_audit()["ok"]}

    @r.get("/directory")
    def directory(request: Request) -> dict[str, Any]:
        """Technicians (demo data) with their repair record here, and parts stock (mock) with cited price ranges."""
        auth.current(request)
        from .maint.tools import OWNER_WORD, cost_range

        with brain.lock:
            techs = []
            for t in brain.agent.t.technicians.people:
                reps = brain.store.repairs(technician=t["id"])
                techs.append({**t, "source": brain.agent.t.technicians.data["_source"], "jobs": len(reps),
                              "fixed": sum(x["outcome"] == "fixed" for x in reps)})  # fmt: skip
        parts = []
        for f, word in OWNER_WORD.items():
            for mtype in sorted({m["type"] for m in brain.c.machines.values()}):
                for row in brain.agent.t.parts.stock(word, mtype):
                    parts.append({**row, "fault": f, "cost": cost_range(f, 5.0)})
        return {"technicians": techs, "parts": parts}

    @r.post("/decide")
    def decide(body: DecideIn, request: Request) -> dict[str, Any]:
        who = auth.require(request, "owner")
        try:
            return brain.decide(body.approval_id, body.decision, by=who["name"], channel="command centre")
        except KeyError as e:
            raise HTTPException(404, "no such approval") from e

    @r.post("/repair_done")
    def repair_done(body: RepairIn, request: Request) -> dict[str, Any]:
        auth.require(request, "technician", "owner")
        try:
            brain.repair_done(body.case_id, body.technician, body.work, body.simulate_recovery)
        except KeyError as e:
            raise HTTPException(404, "no such case") from e
        return {"ok": True}

    @r.post("/scenario")
    def scenario(body: ScenarioIn, request: Request) -> dict[str, Any]:
        """SIMULATED: switch a machine's replayed recordings to its recorded FAULT clips (or back to normal)."""
        auth.require(request, "owner")
        if body.machine not in brain.c.machines:
            raise HTTPException(404, "unknown machine")
        brain.live.set_mode(body.machine, body.mode)
        brain.store.log("system", "simulated_scenario", None, machine=body.machine, mode=body.mode)
        return {"machine": body.machine, "mode": body.mode, "simulated": True}

    return r
