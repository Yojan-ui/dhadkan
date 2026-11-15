"""The quiet agent: reads risk, plans the technician's day, drafts owner messages.

It has four tools and no others. None of them can stop, slow or switch off a
machine; the agent only recommends, and an owner's yes/no reply decides
whether the technician comes. Every tool call is logged to node.db.

    python -m dhadkan.agent            # Claude decides the order (needs ANTHROPIC_API_KEY)
    python -m dhadkan.agent --rules    # same tools in a fixed order, no LLM
"""

import argparse
import json
import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Any

from . import alerts, costs, forecast, registry, route
from .config import PARTS
from .sim import Cluster

MODEL = "claude-opus-5-5"
RISK_HORIZON_DAYS = 2.0  # rupees at risk = chance of reaching the fault level within 2 days x breakdown cost
WATCH = 0.95  # risk at which a machine is shown as "watch" (display only)
DAYS = alerts.DAY["en"]

TOOLS = [
    {
        "name": "get_risk_ranking",
        "description": "Every machine in the cluster ranked by risk. risk = latest anomaly score / "
        "that machine's own alarm level (1.0 = at alarm), averaged over the last 3 "
        "readings. status is alarm, early_match (2 of the last 3 readings resemble a fault "
        "confirmed at another factory, below alarm), watch, or ok. days_to_fault_level is a "
        "straight-line estimate (10th-90th percentile range, days) of when the machine will sound "
        "like its recorded faulty clips; status not_separable/no_trend/too_few means no estimate. "
        "rupees_at_risk = chance of reaching that level within 2 days x breakdown cost (assumed costs).",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_risk": {"type": "number", "description": "only return machines at or above this risk"}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "plan_technician_route",
        "description": "Shortest route for today over machines with risk at or above risk_threshold "
        "(plus early_match machines and any machine that may reach its fault level within 2 days), "
        "fitted into the technician's hours, choosing the stops with the most rupees at risk. "
        "Returns the stops in order and the machines that do not fit today.",
        "input_schema": {
            "type": "object",
            "properties": {
                "risk_threshold": {"type": "number"},
                "hours_available": {"type": "number", "description": "defaults to the technician's day"},
            },
            "required": ["risk_threshold"],
            "additionalProperties": False,
        },
    },
    {
        "name": "draft_owner_alert",
        "description": "Writes the short message to a machine's owner in the owner's language "
        "(Hindi/Marathi/English), ending with a yes/no question about the technician "
        "visit. kind=rising for a machine whose own sound is getting worse, "
        "kind=match for an early_match machine. The only way to produce owner text. Returns "
        "skipped if the owner already has an open question or approved visit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["rising", "match"]},
                "visit_day": {"type": "string", "enum": DAYS},
            },
            "required": ["machine_id", "kind", "visit_day"],
            "additionalProperties": False,
        },
    },
    {
        "name": "quote_parts",
        "description": "Parts a technician may need for a fault on a machine type, from local "
        "dealers (mock data): price in INR, stock, lead days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fault": {"type": "string", "enum": ["bearing", "imbalance", "loose_belt", "clog"]},
                "machine_type": {"type": "string", "enum": ["pump", "fan", "gearbox"]},
            },
            "required": ["fault", "machine_type"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)

SYSTEM = """You prepare the daily maintenance digest for a cluster of small factories.

Rules:
- You never stop, slow down or switch off a machine and never tell an owner to. You recommend a check; the owner approves a technician visit by replying yes or no.
- Owner-facing text comes only from draft_owner_alert. Do not write it yourself.
- Stay quiet: if nothing is at alarm or early_match, draft nothing.

Each run: call get_risk_ranking; if anything is at alarm or early_match, call plan_technician_route (risk_threshold 1.0 unless you have a reason); call draft_owner_alert once per machine on the route (visit_day = the route's weekday) and once per machine left out of the route (visit_day = the day after); call quote_parts for machines with a known fault.
Finish with at most 4 plain lines in English for the technician: stops in order and parts to carry."""


