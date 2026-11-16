// Flour-mill demonstration (desktop, black): one chakki from learning to a verified repair. ?view=mill
// Every number comes from /api/mill/state (server/flourmill.py). Orange only means "needs attention".
import { useCallback, useEffect, useState } from "react";
import { get, post } from "../api";
import "./mill.css";

interface Point {
  i: number;
  ts: string;
  phase: string;
  clip: string;
  risk: number | null;
  status: string;
  urgency: string;
  online: boolean;
  events_waiting: number;
}
interface Step {
  idx: number;
  name: string;
  status: string;
  attempts: number;
  error: string | null;
  result: Record<string, unknown> | null;
}
interface Approval {
  id: string;
  kind: string;
  summary: string;
  amount_low: number | null;
  amount_high: number | null;
  status: string;
  decided_by: string | null;
  channel: string | null;
}
interface Health {
  status: string;
  urgency: string;
  severity: string;
  trend: string;
  confidence: string;
  confidence_reasons: string[];
  evidence: string[];
  next_inspection: string | null;
  note: string;
}
interface Cost {
  fault: string;
  low_inr: number;
  high_inr: number;
  part: string;
  price_source: string | null;
}
interface MillState {
  machine: { id: string; name: string; factory: string; learning_days: number };
  provenance: Record<string, string>;
  i: number;
  n: number;
  done: boolean;
  now: string;
  phase: string;
  waiting_for: string | null;
  declined: boolean;
  health: Health | null;
  timeline: Point[];
  events: { i: number; kind: string }[];
  baseline: { version: number; reason: string; status: string; n_rows: number }[];
  case: { id: string; state: string; urgency: string; outcome: string | null } | null;
  steps: Step[];
  approvals: Approval[];
  messages: { kind: string; to: string | null; text: string | null; technician: string | null }[];
  queued: { kind: string; attempts: number; error: string | null }[];
  audit: { seq: number; ts: number; actor: string; event: string; detail: Record<string, unknown> }[];
  audit_ok: boolean;
  sync: { network: string; events_waiting: number; actions_waiting: number; cloud_events: number };
  repairs: { technician: string; outcome: string; before_median: number; after_median: number }[];
  costs: { at_risk_inr: number; unplanned_downtime_hours: number; output_per_hour_inr: number; note: string };
}

const STEP_WORDS: Record<string, string> = {
  recall: "Check this machine's history",
  request_measurement: "Ask for another reading",
  notify_watch: "Tell the owner (daily digest)",
  await_change: "Watch the next readings",
  notify_owner_urgent: "Tell the owner today",
  find_technician: "Find a technician",
  check_parts: "Check likely parts",
  estimate_cost: "Estimate the cost",
  request_approval: "Ask the owner",
  await_approval: "Wait for the owner's answer",
  book_job: "Book the visit",
  await_repair: "Wait for the repair",
  request_post_repair_readings: "Ask for readings after the repair",
  verify_repair: "Check against the original normal",
};

const inr = (n: number | null | undefined) => (n == null ? "–" : `₹${n.toLocaleString("en-IN")}`);

function headline(s: MillState): { text: string; attention: boolean } {
  const name = s.machine.name;
  const h = s.health;
  if (s.case?.outcome === "repair_fixed") return { text: `${name} is back to its normal.`, attention: false };
  if (s.case?.outcome?.startsWith("repair_"))
    return { text: `${name}: the repair did not bring it back.`, attention: true };
  if (!h || h.status === "learning")
    return {
      text: `${name} is learning its normal.`,
      attention: false,
    };
  const words: Record<string, string> = {
    normal: `${name} sounds normal.`,
    watch: `${name} sounds a little different.`,
    inspect: `${name} needs an inspection.`,
    repair_soon: `${name} needs a repair soon.`,
    urgent: `${name} needs attention today.`,
  };
  return {
    text: words[h.status] ?? `${name}: ${h.status}`,
    attention: !["normal", "watch"].includes(h.status),
  };
}

