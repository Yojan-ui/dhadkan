// Overview panels. Measured values only: temperature and vibration show "no sensor" because these machines
// have none; RPM is the nameplate figure (assumption); sound risk is the reading against its own normal.
import { useEffect, useMemo, useState } from "react";
import { get, post } from "../api";
import { HEALTH_WORD, healthClass, ts, type CCState, type Detail, type Row } from "./data";
import { Bars, Dot, Lines, Panel } from "./ui";

type Key = "id" | "name" | "sound_risk" | "health" | "last_ts";
const ORDER: Record<string, number> = {
  urgent: 0,
  repair_soon: 1,
  inspect: 2,
  watch: 3,
  no_signal: 4,
  not_running: 4,
  learning: 5,
  normal: 6,
};

export function TelemetryTable({
  s,
  factory,
  selected,
  onPick,
  compact = false,
}: {
  s: CCState;
  factory: string;
  selected: string | null;
  onPick: (id: string) => void;
  compact?: boolean;
}) {
  const [q, setQ] = useState("");
  const [only, setOnly] = useState<"all" | "attention">("all");
  const [key, setKey] = useState<Key>("health");
  const [asc, setAsc] = useState(true);
  const rows = useMemo(() => {
    const f = s.machines.filter(
      (m) =>
        (factory === "all" || m.factory === factory) &&
        (only === "all" || !["normal", "learning"].includes(m.health)) &&
        `${m.id} ${m.name} ${m.type}`.toLowerCase().includes(q.toLowerCase()),
    );
    const v = (m: Row) =>
      key === "health" ? ORDER[m.health] : key === "sound_risk" ? (m.sound_risk ?? -9) : (m[key] ?? "");
    return [...f].sort((a, b) => (v(a) > v(b) ? 1 : v(a) < v(b) ? -1 : 0) * (asc ? 1 : -1));
  }, [s.machines, factory, only, q, key, asc]);
  const th = (k: Key, label: string) => (
    <th
      onClick={() => (k === key ? setAsc(!asc) : (setKey(k), setAsc(true)))}
      className={k === key ? "on" : ""}
    >
      {label}
      {k === key ? (asc ? " ▴" : " ▾") : ""}
    </th>
  );
  return (
    <Panel
      title="Machine telemetry"
      className="cc-tele"
      right={
        <span className="small muted">
          {rows.length}/{s.machines.length}
        </span>
      }
    >
      <div className="cc-tele-tools">
        <input
          placeholder="search id or name"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="search machines"
        />
        <select
          value={only}
          onChange={(e) => setOnly(e.target.value as "all" | "attention")}
          aria-label="filter"
        >
          <option value="all">all</option>
          <option value="attention">needs attention</option>
        </select>
      </div>
      <table className="cctab">
        <thead>
          <tr>
            {th("id", "ID")}
            {!compact && th("name", "Machine")}
            <th title="no temperature sensor on these machines">Temp</th>
            <th title="no vibration sensor on these machines">Vib</th>
            {th("sound_risk", "Sound")}
            <th title="nameplate speed (assumption), not measured">RPM*</th>
            {th("health", "Health")}
            {th("last_ts", "Last")}
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.id} className={`${m.id === selected ? "sel" : ""}`} onClick={() => onPick(m.id)}>
              <td className="mono">
                <Dot cls={m.sensor === "lost" || m.run === "stopped" ? "dim" : healthClass(m.health)} />
                {m.id}
                {compact && <div className="muted tiny">{m.name}</div>}
              </td>
              {!compact && <td>{m.name}</td>}
              <td className="mono muted">—</td>
              <td className="mono muted">—</td>
              <td
                className={`mono ${m.sound_risk != null && m.sound_risk >= 1 ? "crit" : m.sound_risk != null && m.sound_risk >= 0.8 ? "warn" : ""}`}
              >
                {m.sound_risk == null ? "—" : `${m.sound_risk.toFixed(2)}×`}
              </td>
              <td className="mono muted">{m.rpm ?? "—"}</td>
              <td className={healthClass(m.health)}>
                {m.run === "stopped" ? "Stopped" : m.sensor === "lost" ? "No signal" : HEALTH_WORD[m.health]}
              </td>
              <td className="mono muted tiny">{m.last_ts ? m.last_ts.slice(5).replace("T", " ") : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="tiny muted pad">
        Sound = latest reading against that machine&rsquo;s own alarm level (1.00× = at alarm). Temp / Vib: no
        sensor fitted. RPM* = nameplate (assumption). Readings: recorded clips replayed by the simulator.
      </div>
    </Panel>
  );
}

