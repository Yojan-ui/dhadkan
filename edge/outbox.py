"""What the phone keeps when it has no signal: a small queue of messages for the cluster registry.

Factory sheds lose signal. A confirmed fault's fingerprint must still reach the
registry, exactly once, in the order it was confirmed. Items wait in SQLite (so
they survive a restart), are retried with backoff, and carry an idempotency key:
if the registry stored an item but the phone never heard back, the retry is
recognised and not stored twice. Only fingerprints (64 floats + words) are ever
queued; there is no field for audio.

    box = Outbox("outbox.db", send)        # send(kind, payload) -> response dict
    box.put("fingerprint", {...})
    box.flush()                            # {"sent": [...], "pending": n, "rejected": [...]}

`send` raises ConnectionError (or OSError / TimeoutError) when the registry can't
be reached: the item stays queued. Any other exception means the registry refused
it (bad payload): the item moves to `rejected` and is not retried.
"""

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

Send = Callable[[str, dict[str, Any]], dict[str, Any]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  seq INTEGER PRIMARY KEY, idem TEXT UNIQUE, kind TEXT, payload TEXT, created REAL,
  attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, last_error TEXT,
  state TEXT DEFAULT 'pending', result TEXT);
"""
MAX_BACKOFF_S = 300.0
OFFLINE = (ConnectionError, OSError, TimeoutError)


class Outbox:
    def __init__(self, path: str | Path, send: Send, clock: Callable[[], float] = time.time) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.send, self.clock = send, clock

    def put(self, kind: str, payload: dict[str, Any]) -> str:
        """Queue one message. Returns its idempotency key."""
        if "audio" in payload:
            raise ValueError("the outbox never carries audio")
        idem = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO outbox (idem, kind, payload, created) VALUES (?,?,?,?)",
            (idem, kind, json.dumps(payload), self.clock()),
        )
        self.db.commit()
        return idem

    def pending(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY seq").fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def flush(self, force: bool = False) -> dict[str, Any]:
        """Send pending items oldest first. Stops at the first connection failure to keep order."""
        sent, rejected = [], []
        now = self.clock()
        for item in self.pending():
            if not force and item["next_try"] > now:
                break  # still backing off; later items wait so order is kept
            try:
                result = self.send(item["kind"], {**item["payload"], "idem": item["idem"]})
            except OFFLINE as e:
                attempts = item["attempts"] + 1
                self.db.execute(
                    "UPDATE outbox SET attempts=?, next_try=?, last_error=? WHERE seq=?",
                    (attempts, now + min(MAX_BACKOFF_S, 2.0**attempts), str(e), item["seq"]),
                )
                self.db.commit()
                break
            except Exception as e:  # the registry refused it; retrying won't help
                self.db.execute(
                    "UPDATE outbox SET state='rejected', last_error=? WHERE seq=?", (str(e), item["seq"])
                )
                rejected.append({"idem": item["idem"], "error": str(e)})
            else:
                self.db.execute(
                    "UPDATE outbox SET state='sent', result=? WHERE seq=?", (json.dumps(result), item["seq"])
                )
                sent.append(
                    {"idem": item["idem"], "kind": item["kind"], "payload": item["payload"], "result": result}
                )
            self.db.commit()
        return {"sent": sent, "rejected": rejected, "pending": len(self.pending())}


def http_send(base_url: str, client: Any = None, timeout: float = 5.0) -> Send:
    """Transport to the registry API (server/registry_api.py) over HTTP."""
    import httpx

    http = client or httpx.Client(base_url=base_url, timeout=timeout)

    def send(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind != "fingerprint":
            raise ValueError(f"unknown outbox kind {kind!r}")
        try:
            r = http.post("/fingerprints", json=payload)
        except httpx.TransportError as e:  # no route, DNS, timeout: try again later
            raise ConnectionError(str(e)) from e
        if r.status_code >= 500:
            raise ConnectionError(f"registry error {r.status_code}")
        if r.status_code >= 400:
            raise ValueError(f"registry refused: {r.status_code} {r.text[:200]}")
        result: dict[str, Any] = r.json()
        return result

    return send
