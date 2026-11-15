"""Run the demo replay's script without waiting, keep going until B-F1 alarms, log what happened.

    python -m server.story   ->  docs/runs/story_<UTC>.json

Same steps and timings (in readings) as the dashboard's Demo replay (server/app.py),
on throwaway databases. The README's "B-F1 was flagged N readings before its own
alarm" sentence is read from this log.
"""

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from edge.config import RUNS

from . import app
from .agent import Tools

NOTE = "ब्लोअर में खट-खट की आवाज़ आ रही है"  # typed version of the sample voice note
MAX_ONSET_READINGS = 12


def main() -> None:
    d = Path(tempfile.mkdtemp())
    lv = app.Live(d / "node.db", d / "registry.db")
    log: dict[str, Any] = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "steps": []}

    def reading(tag: str) -> dict[str, dict[str, Any]]:
        lv.step()
        rank = {r["machine_id"]: r for r in Tools(lv.c, "story", "story").get_risk_ranking()}
        for mid in (app.FAULT_MACHINE, app.SIMILAR_MACHINE):
            h = lv.c.history(mid, 1)[0]
            log["steps"].append(
                {"tag": tag, "ts": h["ts"], "machine": mid, "mode": h["mode"], "mix": h["mix"],
                 "risk": h["risk"], "risk3": rank[mid]["risk"], "match_cos": h["match_cos"],
                 "match_level": h["match_level"], "status": rank[mid]["status"]}
            )  # fmt: skip
        return rank

    for _ in range(app.REPLAY_FAULT_AT):
        reading("baseline")
    lv.set_mode(app.FAULT_MACHINE, "fault")
    for _ in range(app.REPLAY_PUBLISH_AT - app.REPLAY_FAULT_AT):
        reading("A-F1 fault")
    log["label"] = lv.label(NOTE)
    log["published"] = lv.publish(app.REPLAY_PUBLISH_AT - app.REPLAY_FAULT_AT)["published"]
    lv.set_mode(app.SIMILAR_MACHINE, "onset")
    early = alarm = None
    for i in range(MAX_ONSET_READINGS):
        status = reading("B-F1 onset")[app.SIMILAR_MACHINE]["status"]
        if status == "early_match" and early is None:
            early = i
            log["digest"] = lv.run_agent("rules")
        if status == "alarm":
            alarm = i
            break
    b = [s for s in log["steps"] if s["machine"] == app.SIMILAR_MACHINE and s["tag"] == "B-F1 onset"]
    log["messages"] = [{"factory": m["factory"], "body": m["body"]} for m in lv.messages()]
    log["summary"] = {
        "b_first_early_match_reading": early,
        "b_first_alarm_reading": alarm,
        "b_mix_at_early_match": None if early is None else round(b[early]["mix"], 2),
        "readings_of_warning": None if None in (early, alarm) else alarm - early,
    }
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"story_{log['run_id']}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out)
    print(json.dumps(log["summary"]))


if __name__ == "__main__":
    main()
