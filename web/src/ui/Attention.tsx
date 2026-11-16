// Left column: every machine, the ones that need attention first. Click a row to fly to it.
import type { Alert, Machine, Message, Meta, State } from "../api";
import { inr } from "../theme";
import { HealthRing } from "./bits";
import { alertWord } from "./Diagnosis";

const RANK: Record<string, number> = { alarm: 0, early_match: 0, watch: 1, ok: 2, learning: 2, no_data: 3 };
const FAULT: Record<string, string> = {
  bearing: "bearing",
  imbalance: "imbalance",
  loose_belt: "loose belt",
  clog: "clogging",
};

/** One line, the way the mechanic would put it. */
export function shortReason(m: Machine) {
  const fault =
    m.fault_name?.en.toLowerCase() ?? (m.known_fault ? (FAULT[m.known_fault] ?? m.known_fault) : null);
  const pct = Math.round((m.risk3 ?? 0) * 100);
  switch (m.status) {
    case "alarm":
      return fault ? `Past its alarm, ${fault} sound` : `Past its alarm (${pct}%)`;
    case "early_match":
      return `Sounds like ${m.registry?.from_area ?? "another shed"}'s ${(m.registry?.fault ?? "fault").replace("_", " ")} fault`;
    case "watch":
      return m.days_rising > 1 ? `Near its alarm, worse ${m.days_rising} days` : `Near its alarm (${pct}%)`;
    case "learning":
      return `Learning its normal · day ${m.learning?.day ?? "?"} of ${m.learning?.of ?? "?"}`;
    case "no_data":
      return "No reading yet";
    default:
      return "Running normally";
  }
}

export function daysShort(m: Machine) {
  const f = m.forecast;
  if (!f) return "";
  if (f.status === "estimate") return f.high == null ? `>${f.low} d` : `${f.low}–${f.high} d`;
  if (f.status === "at_fault_level") return "now";
  return "";
}

export function sortByAttention(ms: Machine[]) {
  return [...ms].sort(
    (a, b) =>
      RANK[a.status] - RANK[b.status] ||
      b.rupees_at_risk - a.rupees_at_risk ||
      (b.risk3 ?? 0) - (a.risk3 ?? 0),
  );
}

function Row({
  m,
  meta,
  open,
  alert,
  on,
  hover,
  onPick,
  onHover,
}: {
  m: Machine;
  meta: Meta;
  open: Message | undefined;
  alert: Alert | undefined;
  on: boolean;
  hover: boolean;
  onPick: () => void;
  onHover: (on: boolean) => void;
}) {
  const f = meta.factories.find((x) => x.id === m.factory);
  const hot = m.status === "alarm" || m.status === "early_match";
  return (
    <li>
      <button
        className={`arow ${hot ? "hot" : ""} ${on ? "on" : ""} ${hover ? "hover" : ""}`}
        onClick={onPick}
        onPointerEnter={() => onHover(true)}
        onPointerLeave={() => onHover(false)}
      >
        <HealthRing m={m} size={30} />
        <span className="arow-main">
          <span className="arow-name">
            <b>{m.name}</b> <span className="muted">{f?.name.split(" ")[0]}</span>
          </span>
          <span className="arow-why">{shortReason(m)}</span>
          {alert ? (
            <span className="arow-sms mono">
              WhatsApp {alertWord(alert)}
              {alert.choice ? ` · replied ${alert.reply}` : ""}
            </span>
          ) : (
            open && <span className="arow-sms mono">owner message ready · no reply yet</span>
          )}
        </span>
        <span className="arow-num mono">
          <span className={hot ? "accent" : ""}>{daysShort(m)}</span>
          {m.rupees_at_risk > 0 && <span className="accent">{inr(m.rupees_at_risk)}</span>}
        </span>
      </button>
    </li>
  );
}

export function Attention({
  state,
  meta,
  machines,
  selected,
  hover,
  onPick,
  onHover,
}: {
  state: State;
  meta: Meta;
  machines: Machine[];
  selected: string | null;
  hover: string | null;
  onPick: (id: string) => void;
  onHover: (id: string | null) => void;
}) {
  const sorted = sortByAttention(machines);
  const quiet = (m: Machine) => m.status === "ok" || m.status === "no_data" || m.status === "learning";
  const need = sorted.filter((m) => !quiet(m));
  const fine = sorted.filter(quiet);
  const openFor = (id: string) => state.messages.find((x) => x.machine === id && x.approved === null);
  const row = (m: Machine) => (
    <Row
      key={m.id}
      m={m}
      meta={meta}
      open={openFor(m.id)}
      alert={state.alerts.find((a) => a.machine === m.id && a.kind === "alert")}
      on={m.id === selected}
      hover={m.id === hover}
      onPick={() => onPick(m.id)}
      onHover={(on) => onHover(on ? m.id : null)}
    />
  );
  return (
    <aside className="attention" aria-label="needs attention">
      <h2 className="col-head">
        Needs attention <span className="mono muted">{need.length}</span>
      </h2>
      {state.tick < 0 ? (
        <p className="small muted pad">
          No readings yet. Press <b>D</b> (Demo replay): it records 14 days, then plays live with a fault on{" "}
          {meta.fault_machine} at 0:40.
        </p>
      ) : need.length === 0 ? (
        <p className="small muted pad">Nothing needs attention. Every machine sounds like its normal self.</p>
      ) : (
        <ul className="alist">{need.map(row)}</ul>
      )}
      {fine.length > 0 && (
        <details className="fine">
          <summary className="col-sub">
            Running normally <span className="mono muted">{fine.length}</span>
          </summary>
          <ul className="alist">{fine.map(row)}</ul>
        </details>
      )}
      <div className="col-foot small muted">
        Savings reports:{" "}
        {meta.factories.map((f, i) => (
          <span key={f.id}>
            {i > 0 && " · "}
            <a href={`/api/report/${f.id}.pdf`} target="_blank" rel="noreferrer" title={`${f.name} (PDF)`}>
              {f.id}
            </a>
          </span>
        ))}
        {!state.link.up && (
          <div className="accent">Shed offline: {state.link.pending} fingerprint(s) waiting</div>
        )}
      </div>
    </aside>
  );
}
