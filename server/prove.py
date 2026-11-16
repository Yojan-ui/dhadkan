"""Prove: did the repair work, a signed record of it, and a machine health passport.

After a repair the owner re-records the machine for 60 s (six 10 s readings). Against the score that
raised the alarm, the outcome is one of:
- fixed   "Theek ho gaya": the re-record sits below FIXED_BELOW of the alarm level (where adaptation
          also stops learning, edge/adapt.py), i.e. clearly normal again;
- partly  "Aadha theek": lower, by at least PARTLY_DROP of the excess over normal, but not normal;
- failed  "Repair kaam nahi aaya": no real change. A wrong or fake part is one possible reason; the
          record says "possible", never "fake".
The cut-offs are assumptions, not measurements.

The repair record and the passport are canonical JSON signed with Ed25519. The private key lives in the
git-ignored keys/ folder (created on first use, mode 0600). Anyone with the public key
(/api/records/public-key) can check a record; changing any field breaks the signature.

The passport holds numbers only: daily scores, alert counts, verified repairs and a grade. No audio,
no phone numbers, and no mechanic statistics (those are for the owner only: mechanic_stats()).
Verified repairs are appended to data/labels/verified.jsonl (git-ignored) as labelled training data.
"""

import base64
import hashlib
import io
import json
import os
import time
from typing import Any

import numpy as np

from .config import ROOT

FIXED_BELOW = 0.8
PARTLY_DROP = 0.5
KEYS = ROOT / "keys"
LABELS = ROOT / "data" / "labels" / "verified.jsonl"
WORDS = {
    "fixed": {"en": "Fixed", "hi": "ठीक हो गया", "hi_latn": "Theek ho gaya", "mr": "दुरुस्त झाले", "gu": "ઠીક થઈ ગયું"},
    "partly": {"en": "Partly fixed", "hi": "आधा ठीक", "hi_latn": "Aadha theek", "mr": "अर्धवट दुरुस्त", "gu": "અડધું ઠીક"},
    "failed": {"en": "The repair did not work (possibly a wrong or fake part)",
               "hi": "रिपेयर काम नहीं आया (गलत या नकली पार्ट हो सकता है)",
               "hi_latn": "Repair kaam nahi aaya (galat ya nakli part ho sakta hai)",
               "mr": "दुरुस्तीचा उपयोग झाला नाही (चुकीचा किंवा बनावट पार्ट असू शकतो)",
               "gu": "રિપેર કામ ન આવ્યું (ખોટો કે નકલી પાર્ટ હોઈ શકે)"},
}  # fmt: skip
SCHEMA = """
CREATE TABLE IF NOT EXISTS repair_records (
  id INTEGER PRIMARY KEY, machine TEXT, factory TEXT, created REAL, record TEXT, sha256 TEXT, signature TEXT,
  mechanic TEXT);
"""


def verify_repair(before_risk: list[float], after_risk: list[float]) -> dict[str, Any]:
    """before: risk (score / alarm level) of the readings that raised the alarm; after: the 60 s re-record."""
    b, a = float(np.median(before_risk)), float(np.median(after_risk))
    if a < FIXED_BELOW:
        outcome = "fixed"
    elif b > 0 and (b - a) >= PARTLY_DROP * max(b - FIXED_BELOW, 1e-9):
        outcome = "partly"
    else:
        outcome = "failed"
    return {"outcome": outcome, "before_median_risk": round(b, 3), "after_median_risk": round(a, 3),
            "after_readings": len(after_risk), "words": WORDS[outcome],
            "rule": f"fixed if after < {FIXED_BELOW}; partly if it fell by >= {PARTLY_DROP:.0%} of the excess"}  # fmt: skip


# ------------------------------------------------------------------ signing
def _keys() -> tuple[Any, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = KEYS / "records_ed25519.pem"
    if not path.exists():
        KEYS.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())  # fmt: skip
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return key, pub


def public_key_pem() -> str:
    return _keys()[1].decode()


def canonical(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def sign(doc: dict[str, Any]) -> dict[str, str]:
    key, _ = _keys()
    raw = canonical(doc)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "signature": base64.b64encode(key.sign(raw)).decode(),
            "algorithm": "Ed25519 over canonical JSON (sorted keys, no spaces)"}  # fmt: skip


