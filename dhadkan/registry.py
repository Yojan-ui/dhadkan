"""Cluster registry storage (SQLite): fault fingerprints and weak labels.

Only 64-float embeddings, machine type and words are stored. There is no
column that could hold audio.
"""
import json
import sqlite3
import time

import numpy as np

from . import embed
from .config import EMBED_DIM

SCHEMA = """
CREATE TABLE IF NOT EXISTS weak_labels (
  id INTEGER PRIMARY KEY, created REAL, factory TEXT, machine TEXT,
  text TEXT, language TEXT, sound TEXT, fault TEXT, scores TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS fingerprints (
  id INTEGER PRIMARY KEY, created REAL, machine_type TEXT, fault TEXT,
  owner_label TEXT, factory TEXT, machine TEXT, embedding TEXT,
  weak_label_id INTEGER REFERENCES weak_labels(id));
"""


def connect(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def add_weak_label(db, factory, machine, text, language, label):
    cur = db.execute(
        "INSERT INTO weak_labels (created, factory, machine, text, language, sound, fault, scores, source)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (time.time(), factory, machine, text, language, label.get("sound"), label["fault"],
         json.dumps(label.get("scores", {})), label["source"]))
    db.commit()
    return cur.lastrowid


def check_embedding(vec):
    v = np.asarray(vec, dtype=float)
    if v.shape != (EMBED_DIM,) or not np.all(np.isfinite(v)):
        raise ValueError(f"embedding must be {EMBED_DIM} finite floats")
    n = np.linalg.norm(v)
    if n == 0:
        raise ValueError("embedding is all zeros")
    return v / n


def publish(db, machine_type, fault, owner_label, factory, machine, embedding, weak_label_id=None):
    v = check_embedding(embedding)
    cur = db.execute(
        "INSERT INTO fingerprints (created, machine_type, fault, owner_label, factory, machine,"
        " embedding, weak_label_id) VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), machine_type, fault, owner_label, factory, machine,
         json.dumps(v.round(6).tolist()), weak_label_id))
    db.commit()
    return cur.lastrowid


def fingerprints(db, machine_type=None):
    q, args = "SELECT * FROM fingerprints", ()
    if machine_type:
        q, args = q + " WHERE machine_type = ?", (machine_type,)
    rows = [dict(r) for r in db.execute(q + " ORDER BY id", args)]
    for r in rows:
        r["embedding"] = json.loads(r["embedding"])
    return rows


def match(db, machine_type, embedding, exclude_machine=None):
    """Cosine similarity of one reading against every fingerprint of the same type."""
    v = check_embedding(embedding)
    fps = [f for f in fingerprints(db, machine_type) if f["machine"] != exclude_machine]
    if not fps:
        return []
    sims = embed.cosine(v, np.array([f["embedding"] for f in fps]))
    out = [{"fingerprint_id": f["id"], "fault": f["fault"], "owner_label": f["owner_label"],
            "factory": f["factory"], "machine": f["machine"], "cosine": float(s)}
           for f, s in zip(fps, sims)]
    return sorted(out, key=lambda m: -m["cosine"])
