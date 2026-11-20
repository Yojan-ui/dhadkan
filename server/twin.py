"""What the Digital Twin screen's HUD panels show, all from the live readings (no made-up numbers).

- status chips: machines per status
- per shift (06-14, 14-22, 22-06 on the factory clock): mean and max anomaly score across machines
  (score / the machine's own alarm level; 1.0 = alarm), last 9 shifts
- risk ranking: every machine by its latest 3-reading score
- focus machine (selected, else the highest risk): its last 42 scores
- rupees at risk in the next 2 days: sum over machines (forecast x the factory's cost profile)
- overall health: share of machines not at watch / alarm (learning counts as fine)
- agent checklist: steps of the last agent run (observe, diagnose, plan, ask owner, act, verify)

A machine the live recorder feeds (server/live_feed.py) is ranked on its live readings, flagged `live`.
"""

from collections import defaultdict
from datetime import date, datetime
from typing import Any

SHIFTS = (("A", 6, 14), ("B", 14, 22), ("C", 22, 6))


def shift_of(ts: str) -> tuple[str, str]:
    dt = datetime.fromisoformat(ts)
    for name, a, b in SHIFTS:
        if (a < b and a <= dt.hour < b) or (a > b and (dt.hour >= a or dt.hour < b)):
            day = dt.date() if not (a > b and dt.hour < b) else date.fromordinal(dt.date().toordinal() - 1)
            return f"{day.isoformat()} {name}", name
    return f"{dt.date().isoformat()} ?", "?"


def summary(live: Any, focus: str | None = None, n_shifts: int = 9) -> dict[str, Any]:
    rank = live.ranking()
    counts: dict[str, int] = defaultdict(int)
    for r in rank:
        counts[r["status"]] += 1
    rows = live.c.db.execute("SELECT machine, ts, risk FROM readings ORDER BY tick").fetchall()
    by_shift: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_shift[shift_of(r["ts"])[0]].append(r["risk"])
    keys = sorted(by_shift)[-n_shifts:]
    shifts = [{"shift": k, "mean": round(sum(by_shift[k]) / len(by_shift[k]), 3), "max": round(max(by_shift[k]), 3),
               "readings": len(by_shift[k])} for k in keys]  # fmt: skip
    feed = getattr(getattr(live, "brain", None), "feed", None)
    live_ids = set(feed.live_ids()) if feed is not None else set()
    rank = [r for r in rank if r["machine_id"] not in live_ids]  # the simulator does not speak for them
    ranking = [{"id": r["machine_id"], "name": r["machine"], "factory": r["factory"], "risk": r["risk"],
                "status": r["status"], "live": False} for r in rank]  # fmt: skip
    for mid in sorted(live_ids):
        e = feed.twin_entry(mid)
        m = live.c.machines[mid]
        counts[e["status"]] += 1
        ranking.append({"id": mid, "name": m["name"], "factory": m["factory"], "live": True,
                        "risk": round(float((e["latest"] or {}).get("risk") or 0.0), 3), "status": e["status"]})  # fmt: skip
        rank.append({"status": e["status"], "rupees_at_risk": 0})
    ranking.sort(key=lambda r: -r["risk"])
    focus = focus if focus in live.c.machines else (ranking[0]["id"] if ranking else None)
    if focus in live_ids:
        trend = [h["risk"] for h in feed.twin_entry(focus)["history"]][-42:]
    else:
        trend = [round(h["risk"], 3) for h in live.c.history(focus, 42)] if focus else []
    flagged = sum(counts[s] for s in ("alarm", "early_match", "watch"))
    agent = getattr(live, "last_agent", None) or {}
    return {
        "clock": live.c.now().isoformat(timespec="minutes") if live.c.tick_no() >= 0 else None,
        "chips": {"ok": counts["ok"], "learning": counts["learning"], "watch": counts["watch"],
                  "alert": counts["alarm"] + counts["early_match"], "total": len(rank)},
        "shifts": shifts,
        "ranking": ranking,
        "focus": focus,
        "focus_name": live.c.machines[focus]["name"] if focus else None,
        "trend": trend,
        "rupees_at_risk_2d": sum(r["rupees_at_risk"] for r in rank),
        "health_pct": round(100 * (1 - flagged / len(rank))) if rank else None,
        "health_rule": "share of machines not at watch or alarm",
        "checklist": agent.get("checklist") or [{"step": s, "done": False} for s in
                     ("observe", "diagnose", "plan", "ask owner", "act", "verify")],
        "agent_run": agent.get("run_id"),
    }  # fmt: skip