export function Intelligence({
  s,
  onPick,
  onChanged,
}: {
  s: CCState;
  onPick: (id: string) => void;
  onChanged: () => void;
}) {
  const k = s.kpis;
  const anomalies = s.machines.filter(
    (m) => !["normal", "learning", "no_signal", "not_running"].includes(m.health),
  );
  const critical = s.machines.filter((m) => m.health === "urgent" || m.health === "repair_soon");
  const jobs = s.workflows.filter(
    (w) =>
      w.state !== "closed" &&
      w.stages.some((x) => x.stage === "Repair" && x.status !== "n/a" && x.status !== "pending"),
  );
  const pending = s.approvals.filter((a) => a.status === "requested");
  const perf = s.performance.per_machine;
  const avg = (f: (p: (typeof perf)[number]) => number) =>
    perf.length ? perf.reduce((a, p) => a + f(p), 0) / perf.length : 0;
  const pct = (v: number) => (perf.length ? `${Math.round(v * 100)}%` : "—");
  return (
    <div className="cc-intel">
      <Panel title="Operational intelligence">
        <div className="cc-mini">
          <Mini label="active anomalies" v={anomalies.length} cls={anomalies.length ? "warn" : ""} />
          <Mini label="critical" v={critical.length} cls={critical.length ? "crit" : ""} />
          <Mini label="investigations" v={k.open_cases} cls="cy" />
          <Mini label="repair jobs" v={jobs.length} />
          <Mini label="owner approvals" v={pending.length} cls={pending.length ? "warn" : ""} />
          <Mini
            label="connectivity issues"
            v={k.offline + (k.link_up ? 0 : 1)}
            cls={k.offline || !k.link_up ? "crit" : ""}
          />
        </div>
      </Panel>
      <Panel
        title="Factory performance"
        right={<span className="tiny muted">measured: reported running</span>}
      >
        <div className="cc-mini">
          <Mini label="running (share of cycles)" v={pct(avg((p) => p.running_share))} />
          <Mini label="downtime, all machines" v={`${perf.reduce((a, p) => a + p.downtime_h, 0)} h`} />
          <Mini label="monitored cycles" v={pct(avg((p) => p.monitored_share))} />
          <Mini label="repairs logged" v={perf.reduce((a, p) => a + p.repairs, 0)} />
        </div>
        <div className="tiny muted">
          {perf.length
            ? s.performance.note
            : "No reading cycle logged since the agents started: press DEMO play or +1."}
        </div>
      </Panel>
      <Panel title="Predictive maintenance">
        <ul className="cc-list">
          {s.machines
            .filter((m) => m.urgency && m.urgency !== "none")
            .map((m) => {
              const r = s.recommendations.find((x) => x.machine === m.id);
              return (
                <li key={m.id} onClick={() => onPick(m.id)}>
                  <Dot cls={healthClass(m.health)} />
                  <b>{m.name}</b> <span className="mono muted">{m.id}</span> · {HEALTH_WORD[m.health]} ·{" "}
                  {m.confidence} confidence
                  {r?.evidence[0] && <div className="tiny muted">{r.evidence[0]}</div>}
                </li>
              );
            })}
          {!s.machines.some((m) => m.urgency && m.urgency !== "none") && (
            <li className="muted small">No machine is drifting from its own normal.</li>
          )}
        </ul>
      </Panel>
      <Panel
        title="AI recommendations"
        right={<span className="tiny muted">intelligence + planning agents</span>}
      >
        <Recs s={s} onPick={onPick} onChanged={onChanged} />
      </Panel>
    </div>
  );
}

