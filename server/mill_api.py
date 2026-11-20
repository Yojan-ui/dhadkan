"""HTTP routes for the flour-mill demonstration (server/flourmill.py). Web screen: ?view=mill."""

import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from edge.config import ROOT

from .flourmill import PACK, FlourMillDemo


class StepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = 1


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str
    decision: str


class NetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    online: bool


def router(workdir: Path | None = None) -> APIRouter:
    r = APIRouter(prefix="/api/mill")
    lock = threading.Lock()
    box: dict[str, FlourMillDemo] = {}
    wd = workdir or ROOT / "data" / "demo_run"

    def demo() -> FlourMillDemo:
        if not PACK.exists():
            raise HTTPException(409, "flour-mill pack missing: python -m server.flourmill --pack")
        if "d" not in box:
            box["d"] = FlourMillDemo(wd)
        return box["d"]

    @r.get("/state")
    def state() -> dict[str, Any]:
        with lock:
            return demo().state()

    @r.post("/reset")
    def reset() -> dict[str, Any]:
        with lock:
            old = box.pop("d", None)
            if old is not None:
                old.close()
            return demo().state()

    @r.post("/step")
    def step(body: StepIn) -> dict[str, Any]:
        with lock:
            d = demo()
            for _ in range(max(1, min(body.n, 60))):
                before = d.i
                d.step()
                if d.done or d.i == before:
                    break
            return d.state()

    @r.post("/decide")
    def decide(body: DecideIn) -> dict[str, Any]:
        if body.decision not in ("approved", "rejected"):
            raise HTTPException(422, "decision must be approved or rejected")
        with lock:
            d = demo()
            if d.store.approval(body.approval_id) is None:
                raise HTTPException(404, "no such approval")
            d.decide(body.approval_id, body.decision, channel="owner screen")
            return d.state()

    @r.post("/network")
    def network(body: NetIn) -> dict[str, Any]:
        with lock:
            d = demo()
            d.set_network(body.online, "owner screen switch")
            d.agent.tick()
            d.sync.run()
            return d.state()

    @r.get("/audio/{which}.wav")
    def audio(which: str) -> Response:
        import io

        import soundfile as sf

        d = demo()
        if which not in d.audio:
            raise HTTPException(404)
        buf = io.BytesIO()
        sf.write(buf, d.audio[which], 16000, format="WAV", subtype="PCM_16")
        return Response(buf.getvalue(), media_type="audio/wav")

    return r
