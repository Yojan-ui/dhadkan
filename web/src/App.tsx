import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { LazyMotion, domAnimation } from "framer-motion";
import {
  get,
  post,
  store,
  useConnected,
  useFrame,
  useLive,
  useMeta,
  type Machine,
  type PastReading,
} from "./api";
import type { View } from "./scene/CameraRig";
import { fmtClock, fmtT, inr, mechanicLine, pct, ringCSS, usePhone, useReducedMotion } from "./theme";
import { ApprovalBar } from "./ui/ApprovalBar";
import { Drawer, Evaluation, Log, Operator } from "./ui/Drawers";
import { useExplain, useHistory } from "./ui/hooks";
import { MachinePanel, type TM } from "./ui/MachinePanel";
import { PhoneList } from "./ui/PhoneList";
import { Shortcuts, useShortcuts } from "./ui/Shortcuts";
import { BootSkeleton } from "./ui/Skeleton";

// Desktop starts fetching the 3D code at once, in parallel with the first state; phones never download it.
const sceneChunk = window.matchMedia("(max-width: 767px)").matches ? null : import("./scene/Scene");
const Scene = lazy(() => sceneChunk ?? import("./scene/Scene"));

type DrawerName = "operator" | "eval" | "log" | "keys" | null;

const pointer = { x: -999, y: -999 };
window.addEventListener("pointermove", (e) => {
  pointer.x = e.clientX;
  pointer.y = e.clientY;
});

export default function App() {
  return (
    <LazyMotion features={domAnimation} strict>
      <Main />
    </LazyMotion>
  );
}