function Chart({ s }: { s: MillState }) {
  const W = 760;
  const H = 220;
  const pad = { l: 34, r: 10, t: 12, b: 22 };
  const pts = s.timeline;
  const n = Math.max(s.n, 1);
  const top = Math.max(4, ...pts.map((p) => p.risk ?? 0));
  const x = (i: number) => pad.l + (i / (n - 1)) * (W - pad.l - pad.r);
  const y = (r: number) =>
    pad.t + (1 - (Math.max(-1, Math.min(top, r)) + 1) / (top + 1)) * (H - pad.t - pad.b);
  const learnEnd = pts.filter((p) => p.phase === "learning").length;
  const off = pts.filter((p) => !p.online);
  const line = pts
    .filter((p) => p.risk != null)
    .map((p, k) => `${k ? "L" : "M"}${x(p.i).toFixed(1)},${y(p.risk as number).toFixed(1)}`)
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="mill-chart"
      role="img"
      aria-label="Risk against this machine's own alarm level"
    >
      {learnEnd > 0 && (
        <rect
          x={x(0)}
          y={pad.t}
          width={x(learnEnd - 1) - x(0)}
          height={H - pad.t - pad.b}
          className="band-learn"
        />
      )}
      {off.length > 0 && (
        <rect
          x={x(off[0].i)}
          y={pad.t}
          width={Math.max(4, x(off[off.length - 1].i) - x(off[0].i))}
          height={H - pad.t - pad.b}
          className="band-off"
        />
      )}
      <line x1={pad.l} x2={W - pad.r} y1={y(1)} y2={y(1)} className="alarm" />
      <text x={pad.l + 4} y={y(1) - 4} className="lbl">
        alarm level (its own normal, 95th pct)
      </text>
      <line x1={pad.l} x2={W - pad.r} y1={y(0)} y2={y(0)} className="zero" />
      {learnEnd > 0 && (
        <text x={x(0) + 4} y={H - pad.b - 6} className="lbl">
          learning ({s.machine.learning_days} days)
        </text>
      )}
      {off.length > 0 && (
        <text x={x(off[0].i) + 4} y={pad.t + 12} className="lbl">
          network cut
        </text>
      )}
      <path d={line} className="risk" />
      {pts
        .filter((p) => p.risk != null)
        .map((p) => (
          <circle
            key={p.i}
            cx={x(p.i)}
            cy={y(p.risk as number)}
            r={2.6}
            className={(p.risk as number) >= 1 ? "hot" : "dot"}
          />
        ))}
      {[0, 1, 2, top].map((v) => (
        <text key={v} x={4} y={y(v) + 4} className="axis">
          {v.toFixed(0)}x
        </text>
      ))}
    </svg>
  );
}