class Tools:
    def __init__(self, cluster: Cluster, run_id: str, actor: str) -> None:
        self.c, self.run_id, self.actor = cluster, run_id, actor

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name not in TOOL_NAMES:  # never getattr an arbitrary name the model chose
            raise ValueError(f"unknown tool {name!r}; available: {sorted(TOOL_NAMES)}")
        out = getattr(self, name)(**args)
        self.c.db.execute(
            "INSERT INTO tool_calls (run_id, ts, actor, tool, input, output) VALUES (?,?,?,?,?,?)",
            (
                self.run_id,
                self.c.now().isoformat(timespec="minutes"),
                self.actor,
                name,
                json.dumps(args, ensure_ascii=False),
                json.dumps(out, ensure_ascii=False),
            ),
        )
        self.c.db.commit()
        return out

    # -- tools ------------------------------------------------------------
    def get_risk_ranking(self, min_risk: float = 0.0) -> list[dict[str, Any]]:
        rows = []
        for mid, m in self.c.machines.items():
            hist = self.c.history(mid)
            if not hist:
                continue
            risk, matched, status = status_of(hist[-3:])
            latest = hist[-1]

            fp = None
            if latest["match_fp"] is not None:
                fp = next(
                    (
                        f
                        for f in registry.fingerprints(self.c.reg, m["type"])
                        if f["id"] == latest["match_fp"]
                    ),
                    None,
                )
            own = [x for x in registry.fingerprints(self.c.reg, m["type"]) if x["machine"] == mid]
            known = own[-1]["fault"] if own else (fp["fault"] if fp and matched else None)
            f = self.c.factories[m["factory"]]
            est = forecast.from_history(hist, self.c.cfg["tick_hours"], m["source"])
            cost = costs.breakdown(m["factory"], m["type"], known, self.c.cfg["service_hours"][m["type"]])
            p_soon = forecast.p_before(est, RISK_HORIZON_DAYS)
            rows.append(
                {
                    "machine_id": mid,
                    "factory": f["name"],
                    "area": f["area"],
                    "machine": m["name"],
                    "type": m["type"],
                    "risk": round(risk, 3),
                    "days_rising": days_rising(hist),
                    "status": status,
                    "known_fault": known,
                    "days_to_fault_level": {k: est.get(k) for k in ("status", "low", "mid", "high", "level")},
                    "rupees_at_risk": round(p_soon * cost["breakdown_cost_inr"]),
                    "registry": (
                        None
                        if fp is None
                        else {
                            "cosine": round(latest["match_cos"], 3),
                            "flag_level": round(latest["match_level"], 3),
                            "matched": matched,
                            "fault": fp["fault"],
                            "owner_label": fp["owner_label"],
                            "from_area": self.c.factories[fp["factory"]]["area"],
                            "from_area_local": self.c.factories[fp["factory"]].get("area_local", ""),
                        }
                    ),
                }
            )
        rows.sort(
            key=lambda r: (-(r["status"] in ("alarm", "early_match")), -r["rupees_at_risk"], -r["risk"])
        )
        return [r for r in rows if r["risk"] >= min_risk or r["status"] == "early_match"]

    def plan_technician_route(
        self, risk_threshold: float, hours_available: float | None = None
    ) -> dict[str, Any]:
        tech = self.c.cfg["technician"]
        stops = []
        for r in self.get_risk_ranking():
            soon = (r["days_to_fault_level"]["low"] or 99) <= RISK_HORIZON_DAYS
            if r["risk"] >= risk_threshold or r["status"] == "early_match" or soon:
                m = self.c.machines[r["machine_id"]]
                risk = max(r["risk"], 1.0 if r["status"] == "early_match" else 0)
                stops.append(
                    {
                        "id": m["id"],
                        "xy_km": self.c.factories[m["factory"]]["xy_km"],
                        "service_h": self.c.cfg["service_hours"][m["type"]],
                        "risk": risk,
                        # rupees at risk decide who is visited first; risk breaks ties
                        "value": r["rupees_at_risk"] + risk,
                    }
                )
        hours = hours_available or tech["hours_per_day"]
        p = route.plan(stops, tech["xy_km"], hours, self.c.cfg["road_kmph"])

        def name(i: str) -> str:
            return f"{i} ({self.c.factories[self.c.machines[i]['factory']]['area']})"

        day = self.c.now() + timedelta(days=0 if self.c.now().hour < 12 else 1)  # after noon: plan tomorrow
        return {
            "date": day.date().isoformat(),
            "weekday": DAYS[day.weekday()],
            "technician": tech["name"],
            "hours_available": hours,
            "stops": [name(i) for i in p["order"]],
            "drive_km": p["km"],
            "hours_used": p["hours"],
            "not_today": [name(i) for i in p["skipped"]],
            "rupees_at_risk_visited": round(
                sum(s["value"] - s["risk"] for s in stops if s["id"] in p["order"])
            ),
        }

    def draft_owner_alert(self, machine_id: str, kind: str, visit_day: str) -> dict[str, Any]:
        open_ = self.c.db.execute(
            "SELECT ts, approved FROM messages WHERE ask LIKE ? AND (approved IS NULL OR approved=1)"
            " ORDER BY id DESC LIMIT 1",
            (f"{machine_id}|%",),
        ).fetchone()
        if open_:  # stay quiet: the owner already has this question or already said yes
            state = "awaiting reply" if open_["approved"] is None else "visit already approved"
            return {"skipped": f"owner already asked on {open_['ts']} ({state})"}
        m = self.c.machines[machine_id]
        f = self.c.factories[m["factory"]]
        rank = next(r for r in self.get_risk_ranking() if r["machine_id"] == machine_id)
        reg = rank["registry"] or {}
        lang = f["language"]
        local = lang in ("hi", "mr")
        tech = self.c.cfg["technician"]
        area = reg.get("from_area_local" if local else "from_area", "")
        text = alerts.draft(
            lang,
            kind,
            m["name_local"] if local else m["name"],
            DAYS.index(visit_day),
            fault=rank["known_fault"],
            days=rank["days_rising"],
            area=area,
            tech=tech["name_local"] if local else tech["name"].split()[0],
        )
        self.c.db.execute(
            "INSERT INTO messages (run_id, ts, factory, language, body, ask) VALUES (?,?,?,?,?,?)",
            (
                self.run_id,
                self.c.now().isoformat(timespec="minutes"),
                f["id"],
                lang,
                text,
                f"{machine_id}|{visit_day}",
            ),
        )
        self.c.db.commit()
        text_en = alerts.draft(
            "en",
            kind,
            m["name"],
            DAYS.index(visit_day),
            fault=rank["known_fault"],
            days=rank["days_rising"],
            area=reg.get("from_area", ""),
            tech=tech["name"].split()[0],
        )
        return {"to": f["owner"], "phone": f["phone"], "language": lang, "text": text, "text_en": text_en}

    def quote_parts(self, fault: str, machine_type: str) -> dict[str, Any]:
        parts = json.loads(PARTS.read_text(encoding="utf-8"))["parts"]
        return {
            "source": "MOCK dealer data",
            "parts": [p for p in parts if p["fault"] == fault and p["type"] == machine_type],
        }