export function Recs({
  s,
  onPick,
  onChanged,
  machine,
}: {
  s: CCState;
  onPick?: (id: string) => void;
  onChanged: () => void;
  machine?: string;
}) {
  const recs = s.recommendations.filter((r) => !machine || r.machine === machine);
  if (!recs.length)
    return <div className="small muted">Nothing recommended: every machine sounds like its normal.</div>;
  return (
    <div className="cc-recs">
      {recs.map((r) => {
        const appr = s.approvals.filter((a) => a.case_id === r.case && a.status === "requested");
        return (
          <article key={r.machine} className={`cc-rec ${healthClass(r.condition)}`}>
            <header onClick={() => onPick?.(r.machine)}>
              <b>{r.name}</b> <span className="mono muted">{r.machine}</span>
              <span className={`tag ${healthClass(r.condition)}`}>{HEALTH_WORD[r.condition]}</span>
            </header>
            <div className="small">{r.action}</div>
            <ul className="tiny muted">
              {r.evidence.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
            <div className="tiny">
              urgency <b>{r.urgency.replace(/_/g, " ")}</b> · confidence <b>{r.confidence}</b>
              {r.confidence_reasons.length > 0 && (
                <span className="muted"> ({r.confidence_reasons.join("; ")})</span>
              )}
            </div>
            {r.plan && (
              <div className="tiny muted">
                plan: priority {r.plan.priority}/{r.plan.of}, from {r.plan.earliest_day}, {r.plan.window},
                about {r.plan.expected_downtime_h} h down
              </div>
            )}
            <div className="tiny">
              approval needed: {r.approval_required.length ? r.approval_required.join(", ") : "none yet"}
            </div>
            {appr.map((a) => (
              <Approve key={a.id} a={a} onChanged={onChanged} />
            ))}
            <div className="tiny muted">{r.note}</div>
          </article>
        );
      })}
    </div>
  );
}

export function Approve({ a, onChanged }: { a: CCState["approvals"][number]; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const act = (decision: "approved" | "rejected") => {
    setBusy(true);
    post("/api/cc/decide", { approval_id: a.id, decision })
      .then(onChanged)
      .catch((e: unknown) => setErr(String(e)))
      .finally(() => setBusy(false));
  };
  return (
    <div className="cc-approve">
      <div className="small">
        <b>{a.kind === "production_stop" ? "Your decision" : "Approve?"}</b>{" "}
        <span className="mono muted">{a.case_id}</span> {a.summary}
      </div>
      {a.kind === "production_stop" && (
        <div className="tiny muted">Dhadkan cannot stop a machine; your answer is only recorded.</div>
      )}
      <div className="row gap">
        <button className="cc-btn pri" disabled={busy} onClick={() => act("approved")}>
          {a.kind === "production_stop" ? "I stopped it" : "Approve"}
        </button>
        <button className="cc-btn" disabled={busy} onClick={() => act("rejected")}>
          {a.kind === "production_stop" ? "Keep running" : "Not now"}
        </button>
        {err && <span className="tiny crit">{err}</span>}
      </div>
    </div>
  );
}

function Mini({ label, v, cls = "" }: { label: string; v: string | number; cls?: string }) {
  return (
    <div className="cc-m">
      <span className={`v mono ${cls}`}>{v}</span>
      <span className="l">{label}</span>
    </div>
  );
}

export function PerformanceChart({ s }: { s: CCState }) {
  const p = s.performance;
  return (
    <Panel
      title="Factory performance"
      right={<span className="tiny muted">share of cycles reported running</span>}
    >
      {p.per_machine.length === 0 && <div className="small muted">No reading cycle logged yet.</div>}
      <Bars
        rows={p.per_machine.map((m) => ({
          label: m.machine,
          value: m.running_share,
          cls: m.running_share < 0.9 ? "warn" : "ok",
        }))}
      />
      <Lines
        height={60}
        yMin={0}
        yMax={1}
        series={[{ values: p.trend.map((t) => t.flagged_share), cls: "warn" }]}
        refLabel="share flagged per day"
        refY={1}
      />
    </Panel>
  );
}

export function PredictChart({ s, selected }: { s: CCState; selected: string | null }) {
  const focus = selected ?? s.recommendations[0]?.machine ?? s.machines[0]?.id;
  const [d, setD] = useState<Detail | null>(null);
  useEffect(() => {
    if (!focus) return;
    let on = true;
    get<Detail>(`/api/cc/machine/${focus}`)
      .then((x) => on && setD(x))
      .catch(() => undefined);
    return () => {
      on = false;
    };
  }, [focus, s.version]);
  const vals = d?.trend.map((t) => t.risk) ?? [];
  const rep = d?.repairs ?? [];
  const markers = rep
    .map((r) => ({
      i: (d?.trend ?? []).findIndex((t) => new Date(t.ts).getTime() / 1000 >= r.done),
      label: `repair (${r.outcome})`,
    }))
    .filter((m) => m.i >= 0);
  const est = d?.health?.time_to_threshold;
  return (
    <Panel
      title={`Predictive maintenance · ${focus ?? ""}`}
      right={<span className="tiny muted">risk against its own normal</span>}
    >
      <Lines
        height={110}
        series={[{ values: vals, cls: "cy" }]}
        refY={1}
        refLabel="alarm"
        markers={markers}
      />
      <div className="tiny muted">
        {est
          ? est.status === "estimate"
            ? `Maintenance threshold in ${est.low}-${est.high} days (10-90% range; ${est.meaning}).`
            : est.status === "insufficient_data"
              ? `Time to threshold: not enough data (${est.reason ?? "too few readings"}).`
              : est.status === "no_trend"
                ? "No rising trend: no time-to-threshold estimate."
                : "At the maintenance threshold now."
          : "No estimate yet."}
      </div>
    </Panel>
  );
}

const URG: Record<string, number> = {
  none: 0,
  monitor: 1,
  measure_again: 1,
  inspect: 2,
  maintenance: 3,
  urgent_owner_attention: 4,
};

export function WorkflowStrip({ s, onPick }: { s: CCState; onPick: (id: string) => void }) {
  // the open case that matters most: waiting for the owner first, then by urgency, then newest
  const waiting = new Set(s.approvals.filter((a) => a.status === "requested").map((a) => a.case_id));
  const open = s.workflows.filter((x) => x.state !== "closed");
  const w =
    [...open].sort(
      (a, b) =>
        Number(waiting.has(b.case)) - Number(waiting.has(a.case)) ||
        URG[b.urgency] - URG[a.urgency] ||
        b.opened - a.opened,
    )[0] ?? s.workflows[0];
  return (
    <Panel
      title="Agent workflow"
      right={
        <span className="tiny muted">
          {w
            ? `${w.case} · ${w.urgency.replace(/_/g, " ")} · ${w.state}${w.outcome ? ` · ${w.outcome}` : ""}`
            : "no case yet"}
        </span>
      }
    >
      {w ? (
        <Pipeline w={w} onPick={onPick} compact />
      ) : (
        <div className="small muted">
          No anomaly has opened a case. The observation agent is watching every reading.
        </div>
      )}
      {open.length > 1 && (
        <div className="tiny muted">{open.length} open cases · all on Maintenance Jobs</div>
      )}
    </Panel>
  );
}

export function Pipeline({
  w,
  onPick,
  compact = false,
}: {
  w: CCState["workflows"][number];
  onPick?: (id: string) => void;
  compact?: boolean;
}) {
  return (
    <ol className={`cc-pipe ${compact ? "compact" : ""}`} onClick={() => onPick?.(w.machine)}>
      {w.stages.map((st) => (
        <li key={st.stage} className={`p-${st.status}`} title={st.detail ?? st.error ?? ""}>
          <span className="pd" />
          <span className="pn">{st.stage}</span>
          <span className="pt mono">{st.status === "n/a" ? "—" : st.status}</span>
          <span className="pt mono muted">{st.at ? ts(st.at) : ""}</span>
          {st.detail && <span className="px tiny muted">{st.detail}</span>}
        </li>
      ))}
    </ol>
  );
}