export function MillScreen() {
  const [s, setS] = useState<MillState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = useCallback(async (p: Promise<MillState>) => {
    setBusy(true);
    try {
      setS(await p);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => {
    void run(get<MillState>("/api/mill/state"));
  }, [run]);
  if (!s) return <div className="mill-wrap muted">{err ?? "Loading the flour mill"}</div>;

  const hl = headline(s);
  const steps = s.steps;
  const res = Object.fromEntries(steps.map((x) => [x.name, x.result]));
  const tech = res.find_technician as { name: string; day: string; why: string; source: string } | undefined;
  const cost = res.estimate_cost as
    { inspection: { low_inr: number; high_inr: number; basis: string }; if_found: Cost[] } | undefined;
  const pending = s.approvals.filter((a) => a.status === "requested");
  const verify = res.verify_repair as
    { outcome: string; before_median_risk: number; after_median_risk: number; rule: string } | undefined;
  const learnDay = s.timeline.filter((p) => p.phase === "learning").length;
  const online = s.sync.network === "online";

  return (
    <div className="mill-wrap">
      <header className="mill-top">
        <div>
          <div className="small muted">
            {s.machine.factory} · flour mill · {s.now.replace("T", " ")}
          </div>
          <h1 className={hl.attention ? "accent" : ""}>{hl.text}</h1>
          {s.health?.status === "learning" && (
            <div className="muted">
              Learning: reading {learnDay} of the first {s.machine.learning_days} days. No alarms while it
              learns.
            </div>
          )}
        </div>
        <div className="mill-nums">
          <div>
            <div className="big mono">{s.health ? s.health.confidence : "–"}</div>
            <div className="small muted">how sure</div>
          </div>
          <div>
            <div className={`big mono ${hl.attention ? "accent" : ""}`}>{inr(s.costs.at_risk_inr)}</div>
            <div className="small muted">
              output at risk if it breaks ({s.costs.unplanned_downtime_hours} h, assumption)
            </div>
          </div>
          <div>
            <div className="big mono">
              {!s.health?.next_inspection
                ? "–"
                : s.health.next_inspection <= s.now.slice(0, 10)
                  ? "today"
                  : s.health.next_inspection}
            </div>
            <div className="small muted">next check</div>
          </div>
        </div>
      </header>

      <div className="mill-controls row gap">
        <button
          disabled={busy || s.done || !!s.waiting_for}
          onClick={() => void run(post("/api/mill/step", { n: 1 }))}
        >
          Next reading
        </button>
        <button
          disabled={busy || s.done || !!s.waiting_for}
          onClick={() => void run(post("/api/mill/step", { n: 60 }))}
        >
          Play to the next decision
        </button>
        <button disabled={busy} onClick={() => void run(post("/api/mill/reset"))}>
          Start again
        </button>
        <span className={`pill ${online ? "" : "pill-off"}`}>
          {online ? "Online" : "Offline"} · {s.sync.events_waiting} events and {s.sync.actions_waiting}{" "}
          messages waiting to sync
        </span>
        <button disabled={busy} onClick={() => void run(post("/api/mill/network", { online: !online }))}>
          {online ? "Cut the network" : "Restore the network"}
        </button>
        <span className="small muted mono">
          reading {Math.min(s.i, s.n)} of {s.n}
        </span>
        {err && <span className="small accent">{err}</span>}
      </div>

      <main className="mill-grid">
        <section className="mill-main">
          <div className="card">
            <div className="row between">
              <h2>What changed</h2>
              <span className="small muted">risk = this reading against the chakki's own learned normal</span>
            </div>
            <Chart s={s} />
            <ul className="evidence">
              {(s.health?.evidence ?? []).map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
            {s.health && s.health.confidence_reasons.length > 0 && (
              <div className="small muted">Less sure because: {s.health.confidence_reasons.join("; ")}</div>
            )}
            <div className="small muted">{s.health?.note}</div>
            <div className="row gap audio">
              {(["before", "now", "after"] as const).map((k) => (
                <label key={k} className="small muted">
                  {k === "before" ? "While learning" : k === "now" ? "Recorded fault" : "After repair"}
                  <audio controls preload="none" src={`/api/mill/audio/${k}.wav`} />
                </label>
              ))}
            </div>
          </div>

          <div className="card">
            <h2>What Dhadkan did</h2>
            <ol className="steps">
              {steps.map((x) => (
                <li key={x.idx} className={`st-${x.status}`}>
                  <span>{STEP_WORDS[x.name] ?? x.name}</span>
                  <span className="mono small">
                    {x.status}
                    {x.attempts > 0 ? ` · ${x.attempts} retries` : ""}
                  </span>
                  {x.error && x.status !== "done" && <div className="small muted">{x.error}</div>}
                </li>
              ))}
              {steps.length === 0 && <li className="muted">No case open. Nothing to do.</li>}
            </ol>
          </div>
        </section>

        <aside className="mill-side">
          {pending.map((a) => (
            <div key={a.id} className="card attention">
              <h2>{a.kind === "production_stop" ? "Your decision" : "Approve this visit?"}</h2>
              <p>{a.summary}</p>
              {a.kind === "production_stop" ? (
                <div className="small muted">
                  Dhadkan cannot stop a machine. Your answer is only recorded.
                </div>
              ) : null}
              <div className="row gap">
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() =>
                    void run(post("/api/mill/decide", { approval_id: a.id, decision: "approved" }))
                  }
                >
                  {a.kind === "production_stop" ? "I stopped it" : "Yes, book it"}
                </button>
                <button
                  disabled={busy}
                  onClick={() =>
                    void run(post("/api/mill/decide", { approval_id: a.id, decision: "rejected" }))
                  }
                >
                  {a.kind === "production_stop" ? "Keep running" : "Not now"}
                </button>
              </div>
            </div>
          ))}
          {s.waiting_for && pending.length === 0 && (
            <div className="card small muted">Waiting for {s.waiting_for}.</div>
          )}

          {(tech || cost) && (
            <div className="card">
              <h2>Repair options</h2>
              {tech && (
                <p>
                  {tech.name}, {tech.day}. <span className="muted">{tech.why}.</span>
                  <br />
                  <span className="small muted">{tech.source}</span>
                </p>
              )}
              {cost && (
                <table className="costs small">
                  <tbody>
                    <tr>
                      <td>Inspection visit</td>
                      <td className="mono">
                        {inr(cost.inspection.low_inr)}–{inr(cost.inspection.high_inr)}
                      </td>
                    </tr>
                    {cost.if_found.map((c) => (
                      <tr key={c.fault}>
                        <td>
                          If it is {c.fault.replace("_", " ")}: {c.part}
                        </td>
                        <td className="mono">
                          {c.price_source ? (
                            <a href={c.price_source} target="_blank" rel="noreferrer">
                              {inr(c.low_inr)}–{inr(c.high_inr)}
                            </a>
                          ) : (
                            `${inr(c.low_inr)}–${inr(c.high_inr)}`
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="small muted">
                Typical prices from cited listings, confirm with dealer. Labour rate is an assumption.
              </div>
            </div>
          )}

          {verify && (
            <div className="card">
              <h2>After the repair</h2>
              <p className={verify.outcome === "fixed" ? "" : "accent"}>
                {verify.outcome === "fixed"
                  ? "Back to its original normal."
                  : verify.outcome === "partly"
                    ? "Better, not back to normal."
                    : "Not better. Possibly a wrong or poor part."}
              </p>
              <div className="small mono muted">
                median risk {verify.before_median_risk} → {verify.after_median_risk} against baseline v1
              </div>
            </div>
          )}

          <div className="card">
            <h2>Messages</h2>
            <ul className="msgs small">
              {s.messages.slice(-6).map((m, k) => (
                <li key={k}>
                  <span className="muted mono">
                    {m.kind === "book_job" ? `job → ${m.technician}` : `→ ${m.to}`}
                  </span>{" "}
                  {m.text ?? ""}
                </li>
              ))}
              {s.queued.map((q, k) => (
                <li key={`q${k}`} className="muted">
                  queued: {q.kind} ({q.error ?? "waiting"})
                </li>
              ))}
              {s.messages.length + s.queued.length === 0 && <li className="muted">None yet.</li>}
            </ul>
            <div className="small muted">
              WhatsApp is not connected in this demo: messages are recorded, not sent.
            </div>
          </div>

          <div className="card">
            <h2>Record</h2>
            <div className="small muted">
              Baseline:{" "}
              {s.baseline
                .map((b) => `v${b.version} (${b.reason}, ${b.n_rows} readings, ${b.status})`)
                .join("; ") || "not learned yet"}
            </div>
            <div className="small muted">
              Audit log {s.audit_ok ? "intact" : "BROKEN"} · {s.sync.cloud_events} events copied to the cloud
            </div>
            <ul className="audit mono small">
              {s.audit.slice(-8).map((e) => (
                <li key={e.seq}>
                  {e.seq} {e.actor} {e.event}
                </li>
              ))}
            </ul>
          </div>

          <div className="card small muted">
            {Object.entries(s.provenance).map(([k, v]) => (
              <div key={k}>{v}</div>
            ))}
          </div>
        </aside>
      </main>
    </div>
  );
}
