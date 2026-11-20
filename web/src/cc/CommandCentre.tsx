// Dhadkan command centre (default page): top bar, icon sidebar, telemetry table, the 3D digital twin,
// intelligence panels and workflow charts. Every number comes from /api/cc/state or the live WebSocket.
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { post, useLive, useMeta } from "../api";
import type { View } from "../scene/CameraRig";
import { useReducedMotion } from "../theme";
import { useCC, withHealth, type CCState } from "./data";
import { MachineDetail } from "./MachineDetail";
import {
  AgentsPage,
  AnalyticsPage,
  EventsPage,
  JobsPage,
  MachinesPage,
  PartsPage,
  PredictPage,
  SettingsPage,
  TechsPage,
  TelemetryPage,
} from "./pages";
import { Intelligence, PerformanceChart, PredictChart, TelemetryTable, WorkflowStrip } from "./panels";
import { Icon } from "./ui";
import "./cc.css";

const Scene = lazy(() => import("../scene/Scene"));

export const SECTIONS = [
  ["overview", "Overview"],
  ["twin", "Digital Twin"],
  ["machines", "Machines"],
  ["telemetry", "Live Telemetry"],
  ["predict", "Predictive Maintenance"],
  ["agents", "AI Agents"],
  ["jobs", "Maintenance Jobs"],
  ["techs", "Technicians"],
  ["parts", "Spare Parts"],
  ["analytics", "Analytics"],
  ["events", "Event History"],
  ["settings", "Settings"],
] as const;
export type Section = (typeof SECTIONS)[number][0];

function readHash(): Section {
  const h = location.hash.replace("#", "") as Section;
  return SECTIONS.some(([k]) => k === h) ? h : "overview";
}

