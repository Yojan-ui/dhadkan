"""Print the newest evaluation run as Markdown tables (pasted into the README).

    python -m dhadkan.report
"""
import json

from .config import RUNS


def main():
    path = sorted(RUNS.glob("eval_*.json"))[-1]
    r = json.loads(path.read_text())
    print(f"From `runs/{path.name}` (code sha256 {r['code_sha256'][:12]}).\n")
    print("| type | condition | AUC | pAUC | normal + anomalous | false alarms | faults caught |")
    print("|---|---|---|---|---|---|---|")
    for t, rep in r["types"].items():
        for cond, m in rep["conditions"].items():
            tag = "" if cond == rep["headline_condition"] else " (stress test)"
            print(f"| {t} | {cond}{tag} | {m['auc']:.3f} | {m['pauc']:.3f} | "
                  f"{m['n_normal']} + {m['n_anomalous']} | {m['false_alarm_rate']:.0%} | "
                  f"{m['detection_rate']:.0%} |")
    print("\n| recorded unit | AUC | pAUC |")
    print("|---|---|---|")
    for rep in r["types"].values():
        for u, m in rep["per_unit"].items():
            print(f"| {u} | {m['auc']:.3f} | {m['pauc']:.3f} |")
    print("\nRegistry, synthetic onset 0.25 (alarm / registry), registry false flags on normal clips:\n")
    print("| case | from | to | alarm | registry | false flags |")
    print("|---|---|---|---|---|---|")
    for rep in r["types"].values():
        for x in rep["registry_transfer"]:
            m = x["by_mix"]["0.25"]
            print(f"| {x['case']} | {x['from']} | {x['to']} | {m['alarm']:.0%} | "
                  f"{m['registry']:.0%} | {x['registry_false_flag_rate']:.0%} |")


if __name__ == "__main__":
    main()
