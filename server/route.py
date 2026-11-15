"""Technician route: which at-risk machines to visit today, and in what order.

Exact search (bitmask DP, fine up to ~15 stops): visit the set of machines
with the largest total risk that fits in the technician's hours, in the order
with the least driving. Machines in the same factory are 0 km apart.
"""

import itertools
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any


def plan(stops: list[dict[str, Any]], start_xy: Sequence[float], hours: float, kmph: float) -> dict[str, Any]:
    """stops: [{id, xy_km, service_h, risk, value?}] -> {order, hours, km, skipped}.

    Maximises the total `value` of the visited stops (falls back to `risk`).
    """
    w = [s.get("value", s["risk"]) for s in stops]
    n = len(stops)
    if n > 15:
        top = sorted(range(n), key=lambda i: -w[i])[:15]
        stops, w = [stops[i] for i in top], [w[i] for i in top]
        n = 15

    def drive(a: Sequence[float], b: Sequence[float]) -> float:
        return math.dist(a, b) / kmph

    full = 1 << n
    INF = float("inf")
    # best[mask][j] = least hours to start at base, visit `mask`, end at stop j (service done)
    best = [[INF] * n for _ in range(full)]
    prev = [[-1] * n for _ in range(full)]
    for j, s in enumerate(stops):
        best[1 << j][j] = drive(start_xy, s["xy_km"]) + s["service_h"]
    for mask in range(1, full):
        for j in range(n):
            t = best[mask][j]
            if t == INF or not mask >> j & 1:
                continue
            for k in range(n):
                if mask >> k & 1:
                    continue
                nt = t + drive(stops[j]["xy_km"], stops[k]["xy_km"]) + stops[k]["service_h"]
                m2 = mask | 1 << k
                if nt < best[m2][k]:
                    best[m2][k], prev[m2][k] = nt, j

    choice = (0.0, 0.0, 0, -1)  # (risk, -hours, mask, last)
    for mask in range(1, full):
        r = sum(w[j] for j in range(n) if mask >> j & 1)
        for j in range(n):
            if best[mask][j] == INF:
                continue
            total = best[mask][j] + drive(stops[j]["xy_km"], start_xy)  # back to base
            if total <= hours and (r, -total) > choice[:2]:
                choice = (r, -total, mask, j)

    _, neg_h, mask, last = choice
    order = []
    while last >= 0:
        order.append(stops[last]["id"])
        mask, last = mask & ~(1 << last), prev[mask][last]
    order.reverse()
    xy = [start_xy] + [next(s["xy_km"] for s in stops if s["id"] == i) for i in order] + [start_xy]
    km = sum(math.dist(a, b) for a, b in itertools.pairwise(xy)) if order else 0.0
    return {
        "order": order,
        "hours": round(-neg_h, 2),
        "km": round(km, 1),
        "skipped": [s["id"] for s in stops if s["id"] not in order],
    }


def timeline(
    order: list[dict[str, Any]], start_xy: Sequence[float], kmph: float, start: datetime
) -> list[dict[str, Any]]:
    """Arrival and departure per stop. order: [{id, xy_km, service_h}], start: datetime."""
    t, here, out = start, start_xy, []
    for s in order:
        t += timedelta(hours=math.dist(here, s["xy_km"]) / kmph)
        leave = t + timedelta(hours=s["service_h"])
        out.append({"id": s["id"], "arrive": t, "leave": leave})
        t, here = leave, s["xy_km"]
    return out