export function CommandCentre() {
  const live = useLive();
  const meta = useMeta();
  const { s, err, reload } = useCC(live);
  const reduced = useReducedMotion();
  const [section, setSection] = useState<Section>(readHash);
  const [view, setView] = useState<View>({ kind: "cluster" });
  const [hover, setHover] = useState<string | null>(null);
  const [factory, setFactory] = useState<string>("all");
  const [autoFocus, setAutoFocus] = useState(false);
  const [playing, setPlaying] = useState(false);
  useEffect(() => {
    const on = () => setSection(readHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const go = (k: Section) => {
    location.hash = k;
    setSection(k);
  };
  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => void post("/api/step", { n: 1 }), 2500);
    return () => clearInterval(t);
  }, [playing]);
  // optional: fly to a machine the moment it becomes urgent
  const seen = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!s) return;
    for (const m of s.machines) {
      const hot = m.health === "urgent" || m.health === "repair_soon";
      if (hot && !seen.current.has(m.id)) {
        seen.current.add(m.id);
        if (autoFocus) setView({ kind: "machine", id: m.id });
      }
      if (!hot) seen.current.delete(m.id);
    }
  }, [s, autoFocus]);

  const machines = useMemo(() => withHealth(live?.machines ?? [], s?.machines), [live, s]);
  const selected = view.kind === "machine" ? view.id : null;
  const pickFactory = (f: string) => {
    setFactory(f);
    setView(f === "all" ? { kind: "cluster" } : { kind: "shed", factory: f });
  };
  const select = (id: string) => setView({ kind: "machine", id });

  if (!live || !meta || !s)
    return (
      <div className="cc cc-loading mono">
        {err ? `Cannot reach the server: ${err}` : "Connecting to the factory node"}
      </div>
    );

  const twin = (
    <div className="cc-twin">
      <Suspense fallback={<div className="cc-loading">Loading the 3D factory</div>}>
        <Scene
          state={live}
          machines={machines}
          meta={meta}
          view={view}
          reduced={reduced}
          hover={hover}
          onHover={setHover}
          onView={setView}
        />
      </Suspense>
      <div className="cc-twin-tools">
        <label className="small">
          <input type="checkbox" checked={autoFocus} onChange={(e) => setAutoFocus(e.target.checked)} />{" "}
          auto-focus new urgent machines
        </label>
      </div>
    </div>
  );
  const right = selected ? (
    <MachineDetail id={selected} s={s} onClose={() => setView({ kind: "cluster" })} onChanged={reload} />
  ) : (
    <Intelligence s={s} onPick={select} onChanged={reload} />
  );

  return (
    <div className="cc">
      <TopBar
        s={s}
        live={live.link}
        factories={meta.factories.map((f) => ({ id: f.id, name: f.name }))}
        factory={factory}
        onFactory={pickFactory}
        playing={playing}
        onPlay={setPlaying}
        onGo={go}
      />
      <nav className="cc-nav" aria-label="sections">
        {SECTIONS.map(([k, label]) => (
          <button
            key={k}
            className={section === k ? "on" : ""}
            onClick={() => go(k)}
            title={label}
            aria-label={label}
          >
            <Icon name={k} />
            <span className="cc-nav-l">{label}</span>
          </button>
        ))}
      </nav>
      <main className={`cc-main cc-sec-${section}`}>
        {section === "overview" && (
          <div className="cc-grid">
            <div className="cc-left">
              <TelemetryTable s={s} factory={factory} selected={selected} onPick={select} compact />
            </div>
            <div className="cc-centre">{twin}</div>
            <div className="cc-right">{right}</div>
            <div className="cc-bottom">
              <PerformanceChart s={s} />
              <PredictChart s={s} selected={selected} />
              <WorkflowStrip s={s} onPick={select} />
            </div>
          </div>
        )}
        {section === "twin" && (
          <div className="cc-grid cc-grid-twin">
            <div className="cc-centre">{twin}</div>
            <div className="cc-right">{right}</div>
          </div>
        )}
        {section === "machines" && (
          <MachinesPage
            s={s}
            onPick={select}
            selected={selected}
            onChanged={reload}
            onClose={() => setView({ kind: "cluster" })}
          />
        )}
        {section === "telemetry" && (
          <TelemetryPage s={s} factory={factory} selected={selected} onPick={select} />
        )}
        {section === "predict" && <PredictPage s={s} onPick={select} />}
        {section === "agents" && <AgentsPage s={s} />}
        {section === "jobs" && <JobsPage s={s} onChanged={reload} />}
        {section === "techs" && <TechsPage />}
        {section === "parts" && <PartsPage />}
        {section === "analytics" && <AnalyticsPage s={s} />}
        {section === "events" && <EventsPage />}
        {section === "settings" && <SettingsPage s={s} />}
      </main>
    </div>
  );
}

