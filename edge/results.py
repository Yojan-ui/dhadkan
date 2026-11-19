"""Write docs/RESULTS.md from the newest evaluation records. Nothing in it is typed by hand.

    python -m edge.results

Reads docs/runs/eval_*.json (anomaly model), faulttype_*.json (CWRU classifier) and ims_*.json
(run-to-failure). The DCASE 2020 baseline numbers are copied from the challenge page (cited below).
"""

import json
from pathlib import Path
from typing import Any

from . import results_more
from .config import ROOT, RUNS

# DCASE 2020 Task 2 baseline on the development test split (AUC %, pAUC % with p = 0.1), from
# https://dcase.community/challenge2020/task-unsupervised-detection-of-anomalous-sounds (accessed 2026-11-16).
BASELINE = {
    "fan": {
        "id_00": (54.41, 49.37),
        "id_02": (73.40, 54.81),
        "id_04": (61.61, 53.26),
        "id_06": (73.92, 52.35),
    },
    "pump": {
        "id_00": (67.15, 56.74),
        "id_02": (61.53, 58.10),
        "id_04": (88.33, 67.10),
        "id_06": (74.55, 58.02),
    },
    "slider": {
        "id_00": (96.19, 81.44),
        "id_02": (78.97, 63.68),
        "id_04": (94.30, 71.98),
        "id_06": (69.59, 49.02),
    },
    "valve": {
        "id_00": (68.76, 51.70),
        "id_02": (68.18, 51.83),
        "id_04": (74.30, 51.97),
        "id_06": (53.90, 48.43),
    },
}
BASELINE_AVG = {
    "fan": (65.83, 52.45),
    "pump": (72.89, 59.99),
    "slider": (84.76, 66.53),
    "valve": (66.28, 50.98),
}


def newest(prefix: str) -> tuple[dict[str, Any] | None, Path | None]:
    runs = sorted(RUNS.glob(f"{prefix}_*.json"))
    return (json.loads(runs[-1].read_text(encoding="utf-8")), runs[-1]) if runs else (None, None)


def pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:.1f}"


def anomaly_section(ev: dict[str, Any], path: Path) -> list[str]:
    out = [
        "## 1. Anomaly detection (normal-only training)",
        "",
        f"Record: `docs/runs/{path.name}` (code sha256 {ev['code_sha256'][:12]}). One conv autoencoder per",
        "machine type, trained on normal clips only; score = mean reconstruction error of 1.06 s log-mel",
        "windows; AUC and pAUC (FPR <= 0.1) per machine ID on the **official DCASE 2020 Task 2",
        "development test split** (every test clip). Training used the first "
        f"{ev.get('train_per_id', 'N')} normal clips per ID of the official train split, minus 10 held out for the alarm level;",
        "the DCASE baseline trained on all of them. Numbers are AUC % / pAUC %. pAUC is computed exactly as",
        "the baseline code does (`sklearn.metrics.roc_auc_score(..., max_fpr=0.1)`, McClish-standardised:",
        "50 = chance), so the columns compare like with like; the raw partial area is in the record as `pauc_raw`.",
        "",
        "| machine | ID | Dhadkan AUC | Dhadkan pAUC | baseline AUC | baseline pAUC |",
        "|---|---|---|---|---|---|",
    ]
    for mtype in ("fan", "pump", "slider", "valve"):
        rep = ev["types"].get(mtype)
        if not rep:
            continue
        aucs, paucs = [], []
        for unit, m in sorted(rep["per_unit"].items()):
            if not m:
                continue
            uid = unit.split("/")[1]
            b = BASELINE[mtype].get(uid, (None, None))
            aucs.append(m["auc"])
            paucs.append(m["pauc"])
            out.append(
                f"| {mtype} | {uid} | {pct(m['auc'])} | {pct(m['pauc'])} | {b[0] or '–'} | {b[1] or '–'} |"
            )
        if aucs:
            out.append(f"| **{mtype}** | **average** | **{pct(sum(aucs) / len(aucs))}** | **{pct(sum(paucs) / len(paucs))}** "
                       f"| **{BASELINE_AVG[mtype][0]}** | **{BASELINE_AVG[mtype][1]}** |")  # fmt: skip
    gb = ev["types"].get("gearbox")
    if gb:
        out += ["", "Gearbox (MIMII DUE = DCASE 2021 Task 2 development set, source domain and the changed-speed/load",
                "target domain):", "", "| condition | AUC | pAUC | false alarms | faults caught |", "|---|---|---|---|---|"]  # fmt: skip
        for cond, m in gb["conditions"].items():
            if m:
                out.append(
                    f"| {cond} | {pct(m['auc'])} | {pct(m['pauc'])} | {pct(m.get('false_alarm_rate'))} | {pct(m.get('detection_rate'))} |"
                )
    out += ["", "Alarm level per machine = 95th percentile of its 10 held-out normal training clips, so about 1 in",
            "20 healthy readings crosses it by design.", ""]  # fmt: skip
    return out


