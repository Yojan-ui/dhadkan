"""Measure a live trial: normal running, then a fault you create (a coin taped to a fan blade), on a real
machine fed by the live recorder.

    python -m scripts.live_trial A-F1 [--note "table fan, coin on one blade"]     (.\\dev.ps1 live-trial A-F1)

What it reads (written by the server while you recorded, server/live_feed.py):
  * the machine's live readings (data/brain_edge.db): one per 10 s clip, with its risk against the
    machine's own learned normal (1.0 = its alarm level);
  * the marks you tapped in the recorder (data/brain_maint.db): "fault added" (fault_on), "fault removed".

What it computes, and writes to docs/runs/live_<UTC>.json:
  * normal period (learned normal -> fault_on): readings, risk median / 95th percentile / max, readings at
    or above the alarm level, and false alarms = times the health rule (server/health.assess, the same
    one the brain uses) reached "inspect" or above, which is when the planner gets an alarm event;
  * fault period (fault_on -> fault_off or the end): the same numbers, the score jump (median fault risk minus
    median normal risk, in multiples of the alarm level), and the detection delay in seconds from the mark to the end of the first clip at
    or above the alarm level, and to the end of the clip that raised the alarm.
Nothing is written without live readings and a fault_on mark. The recipe: docs/DEMO_LIVE.md.
"""

import argparse
import json
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from edge.baseline import BaselineStore
from edge.config import ROOT, RUNS
from server import health
from server.maint.agent import RANK

CLIP_S = 10.0


def _stats(xs: list[float]) -> dict[str, Any]:
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    return {"n": len(xs), "median": round(statistics.median(s), 3), "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 3),
            "max": round(s[-1], 3), "at_or_above_alarm": sum(x >= 1.0 for x in s)}  # fmt: skip


def alarm_flags(hist: list[dict[str, Any]], machine: str, baseline_rows: int | None) -> list[bool]:
    """Per reading: would the brain's health rule have put this machine at 'inspect' or above here?"""
    out = []
    for i in range(len(hist)):
        h = health.assess(
            machine, hist[: i + 1], now=datetime.fromtimestamp(hist[i]["ts"]), baseline_rows=baseline_rows
        )
        out.append(RANK.get(h.urgency, 0) >= RANK["inspect"])
    return out


def compute(hist: list[dict[str, Any]], marks: list[dict[str, Any]], machine: str = "m",
            baseline_rows: int | None = None) -> dict[str, Any]:  # fmt: skip
    """hist: the machine's live readings, oldest first (ts, risk, running, excluded...); marks: [{label, ts}]."""
    on = next((m["ts"] for m in marks if m["label"] == "fault_on"), None)
    if on is None:
        raise ValueError("no 'fault added' mark: tap it in the recorder when the fault goes on")
    off = next((m["ts"] for m in marks if m["label"] == "fault_off" and m["ts"] > on), None)
    scored = [h for h in hist if h["risk"] is not None]
    if not scored:
        raise ValueError("no scored live readings: learn the normal first, then monitor")
    flags = dict(zip([h["id"] for h in scored], alarm_flags(scored, machine, baseline_rows), strict=True))
    normal = [h for h in scored if h["ts"] < on]
    fault = [h for h in scored if h["ts"] >= on and (off is None or h["ts"] < off)]
    # an alarm episode starts when the rule goes from off to on
    normal_alarms, prev = 0, False
    for h in normal:
        normal_alarms += flags[h["id"]] and not prev
        prev = flags[h["id"]]
    first_over = next((h for h in fault if h["risk"] >= 1.0), None)
    first_alarm = next((h for h in fault if flags[h["id"]]), None)
    nm = statistics.median([h["risk"] for h in normal]) if normal else None
    fm = statistics.median([h["risk"] for h in fault]) if fault else None
    return {
        "normal": {**_stats([h["risk"] for h in normal]), "seconds": round(normal[-1]["ts"] - normal[0]["ts"] + CLIP_S) if normal else 0,
                   "false_alarms": int(normal_alarms)},
        "fault": {**_stats([h["risk"] for h in fault]), "seconds": round(fault[-1]["ts"] - fault[0]["ts"] + CLIP_S) if fault else 0},
        # risk is in units of the alarm level and can be negative (quieter than the median normal), so the
        # jump is a difference, not a ratio
        "score_jump": {"normal_median": round(nm, 3) if nm is not None else None,
                       "fault_median": round(fm, 3) if fm is not None else None,
                       "difference": round(fm - nm, 2) if nm is not None and fm is not None else None,
                       "unit": "x the machine's own alarm level"},
        "detection_delay_s": {
            "first_clip_over_alarm": round(first_over["ts"] + CLIP_S - on, 1) if first_over else None,
            "alarm_raised": round(first_alarm["ts"] + CLIP_S - on, 1) if first_alarm else None,
            "note": "from the 'fault added' mark to the end of the clip (upload and processing add a few seconds)",
        },
        "detected": first_alarm is not None,
        "readings": [{"t": round(h["ts"] - on, 1), "risk": round(h["risk"], 3), "alarm": flags[h["id"]]} for h in scored],
    }  # fmt: skip


def load(machine: str, data: Path) -> tuple[list[dict[str, Any]], dict[str, Any], int | None]:
    base = BaselineStore(data / "brain_edge.db")
    hist = [h for h in base.history(machine) if h["source"] == "live"]
    db = sqlite3.connect(data / "brain_maint.db")
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT * FROM live_machines WHERE machine=?", (machine,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        raise ValueError(f"{machine} has never been fed by the live recorder on this server")
    v = base.versions(machine)
    return hist, dict(row), (v[-1]["n_rows"] if v else None)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m scripts.live_trial")
    ap.add_argument("machine", nargs="?", default="A-F1")
    ap.add_argument("--data", default=str(ROOT / "data"), help="the server's data folder")
    ap.add_argument(
        "--note", default="", help="what the machine and the fault were, e.g. 'table fan, coin on one blade'"
    )
    a = ap.parse_args()
    try:
        hist, row, n_rows = load(a.machine, Path(a.data))
        res = compute(hist, json.loads(row["marks"] or "[]"), a.machine, n_rows)
    except ValueError as e:
        print(f"nothing written: {e}")
        return 1
    rec = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "machine": a.machine, "note": a.note,
           "source": "LIVE: a real machine recorded with the live recorder (not simulated)",
           "device": row["device"], "learning": {"quick": bool(row["quick"]), "baseline_readings": n_rows,
           "label": "short learning period, demo" if row["quick"] else None},
           "rule": "alarm = server/health.assess at 'inspect' or above (2 of the last 3 readings at or above the alarm level, "
                   "median at least 2x); the same rule that sends the planner an alarm event", **res}  # fmt: skip
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"live_{rec['run_id']}.json"
    out.write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    n, f, d = res["normal"], res["fault"], res["detection_delay_s"]
    print(f"normal: {n['n']} readings over {n['seconds']} s, median risk {n.get('median')}, max {n.get('max')}, "
          f"false alarms {n['false_alarms']}")  # fmt: skip
    print(
        f"fault:  {f['n']} readings, median risk {f.get('median')}, max {f.get('max')}, "
        f"score jump +{res['score_jump']['difference']} x its alarm level"
    )
    print(
        f"detection: first clip over the alarm level after {d['first_clip_over_alarm']} s, alarm after {d['alarm_raised']} s"
    )
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
