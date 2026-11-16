// The command centre's other sections. Each reads /api/cc/* (or the existing /api/*) and nothing else.
import { useEffect, useState } from "react";
import { get, post } from "../api";
import { HEALTH_WORD, healthClass, ts, type AgentRun, type CCState, type Detail } from "./data";
import { MachineDetail } from "./MachineDetail";
import { Approve, Pipeline, TelemetryTable } from "./panels";
import { Bars, Dot, Lines, Panel } from "./ui";

export function MachinesPage({
  s,
  onPick,
  selected,
  onChanged,
  onClose,
}: {
  s: CCState;
  onPick: (id: string) => void;
  selected: string | null;
  onChanged: () => void;
  onClose: () => void;
}) {
  return (
    <div className="cc-page cc-2col">
      <div className="cc-cards">
        {s.machines.map((m) => (
          <button
            key={m.id}
            className={`cc-card ${m.id === selected ? "sel" : ""}`}
            onClick={() => onPick(m.id)}
          >
            <div className="row between">
              <b>{m.name}</b>
              <span className="mono muted small">{m.id}</span>
            </div>
            <div className={`small ${healthClass(m.health)}`}>
              <Dot cls={healthClass(m.health)} />{" "}
              {m.run === "stopped" ? "Stopped" : m.sensor === "lost" ? "No signal" : HEALTH_WORD[m.health]}
            </div>
            <div className="tiny muted">
              {m.type} · factory {m.factory} · sound{" "}
              {m.sound_risk == null ? "—" : `${m.sound_risk.toFixed(2)}×`} ·{" "}
              {m.last_ts?.slice(5).replace("T", " ") ?? "no reading"}
            </div>
            {m.case && <div className="tiny cy">case {m.case}</div>}
          </button>
        ))}
      </div>
      <div>
        {selected ? (
          <MachineDetail id={selected} s={s} onClose={onClose} onChanged={onChanged} />
        ) : (
          <Panel title="Machine">Pick a machine.</Panel>
        )}
      </div>
    </div>
  );
}