def classifier_section(ft: dict[str, Any], path: Path) -> list[str]:
    ev = ft["eval"]
    out = [
        "## 2. Fault type (CWRU-trained classifier)",
        "",
        f"Record: `docs/runs/{path.name}`. Labels from CWRU's own file listing; {ft['recordings']} recordings,",
        f"windows per class {ft['windows']}. Features: {ft['features']}. Random forest.",
        "",
        "| test | accuracy | normal | inner race | outer race | ball |",
        "|---|---|---|---|---|---|",
    ]
    for k, v in ev["leave_one_load_out"].items():
        r = v["per_class_recall"]
        out.append(
            f"| {k.replace('_', ' ')} | {pct(v['accuracy'])} | {pct(r['normal'])} | {pct(r['inner_race'])} | {pct(r['outer_race'])} | {pct(r['ball'])} |"
        )
    u = ev["unseen_fault_size_0p028in"]
    r = u["per_class_recall"]
    out.append(
        f"| unseen fault size 0.028 in | {pct(u['accuracy'])} | – | {pct(r['inner_race'])} | – | {pct(r['ball'])} |"
    )
    out += ["", f"Mean over held-out loads: **{pct(ev['mean_accuracy_leave_one_load_out'])}%**. The CWRU normal baseline is",
            "recorded at 48 kHz (its shaft-speed peak only matches the logged rpm at 48 kHz) and is resampled to 12 kHz;",
            "mixing rates unnoticed would hand the classifier a shortcut.", ""]  # fmt: skip
    return out


