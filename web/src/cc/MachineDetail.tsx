// One machine: identification, telemetry, health with reasons, trend, anomalies, the agents' findings and
// activity, maintenance history and baseline versions. From /api/cc/machine/{id}.
import { useEffect, useState } from "react";
import { get, post } from "../api";
import { HEALTH_WORD, healthClass, ts, type CCState, type Detail } from "./data";
import { Messages } from "./Messages";
import { Recs } from "./panels";
import { Lines, Panel } from "./ui";

export function MachineDetail({
  id,
  s,
  onClose,
  onChanged,
}: {
  id: string;
  s: CCState;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [d, setD] = useState<Detail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let on = true;
    get<Detail>(`/api/cc/machine/${id}`)
      .then((x) => on && (setD(x), setErr(null)))
      .catch((e: unknown) => on && setErr(String(e)));
    return () => {
      on = false;
    };
  }, [id, s.version]);
  if (!d)
    return (
      <Panel title={id}>
        {err ? <span className="crit small">{err}</span> : <span className="muted small">Loading</span>}
      </Panel>
    );
  const m = d.machine;
  const h = d.health;
  const wf = s.workflows.find((w) => w.machine === id && w.state !== "closed");
  const waitingRepair = wf?.stages.find((x) => x.stage === "Repair")?.status === "waiting";
  return (
    <div className="cc-detail">
      <Panel
        title={`${m.name} · ${m.id}`}
        right={
          <button className="cc-btn" onClick={onClose}>
            Back to factory
          </button>
        }
      >
        <div className="cc-kv small">
          <span>type</span>
          <b>{m.type}</b>
          <span>factory</span>
          <b>{m.factory}</b>
          <span>operating</span>
          <b className={m.run === "stopped" ? "dim" : ""}>{m.run}</b>
          <span>sensor</span>
          <b className={m.sensor === "lost" ? "crit" : ""}>
            {m.sensor === "lost" ? "no signal" : "connected"}
          </b>
          <span>sound risk</span>
          <b className="mono">{m.sound_risk == null ? "—" : `${m.sound_risk.toFixed(2)}× of its alarm`}</b>
          <span>temperature</span>
          <b className="muted">no sensor</b>
          <span>vibration</span>
          <b className="muted">no sensor</b>
          <span>speed</span>
          <b className="mono muted">{m.rpm ? `${m.rpm} rpm (nameplate, assumption)` : "—"}</b>
          <span>last reading</span>
          <b className="mono">{m.last_ts?.replace("T", " ") ?? "—"}</b>
          <span>readings</span>
          <b className="muted">
            {m.live
              ? `LIVE recorder · ${m.live.state === "learning" ? `learning ${m.live.progress.clips}/${m.live.progress.target} clips` : `${m.live.readings} clips`}${m.live.label ? ` · ${m.live.label}` : ""}`
              : m.source === "simulated"
                ? "recorded clips replayed (simulated)"
                : (m.source ?? "—")}
          </b>
        </div>
      </Panel>
      {h && (
        <Panel
          title="Health"
          right={<span className={`tag ${healthClass(h.status)}`}>{HEALTH_WORD[h.status]}</span>}
        >
          <div className="small">
            urgency <b>{h.urgency.replace(/_/g, " ")}</b> · severity <b>{h.severity}</b> · trend{" "}
            <b>{h.trend}</b> · confidence <b>{h.confidence}</b>
          </div>
          {h.confidence_reasons.length > 0 && (
            <div className="tiny muted">less sure because: {h.confidence_reasons.join("; ")}</div>
          )}
          <ul className="small cc-ev">
            {h.evidence.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
          <div className="tiny muted">
            next check {h.next_inspection ?? "—"} · {h.note}
          </div>
          <Lines
            height={90}
            series={[{ values: d.trend.map((t) => t.risk), cls: "cy" }]}
            refY={1}
            refLabel="alarm"
          />
        </Panel>
      )}
      <Panel title="AI findings and recommendation">
        <Recs s={s} onChanged={onChanged} machine={id} />
        {waitingRepair && wf && (
          <RepairReport caseId={wf.case} technician={wf.technician ?? "technician"} onChanged={onChanged} />
        )}
      </Panel>
      <Panel title="Agent activity on this machine">
        <ul className="cc-runs">
          {d.agent_runs.map((r) => (
            <li key={r.id}>
              <span className="mono tiny muted">{ts(r.updated)}</span> <b>{r.agent}</b> · {r.task} ·{" "}
              <span className={`st-${r.status}`}>{r.status}</span>
              {r.reasoning && <div className="tiny muted">{r.reasoning}</div>}
            </li>
          ))}
          {d.agent_runs.length === 0 && (
            <li className="small muted">No agent has worked on this machine yet.</li>
          )}
        </ul>
      </Panel>
      <Panel title="History">
        <div className="tiny">
          <b>Anomaly cases:</b>{" "}
          {d.cases.length
            ? d.cases
                .map(
                  (c) =>
                    `${c.id} (${c.urgency.replace(/_/g, " ")}, ${c.outcome ?? c.state}, opened ${ts(c.opened)})`,
                )
                .join("; ")
            : "none"}
        </div>
        <div className="tiny">
          <b>Repairs:</b>{" "}
          {d.repairs.length
            ? d.repairs
                .map(
                  (r) =>
                    `${ts(r.done)} ${r.technician}: ${r.work} → ${r.outcome} (median risk ${r.before_median} → ${r.after_median} vs baseline v1)`,
                )
                .join("; ")
            : "none"}
        </div>
        <div className="tiny">
          <b>Learned normal:</b>{" "}
          {d.baselines
            .map((b) => `v${b.version} ${b.reason} (${b.n_rows} readings, ${b.status})`)
            .join("; ") || "still learning"}
        </div>
      </Panel>
      <Messages id={id} version={s.version} />
    </div>
  );
}

function RepairReport({
  caseId,
  technician,
  onChanged,
}: {
  caseId: string;
  technician: string;
  onChanged: () => void;
}) {
  const [sim, setSim] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="cc-approve">
      <div className="small">
        <b>Technician {technician}:</b> report the job done
      </div>
      <label className="tiny">
        <input type="checkbox" checked={sim} onChange={(e) => setSim(e.target.checked)} /> SIMULATE recovery
        (the machine replays its recorded normal clips; no real repair)
      </label>
      <button
        className="cc-btn pri"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          post("/api/cc/repair_done", {
            case_id: caseId,
            technician,
            work: "inspection and repair (demo report)",
            simulate_recovery: sim,
          })
            .then(onChanged)
            .catch((e: unknown) => setErr(String(e)))
            .finally(() => setBusy(false));
        }}
      >
        Repair done
      </button>
      {err && <span className="tiny crit">{err}</span>}
    </div>
  );
}