def status_of(last3: list[dict[str, Any]]) -> tuple[float, bool, str]:
    """(risk over the last 3 readings, registry match in 2 of 3, status) as the dashboard shows it."""
    risk = sum(r["risk"] for r in last3) / len(last3)
    hits = [r["match_cos"] is not None and r["match_cos"] > r["match_level"] for r in last3]
    matched = sum(hits) >= 2  # one stray match is not enough
    status = "alarm" if risk >= 1.0 else "early_match" if matched else "watch" if risk >= WATCH else "ok"
    return risk, matched, status


def days_rising(hist: list[dict[str, Any]]) -> int:
    """Consecutive days (incl. today) the daily mean risk has gone up. 0 = not rising."""
    daily = defaultdict(list)
    for r in hist:
        daily[r["ts"][:10]].append(r["risk"])
    means = [sum(v) / len(v) for _, v in sorted(daily.items())]
    k = 0
    while k + 1 < len(means) and means[-1 - k] > means[-2 - k]:
        k += 1
    return k + 1 if k else 0


def run_rules(cluster: Cluster) -> tuple[str, str]:
    """No LLM: the same tools in a fixed order. Logged exactly like the Claude run."""
    run_id = "rules-" + uuid.uuid4().hex[:8]
    t = Tools(cluster, run_id, "rules")
    ranking = t.call("get_risk_ranking", {})
    if not any(r["status"] in ("alarm", "early_match") for r in ranking):
        return run_id, "Nothing to report."
    plan = t.call("plan_technician_route", {"risk_threshold": 1.0})
    today = plan["weekday"]  # the day the route is for
    tomorrow = DAYS[(DAYS.index(today) + 1) % 7]
    by_id = {r["machine_id"]: r for r in ranking}
    lines = []
    for stop_list, day in ((plan["stops"], today), (plan["not_today"], tomorrow)):
        for s in stop_list:
            mid = s.split()[0]
            kind = "match" if by_id[mid]["status"] == "early_match" else "rising"
            t.call("draft_owner_alert", {"machine_id": mid, "kind": kind, "visit_day": day})
            fault = by_id[mid]["known_fault"]
            if fault in alerts.FAULT["en"]:
                q = t.call("quote_parts", {"fault": fault, "machine_type": by_id[mid]["type"]})
                lines += [f"carry for {mid}: {p['part']}" for p in q["parts"] if p["in_stock"]]
    note = [f"{today}: " + " -> ".join(plan["stops"]) + f" ({plan['drive_km']} km)", *lines]
    return run_id, "\n".join(note[:4])


