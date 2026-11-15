import { lazy, Suspense, useEffect, useState } from "react";
import { post, useConnected, useFrame, useLive, useMeta, type Machine } from "./api";
import type { View } from "./scene/CameraRig";
import { fmtClock, fmtT, mechanicLine, ringCSS, usePhone, useReducedMotion } from "./theme";
import { ApprovalBar } from "./ui/ApprovalBar";
import { Drawer, Evaluation, Log, Operator } from "./ui/Drawers";
import { MachinePanel } from "./ui/MachinePanel";
import { PhoneList } from "./ui/PhoneList";

const Scene = lazy(() => import("./scene/Scene"));   // phones never download three.js

type DrawerName = "operator" | "eval" | "log" | null;

const pointer = { x: -999, y: -999 };
window.addEventListener("pointermove", (e) => { pointer.x = e.clientX; pointer.y = e.clientY; });

export default function App() {
  const state = useLive();
  const meta = useMeta();
  const connected = useConnected();
  const phone = usePhone();
  const reduced = useReducedMotion();
  const [view, setView] = useState<View>(() => {
    const m = new URLSearchParams(location.search).get("m");     // ?m=B-F1 opens a machine directly
    return m ? { kind: "machine", id: m } : { kind: "cluster" };
  });
  const [hover, setHover] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<DrawerName>(null);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (drawer) setDrawer(null);
      else setView((v) => (v.kind === "machine" ? { kind: "shed", factory: state?.machines.find((m) => m.id === v.id)?.factory ?? "A" } : { kind: "cluster" }));
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [drawer, state]);

  if (!state || !meta) {
    return <div className="boot mono">{connected ? "loading..." : "connecting to the Dhadkan server (uvicorn dhadkan.live:create_app --factory)..."}</div>;
  }

  const sel = view.kind === "machine" ? state.machines.find((m) => m.id === view.id) : undefined;
  const shed = view.kind === "shed" ? meta.factories.find((f) => f.id === view.factory) : sel ? meta.factories.find((f) => f.id === sel.factory) : undefined;
  const hovered = hover ? state.machines.find((m) => m.id === hover) : undefined;
  const replay = state.replay;

  return (
    <div className={`app ${phone ? "is-phone" : ""} ${sel ? "has-panel" : ""}`}>
      <header className="top">
        <div className="brand">
          <b>Dhadkan</b>
          <span className="muted small">machine sound watch · 3 sheds · 12 machines</span>
        </div>
        <div className="clock mono" title="simulated clock; one reading = one 10 s clip">
          {state.clock ? fmtClock(state.clock) : "no readings"} <span className="muted">· reading {state.tick + 1}</span>
          {!connected && <span className="accent"> · offline</span>}
        </div>
        <div className="row gap">
          <label className={`switch ${replay.on ? "on" : ""}`}>
            <input type="checkbox" checked={replay.on} onChange={(e) => post("/api/replay", { on: e.target.checked })} />
            <span className="knob" aria-hidden />
            <span>Demo replay</span>
            {(replay.on || replay.phase !== "off") && <span className="mono small muted">{fmtT(replay.t)} · {replay.phase}</span>}
          </label>
          {!phone && <button className={`btn ghost ${drawer === "operator" ? "on" : ""}`} onClick={() => setDrawer(drawer === "operator" ? null : "operator")}>Operator</button>}
          <button className={`btn ghost ${drawer === "eval" ? "on" : ""}`} onClick={() => setDrawer(drawer === "eval" ? null : "eval")}>Evaluation</button>
          {!phone && <button className={`btn ghost ${drawer === "log" ? "on" : ""}`} onClick={() => setDrawer(drawer === "log" ? null : "log")}>Log</button>}
        </div>
      </header>

      {!phone && (
        <nav className="crumbs small" aria-label="view">
          <button onClick={() => setView({ kind: "cluster" })} className={view.kind === "cluster" ? "on" : ""}>Cluster</button>
          {shed && <><span>/</span><button onClick={() => setView({ kind: "shed", factory: shed.id })} className={view.kind === "shed" ? "on" : ""}>{shed.name}</button></>}
          {sel && <><span>/</span><button className="on">{sel.name}</button></>}
        </nav>
      )}

      <main className="stage">
        {phone ? (
          sel ? <MachinePanel m={sel} meta={meta} tickHours={state.tick_hours} onClose={() => setView({ kind: "cluster" })} flat />
              : <PhoneList state={state} meta={meta} onPick={(id) => setView({ kind: "machine", id })} />
        ) : (
          <Suspense fallback={<div className="boot mono">loading 3D...</div>}>
            <Scene state={state} meta={meta} view={view} reduced={reduced} hover={hover} onHover={setHover} onView={setView} />
          </Suspense>
        )}
        {!phone && state.tick < 0 && (
          <div className="empty">
            <p>No readings yet.</p>
            <p className="small muted">Switch on <b>Demo replay</b> (a scripted fault on {meta.fault_machine} at 0:40), or open <b>Operator</b> and press +1 reading.</p>
          </div>
        )}
        {!phone && sel && <MachinePanel m={sel} meta={meta} tickHours={state.tick_hours} onClose={() => setView({ kind: "shed", factory: sel.factory })} />}
        {!phone && <Legend />}
      </main>

      {!phone && hovered && <Tooltip m={hovered} meta={meta} />}

      <ApprovalBar messages={state.messages} factories={meta.factories} machines={state.machines} route={state.route} reduced={reduced} />

      {drawer === "operator" && <Drawer title="Operator" onClose={() => setDrawer(null)} reduced={reduced}><Operator state={state} meta={meta} /></Drawer>}
      {drawer === "eval" && <Drawer title="Evaluation" onClose={() => setDrawer(null)} reduced={reduced}><Evaluation meta={meta} /></Drawer>}
      {drawer === "log" && <Drawer title="Decision log" onClose={() => setDrawer(null)} reduced={reduced}><Log state={state} /></Drawer>}
    </div>
  );
}

function Tooltip({ m, meta }: { m: Machine; meta: NonNullable<ReturnType<typeof useMeta>> }) {
  const fr = useFrame()[m.id];
  const [{ x, y }, setXY] = useState(() => ({ x: pointer.x, y: pointer.y }));
  useEffect(() => {
    const f = (e: PointerEvent) => setXY({ x: e.clientX, y: e.clientY });
    window.addEventListener("pointermove", f);
    return () => window.removeEventListener("pointermove", f);
  }, []);
  const f = meta.factories.find((q) => q.id === m.factory);
  return (
    <div className="tooltip" style={{ left: Math.min(x + 16, window.innerWidth - 300), top: y + 16 }} role="tooltip">
      <div className="row between"><b>{m.name}</b><span className="mono small muted">{m.id}</span></div>
      <div className="small muted">{f?.name}</div>
      <div className="say" style={{ borderColor: ringCSS(m) }}>{mechanicLine(m, meta.factories)}</div>
      <div className="mono small muted">
        risk {m.risk3?.toFixed(2) ?? "--"} · live {fr ? fr.risk.toFixed(2) : "--"} · {fr ? `${fr.energy.toFixed(0)} dBFS` : "--"}
      </div>
    </div>
  );
}

function Legend() {
  return (
    <div className="legend small">
      <div><span className="ring-sw" /> ring fills clockwise toward the machine's own alarm level: grey ok, amber near, orange alarm or registry match</div>
      <div>shake = live anomaly score of the current 1 s window · ripples = loudness</div>
      <div className="muted">Hover for a plain reading; click a shed, then a machine. Esc goes back.</div>
    </div>
  );
}
