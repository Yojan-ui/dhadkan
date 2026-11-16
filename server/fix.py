"""Fix: after the owner says "Mechanic bulao", the owner's own mechanic gets the job; weekly, dealers get a
parts forecast for the cluster.

Owner approval is enforced here, not just in the UI: send_job() refuses unless the owner's reply that
approved this visit is logged (messages.approved = 1). Nothing in this module can control a machine.

Dealer stock comes from data/sim/parts_mock.json, which is MOCK data (labelled as such in every
message). The parts forecast counts machines, not parts: a machine is counted when its days-to-fault
range reaches into the horizon (low end = "may", high end = "likely"), so every number is a count of
real forecasts on the dashboard. The weekly dealer message can be drafted by Claude through the
Message Batches API (50% price; the batch API takes no `fallbacks`), but its numbers are filled in by
code, never by the model.
"""

from datetime import timedelta
from typing import Any

from . import costs, diagnosis
from .notify import env

MOCK_FAULT = {
    "bearing_wear": "bearing",
    "imbalance": "imbalance",
    "belt_slip": "loose_belt",
    "cavitation": "clog",
}
VISIT_HOUR = 10  # no route stop for this machine: the next working morning at 10:00 (assumption)
JOB = {
    "en": "Job: {machine} at {factory} ({area}). Problem: {fault}. Bring: {part}. Visit: {when}. Dealer stock (mock data): {stock}.",
    "hi": "काम: {machine}, {factory} ({area})। खराबी: {fault}। साथ लाएँ: {part}। कब: {when}। डीलर स्टॉक (डेमो डेटा): {stock}।",
    "hi_latn": "Kaam: {machine}, {factory} ({area}). Kharabi: {fault}. Saath laayein: {part}. Kab: {when}. Dealer stock (demo data): {stock}.",
    "mr": "काम: {machine}, {factory} ({area}). बिघाड: {fault}. सोबत आणा: {part}. कधी: {when}. डीलर स्टॉक (डेमो डेटा): {stock}.",
    "gu": "કામ: {machine}, {factory} ({area}). ખામી: {fault}. સાથે લાવો: {part}. ક્યારે: {when}. ડીલર સ્ટોક (ડેમો ડેટા): {stock}.",
}  # fmt: skip


class ApprovalMissing(PermissionError):
    """An outward action was attempted without the owner's logged approval."""


def approved(db: Any, message_id: int | None) -> bool:
    if message_id is None:
        return False
    row = db.execute("SELECT approved FROM messages WHERE id=?", (message_id,)).fetchone()
    return bool(row) and row[0] == 1


def stock(fault: str | None, mtype: str) -> str:
    key = MOCK_FAULT.get(fault or "")
    rows = [p for p in costs.load()["parts"] if p["fault"] == key and p["type"] == mtype] if key else []
    if not rows:
        return "not listed"
    p = rows[0]
    return f"{p['part']} at {p['dealer']}: " + ("in stock" if p["in_stock"] else f"{p['lead_days']} days")


def visit_time(live: Any, mid: str) -> str:
    plan = live.route() or {}
    leg = next((x for x in plan.get("legs", []) if x.get("machine") == mid), None)
    if leg:
        return str(leg["arrive"]).replace("T", " ")[:16]
    now = live.c.now()
    day = now + timedelta(days=1) if now.hour >= VISIT_HOUR else now
    return day.replace(hour=VISIT_HOUR, minute=0).strftime("%Y-%m-%d %H:%M")


