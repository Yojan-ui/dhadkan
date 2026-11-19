// The planner's trace for one case: event -> reasoning -> tool calls -> approval -> outcome, with tokens and
// rupees. Used by the command centre (AI Agents page) and the twin's agent dock. Data: server/brain.planner_trace.
import { inr } from "../theme";
import { ts, type PlannerCase, type PlannerTrace } from "./data";

export const rupees = (v: number) => (v > 0 && v < 1 ? `₹${v.toFixed(2)}` : inr(v));

const EVENT_WORD: Record<string, string> = {
  alarm: "alarm",
  health_change: "health changed",
  owner_reply: "owner replied",
  owner_voice_note: "voice note",
  mechanic_reply: "mechanic replied",
  mechanic_no_show: "mechanic did not come",
  part_out_of_stock: "part out of stock",
  network_restored: "network back",
  repair_done: "repair done",
  verify_result: "repair verified",
  daily_tick: "new day",
};

export function engineWord(engine: string, model: string | null) {
  if (engine.startsWith("claude")) return model ?? "Claude";
  return "rules (fixed plan)";
}

export function TraceCase({ c, compact = false }: { c: PlannerCase; compact?: boolean }) {
  const steps = compact ? c.steps.slice(-4) : c.steps;
  return (
    <div className="cc-trace">
      <div className="row between">
        <b className="small">
          {c.case}{" "}
          {c.machine && c.machine !== c.case ? <span className="mono tiny muted">{c.machine}</span> : null}
        </b>
        <span className="tiny muted mono">
          {c.state ?? "—"} · {c.driver === "planner" ? "planner" : "fixed plan"} ·{" "}
          {c.tokens.toLocaleString("en-IN")} tokens · {rupees(c.cost_inr)}
        </span>
      </div>
      <ol className="cc-timeline">
        {steps.map((s) => (
          <li key={s.decision} className={`st-${s.status === "done" ? "completed" : s.status}`}>
            <span className="mono tiny muted">{ts(s.at)}</span>
            <b className="small">{EVENT_WORD[s.event] ?? s.event}</b>
            {s.said && <span className="small">“{s.said}”</span>}
            <span className={`st st-${s.status === "done" ? "completed" : "failed"}`}>
              {s.status.replace("_", " ")}
            </span>
            <span className="tiny muted mono">
              {engineWord(s.engine, s.model)} · {(s.input_tokens + s.output_tokens).toLocaleString("en-IN")}{" "}
              tok · {rupees(s.cost_inr)}
              {s.latency_ms != null ? ` · ${s.latency_ms} ms` : ""}
            </span>
            {s.reasoning && !compact && <div className="tiny muted">{s.reasoning}</div>}
            {s.tools.length > 0 && (
              <div className="tiny mono">
                {s.tools.map((t, i) => (
                  <div key={i} className={t.ok ? "" : "warn"}>
                    → {t.tool}
                    {t.summary ? `: ${t.summary}` : ""}
                  </div>
                ))}
              </div>
            )}
            {!s.grounding_ok && (
              <div className="tiny warn">reply rejected: a number no tool returned (rules reply sent)</div>
            )}
            {s.outcome && <div className="tiny">{s.outcome}</div>}
          </li>
        ))}
      </ol>
      {c.approvals.length > 0 && (
        <div className="tiny mono">
          approvals:{" "}
          {c.approvals
            .map(
              (a) =>
                `${a.kind} ${a.status}${a.decided_by ? ` by ${a.decided_by}` : ""}${a.channel ? ` (${a.channel})` : ""}`,
            )
            .join("; ")}
        </div>
      )}
      {!compact && c.actions.length > 0 && (
        <div className="tiny muted mono">
          actions: {c.actions.map((a) => `${a.kind} ${a.status}`).join("; ")}
        </div>
      )}
    </div>
  );
}

export function TraceList({ t }: { t: PlannerTrace | undefined }) {
  if (!t) return <p className="small muted">No planner yet.</p>;
  const cases = t.cases.filter((c) => c.case !== "-");
  return (
    <>
      <p className="tiny muted">
        {t.note}. Models:{" "}
        {Object.entries(t.models)
          .map(([k, v]) => `${k} ${v}`)
          .join(" · ")}
        {t.pending_events ? ` · ${t.pending_events} events waiting` : ""}
      </p>
      {cases.map((c) => (
        <TraceCase key={c.case} c={c} />
      ))}
      {cases.length === 0 && <p className="small muted">No case has reached the planner yet.</p>}
    </>
  );
}
