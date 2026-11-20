"""The Guardian: an independent check of what Granite proposes, before it reaches the owner or costs money.

The LLM proposes; code validates. The grounding check (grounding.py) covers numbers, ₹ amounts and part
numbers. The Guardian adds the rules a number check cannot see, and records a verdict for every check:

    G1 no_control_claim     the text never says the agent stopped, started or switched a machine (it cannot;
                            a stop is only ever the owner's decision, asked as a question)
    G2 unknown_citation     every [kb/...#...] or [faults.yaml#...] id cited was actually retrieved this turn
    G3 unsupported_fault    a fault named to the owner was named by a tool (case, diagnose_fault) or by a
                            retrieved knowledge chunk; the model may not diagnose on its own
    G4 untrusted_source     numbers that came only from an external (A2A) agent are not facts: they do not
                            ground anything (the caller leaves untrusted results out of the sources)
    G5 no_logged_approval   book_job / order_part only with an approved, logged owner approval of that kind
                            for that case (or, for a booking, the owner's own autonomy policy); checked
                            before the tool runs, independently of the tool's own gate

    v = guardian.review_text(text, facts)          -> {"ok": bool, "reasons": [{"rule", "detail"}]}
    v = guardian.review_paid(store, agent, tool, args)
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

from server.config import FAULTS

from . import grounding

# first-person claims of having controlled a machine, in the languages the owners use
CONTROL = [
    r"\b(i|we|dhadkan|the (agent|system|planner))\s+(have\s+|has\s+|had\s+)?(just\s+)?"
    r"(stopped|shut( it)? down|switched (it )?off|turned (it )?off|started|restarted|switched (it )?on|slowed)\b",
    r"\b(machine|motor|pump|blower|loom|it)\s+(has been|was|is now)\s+(stopped|shut down|switched off|turned off)\s+by\s+(me|us|dhadkan)\b",
    r"\b(maine|humne|hamne|mi|amhi)\b[^.!?\n]{0,40}\b(band|chalu|off|on)\s+(kar|kel[ae]|kari)\s*(diya|di|dia|diye|dili|dile|deedhu|didhu)\b",
    r"(मैंने|हमने|मी|आम्ही|મેં|અમે)[^.!?\n]{0,40}(बंद|चालू|બંધ|ચાલુ)\s*(कर\s*(दिया|दी|दिए)|केल[ीे]|કર્યું|કરી)",
]
CONTROL_RE = [re.compile(p, re.IGNORECASE) for p in CONTROL]
CITE = re.compile(r"\[((?:kb/[\w.-]+\.md|faults\.yaml)#[\w-]+)\]")
PAID = {"book_job": "booking", "order_part": "purchase"}


@lru_cache(maxsize=1)
def fault_words() -> dict[str, list[str]]:
    """fault key -> its names in every language faults.yaml has, plus the key itself, lower-case."""
    raw = yaml.safe_load(FAULTS.read_text(encoding="utf-8"))["faults"]
    out = {}
    for key, f in raw.items():
        names = {key.replace("_", " "), key}
        for n in f["name"].values():
            names |= {p.strip().lower() for p in re.split(r"[/()]", n) if len(p.strip()) > 3}
        out[key] = sorted(names, key=len, reverse=True)
    return out


def faults_named(text: str) -> set[str]:
    low = text.lower()
    return {k for k, words in fault_words().items() if any(w in low for w in words)}


@dataclass
class Facts:
    """What this turn may rely on: trusted tool results, retrieved chunk ids, and the faults tools named."""

    sources: list[Any] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    retrieved: set[str] = field(default_factory=set)
    faults: set[str] = field(default_factory=set)


def facts_from(results: list[dict[str, Any]], ctx: dict[str, Any] | None = None, extra: list[str] | None = None,
               case_fault: str | None = None) -> Facts:  # fmt: skip
    """Build the facts from this turn's tool results (untrusted ones left out) and the event context."""
    f = Facts(extra=list(extra or []))
    for r in results:
        out = r.get("result", r)
        if not isinstance(out, dict) or out.get("untrusted"):
            continue  # G4: an external agent's words are not facts
        f.sources.append(out)
        f.retrieved |= {c["id"] for c in out.get("chunks", []) if isinstance(c, dict) and "id" in c}
        for k in ("fault", "named"):
            if isinstance(out.get(k), str):
                f.faults.add(out[k])
        f.faults |= {x for x in out.get("likely_faults", []) or [] if isinstance(x, str)}
    if ctx:
        keep = {
            k: v for k, v in ctx.items() if k not in ("event", "partner_offers")
        }  # G4: partners are not facts
        f.sources.append(keep | {"event_type": ctx.get("event", {}).get("type")})
        f.retrieved |= {k["id"] for k in ctx.get("knowledge", [])}
        f.faults |= {ctx.get("case", {}).get("context", {}).get("fault")} - {None}
    if case_fault:
        f.faults.add(case_fault)
    from server import kb

    by_id = {c["id"]: c for c in kb.chunks()}
    for cid in f.retrieved:
        f.faults |= set(by_id.get(cid, {}).get("faults", []))
    return f


