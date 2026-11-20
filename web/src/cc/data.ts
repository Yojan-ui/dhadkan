// Command centre data: typed contract of /api/cc/* (server/brain.py, server/cc_api.py) and one hook that
// refetches when the live WebSocket reports a new version (and every 5 s as a fallback).
import { useEffect, useRef, useState } from "react";
import { get, type Machine, type State, type Status } from "../api";

export type Health =
  "normal" | "watch" | "inspect" | "repair_soon" | "urgent" | "learning" | "no_signal" | "not_running";
export type Urgency =
  "none" | "monitor" | "measure_again" | "inspect" | "maintenance" | "urgent_owner_attention";

export interface Row {
  id: string;
  name: string;
  factory: string;
  type: string;
  shape: string;
  sound_risk: number | null;
  temperature_c: null;
  vibration: null;
  rpm: number | null;
  rpm_basis: string;
  health: Health;
  urgency: Urgency | null;
  confidence: string | null;
  last_ts: string | null;
  run: "running" | "stopped";
  sensor: "ok" | "lost";
  source: string | null;
  case: string | null;
  /** fed by the live recorder, not the simulator */
  live?: CcLive | null;
}
export interface AgentRun {
  id: number;
  agent: string;
  task: string;
  case_id: string | null;
  machine: string | null;
  status: string;
  observation: string | null;
  reasoning: string | null;
  tools: string[];
  result: unknown;
  started: number;
  updated: number;
  ended: number | null;
  retries: number;
  error: string | null;
}
export interface AgentBoard {
  agent: string;
  name: string;
  code: string;
  open_runs: number;
  status: string;
  last: AgentRun | null;
}
export interface Plan {
  priority: number;
  of: number;
  earliest_day: string;
  window: string;
  expected_downtime_h: number;
  downtime_basis: string;
  combine_with: string[];
  alternatives: string[];
}
export interface Recommendation {
  machine: string;
  name: string;
  condition: Health;
  severity: string;
  evidence: string[];
  action: string;
  urgency: Urgency;
  confidence: string;
  confidence_reasons: string[];
  approval_required: string[];
  plan: Plan | null;
  case: string | null;
  by: string;
  note: string;
}
export interface Stage {
  stage: string;
  status: string;
  at: number | null;
  detail: string | null;
  error?: string | null;
}
export interface Workflow {
  case: string;
  machine: string;
  urgency: Urgency;
  state: string;
  technician: string | null;
  outcome: string | null;
  opened: number;
  closed: number | null;
  stages: Stage[];
}
export interface Approval {
  id: string;
  case_id: string;
  kind: string;
  summary: string;
  amount_low: number | null;
  amount_high: number | null;
  status: string;
  requested: number;
  decided: number | null;
  decided_by: string | null;
  channel: string | null;
}
export interface Perf {
  per_machine: {
    machine: string;
    ticks: number;
    running_share: number;
    downtime_h: number;
    no_signal_h: number;
    monitored_share: number;
    flagged_share: number;
    repairs: number;
    cases: number;
  }[];
  trend: { day: string; running_share: number; flagged_share: number; downtime_h: number }[];
  tick_hours: number;
  note: string;
}
export interface Kpis {
  machines: number;
  healthy: number;
  watch: number;
  alerts: number;
  learning: number;
  offline: number;
  factory_health: string;
  agents_active: number;
  pending_approvals: number;
  open_cases: number;
  link_up: boolean;
  sync: {
    online: boolean;
    last_sync: string | null;
    last_error: string | null;
    events_waiting: number;
    actions_waiting: number;
  };
  mode: string;
}
export interface CCState {
  version: number;
  clock: string;
  kpis: Kpis;
  machines: Row[];
  recommendations: Recommendation[];
  agents: AgentBoard[];
  timeline: AgentRun[];
  workflows: Workflow[];
  approvals: Approval[];
  performance: Perf;
  messages: { kind: string; to?: string; text?: string; technician?: string; day?: string; idem: string }[];
  queued: { kind: string; attempts: number; error: string | null }[];
  whatsapp?: {
    mode: "off" | "not_configured" | "dry_run" | "live";
    messages: {
      id: number;
      dir: "out" | "in";
      role: string;
      phone: string | null;
      text: string;
      status: string;
    }[];
  };
  audit_ok: boolean;
  cloud_events: number;
  planner?: PlannerTrace;
  last_tick_ms: number | null;
}
// The planner's trace (server/brain.planner_trace): per case, event -> reasoning -> tools -> approvals.
export interface CcLive {
  quick: boolean;
  label: string | null;
  state: "learning" | "monitoring";
  progress: { clips: number; target: number };
  readings: number;
}
export interface PlannerTool {
  tool: string;
  ok: boolean;
  input: Record<string, unknown>;
  summary: string;
}
export interface PlannerStep {
  decision: number;
  at: number;
  event: string;
  source: string | null;
  said: string;
  engine: string;
  model: string | null;
  status: string;
  reasoning: string;
  tools: PlannerTool[];
  reply: string | null;
  outcome: string | null;
  grounding_ok: boolean;
  knowledge?: string[];
  observed?: string[];
  /** an investigation_result step: what was asked, the owner's answer, the confidence before and after */
  investigation?: {
    kind: string | null;
    asked: string | null;
    answered: string | null;
    confidence_before: number | null;
    confidence_after: number | null;
  } | null;
  guardian?: { target: string; ok: boolean; reasons: string[] }[];
  a2a?: { partner: string; question: string; said: string }[];
  input_tokens: number;
  output_tokens: number;
  cost_inr: number;
  latency_ms: number | null;
}
export interface PlannerCase {
  case: string;
  machine: string | null;
  state: string | null;
  driver: string | null;
  approvals: {
    id: string;
    kind: string;
    status: string;
    decided_by: string | null;
    channel: string | null;
  }[];
  actions: { kind: string; status: string; idem: string }[];
  steps: PlannerStep[];
  tokens: number;
  cost_inr: number;
}
export interface PlannerTrace {
  llm: boolean;
  models: Record<string, string>;
  pending_events: number;
  cases: PlannerCase[];
  note: string;
}
export interface Detail {
  machine: Row;
  health: {
    status: Health;
    urgency: Urgency;
    severity: string;
    trend: string;
    confidence: string;
    confidence_reasons: string[];
    evidence: string[];
    next_inspection: string | null;
    recent_risk: number[];
    time_to_threshold: {
      status: string;
      low?: number;
      mid?: number;
      high?: number;
      meaning: string;
      reason?: string;
    } | null;
    note: string;
  } | null;
  trend: { ts: string; risk: number | null; excluded: string | null; clean: number }[];
  baselines: {
    version: number;
    reason: string;
    status: string;
    n_rows: number;
    approval_ref: string | null;
  }[];
  cases: {
    id: string;
    urgency: string;
    state: string;
    outcome: string | null;
    opened: number;
    closed: number | null;
  }[];
  repairs: {
    technician: string;
    work: string;
    done: number;
    outcome: string;
    before_median: number;
    after_median: number;
  }[];
  agent_runs: AgentRun[];
}