function Main() {
  const state = useLive();
  const meta = useMeta();
  const connected = useConnected();
  const phone = usePhone();
  const reduced = useReducedMotion();
  const [view, setView] = useState<View>(() => {
    const m = new URLSearchParams(location.search).get("m"); // ?m=B-F1 opens a machine directly
    return m ? { kind: "machine", id: m } : { kind: "cluster" };
  });
  const [hover, setHover] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<DrawerName>(null);

  const selId = view.kind === "machine" ? view.id : null;
  const tick = state?.tick ?? -1;
  const hist = useHistory(selId, tick);
  const histRows = useMemo(
    () => (selId && hist.data?.machine === selId ? hist.data.rows : []),
    [selId, hist.data],
  );

  // ---- time machine: idx into the selected machine's history rows; null = live
  const [idx, setIdx] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [pastReading, setPastReading] = useState<PastReading | null>(null);
  const live = useCallback(() => {
    setIdx(null);
    setPlaying(false);
  }, []);
  useEffect(live, [selId, live]);
  const row = idx !== null ? histRows[idx] : undefined;
  useEffect(() => {
    if (!selId || !row) {
      store.backToLive();
      setPastReading(null);
      return;
    }
    let on = true;
    get<PastReading>(`/api/reading/${selId}?tick=${row.tick}`)
      .then((r) => {
        if (!on) return;
        store.travel(selId, r);
        setPastReading(r);
      })
      .catch(() => on && setPastReading(null));
    return () => {
      on = false;
    };
  }, [selId, row]);
  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => {
      setIdx((i) => {
        const next = (i ?? -1) + 1;
        if (next >= histRows.length - 1) {
          setPlaying(false);
          return null;
        }
        return next;
      });
    }, 600);
    return () => clearInterval(t);
  }, [playing, histRows.length]);
  const step = useCallback(
    (n: number) => {
      if (!histRows.length) return;
      setPlaying(false);
      setIdx((i) => {
        const next = Math.max(0, Math.min(histRows.length - 1, (i ?? histRows.length - 1) + n));
        return next === histRows.length - 1 ? null : next;
      });
    },
    [histRows.length],
  );
  const play = useCallback(() => {
    if (playing) setPlaying(false);
    else {
      if (idx === null) setIdx(0);
      setPlaying(true);
    }
  }, [playing, idx]);
  const tm: TM = {
    idx,
    playing,
    setIdx: (i) => {
      setPlaying(false);
      setIdx(i >= histRows.length - 1 ? null : i);
    },
    play,
    live,
  };

  // the selected machine as it stood at the scrubbed reading
  const machines = useMemo<Machine[]>(() => {
    if (!state) return [];
    if (!row || !pastReading || !selId) return state.machines;
    return state.machines.map((m) =>
      m.id !== selId
        ? m
        : {
            ...m,
            risk3: row.risk3,
            status: row.status,
            mode: row.mode,
            mix: row.mix,
            band_z: pastReading.band_z,
            forecast: row.forecast ?? m.forecast,
            rupees_at_risk: 0,
            // only what was known then: the owner's label and registry matches came later
            known_fault: null,
            registry: null,
            days_rising: daysRising(histRows.slice(0, (idx ?? 0) + 1)),
            latest: m.latest && {
              ...m.latest,
              tick: row.tick,
              ts: row.ts,
              risk: row.risk,
              score: pastReading.score,
              clip: pastReading.clip,
              mode: row.mode,
              mix: row.mix,
              match_cos: null,
              match_level: null,
            },
          },
    );
  }, [state, row, pastReading, selId, histRows, idx]);

  const sel = selId ? machines.find((m) => m.id === selId) : undefined;
  const ex = useExplain(selId, tick, Boolean(sel && idx === null));
  const shed =
    meta && view.kind === "shed"
      ? meta.factories.find((f) => f.id === view.factory)
      : sel && meta
        ? meta.factories.find((f) => f.id === sel.factory)
        : undefined;

  const back = useCallback(() => {
    if (drawer) return setDrawer(null);
    setView((v) =>
      v.kind === "machine"
        ? { kind: "shed", factory: state?.machines.find((m) => m.id === v.id)?.factory ?? "A" }
        : { kind: "cluster" },
    );
  }, [drawer, state]);

  useShortcuts({
    back,
    help: () => setDrawer((d) => (d === "keys" ? null : "keys")),
    drawer: (d) => setDrawer((cur) => (cur === d ? null : d)),
    cluster: () => setView({ kind: "cluster" }),
    shed: (i) => {
      const f = meta?.factories[i];
      if (f) setView({ kind: "shed", factory: f.id });
    },
    machine: (dir) => {
      if (!state) return;
      const inShed = state.machines.filter((m) => m.factory === (sel?.factory ?? shed?.id));
      if (!inShed.length) return;
      const at = inShed.findIndex((m) => m.id === sel?.id);
      const next = inShed[(at + dir + inShed.length) % inShed.length];
      setView({ kind: "machine", id: next.id });
    },
    step: sel ? step : undefined,
    play: sel ? play : undefined,
    live: sel ? live : undefined,
    replay: () => {
      if (state) post("/api/replay", { on: !state.replay.on });
    },
  });

  if (!state || !meta) return <BootSkeleton phone={phone} connected={connected} />;

  const hovered = hover ? machines.find((m) => m.id === hover) : undefined;
  const replay = state.replay;
  const seeding = replay.on && replay.t === null && (replay.progress ?? 1) < 1;
  const panel = sel && (
    <MachinePanel
      m={sel}
      meta={meta}
      tickHours={state.tick_hours}
      onClose={phone ? () => setView({ kind: "cluster" }) : back}
      flat={phone}
      hist={hist}
      ex={ex}
      tm={tm}
    />
  );
  const toggle = (d: DrawerName) => setDrawer(drawer === d ? null : d);

  return (
    <div className={`app ${phone ? "is-phone" : ""} ${sel ? "has-panel" : ""}`}>
      <header className="top">
        <div className="brand">
          <b>Dhadkan</b>
          <span className="muted small">machine sound watch · 3 sheds · 12 machines</span>
        </div>
        <div className="clock mono" title="simulated clock; one reading = one 10 s clip">
          {state.clock ? fmtClock(state.clock) : "no readings"}{" "}
          <span className="muted">· reading {state.tick + 1}</span>
          {!state.link.up && <span className="accent"> · shed offline</span>}
          {state.link.pending > 0 && (
            <span className="accent">
              {" "}
              · {state.link.pending} fingerprint{state.link.pending > 1 ? "s" : ""} waiting for signal
            </span>
          )}
        </div>
        <div className="row gap">
          <label className={`switch ${replay.on ? "on" : ""}`}>
            <input
              type="checkbox"
              title="Restarts the cluster from empty and plays the scripted demo (D)"
              checked={replay.on}
              onChange={(e) => post("/api/replay", { on: e.target.checked })}
            />
            <span className="knob" aria-hidden />
            <span>Demo replay</span>
            {replay.on ? (
              <span className="mono small muted">
                {replay.t === null ? replay.phase : `${fmtT(replay.t)} · ${replay.phase}`}
              </span>
            ) : (
              replay.phase === "awaiting owner" && <span className="mono small muted">finished</span>
            )}
          </label>
          {!phone && (
            <button
              className={`btn ghost ${drawer === "operator" ? "on" : ""}`}
              onClick={() => toggle("operator")}
              title="O"
            >
              Operator
            </button>
          )}
          <button
            className={`btn ghost ${drawer === "eval" ? "on" : ""}`}
            onClick={() => toggle("eval")}
            title="E"
          >
            Evaluation
          </button>
          {!phone && (
            <>
              <button
                className={`btn ghost ${drawer === "log" ? "on" : ""}`}
                onClick={() => toggle("log")}
                title="L"
              >
                Log
              </button>
              <button
                className="btn ghost"
                onClick={() => toggle("keys")}
                aria-label="keyboard shortcuts"
                title="?"
              >
                ?
              </button>
            </>
          )}
        </div>
      </header>

      {!connected && (
        <div className="banner" role="status">
          Lost the connection to the server. Retrying every 1.5 s; the last readings stay on screen.
        </div>
      )}

      {!phone && (
        <nav className="crumbs small" aria-label="view">
          <button
            onClick={() => setView({ kind: "cluster" })}
            className={view.kind === "cluster" ? "on" : ""}
          >
            Cluster
          </button>
          {shed && (
            <>
              <span>/</span>
              <button
                onClick={() => setView({ kind: "shed", factory: shed.id })}
                className={view.kind === "shed" ? "on" : ""}
              >
                {shed.name}
              </button>
            </>
          )}
          {sel && (
            <>
              <span>/</span>
              <button className="on">{sel.name}</button>
            </>
          )}
          {shed && (
            <a className="report-link" href={`/api/report/${shed.id}.pdf`} target="_blank" rel="noreferrer">
              Savings report (PDF)
            </a>
          )}
        </nav>
      )}

      <main className="stage">
        {phone ? (
          (panel ?? <PhoneList state={state} meta={meta} onPick={(id) => setView({ kind: "machine", id })} />)
        ) : (
          <Suspense fallback={<div className="skel scene-skel" aria-label="loading the 3D view" />}>
            <Scene
              state={state}
              machines={machines}
              meta={meta}
              view={view}
              reduced={reduced}
              hover={hover}
              onHover={setHover}
              onView={setView}
              hotBands={
                idx === null && ex.data?.status === "ok" ? [ex.data.lo_band ?? 0, ex.data.hi_band ?? 0] : null
              }
            />
          </Suspense>
        )}
        {seeding && (
          <div className="empty" role="status">
            <p>Recording 14 days of readings&hellip;</p>
            <div className="bar">
              <span style={{ width: `${Math.round((replay.progress ?? 0) * 100)}%` }} />
            </div>
            <p className="small muted">
              Real MIMII clips, scored by the phone model, on a simulated clock. A bearing sound starts to
              grow on Blower 1 in Shinde Engineering from day 6.
            </p>
          </div>
        )}
        {!phone && !seeding && state.tick < 0 && (
          <div className="empty">
            <p>No readings yet.</p>
            <p className="small muted">
              Switch on <b>Demo replay</b> (press D): it records 14 days, then plays live with a fault on{" "}
              {meta.fault_machine} at 0:40. Or open <b>Operator</b> and press +1 reading.
            </p>
          </div>
        )}
        {!phone && panel}
        {!phone && <Legend />}
      </main>

      {!phone && hovered && <Tooltip m={hovered} meta={meta} />}

      <ApprovalBar
        messages={state.messages}
        factories={meta.factories}
        machines={state.machines}
        route={state.route}
        reduced={reduced}
      />

      {drawer === "operator" && (
        <Drawer title="Operator" onClose={() => setDrawer(null)} reduced={reduced}>
          <Operator state={state} meta={meta} />
        </Drawer>
      )}
      {drawer === "eval" && (
        <Drawer title="Evaluation" onClose={() => setDrawer(null)} reduced={reduced}>
          <Evaluation meta={meta} />
        </Drawer>
      )}
      {drawer === "log" && (
        <Drawer title="Decision log" onClose={() => setDrawer(null)} reduced={reduced}>
          <Log state={state} />
        </Drawer>
      )}
      {drawer === "keys" && (
        <Drawer title="Keyboard shortcuts" onClose={() => setDrawer(null)} reduced={reduced}>
          <Shortcuts />
        </Drawer>
      )}
    </div>
  );
}

