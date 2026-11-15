"""Run the Demo replay script without waiting, carry on until B-F1 alarms, and log what happened.

    python -m server.story   ->  docs/runs/story_<UTC>.json

Exactly the dashboard's replay (server/app.py, run_replay) on throwaway databases,
including local Whisper on the sample voice note. The README's numbers about the
replay (B-F1's warning lead, the days-to-fault-level check on A-F1) are read from
this log.
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from edge.config import RUNS

from . import app

MAX_EXTRA_READINGS = 12


def main() -> None:
    d = Path(tempfile.mkdtemp())
    lv = app.Live(d / "node.db", d / "registry.db")
    log: dict[str, Any] = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}
    asyncio.run(lv.run_replay(tick_s=0))
    log["replay_phase"] = lv.replay["phase"]
    log["label"] = next((e for e in lv.events if e["kind"] == "label"), None)
    log["messages"] = [{"factory": m["factory"], "body": m["body"]} for m in lv.messages()]

    # B-F1 after the replay stops: keep its onset going until its own alarm
    for _ in range(MAX_EXTRA_READINGS):
        if lv.history(app.SIMILAR_MACHINE)["rows"][-1]["status"] == "alarm":
            break
        lv.step()
    b = [r for r in lv.history(app.SIMILAR_MACHINE)["rows"] if r["mode"] == "onset"]
    early = next((i for i, r in enumerate(b) if r["status"] == "early_match"), None)
    alarm = next((i for i, r in enumerate(b) if r["status"] == "alarm"), None)
    log["b_f1"] = {
        "readings": [{k: r[k] for k in ("ts", "mix", "risk3", "status")} for r in b],
        "first_early_match_reading": early,
        "first_alarm_reading": alarm,
        "mix_at_early_match": None if early is None else b[early]["mix"],
        "readings_of_warning": None if None in (early, alarm) else alarm - early,
    }

    # A-F1: the days-to-fault-level estimate at each reading vs when the fault sound actually began
    h = lv.history(app.FAULT_MACHINE)
    end = h["fault_sound"]["tick"]
    rows = []
    for r in h["rows"]:
        f = r["forecast"]
        if r["tick"] > end or f["status"] != "estimate":
            continue
        actual = round((end - r["tick"]) * h["tick_hours"] / 24, 2)
        rows.append(
            {
                "ts": r["ts"],
                "status": r["status"],
                "low": f["low"],
                "mid": f["mid"],
                "high": f["high"],
                "actual_days": actual,
                "inside": f["low"] <= actual <= (f["high"] or 1e9),
            }
        )
    log["a_f1"] = {
        "first_warning": h["first_warning"],
        "fault_sound": h["fault_sound"],
        "days_warned_before_fault_sound": round((end - h["first_warning"]["tick"]) * h["tick_hours"] / 24, 2),
        "estimates": rows,
        "inside_range": sum(r["inside"] for r in rows),
        "estimates_made": len(rows),
    }
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"story_{log['run_id']}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out)
    print(
        json.dumps(
            {
                "b_f1": {k: v for k, v in log["b_f1"].items() if k != "readings"},
                "a_f1": {k: v for k, v in log["a_f1"].items() if k != "estimates"},
            }
        )
    )


if __name__ == "__main__":
    main()
