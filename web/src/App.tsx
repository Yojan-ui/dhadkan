import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { MechanicScreen } from "./screens/Mechanic";
import { MillScreen } from "./screens/Mill";
import { OwnerScreen } from "./screens/Owner";
import { LazyMotion, domAnimation } from "framer-motion";
import { get, post, store, useConnected, useLive, useMeta, type Machine, type PastReading } from "./api";
import type { View } from "./scene/CameraRig";
import { fmtClock, fmtT, inr, usePhone, useReducedMotion } from "./theme";
import { ApprovalBar } from "./ui/ApprovalBar";
import { Attention } from "./ui/Attention";
import { DemoMode } from "./ui/DemoMode";
import { Drawer, Evaluation, Log, Operator } from "./ui/Drawers";
import { useExplain, useHistory } from "./ui/hooks";
import { MachinePanel, type TM } from "./ui/MachinePanel";
import { PhoneList } from "./ui/PhoneList";
import { Help, useShortcuts } from "./ui/Shortcuts";
import { BootSkeleton } from "./ui/Skeleton";

// Desktop starts fetching the 3D code at once, in parallel with the first state; phones never download it.
const sceneChunk = window.matchMedia("(max-width: 767px)").matches ? null : import("./scene/Scene");
const Scene = lazy(() => sceneChunk ?? import("./scene/Scene"));

type DrawerName = "operator" | "eval" | "log" | "keys" | "demo" | null;

export default function App() {
  const q = new URLSearchParams(location.search);
  const view = q.get("view"); // phone screens: ?view=owner&f=A&lang=hi, ?view=mechanic&f=A
  if (view === "owner") {
    const lang = (q.get("lang") ?? "hi") as "hi" | "hi_latn" | "mr" | "gu" | "en";
    return (
      <OwnerScreen
        factory={q.get("f") ?? "A"}
        lang={["hi", "hi_latn", "mr", "gu", "en"].includes(lang) ? lang : "hi"}
      />
    );
  }
  if (view === "mechanic") return <MechanicScreen factory={q.get("f")} />;
  if (view === "mill") return <MillScreen />;
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
    setView({ kind: "cluster" });
  }, [drawer]);

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

  const replay = state.replay;
  const seeding = replay.on && replay.t === null && (replay.progress ?? 1) < 1;
  const panel = sel && (
    <MachinePanel
      m={sel}
      meta={meta}
      tickHours={state.tick_hours}
      onClose={back}
      flat={phone}
      hist={hist}
      ex={ex}
      tm={tm}
      messages={state.messages}
      route={state.route}
      alerts={state.alerts}
      tick={state.tick}
      hotBands={
        idx === null && ex.data?.status === "ok" ? [ex.data.lo_band ?? 0, ex.data.hi_band ?? 0] : null
      }
    />
  );
  const toggle = (d: DrawerName) => setDrawer(drawer === d ? null : d);
  const right =
    drawer === "operator" ? (
      <Drawer title="Operator" onClose={() => setDrawer(null)} reduced={reduced}>
        <Operator state={state} meta={meta} />
      </Drawer>
    ) : drawer === "eval" ? (
      <Drawer title="Evaluation" onClose={() => setDrawer(null)} reduced={reduced}>
        <Evaluation meta={meta} />
      </Drawer>
    ) : drawer === "log" ? (
      <Drawer title="Decision log" onClose={() => setDrawer(null)} reduced={reduced}>
        <Log state={state} />
      </Drawer>
    ) : drawer === "demo" ? (
      <Drawer title="Demo mode" onClose={() => setDrawer(null)} reduced={reduced}>
        <DemoMode state={state} />
      </Drawer>
    ) : drawer === "keys" ? (
      <Drawer title="How to read this" onClose={() => setDrawer(null)} reduced={reduced}>
        <Help meta={meta} />
      </Drawer>
    ) : (
      panel
    );

  if (phone)
    return (
      <div className="app is-phone">
        <TopBar state={state} meta={meta} phone seeding={seeding} drawer={drawer} toggle={toggle} />
        <main className="stage">
          {right ?? <PhoneList state={state} meta={meta} onPick={(id) => setView({ kind: "machine", id })} />}
        </main>
        <ApprovalBar
          messages={state.messages}
          factories={meta.factories}
          machines={state.machines}
          route={state.route}
          reduced={reduced}
        />
      </div>
    );

  return (
    <div className="app desk">
      <TopBar state={state} meta={meta} phone={false} seeding={seeding} drawer={drawer} toggle={toggle} />
      {!connected && (
        <div className="banner" role="status">
          Lost the connection to the server. Retrying every 1.5 s; the last readings stay on screen.
        </div>
      )}
      <div className={`body ${right ? "with-right" : ""}`}>
        <Attention
          state={state}
          meta={meta}
          machines={machines}
          selected={selId}
          hover={hover}
          onPick={(id) => setView({ kind: "machine", id })}
          onHover={setHover}
        />
        <main className="stage">
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
            />
          </Suspense>
        </main>
        <div className="right" aria-hidden={!right}>
          {right}
        </div>
      </div>
    </div>
  );
}