export function useCC(live: State | null) {
  const [s, setS] = useState<CCState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const last = useRef(0);
  const v = live?.version ?? -1;
  useEffect(() => {
    let on = true;
    const load = () =>
      get<CCState>("/api/cc/state")
        .then((x) => on && (setS(x), setErr(null)))
        .catch((e: unknown) => on && setErr(String(e)));
    const now = Date.now();
    const wait = Math.max(0, 700 - (now - last.current)); // at most ~1.4 fetches a second
    const t = setTimeout(() => {
      last.current = Date.now();
      void load();
    }, wait);
    const iv = setInterval(load, 5000);
    return () => {
      on = false;
      clearTimeout(t);
      clearInterval(iv);
    };
  }, [v]);
  return { s, err, reload: () => get<CCState>("/api/cc/state").then(setS) };
}

/** The 3D twin reads Machine.status; in the command centre it shows the agents' health instead. */
const TO_STATUS: Record<Health, Status> = {
  normal: "ok",
  watch: "watch",
  inspect: "alarm",
  repair_soon: "alarm",
  urgent: "alarm",
  learning: "learning",
  no_signal: "no_data",
  not_running: "no_data",
};
export function withHealth(ms: Machine[], rows: Row[] | undefined): Machine[] {
  if (!rows) return ms;
  const by = Object.fromEntries(rows.map((r) => [r.id, r]));
  return ms.map((m) => {
    const r = by[m.id];
    if (!r) return m;
    const st = TO_STATUS[r.health];
    return { ...m, status: st === "no_data" && m.latest ? m.status : st, risk3: r.sound_risk ?? m.risk3 };
  });
}

export const HEALTH_WORD: Record<Health, string> = {
  normal: "Normal",
  watch: "Watch",
  inspect: "Inspect",
  repair_soon: "Repair soon",
  urgent: "Urgent",
  learning: "Learning",
  no_signal: "No signal",
  not_running: "Not running",
};
export const healthClass = (h: Health | string) =>
  h === "normal"
    ? "ok"
    : h === "watch"
      ? "warn"
      : ["inspect", "repair_soon", "urgent"].includes(h)
        ? "crit"
        : "dim";
export const ts = (sec: number | null | undefined) =>
  sec
    ? new Date(sec * 1000).toLocaleString("en-GB", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "–";
