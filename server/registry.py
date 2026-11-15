"""Cluster registry storage (SQLite): fault fingerprints and weak labels.

Only 64-float embeddings, machine type and words are stored. There is no
column that could hold audio.
"""

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import ArrayLike

from edge import embed
from edge.config import EMBED_DIM

SCHEMA = """
CREATE TABLE IF NOT EXISTS weak_labels (
  id INTEGER PRIMARY KEY, created REAL, factory TEXT, machine TEXT,
  text TEXT, language TEXT, sound TEXT, fault TEXT, scores TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS fingerprints (
  id INTEGER PRIMARY KEY, created REAL, machine_type TEXT, fault TEXT,
  owner_label TEXT, factory TEXT, machine TEXT, embedding TEXT,
  weak_label_id INTEGER REFERENCES weak_labels(id));
"""
# Idempotency key from the phone's outbox: a retried delivery is stored once.
MIGRATIONS = ("ALTER TABLE fingerprints ADD COLUMN idem TEXT",)
IDEM_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS fingerprints_idem ON fingerprints(idem)"


def connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    for sql in MIGRATIONS:
        with contextlib.suppress(sqlite3.OperationalError):  # column already there
            db.execute(sql)
    db.execute(IDEM_INDEX)
    db.commit()
    return db


def add_weak_label(
    db: sqlite3.Connection, factory: str, machine: str, text: str, language: str | None, label: dict[str, Any]
) -> int:
    cur = db.execute(
        "INSERT INTO weak_labels (created, factory, machine, text, language, sound, fault, scores, source)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            time.time(),
            factory,
            machine,
            text,
            language,
            label.get("sound"),
            label["fault"],
            json.dumps(label.get("scores", {})),
            label["source"],
        ),
    )
    db.commit()
    return cur.lastrowid


def check_embedding(vec: ArrayLike) -> np.ndarray:
    v = np.asarray(vec, dtype=float)
    if v.shape != (EMBED_DIM,) or not np.all(np.isfinite(v)):
        raise ValueError(f"embedding must be {EMBED_DIM} finite floats")
    n = np.linalg.norm(v)
    if n == 0:
        raise ValueError("embedding is all zeros")
    return v / n


def publish(
    db: sqlite3.Connection,
    machine_type: str,
    fault: str,
    owner_label: str,
    factory: str,
    machine: str,
    embedding: ArrayLike,
    weak_label_id: int | None = None,
    idem: str | None = None,
) -> int:
    """Store a fingerprint. With `idem`, a repeat of the same delivery returns the first id."""
    v = check_embedding(embedding)
    if idem is not None:
        row = db.execute("SELECT id FROM fingerprints WHERE idem=?", (idem,)).fetchone()
        if row:
            return int(row["id"])
    cur = db.execute(
        "INSERT INTO fingerprints (created, machine_type, fault, owner_label, factory, machine,"
        " embedding, weak_label_id, idem) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            time.time(),
            machine_type,
            fault,
            owner_label,
            factory,
            machine,
            json.dumps(v.round(6).tolist()),
            weak_label_id,
            idem,
        ),
    )
    db.commit()
    return cur.lastrowid


def fingerprints(db: sqlite3.Connection, machine_type: str | None = None) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM fingerprints", ()
    if machine_type:
        q, args = q + " WHERE machine_type = ?", (machine_type,)
    rows = [dict(r) for r in db.execute(q + " ORDER BY id", args)]
    for r in rows:
        r["embedding"] = json.loads(r["embedding"])
    return rows


def match(
    db: sqlite3.Connection, machine_type: str, embedding: ArrayLike, exclude_machine: str | None = None
) -> list[dict[str, Any]]:
    """Cosine similarity of one reading against every fingerprint of the same type."""
    v = check_embedding(embedding)
    fps = [f for f in fingerprints(db, machine_type) if f["machine"] != exclude_machine]
    if not fps:
        return []
    sims = embed.cosine(v, np.array([f["embedding"] for f in fps]))
    out = [
        {
            "fingerprint_id": f["id"],
            "fault": f["fault"],
            "owner_label": f["owner_label"],
            "factory": f["factory"],
            "machine": f["machine"],
            "cosine": float(s),
        }
        for f, s in zip(fps, sims, strict=False)
    ]
    return sorted(out, key=lambda m: -m["cosine"])