def send_job(alerts: Any, alert_row: dict[str, Any]) -> dict[str, Any]:
    """WhatsApp job for the factory's mechanic. Raises ApprovalMissing without the owner's logged '1'."""
    live = alerts.live
    if not approved(live.c.db, alert_row.get("message_id")):
        raise ApprovalMissing(f"no logged owner approval for message {alert_row.get('message_id')}")
    mid = alert_row["machine"]
    m = live.c.machines[mid]
    f = live.c.factories[m["factory"]]
    card = alerts.card(mid, "en") or {}
    lang = env.get(f"DHADKAN_LANG_MECHANIC_{m['factory']}") or f["language"]
    lang = lang if lang in JOB else "en"
    fault_key = card.get("fault")
    names = diagnosis.load()["faults"].get(fault_key or "", {}).get("name", {})
    part = (card.get("fix") or {}).get("part", {})
    body = JOB[lang].format(
        machine=m["name"] if lang in ("en", "hi_latn") else m.get("name_local", m["name"]),
        factory=f["name"], area=f.get("area", ""), fault=names.get(lang) or names.get("en") or "to be checked",
        part=part.get(lang) or part.get("en") or "inspection only", when=visit_time(live, mid),
        stock=stock(fault_key, m["type"]),
    )  # fmt: skip
    to = env.get(f"DHADKAN_MECHANIC_{m['factory']}")
    return alerts.notifier.send(machine=mid, factory=m["factory"], severity=alert_row["severity"], to=to,
                                lang=lang, body=body, kind="job", parent=alert_row["id"])  # fmt: skip


def parts_forecast(live: Any, horizon_days: float = 14.0) -> list[dict[str, Any]]:
    """Per part and area: how many machines may (range low end) and likely (high end) need it in the horizon."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for r in live.ranking():
        est = r.get("days_to_fault_level") or {}
        mid = r["machine_id"]
        card = live.alerts.card(mid, "en") or {}
        if not card.get("fault"):
            continue
        at_fault = est.get("status") == "at_fault_level"
        low, high = est.get("low"), est.get("high")
        may = at_fault or (est.get("status") == "estimate" and low is not None and low <= horizon_days)
        likely = at_fault or (est.get("status") == "estimate" and high is not None and high <= horizon_days)
        if not may:
            continue
        m = live.c.machines[mid]
        area = live.c.factories[m["factory"]].get("area", "")
        part = card["fix"]["part"]["en"]
        e = rows.setdefault((part, area), {"part": part, "area": area, "may": 0, "likely": 0, "machines": []})
        e["may"] += 1
        e["likely"] += int(likely)
        e["machines"].append(mid)
    return sorted(rows.values(), key=lambda e: (-e["may"], e["part"]))


def forecast_text(rows: list[dict[str, Any]], horizon_days: float = 14.0) -> str:
    if not rows:
        return f"No part is forecast to be needed in the next {horizon_days:g} days."
    return "\n".join(
        f"{e['part']}: {e['likely']}-{e['may']} machines, {e['area']}, {horizon_days:g} days" for e in rows
    )


def dealer_batch_requests(rows: list[dict[str, Any]], dealers: dict[str, str]) -> list[dict[str, Any]]:
    """Message Batches requests: Claude only rewords the computed forecast for each dealer (effort low).

    Each item is {"custom_id", "params"} for client.messages.batches.create(requests=...). No `fallbacks`
    (the batch API rejects it); no `thinking` parameter; JSON output so the numbers can be checked.
    """
    schema = {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"],
              "additionalProperties": False}  # fmt: skip
    out = []
    for dealer, lang in dealers.items():
        facts = forecast_text(rows)
        out.append({"custom_id": f"dealer-{dealer}"[:64], "params": {
            "model": "claude-fable-5-2", "max_tokens": 1000,
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            "system": "Rewrite a parts forecast as a short WhatsApp message for a parts dealer. Keep every number "
                      "exactly as given; do not add numbers. Say it is a forecast, not an order.",
            "messages": [{"role": "user", "content": f"Language: {lang}. Forecast:\n{facts}"}]}})  # fmt: skip
    return out


def numbers_kept(text: str, facts: str) -> bool:
    """A drafted message may only use numbers that are in the computed forecast."""
    import re

    allowed = set(re.findall(r"\d+", facts))
    return set(re.findall(r"\d+", text)) <= allowed


def submit_dealer_batch(
    rows: list[dict[str, Any]], dealers: dict[str, str], client: Any = None
) -> str | None:
    """Weekly: submit the dealer drafts as one Message Batch. Needs ANTHROPIC_API_KEY; returns the batch id."""
    if client is None:
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        import anthropic

        client = anthropic.Anthropic(api_key=key)
    batch = client.messages.batches.create(requests=dealer_batch_requests(rows, dealers))
    return str(batch.id)
