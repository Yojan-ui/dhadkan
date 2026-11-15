"""Simulated cluster: 3 factories, 12 machines, each replaying real recordings.

Every tick, each machine plays its next held-out clip (10 s of a real MIMII /
MIMII DUE recording), the phone model scores it, and the reading is matched
against the shared registry. Readings live in node.db.

Fault modes per machine:
  normal  held-out normal clips of its source unit
  fault   held-out abnormal clips of its source unit
  onset   SYNTHETIC: abnormal mixed into normal, strength rising each tick
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from edge import data, embed, features
from edge.runtime import EdgeModel

from . import registry
from .config import CLUSTER, NODE_DB, REGISTRY_DB

ONSET_STEP = 0.1  # onset strength added per tick (3 ticks a day -> ~3 days to full)

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  tick INTEGER, ts TEXT, machine TEXT, clip TEXT, mode TEXT, mix REAL,
  score REAL, threshold REAL, risk REAL, match_cos REAL, match_level REAL,
  match_fp INTEGER, embedding TEXT);
CREATE TABLE IF NOT EXISTS state (machine TEXT PRIMARY KEY, mode TEXT, mix REAL);
CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, actor TEXT, tool TEXT, input TEXT, output TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, factory TEXT, language TEXT, body TEXT,
  ask TEXT, reply TEXT, approved INTEGER);
"""


def load_cluster(path: Path = CLUSTER) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class Cluster:
    def __init__(
        self,
        node_db: str | Path = NODE_DB,
        registry_db: str | Path = REGISTRY_DB,
        cluster: dict[str, Any] | None = None,
    ) -> None:
        self.cfg = cluster or load_cluster()
        self.machines = {m["id"]: m for m in self.cfg["machines"]}
        self.factories = {f["id"]: f for f in self.cfg["factories"]}
        self.db = sqlite3.connect(node_db, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.reg = registry.connect(registry_db)
        self.models = {}
        self._scorers = {}  # one interpreter per machine, so a reading's 12 clips score in parallel
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._pools = None

    # -- clips ------------------------------------------------------------
    def pools(self) -> dict[str, dict[int, list[Path]]]:
        """Held-out test clips per machine. Machines sharing a source split its clips."""
        if self._pools is None:
            by_source = {}
            for t in ("pump", "fan", "gearbox"):
                for c in data.clips_for(t):
                    if c.split == "test" and c.condition in ("6dB", "source"):
                        by_source.setdefault(c.unit, {0: [], 1: []})[c.label].append(c.path)
            sharing = {}
            for m in self.cfg["machines"]:
                sharing.setdefault(m["source"], []).append(m["id"])
            self._pools = {}
            for m in self.cfg["machines"]:
                k, i = len(sharing[m["source"]]), sharing[m["source"]].index(m["id"])
                src = by_source[m["source"]]
                self._pools[m["id"]] = {lab: src[lab][i::k] or src[lab] for lab in (0, 1)}
        return self._pools

    def model(self, mtype: str) -> EdgeModel:
        if mtype not in self.models:
            self.models[mtype] = EdgeModel(mtype)
        return self.models[mtype]

    # -- state ------------------------------------------------------------
    def tick_no(self) -> int:
        r = self.db.execute("SELECT MAX(tick) FROM readings").fetchone()[0]
        return -1 if r is None else r

    def now(self, tick: int | None = None) -> datetime:
        tick = self.tick_no() if tick is None else tick
        start = datetime.fromisoformat(self.cfg["start"])
        return start + timedelta(hours=self.cfg["tick_hours"] * max(tick, 0))

    def mode(self, mid: str) -> tuple[str, float]:
        r = self.db.execute("SELECT mode, mix FROM state WHERE machine=?", (mid,)).fetchone()
        return (r["mode"], r["mix"]) if r else ("normal", 0.0)

    def set_mode(self, mid: str, mode: str, mix: float = 0.0) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)", (mid, mode, mix))
        self.db.commit()

    def reset(self) -> None:
        self.db.executescript(
            "DELETE FROM readings; DELETE FROM state; DELETE FROM tool_calls;" " DELETE FROM messages;"
        )
        self.reg.executescript("DELETE FROM fingerprints; DELETE FROM weak_labels;")
        self.db.commit()
        self.reg.commit()

    # -- one tick ---------------------------------------------------------
    def audio(self, mid: str, tick: int, mode: str, mix: float) -> tuple[np.ndarray, str]:
        """Rebuild the exact 10 s the machine played at `tick` (also used for spectrograms)."""
        pool = self.pools()[mid]
        normal = pool[0][tick % len(pool[0])]
        if mode == "normal":
            return features.load(normal), normal.name
        abnormal = pool[1][tick % len(pool[1])]
        if mode == "fault":
            return features.load(abnormal), abnormal.name
        audio = features.onset_mix(features.load(normal), features.load(abnormal), mix)
        return audio, f"{normal.name} + {mix:.1f} x {abnormal.name}"

    def next_mode(self, mid: str) -> tuple[str, float]:
        mode, mix = self.mode(mid)
        if mode == "onset":
            mix = min(1.0, mix + ONSET_STEP)
            self.set_mode(mid, "onset", mix)
        return mode, (1.0 if mode == "fault" else mix)

    def scorer(self, mid: str) -> EdgeModel:
        if mid not in self._scorers:
            self._scorers[mid] = EdgeModel(self.machines[mid]["type"])
        return self._scorers[mid]

    def step(self) -> dict[str, tuple[np.ndarray, dict[str, Any]]]:
        """Play and score one reading per machine. Returns {machine: (audio, score_audio result)}."""
        tick = self.tick_no() + 1
        ts = self.now(tick).isoformat(timespec="minutes")
        modes = {mid: self.next_mode(mid) for mid in self.machines}

        def play(mid: str) -> tuple[str, str, np.ndarray, dict[str, Any]]:
            audio, clip = self.audio(mid, tick, *modes[mid])
            return mid, clip, audio, self.scorer(mid).score_audio(audio, self.machines[mid]["source"])

        out = {}
        for mid, clip, audio, r in self._pool.map(play, self.machines):
            m = self.machines[mid]
            mode, mix = modes[mid]
            em = self.model(m["type"])
            out[mid] = (audio, r)
            cos, level, fp_id = None, None, None
            matches = registry.match(self.reg, m["type"], r["embedding"], exclude_machine=mid)
            if matches:
                best = matches[0]
                fp = next(
                    f for f in registry.fingerprints(self.reg, m["type"]) if f["id"] == best["fingerprint_id"]
                )
                cos, fp_id = best["cosine"], best["fingerprint_id"]
                level = em.match_level(m["source"], fp["embedding"])
            self.db.execute(
                "INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tick,
                    ts,
                    mid,
                    clip,
                    mode,
                    mix,
                    r["score"],
                    r["threshold"],
                    r["risk"],
                    cos,
                    level,
                    fp_id,
                    json.dumps(r["embedding"]),
                ),
            )
        self.db.commit()
        return out

    # -- queries ----------------------------------------------------------
    def history(self, mid: str, last: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM readings WHERE machine=? ORDER BY tick"
        rows = [dict(r) for r in self.db.execute(q, (mid,))]
        return rows[-last:] if last else rows

    def confirmed_embedding(self, mid: str, last: int = 4) -> list[float]:
        """Fingerprint from a machine's last few readings (the ones the owner confirmed)."""
        embs = [json.loads(r["embedding"]) for r in self.history(mid, last)]
        return embed.fingerprint(np.array(embs)).tolist()