/** Big numbers only: how many machines are fine, near alarm, alerting; rupees at risk; the next stop. */
function TopBar({
  state,
  meta,
  phone,
  seeding,
  drawer,
  toggle,
}: {
  state: NonNullable<ReturnType<typeof useLive>>;
  meta: NonNullable<ReturnType<typeof useMeta>>;
  phone: boolean;
  seeding: boolean;
  drawer: DrawerName;
  toggle: (d: DrawerName) => void;
}) {
  const ms = state.machines.filter((m) => m.status !== "no_data");
  const ok = ms.filter((m) => m.status === "ok").length;
  const watch = ms.filter((m) => m.status === "watch").length;
  const alert = ms.filter((m) => m.status === "alarm" || m.status === "early_match").length;
  const rupees = state.machines.reduce((a, m) => a + m.rupees_at_risk, 0);
  const leg = state.route?.legs[0];
  const legMachine = leg && state.machines.find((m) => m.id === leg.machine);
  const legFactory = leg && meta.factories.find((f) => f.id === leg.factory);
  const replay = state.replay;
  return (
    <header className="top">
      <div className="brand">
        <b>Dhadkan</b>
        <span className="muted small mono">{state.clock ? fmtClock(state.clock) : "no readings"}</span>
      </div>
      {seeding ? (
        <div className="kpis">
          <div className="kpi wide">
            <span className="kpi-label">Recording 14 days of readings</span>
            <div className="bar">
              <span style={{ width: `${Math.round((replay.progress ?? 0) * 100)}%` }} />
            </div>
          </div>
        </div>
      ) : (
        <div className="kpis" aria-label="cluster at a glance">
          <div className="kpi">
            <span className="kpi-num mono">{ok}</span>
            <span className="kpi-label">OK</span>
          </div>
          <div className="kpi">
            <span className="kpi-num mono amber">{watch}</span>
            <span className="kpi-label">watch</span>
          </div>
          <div className="kpi">
            <span className={`kpi-num mono ${alert ? "accent" : ""}`}>{alert}</span>
            <span className="kpi-label">alert</span>
          </div>
          {!phone && (
            <>
              <div className="kpi">
                <span className={`kpi-num mono ${rupees ? "accent" : ""}`}>{inr(rupees)}</span>
                <span className="kpi-label">at risk, next 2 days</span>
              </div>
              <div className="kpi wide">
                <span className="kpi-num small-num">
                  {leg
                    ? `${legMachine?.name ?? leg.machine} · ${legFactory?.name.split(" ")[0] ?? leg.factory}`
                    : "No visit planned"}
                </span>
                <span className="kpi-label">
                  {leg
                    ? `${state.route?.technician.split(" ")[0]}'s next stop · ${fmtClock(leg.arrive)}`
                    : "technician"}
                </span>
              </div>
            </>
          )}
        </div>
      )}
      <div className="row gap controls">
        <button
          className={`btn ${state.demo.on ? "yes" : "ghost"} ${drawer === "demo" ? "on" : ""}`}
          onClick={() => toggle("demo")}
          title="Replay a real bearing failure and send a WhatsApp alert"
        >
          Demo mode
        </button>
        <label className={`switch ${replay.on ? "on" : ""}`}>
          <input
            type="checkbox"
            title="Restarts the cluster from empty and plays the scripted demo (D)"
            checked={replay.on}
            onChange={(e) => post("/api/replay", { on: e.target.checked })}
          />
          <span className="knob" aria-hidden />
          <span>Demo</span>
          {replay.on && replay.t !== null && <span className="mono small muted">{fmtT(replay.t)}</span>}
        </label>
        {!phone && (
          <>
            <button
              className={`btn ghost ${drawer === "operator" ? "on" : ""}`}
              onClick={() => toggle("operator")}
              title="O"
            >
              Operator
            </button>
            <button
              className={`btn ghost ${drawer === "log" ? "on" : ""}`}
              onClick={() => toggle("log")}
              title="L"
            >
              Log
            </button>
          </>
        )}
        <button
          className={`btn ghost ${drawer === "eval" ? "on" : ""}`}
          onClick={() => toggle("eval")}
          title="E"
        >
          Evaluation
        </button>
        {!phone && (
          <button
            className={`btn ghost ${drawer === "keys" ? "on" : ""}`}
            onClick={() => toggle("keys")}
            aria-label="how to read this"
            title="?"
          >
            ?
          </button>
        )}
      </div>
    </header>
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