def run_claude(cluster: Cluster, client: Any = None, max_turns: int = 12) -> tuple[str, str]:
    import anthropic

    client = client or anthropic.Anthropic()
    run_id = "claude-" + uuid.uuid4().hex[:8]
    t = Tools(cluster, run_id, "claude")
    now = cluster.now()
    messages = [
        {
            "role": "user",
            "content": f"Today is {DAYS[now.weekday()]} {now.date()}, {now:%H:%M}. "
            f"Tomorrow is {DAYS[(now + timedelta(days=1)).weekday()]}. Prepare today's digest.",
        }
    ]
    for _ in range(max_turns):
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
        )
        if resp.stop_reason == "refusal":
            return run_id, "Agent declined this run; use --rules."
        messages.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            return run_id, "\n".join(b.text for b in resp.content if b.type == "text").strip()
        results = []
        for u in uses:
            try:
                out = t.call(u.name, dict(u.input))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": u.id,
                        "content": json.dumps(out, ensure_ascii=False),
                    }
                )
            except Exception as e:  # report the failure to the model, keep the log honest
                results.append(
                    {"type": "tool_result", "tool_use_id": u.id, "content": f"error: {e}", "is_error": True}
                )
        messages.append({"role": "user", "content": results})
    return run_id, "Stopped after max turns."


def record_reply(cluster: Cluster, message_id: int, reply: str) -> bool | None:
    """Owner's yes/no. Approval only books the visit; nothing touches the machine."""
    ok = alerts.parse_reply(reply)
    cluster.db.execute(
        "UPDATE messages SET reply=?, approved=? WHERE id=?",
        (reply, None if ok is None else int(ok), message_id),
    )
    cluster.db.commit()
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", action="store_true", help="fixed order, no LLM")
    args = ap.parse_args()
    c = Cluster()
    run_id, note = run_rules(c) if args.rules else run_claude(c)
    print(run_id)
    print(note)


if __name__ == "__main__":
    main()
