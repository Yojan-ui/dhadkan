"""Rupees: what an unplanned breakdown costs, and what a planned visit saves.

Every number comes from data/sim/costs.json (ASSUMPTIONS a factory replaces with
its own) and data/sim/parts_mock.json (MOCK dealer prices). Nothing here is a
measurement.
"""

import json
from functools import cache
from typing import Any

from .config import COSTS, PARTS


@cache
def load() -> dict[str, Any]:
    costs = json.loads(COSTS.read_text(encoding="utf-8"))
    costs["parts"] = json.loads(PARTS.read_text(encoding="utf-8"))["parts"]
    return costs


def parts_cost(mtype: str, fault: str | None) -> int | None:
    """Parts usually needed for this fault on this machine type (None: fault not known yet)."""
    if fault is None:
        return None
    return sum(p["price_inr"] for p in load()["parts"] if p["type"] == mtype and p["fault"] == fault)


def breakdown(factory: str, mtype: str, fault: str | None, service_h: float) -> dict[str, Any]:
    """Cost of letting it break vs a planned visit, and the difference (what the visit saves)."""
    c = load()
    per_hour = c["output_per_hour_inr"][factory] + c["workers_idle"][mtype] * c["wage_per_hour_inr"]
    down = c["unplanned_downtime_hours"][mtype]
    parts = parts_cost(mtype, fault)
    markup = 0 if parts is None else round(parts * c["emergency_part_markup"])
    hours_saved = max(0.0, down - service_h)
    return {
        "unplanned_downtime_h": down,
        "planned_service_h": service_h,
        "downtime_avoided_h": hours_saved,
        "output_inr": round(hours_saved * c["output_per_hour_inr"][factory]),
        "wages_inr": round(hours_saved * c["workers_idle"][mtype] * c["wage_per_hour_inr"]),
        "part_markup_inr": markup,
        "parts_known": parts is not None,
        "breakdown_cost_inr": round(down * per_hour + markup),
        "saved_inr": round(hours_saved * per_hour + markup),
    }