def verify_signature(doc: dict[str, Any], signature_b64: str, public_pem: str | None = None) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization

    pub = serialization.load_pem_public_key((public_pem or public_key_pem()).encode())
    try:
        pub.verify(base64.b64decode(signature_b64), canonical(doc))  # type: ignore[call-arg, union-attr]
        return True
    except (InvalidSignature, ValueError):
        return False


# ------------------------------------------------------------------ records
def make_record(live: Any, mid: str, after_risk: list[float], mechanic: str | None, part: str | None,
                demo_note: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Repair record for the machine's latest alert episode; stored, signed and returned with its signature."""
    live.c.db.executescript(SCHEMA)
    hist = live.c.history(mid)
    at = [h for h in hist if h["risk"] >= 1.0]
    before = [h["risk"] for h in (at[-3:] if at else hist[-3:])]
    m = live.c.machines[mid]
    card = live.alerts.card(mid, "en") or {}
    v = verify_repair(before, after_risk)
    doc = {
        "kind": "dhadkan.repair_record.v1",
        "machine": {"id": mid, "name": m["name"], "type": m["type"], "factory": live.c.factories[m["factory"]]["name"]},
        "fault": card.get("fault"), "named_by": card.get("named_by"),
        "alarm_at": at[-1]["ts"] if at else None, "repaired_at": live.c.now().isoformat(timespec="minutes"),
        "part": part or (card.get("fix") or {}).get("part", {}).get("en"),
        "verification": v, "after_risk": [round(x, 3) for x in after_risk],
        "demo_note": demo_note,
    }  # fmt: skip
    sig = sign(doc)
    cur = live.c.db.execute(
        "INSERT INTO repair_records (machine, factory, created, record, sha256, signature, mechanic) VALUES (?,?,?,?,?,?,?)",
        (mid, m["factory"], time.time(), json.dumps(doc, ensure_ascii=False), sig["sha256"], sig["signature"], mechanic),
    )  # fmt: skip
    live.c.db.commit()
    if v["outcome"] == "fixed" and doc["fault"]:  # a verified repair is a labelled fault for future training
        LABELS.parent.mkdir(parents=True, exist_ok=True)
        with LABELS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"unit": m["source"], "machine": mid, "fault": doc["fault"], "alarm_at": doc["alarm_at"],
                                "repaired_at": doc["repaired_at"], "record_sha256": sig["sha256"]}) + "\n")  # fmt: skip
    return {"id": int(cur.lastrowid or 0), "record": doc, **sig}


def records(live: Any, mid: str | None = None) -> list[dict[str, Any]]:
    live.c.db.executescript(SCHEMA)
    q = "SELECT * FROM repair_records" + (" WHERE machine=?" if mid else "") + " ORDER BY id"
    return [{"id": r["id"], "record": json.loads(r["record"]), "sha256": r["sha256"], "signature": r["signature"],
             "mechanic": r["mechanic"]} for r in live.c.db.execute(q, (mid,) if mid else ())]  # fmt: skip