/** Consecutive days (incl. the last) the daily mean risk went up; 0 = not rising. Same rule as the agent. */
function daysRising(rows: { ts: string; risk: number }[]) {
  const byDay = new Map<string, number[]>();
  for (const r of rows) byDay.set(r.ts.slice(0, 10), [...(byDay.get(r.ts.slice(0, 10)) ?? []), r.risk]);
  const means = [...byDay.values()].map((v) => v.reduce((a, b) => a + b, 0) / v.length);
  let k = 0;
  while (k + 1 < means.length && means[means.length - 1 - k] > means[means.length - 2 - k]) k++;
  return k ? k + 1 : 0;
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
    <div
      className="tooltip"
      style={{ left: Math.min(x + 16, window.innerWidth - 300), top: y + 16 }}
      role="tooltip"
    >
      <div className="row between">
        <b>{m.name}</b>
        <span className="mono small muted">{m.id}</span>
      </div>
      <div className="small muted">{f?.name}</div>
      <div className="say" style={{ borderColor: ringCSS(m) }}>
        {mechanicLine(m, meta.factories)}
      </div>
      {m.rupees_at_risk > 0 && (
        <div className="small accent mono">{inr(m.rupees_at_risk)} at risk in the next 2 days</div>
      )}
      <div className="mono small muted">
        {pct(m.risk3)} of alarm, last 3 readings · this second {pct(fr?.risk)} ·{" "}
        {fr ? `${fr.energy.toFixed(0)} dBFS` : "--"}
      </div>
    </div>
  );
}

function Legend() {
  const [open, setOpen] = useState(false);
  return (
    <div className="legend small">
      <button className="btn ghost small" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Hide key" : "How to read this"}
      </button>
      {open && (
        <div className="legend-body">
          <div>
            <span className="ring-sw" /> ring fills clockwise toward the machine&rsquo;s own alarm level: grey
            ok, amber near, orange alarm or registry match
          </div>
          <div>shake = live anomaly score of the current 1 s window · ripples = loudness</div>
          <div className="muted">
            Hover for a plain reading; click a shed, then a machine. Press ? for keys.
          </div>
        </div>
      )}
    </div>
  );
}