def review_text(text: str, facts: Facts, check_numbers: bool = True, labels: str = "") -> dict[str, Any]:
    """G1-G3 (and the figures, unless the caller already checked them). `labels` are reply options in the
    owner's own voice: checked for citations and faults, not for G1."""
    reasons: list[dict[str, str]] = []
    for rx in CONTROL_RE:
        m = rx.search(text)
        if m:
            reasons.append({"rule": "no_control_claim", "detail": m.group(0)[:80]})
            break
    both = f"{text} {labels}"
    for cid in CITE.findall(both):
        if cid not in facts.retrieved:
            reasons.append({"rule": "unknown_citation", "detail": cid})
    for fk in sorted(faults_named(CITE.sub(" ", both)) - facts.faults):  # a cited id is not a fault claim
        reasons.append({"rule": "unsupported_fault", "detail": fk})
    if check_numbers:
        g = grounding.check(text, facts.sources, facts.extra)
        for n in g["unknown_numbers"] + g["unknown_parts"]:
            reasons.append({"rule": "ungrounded_figure", "detail": n})
    return {"ok": not reasons, "reasons": reasons}


def review_paid(store: Any, agent: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """G5: a paid action needs an approved owner approval of its kind for its case, already logged."""
    kind = PAID[tool]
    cid = args.get("case_id")
    approved = [a for a in store.approvals(cid, status="approved") if a["kind"] == kind] if cid else []
    if tool == "book_job" and approved:
        case = store.case(cid)
        ok_ids = {p["approval"] for p in case["context"].get("proposed", [])
                  if p["technician"] == args.get("technician") and p["day"] == args.get("day")}  # fmt: skip
        approved = [a for a in approved if a["id"] in ok_ids or a.get("decided_by") == "policy"]
    if (
        not approved
        and tool == "book_job"
        and cid
        and (cover := agent.approval_covers(cid, args.get("technician")))
    ):
        return {"ok": True, "reasons": [],
                "basis": f"approval {cover['id']} still covers a replacement mechanic at no extra cost"}  # fmt: skip
    if not approved and tool == "book_job" and cid:
        pol = agent.autonomy(cid)
        if pol and pol.get("mechanic") == args.get("technician"):
            return {"ok": True, "reasons": [], "basis": "owner's autonomy policy (logged when used)"}
    if approved:
        a = approved[-1]
        return {
            "ok": True,
            "reasons": [],
            "basis": f"approval {a['id']} by {a.get('decided_by')} via {a.get('channel')}",
        }
    return {"ok": False, "basis": None,
            "reasons": [{"rule": "no_logged_approval", "detail": f"{tool} for {cid} has no approved {kind}"}]}  # fmt: skip
