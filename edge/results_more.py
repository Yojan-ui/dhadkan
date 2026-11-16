"""RESULTS.md sections for the per-machine detectors, time to threshold, the flour-mill story, the agent
under injected faults and the edge cost (called by edge/results.py; every number from docs/runs/)."""

import json
from pathlib import Path
from typing import Any

from .config import RUNS


def newest(prefix: str) -> tuple[dict[str, Any] | None, Path | None]:
    runs = sorted(RUNS.glob(f"{prefix}_*.json"))
    return (json.loads(runs[-1].read_text()), runs[-1]) if runs else (None, None)


def _f(v: Any, nd: int = 3) -> str:
    return "–" if v is None else f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def detectors_section() -> list[str]:
    rec, p = newest("compare")
    if not rec or not p:
        return []
    sel, held, ch = rec["selection"], rec["held_out"], rec["chosen"]
    out = [
        "## 1c. Machine-specific detectors: chosen on selection units, reported on held-out units",
        "",
        f"Record: `docs/runs/{p.name}` (code sha256 {rec['code_sha256'][:12]}). Every detector is fitted on ONE",
        "machine's normal clips only (train + val), calibrated out-of-fold on them; the alarm is that machine's own",
        "95th percentile. The candidate is chosen by mean pAUC on the SELECTION units "
        f"({', '.join(rec['protocol']['selection_units'])}) and then reported on the HELD-OUT units "
        f"({', '.join(rec['protocol']['held_out_units'])}), which played no part in the choice.",
        "Precision depends on how many faulty clips a test set has (DCASE has about as many as normal ones);",
        "the false-alarm rate (FAR, share of normal test clips at or above the alarm) does not.",
        "",
        f"**Chosen: `{ch}`.** No fusion beat it on the selection units, so none is used.",
        "",
        "| candidate | sel AUC | sel pAUC | held AUC | held pAUC | held precision | held recall | held F1 | held FAR |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for n in rec["candidates"]:
        s, h = sel[n], held[n]
        b = "**" if n == ch else ""
        out.append(f"| {b}{n}{b} | {_f(s['auc'])} | {_f(s['pauc'])} | {_f(h['auc'])} | {_f(h['pauc'])} | "
                   f"{_f(h['precision'])} | {_f(h['recall'])} | {_f(h['f1'])} | {_f(h['false_alarm_rate'])} |")  # fmt: skip
    out += ["", f"Held-out units, chosen detector `{ch}`:", "",
            "| unit | AUC | pAUC | precision | recall | F1 | FAR | normal / faulty clips |", "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for u in rec["protocol"]["held_out_units"]:
        r = rec["per_unit"][u]
        m = r[ch]
        out.append(f"| {u} | {_f(m['auc'])} | {_f(m['pauc'])} | {_f(m['precision'])} | {_f(m['recall'])} | "
                   f"{_f(m['f1'])} | {_f(m['false_alarm_rate'])} | {r['n_normal']} / {r['n_anomalous']} |")  # fmt: skip
    st = rec.get("gearbox_target_domain") or {}
    if st:
        out += ["", "Domain shift (gearbox, changed speed/load, model fitted on the source domain):", ""]
        out += [f"- {u}: AUC {_f(v[ch]['auc'])}, FAR {_f(v[ch]['false_alarm_rate'])}" for u, v in st.items()]
    out += ["", "What this means:", "",
            "- On held-out units the machine's own loudness is a fair detector, and the other single detectors are",
            "  weaker. A higher held-out AUC for some fusions is visible above; switching to one now would be choosing",
            "  on the held-out set, so we have not. Fresh field recordings are the honest way to decide.",
            "- Loudness depends on where the phone sits and on the shed. The baseline is per machine and per",
            "  placement; the noisy-shed flag and the confidence reasons (server/health.py) exist for that.",
            "- The autoencoder's per-machine calibration uses only 10 clips (it was trained on the rest), so its",
            "  false-alarm rate is less reliable than the others'.", ""]  # fmt: skip
    return out


def rul_section() -> list[str]:
    rec, p = newest("rul")
    if not rec or not p:
        return []
    out = ["## 3b. Time to the maintenance threshold (uncertainty-aware RUL)", "",
           f"Record: `docs/runs/{p.name}`. {rec['protocol']}. It estimates {rec['meaning']}.",
           "Two truths per bearing: when it actually reached the threshold borrowed from the other bearings, and",
           "the end of its run (failure). Interval = calibrated 10-90% range, so it should hold the truth ~80% of",
           "the time. States other than an estimate are counted, not hidden.", "",
           "| bearing | lead time (alarm to end) | vs threshold: MAE / RMSE / coverage | vs end of run: MAE / RMSE / coverage | estimate / no trend / insufficient / at threshold |",
           "|---|---|---|---|---|"]  # fmt: skip
    for k, b in rec["bearings"].items():
        t, e = b["metrics"]["vs_threshold"], b["metrics"]["vs_end_of_run"]
        stt = e["states"]
        th = ("never reached" if b["reached_threshold_day"] is None
              else f"{_f(t.get('mae_days'), 1)} d / {_f(t.get('rmse_days'), 1)} d / {_f(t.get('interval_coverage'), 2)}")  # fmt: skip
        out.append(f"| {k} | {_f(b['lead_time_days'], 1)} d | {th} | {_f(e.get('mae_days'), 1)} d / "
                   f"{_f(e.get('rmse_days'), 1)} d / {_f(e.get('interval_coverage'), 2)} | "
                   f"{stt['estimate']} / {stt['no_trend']} / {stt['insufficient_data']} / {stt['at_threshold']} |")  # fmt: skip
    out += ["", "Bearings that did not fail (false alarms):", ""]
    out += [f"- {k}: {v['alarm_share_after_baseline']:.0%} of readings above the alarm; {v['note']}"
            for k, v in rec["healthy_bearings"].items()]  # fmt: skip
    out += ["", "What this means: errors of a week or more on runs of 7-45 days, and intervals that hold the truth",
            "less often than they should on some bearings. The threshold borrowed from other bearings is wrong by a",
            "large factor for some (failure levels differ ~20x). The dashboard therefore shows a range with its",
            "meaning, says 'not enough data' after restarts and gaps, and never shows a failure date.", ""]  # fmt: skip
    return out


def mill_section() -> list[str]:
    rec, p = newest("flourmill")
    if not rec or not p:
        return []
    tl = rec["timeline"]
    first_fault = next(r["i"] for r in tl if r["phase"] == "fault")
    first_alarm = next(
        (r["i"] for r in tl if r["phase"] == "fault" and r["risk"] is not None and r["risk"] >= 1), None
    )
    first_action = next((r["i"] for r in tl if r["phase"] == "fault" and r["urgency"] != "none"), None)
    off = [r for r in tl if not r["online"]]
    normal = [r for r in tl if r["phase"] == "normal" and r["risk"] is not None]
    rep = rec["repairs"][0] if rec["repairs"] else None
    step_h = 3
    return ["## 5. The flour-mill demonstration (one recorded story, not a field result)", "",
           f"Record: `docs/runs/{p.name}` (`make mill`). {rec['provenance']['readings']}. "
           f"{rec['provenance']['people_and_stock']}. Simulated: {rec['provenance']['simulated']}. Owner: {rec['owner']}.", "",
           f"- Learning: {sum(r['phase'] == 'learning' for r in tl)} readings, no alarms. Normal running afterwards: "
           f"{sum(r['risk'] >= 1 for r in normal)} of {len(normal)} readings at or above the alarm.",
           f"- Detection latency: the first recorded fault clip is reading {first_fault}; the first at or above the alarm is "
           f"reading {first_alarm} ({(first_alarm - first_fault) * step_h if first_alarm is not None else '–'} h later at one "
           f"reading per {step_h} h); the agent first acted at reading {first_action}. The first fault clips are not louder than normal.",
           f"- Network cut for {len(off)} readings: up to {max((r['events_waiting'] for r in off), default=0)} events waited; "
           f"after it, {tl[-1]['events_waiting']} waiting, {rec['sync']['cloud_events']} events in the cloud copy, audit chain "
           f"{'intact' if rec['audit_ok'] else 'BROKEN'}.",
           f"- Case outcome: {rec['case']['outcome']}. Repair check against baseline v1: median risk "
           f"{rep['before_median'] if rep else '–'} before, {rep['after_median'] if rep else '–'} after "
           "(the after-repair readings are the unit's own held-out normal recordings: this shows the check works, "
           "not that a repair worked).", ""]  # fmt: skip


def agent_section() -> list[str]:
    rec, p = newest("agent")
    if not rec or not p:
        return []
    s = rec["summary"]
    return ["## 6. The maintenance agent under injected faults", "",
           f"Record: `docs/runs/{p.name}` (`python -m server.maint.evaluate`). {rec['note']}. {s['runs']} randomised runs:",
           "network cuts, provider errors, technicians declining (sometimes all of them), crash and restart mid-step,",
           "repeated observations, and an owner who answers promptly, late, never, with conflicting answers on two",
           "channels, or says no. Measured from the agent's own database and audit log, and the approval state at",
           "the moment each paid action was delivered.", "",
           "| measure | value |", "|---|---|",
           f"| completed (verified repair) when the owner approved and a technician existed | {s['completion_rate_when_approved_and_a_technician_exists']:.0%} |",
           f"| paid actions delivered without a valid approval | {s['approval_violations']} |",
           f"| outward actions delivered twice | {s['duplicate_sends']} |",
           f"| actions that failed first (network/provider) and were delivered later | {s['recovered_failures']} |",
           f"| actions still undelivered after the network came back | {s['undelivered_at_end']} |",
           f"| delivered actions with no audit entry | {s['untraced_actions']} |",
           f"| runs with an intact audit chain fully copied to the cloud | {s['audit_intact_runs']} of {s['runs']} |",
           f"| bookings when every technician declined | {s['booked_without_any_technician']} |", "",
           "Outcomes by owner behaviour: " + "; ".join(f"{k}: {v}" for k, v in s["outcomes_by_owner_behaviour"].items()) + ".",
           "Runs left open are the ones where the owner never answered, said no, or no technician was available:",
           "the agent keeps watching, keeps looking, and tells the owner. The first version of this evaluation found",
           "11 bookings sent after an approval had turned into a conflict; approvals are now re-checked at send time.", ""]  # fmt: skip


def bench_section() -> list[str]:
    rec, p = newest("bench")
    if not rec or not p:
        return []
    r = rec["results"]
    return ["## 7. What the per-machine pipeline costs per reading", "",
            f"Record: `docs/runs/{p.name}` on {rec['machine']['platform']} ({rec['machine']['processor']}), not a phone.", "",
            "| step (10 s reading) | cost |", "|---|---|",
            f"| quality check | {r['quality_ms']} ms |", f"| summary (log-mel, MFCC, band statistics) | {r['summary_ms']} ms |",
            f"| risk, chosen detector ({', '.join(rec['detectors']['names'])}) | {r['risk_ms_chosen']} ms |",
            f"| risk, all five classical detectors | {r['risk_ms_all_classical']} ms |",
            f"| fit a machine's baseline (40 readings) | {r['fit_baseline_ms_chosen_40_readings']} ms |",
            f"| peak Python memory, one reading | {r['peak_python_mib_one_reading']} MiB |",
            f"| stored per reading (summary + flags, SQLite) | {r['disk_bytes_per_reading']} bytes |", "",
            f"Not measured: {rec['not_measured']}.", ""]  # fmt: skip