function TopBar({
  s,
  live,
  factories,
  factory,
  onFactory,
  playing,
  onPlay,
  onGo,
}: {
  s: CCState;
  live: { up: boolean; pending: number };
  factories: { id: string; name: string }[];
  factory: string;
  onFactory: (f: string) => void;
  playing: boolean;
  onPlay: (on: boolean) => void;
  onGo: (k: Section) => void;
}) {
  const k = s.kpis;
  const [bell, setBell] = useState(false);
  const pending = s.approvals.filter((a) => a.status === "requested");
  return (
    <header className="cc-top">
      <div className="cc-logo">
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden>
          <path
            d="M2 13h5l2-6 4 12 3-9 2 3h4"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <b>DHADKAN</b>
        <span className="muted small">command centre</span>
      </div>
      <select
        className="cc-sel"
        value={factory}
        onChange={(e) => onFactory(e.target.value)}
        aria-label="factory"
      >
        <option value="all">All factories ({factories.length})</option>
        {factories.map((f) => (
          <option key={f.id} value={f.id}>
            {f.id} · {f.name}
          </option>
        ))}
      </select>
      <div className="cc-kpis">
        <Kpi label="status" value={k.factory_health} />
        <Kpi label="machines" value={k.machines} />
        <Kpi label="healthy" value={k.healthy} cls="ok" />
        <Kpi label="watch" value={k.watch} cls={k.watch ? "warn" : ""} />
        <Kpi label="alerts" value={k.alerts} cls={k.alerts ? "crit" : ""} />
        {k.offline > 0 && <Kpi label="stopped / no signal" value={k.offline} cls="dim" />}
        <Kpi label="agents active" value={k.agents_active} cls="cy" />
        <button
          className={`cc-link ${k.link_up ? "ok" : "crit"}`}
          onClick={() => void post("/api/link", { up: !k.link_up })}
          title="cloud link (switch to simulate a cut)"
        >
          <Icon name="link" size={15} />
          {k.link_up ? "online" : "offline"}
          <span className="muted"> · {k.sync.events_waiting + live.pending} waiting</span>
        </button>
      </div>
      <div className="cc-top-r">
        <button
          className={`cc-mode ${playing ? "on" : ""}`}
          onClick={() => onPlay(!playing)}
          title="advance the simulated clock: one reading every 2.5 s"
        >
          DEMO · {playing ? "pause" : "play"} readings
        </button>
        <button
          className="cc-ico"
          onClick={() => void post("/api/step", { n: 1 })}
          title="next reading (simulated clock)"
        >
          +1
        </button>
        <span className="mono small muted">{s.clock.replace("T", " ")}</span>
        <div className="cc-bell">
          <button className="cc-ico" onClick={() => setBell(!bell)} aria-label="notifications">
            <Icon name="bell" size={16} />
            {pending.length > 0 && <span className="cc-badge">{pending.length}</span>}
          </button>
          {bell && (
            <div className="cc-pop">
              <b className="small">Waiting for the owner</b>
              {pending.length === 0 && <div className="small muted">Nothing to decide.</div>}
              {pending.map((a) => (
                <button
                  key={a.id}
                  className="cc-pop-row small"
                  onClick={() => (onGo("jobs"), setBell(false))}
                >
                  {a.kind.replace("_", " ")}: {a.summary.slice(0, 90)}
                </button>
              ))}
              <b className="small">WhatsApp · {WA_MODE[s.whatsapp?.mode ?? "off"]}</b>
              {(s.whatsapp?.messages ?? []).length === 0 && (
                <div className="small muted">No messages yet.</div>
              )}
              {(s.whatsapp?.messages ?? []).slice(0, 5).map((m) => (
                <div key={m.id} className="small muted">
                  {m.dir === "in" ? "← " : "→ "}
                  {m.role} {m.phone ?? ""} · {m.status}:{" "}
                  {m.text.replace(/^Dhadkan · [^\n]*\n/, "").slice(0, 80)}
                </div>
              ))}
            </div>
          )}
        </div>
        <User />
      </div>
    </header>
  );
}

const WA_MODE: Record<string, string> = {
  off: "off (set DHADKAN_CC_WHATSAPP=1)",
  not_configured: "no Twilio keys in .env",
  dry_run: "dry run (DHADKAN_DRY_RUN=1)",
  live: "sending",
};

function Kpi({ label, value, cls = "" }: { label: string; value: string | number; cls?: string }) {
  return (
    <div className="cc-kpi">
      <span className={`v mono ${cls}`}>{value}</span>
      <span className="l">{label}</span>
    </div>
  );
}

function User() {
  const [me, setMe] = useState<{ role: string; demo: boolean; name: string } | null>(null);
  useEffect(() => {
    fetch("/api/cc/me")
      .then((r) => (r.ok ? r.json() : null))
      .then(setMe)
      .catch(() => setMe(null));
  }, []);
  return (
    <button className="cc-user small" onClick={() => (location.hash = "settings")} title="account">
      <Icon name="user" size={15} />
      {me ? `${me.name}${me.demo ? "" : ` (${me.role})`}` : "sign in"}
    </button>
  );
}
