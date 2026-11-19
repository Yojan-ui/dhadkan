// Main screen: isometric Digital Twin with floating HUD panels, a machine side panel and the agent dock.
import { lazy, Suspense, useEffect, useState } from "react";
import { get, useLive, useMeta, type Machine } from "../api";
import { useReducedMotion } from "../theme";
import { ApprovalCard } from "../ui/ApprovalBar";
import { HealthRing, Spectro2D } from "../ui/bits";
import { DiagnosisBlock } from "../ui/Diagnosis";
import { playClip, useHistory } from "../ui/hooks";
import { AgentDock } from "./AgentDock";
import {
  Actions,
  Chips,
  Gauge,
  RiskList,
  ShiftChart,
  Spectrum,
  SpectroStrip,
  TrendReadout,
  type Twin,
} from "./Hud";
import "./iso.css";

const IsoScene = lazy(() => import("./IsoScene").then((m) => ({ default: m.IsoScene })));

export function TwinScreen() {
  const state = useLive();
  const meta = useMeta();
  const reduced = useReducedMotion();
  const [sel, setSel] = useState<string | null>(null);
  const [twin, setTwin] = useState<Twin | null>(null);
  const tick = state?.tick ?? -1;
  useEffect(() => {
    let on = true;
    get<Twin>(`/api/twin${sel ? `?focus=${sel}` : ""}`)
      .then((t) => on && setTwin(t))
      .catch(() => undefined);
    return () => {
      on = false;
    };
  }, [tick, sel, state?.version]);
  if (!state || !meta) return <main className="iso boot">Dhadkan · Digital Twin</main>;
  const focusId = sel ?? twin?.focus ?? null;
  const focus = state.machines.find((m) => m.id === focusId) ?? null;
  const f = meta.factories.find((x) => x.id === (focus?.factory ?? "A")) ?? meta.factories[0];
  return (
    <main className={`iso ${sel ? "picked" : ""}`}>
      <div className="stage">
        <Suspense fallback={null}>
          <IsoScene machines={state.machines} selected={sel} onPick={setSel} reduced={reduced} />
        </Suspense>
      </div>
      <div className="title">
        <h1>Dhadkan · Digital Twin</h1>
        <p lang="hi">
          {f?.name_local ?? f?.name} · {f?.area_local}
        </p>
      </div>
      <Chips t={twin} state={state} />
      <ShiftChart t={twin} />
      <RiskList t={twin} onPick={setSel} />
      <Spectrum m={focus} />
      <TrendReadout t={twin} />
      <SpectroStrip m={focus} />
      <Gauge t={twin} />
      <Actions t={twin} state={state} />
      {sel && focus && <SidePanel m={focus} onClose={() => setSel(null)} />}
      <AgentDock factory={f?.id ?? "A"} />
      <p className="demo-tag">Demo data: simulated factories replaying real recordings (DCASE, NASA IMS)</p>
    </main>
  );
}

function SidePanel({ m, onClose }: { m: Machine; onClose: () => void }) {
  const state = useLive();
  const meta = useMeta();
  const tick = state?.tick ?? -1;
  const hist = useHistory(m.id, tick);
  const [err, setErr] = useState<string | null>(null);
  if (!state || !meta) return null;
  const rows = hist.data?.machine === m.id ? hist.data.rows : [];
  const W = 300,
    H = 70,
    top = Math.max(1.5, ...rows.map((r) => r.risk));
  const pts = rows
    .map((r, i) => `${(i / Math.max(1, rows.length - 1)) * W},${H - (Math.min(r.risk, top) / top) * (H - 4)}`)
    .join(" ");
  return (
    <aside className="side" aria-label={`${m.name} details`}>
      <header>
        <HealthRing m={m} size={44} />
        <div>
          <h2>
            {m.name} {m.live && <b className="live-badge">LIVE</b>}
          </h2>
          {m.live && (
            <p className="small live-note">
              {m.live.state === "learning"
                ? `learning from the live recorder: ${m.live.progress.clips}/${m.live.progress.target} clips`
                : `live recorder · ${m.live.readings} clips`}
              {m.live.label ? ` · ${m.live.label}` : ""}
            </p>
          )}
          <p className="dim small">
            {meta.factories.find((x) => x.id === m.factory)?.name} · {m.type} · {m.id}
          </p>
        </div>
        <button className="x" onClick={onClose} aria-label="close">
          ×
        </button>
      </header>
      <h3>Live spectrogram</h3>
      <Spectro2D m={m} height={96} />
      <div className="row">
        {(["before", "now"] as const).map((w) => (
          <button key={w} onClick={() => playClip(`/api/explain/${m.id}/${w}.wav`, setErr)}>
            {w === "before" ? "Before · 3 s" : "Now · 3 s"}
          </button>
        ))}
      </div>
      {err && <p className="err small">{err}</p>}
      <h3>Last 14 days</h3>
      <svg viewBox={`0 0 ${W} ${H}`} className="trend">
        <line
          x1={0}
          x2={W}
          y1={H - (1 / top) * (H - 4)}
          y2={H - (1 / top) * (H - 4)}
          stroke="#FF4D2E"
          strokeDasharray="3 4"
          opacity={0.6}
        />
        {rows.length > 1 && <polyline points={pts} fill="none" stroke="#2FD4A3" strokeWidth={2} />}
      </svg>
      <ApprovalCard machine={m} messages={state.messages} factories={meta.factories} route={state.route} />
      <DiagnosisBlock id={m.id} tick={tick} alerts={state.alerts} />
    </aside>
  );
}