def ims_section(rec: dict[str, Any], path: Path) -> list[str]:
    out = [
        "## 3. Run-to-failure (NASA IMS bearings)",
        "",
        f"Record: `docs/runs/{path.name}`. Health score per bearing from its own first 10% of snapshots",
        "(1.0 = the 95th percentile of that healthy start). Days-to-failure = the dashboard's trend estimator",
        "on log10(score). The failure level, trend window and range calibration all come from the OTHER",
        "failed bearings (leave one out). **Raw** = the estimator's own 10-90% bootstrap range; **calibrated**",
        "= mid x the 10th-90th percentile of real/estimated ratios on the other bearings (the range the",
        "dashboard shows). A well-calibrated 10-90% range holds the real remaining time ~80% of the time.",
        "",
        "| bearing | fault (IMS readme) | days run | first alarm (day) | alarm before end | estimates | raw range held truth | calibrated range held truth | calibrated range width | classifier on last 5% |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k, b in rec["bearings"].items():
        if not b["failed"]:
            continue
        d = b["days_to_failure"]
        n = d.get("estimates", 0)
        band = b.get("calibration_log10_ratio_band")
        width = f"x{10 ** band[0]:.2f} to x{10 ** band[1]:.2f} of mid" if band else "–"
        fa = "–" if b["first_alarm_day"] is None else round(b["first_alarm_day"], 2)
        warned = "no alarm" if b["warned_days_before_end"] is None else f"{b['warned_days_before_end']} d"
        raw = f"{d['inside_range']}/{n} ({pct(d['coverage'])}%)" if n else "–"
        cal = (
            f"{d['calibrated_inside']}/{n} ({pct(d['calibrated_coverage'])}%)"
            if n and "calibrated_inside" in d
            else "–"
        )
        cls = b["classifier_last_5pct"]["class"].replace("_", " ")
        ok = "✓" if b["classifier_last_5pct"]["class"] == b["true_fault"] else "✗"
        out.append(f"| {k} | {b['true_fault'].replace('_', ' ')} | {b['days']} | {fa} | {warned} | {n} | {raw} | {cal} | {width} | {cls} {ok} |")  # fmt: skip
    healthy = {k: b for k, b in rec["bearings"].items() if not b["failed"]}
    if healthy:
        out += ["", "Bearings that did NOT fail (test 2, same shaft as the failing bearing 1): share of readings at",
                "or above the alarm after the baseline, and when the alarm first held for an hour:", ""]  # fmt: skip
        out += [f"- {k}: {pct(b['alarm_rate_after_baseline'])}% of readings (first alarm day {'–' if b['first_alarm_day'] is None else round(b['first_alarm_day'], 2)})"
                for k, b in healthy.items()]  # fmt: skip
    out.append("")
    return out


def summary(ev: dict[str, Any] | None, ft: dict[str, Any] | None, ims: dict[str, Any] | None) -> list[str]:
    """The short table that goes between the results markers in README.md and docs/PITCH.md."""
    rows = ["| what | measured | compared with |", "|---|---|---|"]
    for mtype in ("fan", "pump", "slider", "valve"):
        rep = (ev or {}).get("types", {}).get(mtype)
        units = [m for m in (rep or {}).get("per_unit", {}).values() if m]
        if units:
            auc = sum(m["auc"] for m in units) / len(units)
            pauc = sum(m["pauc"] for m in units) / len(units)
            b = BASELINE_AVG[mtype]
            rows.append(f"| {mtype}: fault vs normal sound (DCASE 2020 official test split) "
                        f"| AUC {pct(auc)}% · pAUC {pct(pauc)}% | DCASE baseline {b[0]}% · {b[1]}% |")  # fmt: skip
    ls, _ = newest("listen")
    if ls and ev:
        pairs = [(ev["types"][t]["per_unit"].get(u), m) for t, us in ls["rms_baseline"].items() if t in ev["types"]
                 for u, m in us.items()]  # fmt: skip
        pairs = [(a, m) for a, m in pairs if a and m]
        rms_wins = sum(m["auc"] > a["auc"] for a, m in pairs)
        rows.append(f"| loudness (RMS) threshold vs our autoencoder, same test clips | RMS has the higher AUC on {rms_wins} of {len(pairs)} units | failure case |")  # fmt: skip
    gb = ((ev or {}).get("types", {}).get("gearbox") or {}).get("conditions", {})
    if gb.get("source"):
        rows.append(f"| gearbox (MIMII DUE, source domain) | AUC {pct(gb['source']['auc'])}% · pAUC {pct(gb['source']['pauc'])}% | – |")  # fmt: skip
    if ft:
        e = ft["eval"]
        rows.append(f"| fault type, CWRU bearing load never seen in training | accuracy {pct(e['mean_accuracy_leave_one_load_out'])}% (mean of 4 loads) | 4 classes |")  # fmt: skip
        rows.append(f"| fault type, CWRU fault size never seen (0.028 in) | accuracy {pct(e['unseen_fault_size_0p028in']['accuracy'])}% | failure case |")  # fmt: skip
    for k, b in (ims or {}).get("bearings", {}).items():
        if b["failed"]:
            d = b["days_to_failure"]
            warned = (
                "no alarm"
                if b["warned_days_before_end"] is None
                else f"alarm {b['warned_days_before_end']} days before the end"
            )
            cov = (
                f"{d['calibrated_inside']}/{d['estimates']} calibrated day ranges held the real remaining time"
                if d.get("estimates") and "calibrated_inside" in d
                else "no estimate"
            )
            rows.append(f"| NASA IMS {k} ({b['true_fault'].replace('_', ' ')}) | {warned}; {cov} | classifier said {b['classifier_last_5pct']['class'].replace('_', ' ')} |")  # fmt: skip
        else:
            rows.append(f"| NASA IMS {k} (did not fail) | {pct(b['alarm_rate_after_baseline'])}% of readings at alarm | false alarms |")  # fmt: skip
    return rows if len(rows) > 2 else ["*No evaluation records yet.*"]


def fill_markers(path: Path, lines: list[str]) -> None:
    start, end = "<!-- results:start -->", "<!-- results:end -->"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if start not in text or end not in text:
        return
    head, rest = text.split(start, 1)
    tail = rest.split(end, 1)[1]
    note = "*Generated by `python -m edge.results`; details and sources in docs/RESULTS.md.*"
    path.write_text(
        head + start + "\n" + "\n".join([*lines, "", note]) + "\n" + end + tail,
        encoding="utf-8",
        newline="\n",
    )


def listen_section(ls: dict[str, Any], path: Path, ev: dict[str, Any] | None) -> list[str]:
    out = [
        "## 1b. Against a loudness (RMS) threshold, and what it costs to run",
        "",
        f"Record: `docs/runs/{path.name}`. RMS baseline: each clip's log10 RMS against the machine's own normal",
        "clips (median, MAD, two-sided), on the same test clips as section 1. AUC % / pAUC %.",
        "",
        "| machine unit | autoencoder AUC | autoencoder pAUC | RMS AUC | RMS pAUC | better |",
        "|---|---|---|---|---|---|",
    ]
    wins = total = 0
    for mtype, units in ls["rms_baseline"].items():
        for unit, m in sorted(units.items()):
            a = ((ev or {}).get("types", {}).get(mtype, {}).get("per_unit", {}) or {}).get(unit)
            if not m or not a:
                continue
            total += 1
            better = "autoencoder" if a["auc"] > m["auc"] else "RMS"
            wins += better == "RMS"
            out.append(
                f"| {unit} | {pct(a['auc'])} | {pct(a['pauc'])} | {pct(m['auc'])} | {pct(m['pauc'])} | {better} |"
            )
    out += ["", f"**The loudness threshold has the higher AUC on {wins} of {total} units.**", "",
            f"Edge cost of the exported .tflite on {ls['device']['machine']} ({ls['device']['system']}, laptop CPU, not a phone):", "",
            "| type | size | ms / 1.06 s window (1 thread) | ms / 10 s reading (log-mel + model, 4 threads) |", "|---|---|---|---|"]  # fmt: skip
    for mtype, e in ls["edge"].items():
        out.append(
            f"| {mtype} | {e['size_bytes'] / 1024:.0f} KB | {e['ms_per_window_1_threads']} | {e['ms_per_10s_reading_4_threads']} |"
        )
    out += ["", f"Battery: not measured ({ls['battery_reason']}).", ""]
    return out


def understand_section(un: dict[str, Any] | None, path: Path | None) -> list[str]:
    out = ["## 4a. Granite (local Ollama) vs the rules on labelled faults, and cost per alert", ""]
    if not un or not path:
        return [
            *out,
            "**Not run.** `python -m scripts.eval_understand` needs Ollama with the Granite model (slow on a CPU),",
            "and no record exists yet. Until then, nothing here claims that Granite helps: in the product, every alert",
            "works without it (rules, marked 'expert check pending').", ""]  # fmt: skip
    return [
        *out,
        f"Record: `docs/runs/{path.name}` (model {un['model']}). {un['cases']} CWRU recordings; rules = the",
        "CWRU classifier retrained without the case's load.", "",
        "| | accuracy | answered | asked for a mechanic | INR per call (median) |", "|---|---|---|---|---|",
        f"| rules | {pct(un['rules_accuracy'])}% | {un['cases']} | – | 0 |",
        f"| Granite | {pct(un['llm_accuracy'])}% | {un['llm_answered']} | {pct(un['llm_mechanic_check_share'])}% "
        f"| {un['inr_per_call_median'] if un['inr_per_call_median'] is not None else '–'} |", ""]  # fmt: skip


def main() -> None:
    ev, evp = newest("eval")
    ft, ftp = newest("faulttype")
    ims, imsp = newest("ims")
    lines = [
        "# Results",
        "",
        "Every number here is generated by `python -m edge.results` from the evaluation records in",
        "`docs/runs/`; regenerate with `make results`. Small test sets mean wide error bars.",
        "",
    ]
    if ev and evp:
        lines += anomaly_section(ev, evp)
    ls, lsp = newest("listen")
    if ls and lsp:
        lines += listen_section(ls, lsp, ev)
    lines += results_more.detectors_section()
    if ft and ftp:
        lines += classifier_section(ft, ftp)
    if ims and imsp:
        lines += ims_section(ims, imsp)
    lines += results_more.rul_section()
    un, unp = newest("understand")
    lines += understand_section(un, unp)
    lines += results_more.mill_section() + results_more.agent_section() + results_more.bench_section()
    lines += results_more.planner_section()
    lines += (
        (ROOT / "docs" / "results_notes.md").read_text(encoding="utf-8").splitlines()
        if (ROOT / "docs" / "results_notes.md").exists()
        else []
    )
    (ROOT / "docs" / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    short = summary(ev, ft, ims)
    for p in (ROOT / "README.md", ROOT / "docs" / "PITCH.md", ROOT / "docs" / "JUDGES.md"):
        fill_markers(p, short)
    print(ROOT / "docs" / "RESULTS.md")


if __name__ == "__main__":
    main()
