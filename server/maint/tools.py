"""What the maintenance agent can do in the world. Nothing here can stop or control a machine.

    Technicians  the factory's own mechanics and nearby fitters: skills, days they work
                 (data/demo/technicians.json: DEMO DATA, invented people, labelled on every result)
    Parts        is the likely part in stock at the dealer (data/sim/parts_mock.json: MOCK stock, labelled)
                 and what it typically costs (server/faults.yaml: cited listings, "typical, confirm with dealer")
    Transport    sends messages and job requests. Raises ToolError when the network or provider fails
                 (the agent retries later) and Unavailable when the answer is "no" (the agent tries another way)
    Network      online / offline, as the factory node sees it

The agent never sees a price that is not in faults.yaml with a source, and never invents availability:
when the demo files do not list a technician or a part, the answer is "none found".
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from server import diagnosis

ROOT = Path(__file__).resolve().parents[2]
TECHNICIANS = ROOT / "data" / "demo" / "technicians.json"
PARTS = ROOT / "data" / "sim" / "parts_mock.json"
COSTS = ROOT / "data" / "sim" / "costs.json"
INSPECTION_HOURS = (1, 2)  # assumption: an inspection visit with a stethoscope / hand checks


class ToolError(Exception):
    """Transient: network down, provider error, timeout. Retry later."""


class Unavailable(Exception):
    """Definite no for this option (technician declined, part not stocked). Try another way."""


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Network:
    online: bool = True
    probe: Callable[[], bool] | None = None

    def check(self) -> bool:
        if self.probe is not None:
            try:
                self.online = bool(self.probe())
            except Exception:
                self.online = False
        return self.online


class Technicians:
    def __init__(self, path: Path = TECHNICIANS) -> None:
        self.data = _load(path)
        self.people = self.data["technicians"]

    def find(self, skill: str, day: date, exclude: set[str] | None = None) -> list[dict[str, Any]]:
        """Technicians with the skill who work on that weekday, nearest first. Demo data."""
        exclude = exclude or set()
        wd = day.strftime("%a").lower()[:3]
        out = [dict(p, source=self.data["_source"]) for p in self.people
               if skill in p["skills"] and wd in p["works"] and p["id"] not in exclude]  # fmt: skip
        return sorted(out, key=lambda p: p["km"])

    def get(self, tid: str) -> dict[str, Any]:
        return next(dict(p, source=self.data["_source"]) for p in self.people if p["id"] == tid)


class Parts:
    def __init__(self, path: Path = PARTS) -> None:
        self.data = _load(path)

    def stock(self, owner_word_fault: str, mtype: str) -> list[dict[str, Any]]:
        rows = [p for p in self.data["parts"] if p["fault"] == owner_word_fault and p["type"] == mtype]
        return [{k: v for k, v in p.items() if k != "price_inr"} | {"source": "MOCK dealer stock (demo)"}
                for p in rows]  # fmt: skip


def cost_range(fault: str, hp: float) -> dict[str, Any]:
    """Typical cost of fixing `fault` (faults.yaml key): part + labour, cited. Never invented."""
    kb = diagnosis.load()
    f = kb["faults"][fault]
    rate = _load(COSTS)["technician_rate_inr_per_hour"]
    lo, hi = diagnosis._fix_cost(f, hp, rate) or (0, 0)
    src = kb["sources"].get(f["fix"].get("price_source") or "", {})
    return {"fault": fault, "low_inr": lo, "high_inr": hi, "part": f["fix"]["part"]["en"],
            "price_source": src.get("url"), "price_note": kb["disclaimer"]["en"],
            "labour_rate_note": "technician rate is an assumption (data/sim/costs.json)"}  # fmt: skip


def inspection_cost(tech: dict[str, Any] | None = None) -> dict[str, Any]:
    """The visit's cost: inspection hours at the technician's own rate (technicians.json rate_inr_per_hour)
    or, when none is listed, the assumed rate in costs.json."""
    rate = (tech or {}).get("rate_inr_per_hour") or _load(COSTS)["technician_rate_inr_per_hour"]
    return {"low_inr": INSPECTION_HOURS[0] * rate, "high_inr": INSPECTION_HOURS[1] * rate,
            "basis": f"{INSPECTION_HOURS[0]}-{INSPECTION_HOURS[1]} h at the assumed technician rate"}  # fmt: skip


OWNER_WORD = {
    "bearing_wear": "bearing",
    "imbalance": "imbalance",
    "belt_slip": "loose_belt",
    "cavitation": "clog",
}


@dataclass
class Transport:
    """Outward messages and bookings. `send` is the real channel (WhatsApp/SMS via server/notify, or the
    technician's job request). Tests and the demo inject failures through `fail_next` and `decline`."""

    send: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
    network: Network = field(default_factory=Network)
    fail_next: int = 0  # the next N calls raise ToolError (provider/network failure)
    decline: set[str] = field(default_factory=set)  # technicians who answer "not available"
    sent: list[dict[str, Any]] = field(default_factory=list)

    def call(self, kind: str, payload: dict[str, Any], idem: str) -> dict[str, Any]:
        if not self.network.check():
            raise ToolError("offline")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ToolError("provider error (injected)")
        if kind == "book_job" and payload.get("technician") in self.decline:
            raise Unavailable(f"{payload['technician']} is not available")
        result = (
            self.send(kind, {**payload, "idem": idem})
            if self.send
            else {"id": f"{kind}:{idem[:10]}", "dry_run": True}
        )
        self.sent.append({"kind": kind, "idem": idem, **payload})
        return result
