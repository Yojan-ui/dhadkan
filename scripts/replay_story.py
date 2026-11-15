"""Run the dashboard's demo script end to end and log what happened.

    python scripts/replay_story.py   ->  runs/story_<UTC>.json

Uses throwaway databases, so the dashboard's state is untouched. The README's
"B-F1 was flagged N readings before its alarm" sentence is read from this log.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dhadkan import agent, folk, registry  # noqa: E402
from dhadkan.config import RUNS  # noqa: E402
from dhadkan.sim import Cluster  # noqa: E402

NOTE = "ब्लोअर में खट-खट की आवाज़ आ रही है"   # typed version of the sample voice note


def main():
    d = Path(tempfile.mkdtemp())
    c = Cluster(d / "node.db", d / "registry.db")
    log = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "steps": []}
    rank = lambda: {r["machine_id"]: r for r in agent.Tools(c, "story", "story").get_risk_ranking()}

    def reading(tag):
        c.step()
        r = rank()
        for mid in ("A-F1", "B-F1"):
            h = c.history(mid)[-1]
            log["steps"].append({"tag": tag, "ts": h["ts"], "machine": mid, "mode": h["mode"],
                                 "mix": h["mix"], "risk": h["risk"], "risk3": r[mid]["risk"],
                                 "match_cos": h["match_cos"], "match_level": h["match_level"],
                                 "status": r[mid]["status"]})

    for _ in range(3):
        reading("baseline")
    c.set_mode("A-F1", "fault")
    for _ in range(3):
        reading("A-F1 fault")
    label = folk.label_text(NOTE)
    wid = registry.add_weak_label(c.reg, "A", "A-F1", NOTE, "hi", label)
    registry.publish(c.reg, "fan", label["fault"], label["sound"], "A", "A-F1",
                     c.confirmed_embedding("A-F1"), wid)
    log["label"] = label
    log["digest_before"] = agent.run_rules(c)
    c.set_mode("B-F1", "onset", 0.0)
    for _ in range(9):
        reading("B-F1 onset")
    log["digest_after"] = agent.run_rules(c)
    log["messages"] = [dict(r) for r in c.db.execute("SELECT factory, body FROM messages")]

    b = [s for s in log["steps"] if s["machine"] == "B-F1" and s["tag"] == "B-F1 onset"]
    first = lambda st: next((i for i, s in enumerate(b) if s["status"] == st), None)
    early, alarm = first("early_match"), first("alarm")
    log["summary"] = {"b_first_early_match_reading": early, "b_first_alarm_reading": alarm,
                      "b_mix_at_early_match": None if early is None else round(b[early]["mix"], 2),
                      "readings_of_warning": None if None in (early, alarm) else alarm - early}
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"story_{log['run_id']}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out)
    print(json.dumps(log["summary"]))
    for s in b:
        print(s["ts"], f'mix {s["mix"]:.1f} risk3 {s["risk3"]:.2f} cos {s["match_cos"]:.2f}/{s["match_level"]:.2f}', s["status"])


if __name__ == "__main__":
    main()
