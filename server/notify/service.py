"""Sending owner alerts and handling replies.

    send()       WhatsApp first; if it is unreachable, SMS; if that is unreachable too, the alert
                 waits in an outbox (SQLite) and goes out when the network is back.
    reply()      "1" books the technician (via the agent's reply path), "2" = later, "3" = details.
                 Nothing here can stop, slow or switch off a machine.
    escalate()   a Stop-now alert with no reply after 2 hours goes to the factory's second contact.

Every alert, reply and escalation is logged in node.db (table alerts).
"""

import json
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from edge.outbox import OFFLINE, Outbox

from .whatsapp import DryRun, Media

ESCALATE_AFTER_S = 2 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, created REAL, machine TEXT, factory TEXT, severity TEXT, kind TEXT,
  to_phone TEXT, lang TEXT, body TEXT, media TEXT, message_id INTEGER, status TEXT, channel TEXT,
  provider_ids TEXT, error TEXT, sent_at REAL, reply TEXT, choice TEXT, replied_at REAL,
  escalated_alert INTEGER, parent INTEGER, last_reply TEXT);
"""
NEW_COLUMNS = {"template": "TEXT", "send_after": "REAL"}  # added later; older databases get them on open

CHOICES = {  # digits (Latin, Devanagari, Gujarati), the button titles, and the usual words
    "book": {
        "1",
        "१",
        "૧",
        "haan",
        "हाँ",
        "हां",
        "हो",
        "yes",
        "ha",
        "ho",
        "હા",
        "mechanic",
        "mistri",
        "bulao",
        "bolva",
        "bolavo",
        "मिस्त्री",
        "बुलाओ",
        "बोलवा",
        "મિસ્ત્રી",
        "બોલાવો",
    },
    "later": {
        "2",
        "२",
        "૨",
        "baad",
        "बाद",
        "नंतर",
        "later",
        "nahi",
        "नहीं",
        "नाही",
        "ना",
        "no",
        "kal",
        "कल",
        "उद्या",
        "કાલે",
        "udya",
        "kale",
        "પછી",
        "ના",
    },
    "details": {"3", "३", "૩", "details", "जानकारी", "माहिती", "info", "mahiti", "vigat", "માહિતી"},
}


def parse_choice(text: str) -> str | None:
    """'1' / '2' / '3' (or the words for them) -> book / later / details; anything else -> None."""
    words = re.findall(r"[\wऀ-ॿ઀-૿]+", text.lower())
    hits = {c for c, vocab in CHOICES.items() for w in words if w in vocab}
    return hits.pop() if len(hits) == 1 else None


class Notifier:
    def __init__(
        self,
        db: sqlite3.Connection,
        whatsapp: Any,
        sms: Any,
        outbox_path: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db, self.wa, self.sms, self.clock = db, whatsapp, sms, clock
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(alerts)")}
        for col, typ in NEW_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE alerts ADD COLUMN {col} {typ}")
        self.db.commit()
        self.outbox = Outbox(outbox_path, self._deliver, clock)

    def clear(self) -> None:
        """Forget every alert and anything still queued (a new replay starts from nothing)."""
        self.db.execute("DELETE FROM alerts")
        self.db.commit()
        self.outbox.db.execute("DELETE FROM outbox")
        self.outbox.db.commit()

    # -- sending ------------------------------------------------------------
    def _deliver(self, kind: str, p: dict[str, Any]) -> dict[str, Any]:
        """Outbox transport: WhatsApp, else SMS; raises ConnectionError when both are unreachable."""
        media = [
            Media(Path(m["path"]), m["mime"], m.get("caption", ""), m.get("public_url")) for m in p["media"]
        ]
        try:
            res = self.wa.send(p["to"], p["body"], media, lang=p["lang"], template=p.get("template"),
                               buttons=p.get("buttons"))  # fmt: skip
            channel = self.wa.name
        except OFFLINE:
            if isinstance(self.sms, DryRun) and not isinstance(self.wa, DryRun):
                raise
            res = self.sms.send(p["to"], p["body"], [], lang=p["lang"])  # SMS: plain text, no buttons
            channel = self.sms.name
        self._mark(p["alert_id"], "dry_run" if channel == "dry_run" else "sent", channel, res.get("ids", []))
        return res

    def _mark(
        self, alert_id: int, status: str, channel: str | None, ids: list[str], error: str | None = None
    ) -> None:
        self.db.execute(
            "UPDATE alerts SET status=?, channel=?, provider_ids=?, error=?, sent_at=? WHERE id=?",
            (
                status,
                channel,
                json.dumps(ids),
                error,
                self.clock() if status in ("sent", "dry_run") else None,
                alert_id,
            ),
        )
        self.db.commit()

    def send(
        self,
        *,
        machine: str,
        factory: str,
        severity: str,
        to: str | None,
        lang: str,
        body: str,
        media: list[dict[str, Any]] | None = None,
        message_id: int | None = None,
        kind: str = "alert",
        parent: int | None = None,
        template: str | None = None,
        buttons: list[str] | None = None,
        hold_until: float | None = None,
        digest: bool = False,
    ) -> dict[str, Any]:
        """hold_until: quiet hours / power cut, released by release(); digest: waits for the daily digest."""
        media = media or []
        status = "no_contact" if not to else "digest" if digest else "held" if hold_until else "queued"
        cur = self.db.execute(
            "INSERT INTO alerts (created, machine, factory, severity, kind, to_phone, lang, body, media, message_id,"
            " status, parent, template, send_after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.clock(), machine, factory, severity, kind, to, lang, body, json.dumps(media), message_id,
             status, parent, template, hold_until),
        )  # fmt: skip
        self.db.commit()
        aid = int(cur.lastrowid or 0)
        if status != "queued":  # no phone on file, or waiting for the right time: logged, not sent yet
            return self.alert(aid)
        self._queue(aid, to or "", lang, body, media, template, buttons)
        return self.alert(aid)

    def _queue(self, aid: int, to: str, lang: str, body: str, media: list[dict[str, Any]],
               template: str | None, buttons: list[str] | None) -> None:  # fmt: skip
        self.db.execute("UPDATE alerts SET status='queued' WHERE id=?", (aid,))
        self.db.commit()
        self.outbox.put("alert", {"alert_id": aid, "to": to, "lang": lang, "body": body, "media": media,
                                  "template": template, "buttons": buttons})  # fmt: skip
        out = self.outbox.flush(force=True)
        for r in out["rejected"]:
            self._mark(aid, "failed", None, [], r["error"])

    def release(self, now_ts: float) -> list[int]:
        """Send alerts held for quiet hours or a power cut once their time has come (factory clock)."""
        from .policy import BUTTONS

        due = self.db.execute(
            "SELECT * FROM alerts WHERE status='held' AND send_after <= ? ORDER BY id", (now_ts,)
        ).fetchall()
        for r in due:
            self._queue(int(r["id"]), r["to_phone"], r["lang"], r["body"], json.loads(r["media"] or "[]"),
                        r["template"], BUTTONS.get(r["lang"]))  # fmt: skip
        return [int(r["id"]) for r in due]

    def pending_digest(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM alerts WHERE status='digest' ORDER BY id")]

    def mark_digested(self, ids: list[int], digest_id: int) -> None:
        self.db.executemany(
            "UPDATE alerts SET status='in_digest', parent=? WHERE id=?", [(digest_id, i) for i in ids]
        )
        self.db.commit()

    def retry(self) -> dict[str, Any]:
        """Send what waited for the network (called periodically)."""
        return self.outbox.flush()

    # -- replies ------------------------------------------------------------
    def reply(self, phone: str, text: str) -> dict[str, Any] | None:
        """Log the owner's reply against their latest unanswered alert; returns the alert and the choice."""
        digits = "".join(c for c in phone if c.isdigit())
        rows = self.db.execute(
            "SELECT * FROM alerts WHERE kind IN ('alert','escalation') AND reply IS NULL ORDER BY id DESC"
        ).fetchall()
        row = next(
            (r for r in rows if r["to_phone"] and "".join(c for c in r["to_phone"] if c.isdigit()) == digits),
            None,
        )
        if row is None:
            return None
        choice = parse_choice(text)
        if choice not in ("book", "later"):  # details or an unclear answer: logged, the question stays open
            self.db.execute("UPDATE alerts SET last_reply=? WHERE id=?", (text, row["id"]))
            self.db.commit()
            return {"alert": self.alert(int(row["id"])), "choice": choice}
        self.db.execute(
            "UPDATE alerts SET reply=?, choice=?, replied_at=?, last_reply=? WHERE id=?",
            (text, choice, self.clock(), text, row["id"]),
        )
        if row["parent"]:  # an answer to the escalation also answers the original alert
            self.db.execute(
                "UPDATE alerts SET reply=COALESCE(reply, ?), choice=COALESCE(choice, ?) WHERE id=?",
                (text, choice, row["parent"]),
            )
        self.db.commit()
        return {"alert": self.alert(int(row["id"])), "choice": choice}

    # -- escalation ---------------------------------------------------------
    def escalate(
        self, contact_for: Callable[[str], tuple[str | None, str]], text_for: Callable[[sqlite3.Row], str]
    ) -> list[int]:
        """Stop-now alerts unanswered for 2 hours go to the second contact (once). Returns new alert ids."""
        due = self.db.execute(
            "SELECT * FROM alerts WHERE kind='alert' AND severity IN ('stop_now','replace') AND reply IS NULL"
            " AND escalated_alert IS NULL AND status IN ('sent','dry_run') AND sent_at < ?",
            (self.clock() - ESCALATE_AFTER_S,),
        ).fetchall()
        made = []
        for r in due:
            phone, lang = contact_for(r["factory"])
            new = self.send(
                machine=r["machine"], factory=r["factory"], severity=r["severity"], to=phone, lang=lang,
                body=text_for(r), media=json.loads(r["media"]), message_id=r["message_id"],
                kind="escalation", parent=r["id"],
            )  # fmt: skip
            self.db.execute("UPDATE alerts SET escalated_alert=? WHERE id=?", (new["id"], r["id"]))
            self.db.commit()
            made.append(int(new["id"]))
        return made

    # -- queries ------------------------------------------------------------
    def latest_for(self, phone: str) -> dict[str, Any] | None:
        """The newest alert sent to this phone, answered or not (a voice note is linked to it)."""
        digits = "".join(c for c in phone if c.isdigit())
        for r in self.db.execute(
            "SELECT * FROM alerts WHERE kind IN ('alert','escalation') ORDER BY id DESC"
        ):
            if r["to_phone"] and "".join(c for c in r["to_phone"] if c.isdigit()) == digits:
                return dict(r)
        return None

    def alert(self, aid: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
        return {**dict(row), "media": json.loads(row["media"] or "[]")}

    def recent(self, n: int = 30) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT id FROM alerts ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [self.alert(int(r["id"])) for r in rows]