export function TelemetryPage({
  s,
  factory,
  selected,
  onPick,
}: {
  s: CCState;
  factory: string;
  selected: string | null;
  onPick: (id: string) => void;
}) {
  const id = selected ?? s.machines[0]?.id;
  const [d, setD] = useState<Detail | null>(null);
  useEffect(() => {
    if (!id) return;
    get<Detail>(`/api/cc/machine/${id}`)
      .then(setD)
      .catch(() => undefined);
  }, [id, s.version]);
  return (
    <div className="cc-page cc-2col">
      <TelemetryTable s={s} factory={factory} selected={id ?? null} onPick={onPick} />
      <Panel title={`Readings history · ${id ?? ""}`}>
        {d && (
          <>
            <Lines
              height={180}
              series={[{ values: d.trend.map((t) => t.risk), cls: "cy" }]}
              refY={1}
              refLabel="alarm level"
            />
            <table className="cctab">
              <thead>
                <tr>
                  <th>time</th>
                  <th>sound risk</th>
                  <th>learned from</th>
                  <th>excluded because</th>
                </tr>
              </thead>
              <tbody>
                {[...d.trend]
                  .reverse()
                  .slice(0, 40)
                  .map((t) => (
                    <tr key={t.ts}>
                      <td className="mono">{t.ts.replace("T", " ")}</td>
                      <td className={`mono ${t.risk != null && t.risk >= 1 ? "crit" : ""}`}>
                        {t.risk == null ? "—" : `${t.risk.toFixed(2)}×`}
                      </td>
                      <td className="mono">{t.clean ? "yes" : "no"}</td>
                      <td className="muted">{t.excluded ?? ""}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </>
        )}
      </Panel>
    </div>
  );
}

export function PredictPage({ s, onPick }: { s: CCState; onPick: (id: string) => void }) {
  const [all, setAll] = useState<Record<string, Detail>>({});
  useEffect(() => {
    let on = true;
    Promise.all(s.machines.map((m) => get<Detail>(`/api/cc/machine/${m.id}`).then((d) => [m.id, d] as const)))
      .then((xs) => on && setAll(Object.fromEntries(xs)))
      .catch(() => undefined);
    return () => {
      on = false;
    };
  }, [s.version, s.machines]);
  return (
    <div className="cc-page cc-cards3">
      {s.machines.map((m) => {
        const d = all[m.id];
        const est = d?.health?.time_to_threshold;
        return (
          <Panel
            key={m.id}
            title={`${m.name} · ${m.id}`}
            right={<span className={`tag ${healthClass(m.health)}`}>{HEALTH_WORD[m.health]}</span>}
          >
            <div onClick={() => onPick(m.id)}>
              <Lines
                height={70}
                series={[{ values: d?.trend.map((t) => t.risk) ?? [], cls: "cy" }]}
                refY={1}
              />
              <div className="tiny">
                confidence <b>{d?.health?.confidence ?? "—"}</b> · trend <b>{d?.health?.trend ?? "—"}</b>
              </div>
              <div className="tiny muted">
                {!est
                  ? "no estimate"
                  : est.status === "estimate"
                    ? `threshold in ${est.low}-${est.high} d (10-90%)`
                    : est.status === "insufficient_data"
                      ? `not enough data: ${est.reason ?? ""}`
                      : est.status.replace(/_/g, " ")}
              </div>
              {d?.health?.evidence[0] && <div className="tiny muted">{d.health.evidence[0]}</div>}
            </div>
          </Panel>
        );
      })}
    </div>
  );
}

export function AgentsPage({ s }: { s: CCState }) {
  const [sel, setSel] = useState<string | null>(null);
  const runs = s.timeline.filter((r) => !sel || r.agent === sel);
  return (
    <div className="cc-page cc-2col">
      <div className="cc-agents">
        {s.agents.map((a) => (
          <button
            key={a.agent}
            className={`cc-agent ${sel === a.agent ? "sel" : ""}`}
            onClick={() => setSel(sel === a.agent ? null : a.agent)}
          >
            <div className="row between">
              <b>{a.name}</b>
              <span className={`st st-${a.status}`}>{a.status.replace("_", " ")}</span>
            </div>
            <div className="tiny">{a.last?.task ?? "no task yet"}</div>
            {a.last?.reasoning && <div className="tiny muted">{a.last.reasoning}</div>}
            {a.last && (
              <div className="tiny muted mono">
                {ts(a.last.updated)} · tools {a.last.tools.join(", ") || "—"}
                {a.last.retries > 0 ? ` · ${a.last.retries} retries` : ""}
              </div>
            )}
            <div className="tiny muted">code: {a.code}</div>
          </button>
        ))}
      </div>
      <Panel title={`Execution timeline${sel ? ` · ${sel}` : ""}`}>
        <Timeline runs={runs} />
      </Panel>
    </div>
  );
}

export function Timeline({ runs }: { runs: AgentRun[] }) {
  return (
    <ol className="cc-timeline">
      {runs.map((r) => (
        <li key={r.id} className={`st-${r.status}`}>
          <span className="mono tiny muted">{ts(r.updated)}</span>
          <b className="small">{r.agent}</b>
          <span className="small">{r.task}</span>
          {r.machine && <span className="mono tiny muted">{r.machine}</span>}
          <span className={`st st-${r.status}`}>{r.status.replace("_", " ")}</span>
          {r.reasoning && <div className="tiny muted">{r.reasoning}</div>}
          {r.error && r.status !== "completed" && r.error !== r.reasoning && (
            <div className="tiny warn">{r.error}</div>
          )}
          {r.tools.length > 0 && <div className="tiny muted mono">tools: {r.tools.join(", ")}</div>}
        </li>
      ))}
      {runs.length === 0 && <li className="small muted">No agent run yet.</li>}
    </ol>
  );
}

export function JobsPage({ s, onChanged }: { s: CCState; onChanged: () => void }) {
  const pending = s.approvals.filter((a) => a.status === "requested");
  return (
    <div className="cc-page">
      <Panel title={`Waiting for the owner (${pending.length})`}>
        {pending.map((a) => (
          <Approve key={a.id} a={a} onChanged={onChanged} />
        ))}
        {pending.length === 0 && <div className="small muted">Nothing to decide.</div>}
      </Panel>
      {s.workflows.map((w) => (
        <Panel
          key={w.case}
          title={`${w.case} · ${w.machine}`}
          right={
            <span className="tiny muted">
              {w.state}
              {w.outcome ? ` · ${w.outcome}` : ""} · opened {ts(w.opened)}
            </span>
          }
        >
          <Pipeline w={w} />
        </Panel>
      ))}
      <Panel title="Decisions (audit)">
        <table className="cctab">
          <tbody>
            {s.approvals
              .filter((a) => a.status !== "requested")
              .map((a) => (
                <tr key={a.id}>
                  <td className="mono">{a.id}</td>
                  <td>{a.kind}</td>
                  <td className={a.status === "approved" ? "ok" : "muted"}>{a.status}</td>
                  <td className="muted">
                    {a.decided_by ?? ""} {a.channel ? `via ${a.channel}` : ""}
                  </td>
                  <td className="mono muted">{ts(a.decided)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

interface Directory {
  technicians: {
    id: string;
    name: string;
    skills: string[];
    works: string[];
    km: number;
    role: string;
    source: string;
    jobs: number;
    fixed: number;
  }[];
  parts: {
    fault: string;
    type: string;
    part: string;
    dealer: string;
    in_stock: boolean;
    lead_days: number;
    source: string;
    cost: { low_inr: number; high_inr: number; price_source: string | null };
  }[];
}
function useDirectory() {
  const [d, setD] = useState<Directory | null>(null);
  useEffect(() => {
    get<Directory>("/api/cc/directory")
      .then(setD)
      .catch(() => undefined);
  }, []);
  return d;
}

export function TechsPage() {
  const d = useDirectory();
  return (
    <div className="cc-page">
      <Panel title="Technicians" right={<span className="tiny muted">{d?.technicians[0]?.source}</span>}>
        <table className="cctab">
          <thead>
            <tr>
              <th>id</th>
              <th>name</th>
              <th>skills</th>
              <th>works</th>
              <th>km</th>
              <th>jobs here</th>
              <th>verified fixed</th>
            </tr>
          </thead>
          <tbody>
            {d?.technicians.map((t) => (
              <tr key={t.id}>
                <td className="mono">{t.id}</td>
                <td>{t.name}</td>
                <td className="small">{t.skills.join(", ").replace(/_/g, " ")}</td>
                <td className="mono small">{t.works.join(" ")}</td>
                <td className="mono">{t.km}</td>
                <td className="mono">{t.jobs}</td>
                <td className="mono">{t.fixed}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

export function PartsPage() {
  const d = useDirectory();
  return (
    <div className="cc-page">
      <Panel
        title="Spare parts"
        right={
          <span className="tiny muted">
            stock: MOCK dealer data · prices: cited typical ranges (faults.yaml), confirm with dealer
          </span>
        }
      >
        <table className="cctab">
          <thead>
            <tr>
              <th>fault</th>
              <th>machine</th>
              <th>part</th>
              <th>dealer</th>
              <th>stock</th>
              <th>typical cost (part + labour)</th>
            </tr>
          </thead>
          <tbody>
            {d?.parts.map((p) => (
              <tr key={`${p.fault}${p.type}${p.part}`}>
                <td>{p.fault.replace(/_/g, " ")}</td>
                <td>{p.type}</td>
                <td>{p.part}</td>
                <td className="muted">{p.dealer}</td>
                <td className={p.in_stock ? "ok" : "warn"}>
                  {p.in_stock ? "in stock" : `order, ${p.lead_days} d`}
                </td>
                <td className="mono">
                  {p.cost.price_source ? (
                    <a href={p.cost.price_source} target="_blank" rel="noreferrer">
                      ₹{p.cost.low_inr}–{p.cost.high_inr}
                    </a>
                  ) : (
                    `₹${p.cost.low_inr}–${p.cost.high_inr}`
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

export function AnalyticsPage({ s }: { s: CCState }) {
  const p = s.performance;
  return (
    <div className="cc-page cc-2col">
      <Panel title="Reported running, per machine" right={<span className="tiny muted">{p.note}</span>}>
        <Bars
          rows={p.per_machine.map((m) => ({
            label: m.machine,
            value: m.running_share,
            cls: m.running_share < 0.9 ? "warn" : "ok",
          }))}
        />
        <Bars rows={p.per_machine.map((m) => ({ label: m.machine, value: m.flagged_share, cls: "warn" }))} />
        <div className="tiny muted">second chart: share of cycles each machine was flagged (not normal)</div>
      </Panel>
      <Panel title="Per day">
        <Lines
          height={140}
          yMin={0}
          yMax={1}
          series={[
            { values: p.trend.map((t) => t.running_share), cls: "ok" },
            { values: p.trend.map((t) => t.flagged_share), cls: "warn" },
          ]}
        />
        <div className="tiny muted">green: share running · amber: share flagged</div>
        <table className="cctab">
          <thead>
            <tr>
              <th>machine</th>
              <th>cycles</th>
              <th>downtime</th>
              <th>no signal</th>
              <th>cases</th>
              <th>repairs</th>
            </tr>
          </thead>
          <tbody>
            {p.per_machine.map((m) => (
              <tr key={m.machine}>
                <td className="mono">{m.machine}</td>
                <td className="mono">{m.ticks}</td>
                <td className="mono">{m.downtime_h} h</td>
                <td className="mono">{m.no_signal_h} h</td>
                <td className="mono">{m.cases}</td>
                <td className="mono">{m.repairs}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

interface AuditRow {
  seq: number;
  ts: number;
  case_id: string | null;
  actor: string;
  event: string;
  detail: Record<string, unknown>;
}
export function EventsPage() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [ok, setOk] = useState<boolean | null>(null);
  const [q, setQ] = useState("");
  useEffect(() => {
    const load = () =>
      get<{ rows: AuditRow[]; ok: boolean }>("/api/cc/audit")
        .then((r) => (setRows(r.rows), setOk(r.ok)))
        .catch(() => undefined);
    void load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, []);
  const shown = rows.filter((r) =>
    `${r.actor} ${r.event} ${r.case_id ?? ""} ${JSON.stringify(r.detail)}`
      .toLowerCase()
      .includes(q.toLowerCase()),
  );
  return (
    <div className="cc-page">
      <Panel
        title="Event history"
        right={
          <span className={`tiny ${ok ? "ok" : "crit"}`}>
            hash chain {ok ? "intact" : ok === false ? "BROKEN" : "…"}
          </span>
        }
      >
        <input
          className="cc-search"
          placeholder="filter"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="filter events"
        />
        <table className="cctab">
          <tbody>
            {[...shown].reverse().map((r) => (
              <tr key={r.seq}>
                <td className="mono tiny">{r.seq}</td>
                <td className="mono tiny muted">{ts(r.ts)}</td>
                <td className="small">{r.actor}</td>
                <td className="small">{r.event}</td>
                <td className="mono tiny muted">{r.case_id ?? ""}</td>
                <td className="tiny muted">{JSON.stringify(r.detail).slice(0, 140)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

export function SettingsPage({ s }: { s: CCState }) {
  const [token, setToken] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [mid, setMid] = useState(s.machines.find((m) => m.type === "fan")?.id ?? s.machines[0]?.id);
  return (
    <div className="cc-page cc-2col">
      <Panel title="Account">
        <div className="small muted">
          Roles: owner (approves), technician (reports repairs), viewer (reads). With no tokens set in .env
          the server runs in demo mode and everyone is the owner.
        </div>
        <div className="row gap">
          <input
            type="password"
            placeholder="access token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            aria-label="token"
          />
          <button
            className="cc-btn pri"
            onClick={() =>
              post<{ role: string }>("/api/cc/login", { token })
                .then((r) => setMsg(`signed in as ${r.role}`))
                .catch((e: unknown) => setMsg(String(e)))
            }
          >
            Sign in
          </button>
          <button className="cc-btn" onClick={() => post("/api/cc/logout").then(() => setMsg("signed out"))}>
            Sign out
          </button>
        </div>
        {msg && <div className="small">{msg}</div>}
      </Panel>
      <Panel title="Connectivity">
        <div className="small">
          Cloud link: <b className={s.kpis.link_up ? "ok" : "crit"}>{s.kpis.link_up ? "up" : "down"}</b> ·{" "}
          {s.kpis.sync.events_waiting} events and {s.kpis.sync.actions_waiting} messages waiting ·{" "}
          {s.cloud_events} events in the cloud copy · audit {s.audit_ok ? "intact" : "BROKEN"}
        </div>
        <div className="tiny muted">
          Local: readings, learned normals, health, agents, cases, approvals. Cloud: the audit copy and the
          owner&rsquo;s WhatsApp replies. Messages wait in the queue while the link is down and go out once.
        </div>
        <button className="cc-btn" onClick={() => void post("/api/link", { up: !s.kpis.link_up })}>
          {s.kpis.link_up ? "Cut the link (simulate)" : "Restore the link"}
        </button>
      </Panel>
      <Panel title="Demo scenario (simulated)">
        <div className="small muted">
          Switch a machine&rsquo;s replayed recordings to its recorded fault clips: the agents then work the
          case end to end. Owner approval, the technician&rsquo;s report and the recovery are steps you take.
        </div>
        <div className="row gap">
          <select value={mid} onChange={(e) => setMid(e.target.value)} aria-label="machine">
            {s.machines.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id} · {m.name}
              </option>
            ))}
          </select>
          <button
            className="cc-btn pri"
            onClick={() =>
              post("/api/cc/scenario", { machine: mid, mode: "fault" }).then(() =>
                setMsg(`${mid}: recorded fault clips (simulated)`),
              )
            }
          >
            Start fault
          </button>
          <button
            className="cc-btn"
            onClick={() =>
              post("/api/cc/scenario", { machine: mid, mode: "normal" }).then(() =>
                setMsg(`${mid}: normal clips`),
              )
            }
          >
            Back to normal
          </button>
        </div>
      </Panel>
      <Panel title="Assumptions">
        <ul className="tiny muted">
          <li>
            Detector: each machine&rsquo;s own loudness, chosen on selection units (docs/RESULTS.md 1c).
          </li>
          <li>Learning period, alarm level (95th percentile), drift limits: edge/baseline.py ASSUMPTIONS.</li>
          <li>
            Urgency cut-offs: server/health.py ASSUMPTIONS. Downtime: planned service hours per type
            (data/sim/cluster.json).
          </li>
          <li>Technicians: DEMO DATA. Dealer stock: MOCK. Prices: cited typical ranges.</li>
          <li>Last agent cycle took {s.last_tick_ms ?? "—"} ms on this computer.</li>
        </ul>
      </Panel>
    </div>
  );
}