def record_pdf(rec: dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    d, ss = rec["record"], getSampleStyleSheet()
    v = d["verification"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Dhadkan repair record")
    rows = [
        Paragraph("Dhadkan repair record", ss["Title"]),
        Paragraph(f"{d['machine']['name']} ({d['machine']['type']}), {d['machine']['factory']}", ss["Heading2"]),
        Paragraph(f"Fault: {d['fault'] or 'not named'} (named by {d['named_by'] or '-'}). Part: {d['part'] or '-'}.", ss["Normal"]),
        Paragraph(f"Alarm: {d['alarm_at'] or '-'}. Repaired: {d['repaired_at']}.", ss["Normal"]),
        Paragraph(f"Result: <b>{v['words']['en']}</b>. Score vs alarm level: before {v['before_median_risk']}, "
                  f"after {v['after_median_risk']} ({v['after_readings']} readings). Rule: {v['rule']}.", ss["Normal"]),
        Spacer(1, 8),
        Paragraph(f"SHA-256: {rec['sha256']}", ss["Code"]),
        Paragraph(f"Ed25519 signature: {rec['signature']}", ss["Code"]),
        Paragraph("Check it with the public key at /api/records/public-key. No audio is stored in this record.", ss["Italic"]),
    ]  # fmt: skip
    if d.get("demo_note"):
        rows.append(Paragraph(f"Demo: {d['demo_note']}", ss["Italic"]))
    doc.build(rows)
    return buf.getvalue()


# ------------------------------------------------------------------ passport and mechanic stats
def grade(
    alarm_days: int, repairs: list[dict[str, Any]], open_alert: bool, at_fault: bool
) -> tuple[str, str]:
    """A-D from the period's history (assumption, shown with the grade)."""
    last = repairs[-1]["outcome"] if repairs else None
    if at_fault or last == "failed":
        return "D", "sounds faulty now, or the last repair did not work"
    if open_alert or last == "partly":
        return "C", "an alert is open, or the last repair only partly worked"
    if alarm_days:
        return "B", "had alerts, and the last one ended in a verified repair"
    return "A", "no alarm in the period"


def passport(live: Any, mid: str) -> dict[str, Any]:
    """Shareable (second-hand sale, loans): numbers only. No audio, no phone numbers, no mechanic stats."""
    hist = live.c.history(mid)
    daily: dict[str, list[float]] = {}
    for h in hist:
        daily.setdefault(h["ts"][:10], []).append(h["risk"])
    days = [{"day": d, "mean_risk": round(float(np.mean(v)), 3), "max_risk": round(float(np.max(v)), 3)}
            for d, v in sorted(daily.items())]  # fmt: skip
    reps = [{"repaired_at": r["record"]["repaired_at"], "fault": r["record"]["fault"],
             "outcome": r["record"]["verification"]["outcome"], "record_sha256": r["sha256"]}
            for r in records(live, mid)]  # fmt: skip
    rank = next((r for r in live.ranking() if r["machine_id"] == mid), {})
    alerts = [a for a in live.alerts.notifier.recent(500) if a["machine"] == mid and a["kind"] == "alert"]
    open_alert = rank.get("status") in ("alarm", "early_match") and not (
        reps and reps[-1]["outcome"] == "fixed"
    )
    at_fault = (rank.get("days_to_fault_level") or {}).get("status") == "at_fault_level" and not reps
    g, why = grade(sum(d["max_risk"] >= 1.0 for d in days), reps, open_alert, at_fault)
    m = live.c.machines[mid]
    doc = {"kind": "dhadkan.passport.v1", "machine": {"id": mid, "name": m["name"], "type": m["type"]},
           "period": {"from": days[0]["day"] if days else None, "to": days[-1]["day"] if days else None},
           "daily": days, "alerts": len(alerts), "verified_repairs": reps, "grade": g, "grade_reason": why,
           "grade_rule": "D: faulty now or last repair failed; C: open alert or partly fixed; B: alerts, last one "
                         "fixed; A: no alarm (assumption)",
           "privacy": "numbers only: no audio, no phone numbers, no mechanic statistics"}  # fmt: skip
    return {"passport": doc, **sign(doc)}


def mechanic_stats(live: Any, factory: str) -> list[dict[str, Any]]:
    """Owner only: per mechanic, how many verified repairs worked. Never in the passport."""
    by: dict[str, list[str]] = {}
    for r in records(live):
        if live.c.machines[r["record"]["machine"]["id"]]["factory"] == factory:
            by.setdefault(r["mechanic"] or "unknown", []).append(r["record"]["verification"]["outcome"])
    return [{"mechanic": k, "repairs": len(v), "fixed": v.count("fixed"), "partly": v.count("partly"),
             "failed": v.count("failed"), "success_rate": round(v.count("fixed") / len(v), 2)} for k, v in by.items()]  # fmt: skip


def demo_after_readings(live: Any, mid: str, n: int = 6) -> tuple[list[float], str]:
    """The demo has no real machine to re-record: after-repair readings are the same unit's own normal
    recordings (MIMII/DCASE normal test clips, or the healthy start of the IMS run). Labelled as such."""
    if mid in live.c.ims:
        src = live.c.ims[mid]
        return [float(src.score[i]) for i in range(min(n, len(src.score)))], (
            "after-repair readings are the healthy start of the same NASA IMS run (a new bearing)"
        )
    m = live.c.machines[mid]
    pool = live.c.pools()[mid][0][:n]
    model = live.c.model(m["type"])
    from edge import features

    return [float(model.score_audio(features.load(p), m["source"])["risk"]) for p in pool], (
        "after-repair readings are this unit's own recorded normal clips"
    )
