// One machine, in the right column: the owner's question first, then the live sound, what changed,
// the 14-day trend, the countdown, and the numbers behind them.
import { useEffect, useState } from "react";
import type { Alert, Explanation, Factory, History, Machine, Message, Meta, Route } from "../api";
import { post, store, useFrame } from "../api";
import { fmtClock, mechanicLine, pct, SHAPE_WORD, sourceWord, STATUS_WORD, twinWords } from "../theme";
import { ApprovalCard } from "./ApprovalBar";
import { DiagnosisBlock } from "./Diagnosis";
import { HealthRing, Spectro2D } from "./bits";
import { ExplainCard } from "./Explain";
import { ForecastBlock } from "./Forecast";
import type { Loaded } from "./hooks";
import { playClip, stopClip } from "./hooks";
import { TimeMachine } from "./TimeMachine";

export interface TM {
  idx: number | null;
  playing: boolean;
  setIdx: (i: number) => void;
  play: () => void;
  live: () => void;
}

export function MachinePanel({
  m,
  meta,
  tickHours,
  onClose,
  flat,
  hist,
  ex,
  tm,
  messages,
  route,
  hotBands = null,
  alerts,
  tick,
}: {
  m: Machine;
  meta: Meta;
  tickHours: number;
  onClose: () => void;
  flat?: boolean;
  hist: Loaded<History>;
  ex: Loaded<Explanation>;
  tm: TM;
  messages: Message[];
  route: Route | null;
  hotBands?: [number, number] | null;
  alerts: Alert[];
  tick: number;
}) {
  const f = meta.factories.find((x) => x.id === m.factory) as Factory;
  const frame = useFrame()[m.id];
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    store.prefill(m.id);
    return stopClip;
  }, [m.id]);
  const ev = meta.eval?.types[m.type];
  const head = ev?.conditions[ev.headline];
  const unit = ev?.per_unit[m.source];
  const l = m.latest;
  const past = tm.idx !== null;
  const flagged = m.status === "alarm" || m.status === "early_match" || m.status === "watch";
  const sound = (kind: "live" | "normal") => {
    setErr(null);
    const q = kind === "live" && past && l ? `?tick=${l.tick}` : "";
    playClip(`/api/audio/${m.id}/${kind}.wav${q}`, setErr);
  };

  return (
    <aside className="panel" aria-label={`${m.name} details`}>
      <header className="row between">
        <div className="row gap">
          <HealthRing m={m} size={36} />
          <div>
            <div className="title">
              {m.name} <span className="muted mono small">{m.id}</span>
            </div>
            <div className="small muted">
              {f.name} · {SHAPE_WORD[m.shape]}
            </div>
          </div>
        </div>
        <button className="btn ghost" onClick={onClose} aria-label="close" title="Esc">
          ✕
        </button>
      </header>

      {!past && <OperatingBlock m={m} />}

      {!past && <ApprovalCard machine={m} messages={messages} factories={meta.factories} route={route} />}

      <p className={`say ${m.status === "alarm" || m.status === "early_match" ? "hot" : ""}`}>
        {past && l ? <span className="muted">On {fmtClock(l.ts)}: </span> : null}
        {mechanicLine(m, meta.factories)}
      </p>

      <section className="spec-block">
        <div className="row between small">
          <b>{past ? "That reading's sound" : "Live sound"}</b>
          <span className="muted mono">
            {frame ? `${frame.energy.toFixed(0)} dBFS · ${pct(frame.risk)} of alarm` : ""}
          </span>
        </div>
        <Spectro2D m={m} height={flat ? 110 : 84} hotBands={hotBands} />
        <div className="small muted">time → · pitch ↑ · orange = bands far from its own normal</div>
        <div className="row gap">
          <button className="btn" onClick={() => sound("live")}>
            {past ? "Play that reading" : "Play live"}
          </button>
          <button className="btn ghost" onClick={() => sound("normal")}>
            Normal reference
          </button>
          <button className="btn ghost" onClick={stopClip} aria-label="stop">
            Stop
          </button>
        </div>
        {err && <p className="small accent">{err}</p>}
      </section>

      {flagged && !past && <ExplainCard id={m.id} ex={ex} />}

      {flagged && !past && <DiagnosisBlock id={m.id} tick={tick} alerts={alerts} />}

      {hist.data ? (
        <TimeMachine
          hist={hist.data}
          idx={tm.idx}
          playing={tm.playing}
          onIdx={tm.setIdx}
          onPlay={tm.play}
          onLive={tm.live}
        />
      ) : hist.error ? (
        <p className="small muted">Couldn&rsquo;t load this machine&rsquo;s history: {hist.error}.</p>
      ) : (
        <div className="skel" style={{ height: 112 }} aria-label="loading history" />
      )}

      <ForecastBlock m={m} rows={hist.data?.rows ?? []} tickHours={tickHours} />

      <details className="more">
        <summary className="small">Readings and model</summary>
        <table className="kv mono">
          <tbody>
            <tr>
              <th>status</th>
              <td>{STATUS_WORD[m.status]}</td>
            </tr>
            <tr>
              <th>last 3 readings</th>
              <td>{pct(m.risk3)} of alarm</td>
            </tr>
            <tr>
              <th>this reading</th>
              <td>{l ? `${pct(l.risk)} (score ${l.score.toFixed(3)} / ${l.threshold.toFixed(3)})` : "--"}</td>
            </tr>
            <tr>
              <th>registry</th>
              <td>
                {l?.match_cos != null && l.match_level != null
                  ? `cos ${l.match_cos.toFixed(2)} vs flag ${l.match_level.toFixed(2)}`
                  : "no fingerprint of this type"}
              </td>
            </tr>
            {m.known_fault && (
              <tr>
                <th>fault</th>
                <td>
                  {m.known_fault}
                  {m.registry?.owner_label ? ` ("${m.registry.owner_label}")` : ""}
                </td>
              </tr>
            )}
            <tr>
              <th>mode</th>
              <td>
                {m.mode}
                {m.mode === "onset" ? ` ${m.mix.toFixed(2)} SYNTHETIC` : ""}
              </td>
            </tr>
            <tr>
              <th>clip</th>
              <td className="clip">{l?.clip ?? "--"}</td>
            </tr>
          </tbody>
        </table>
        <p className="small muted">
          Recording: {sourceWord(m)}.{" "}
          {head && (
            <>
              The {m.type} model scored AUC <span className="mono">{head.auc.toFixed(3)}</span>, pAUC{" "}
              <span className="mono">{head.pauc.toFixed(3)}</span> on held-out clips
              {unit ? (
                <>
                  {" "}
                  (this unit <span className="mono">{unit.auc.toFixed(3)}</span>)
                </>
              ) : null}
              , run <span className="mono">{meta.eval?.run_id}</span>.
            </>
          )}
        </p>
      </details>
      <a className="report-link small" href={`/api/report/${f.id}.pdf`} target="_blank" rel="noreferrer">
        Savings report for {f.name} (PDF)
      </a>
    </aside>
  );
}

/** Running and sensor state as the backend reports it, maintenance status, and the SIMULATE controls. */
function OperatingBlock({ m }: { m: Machine }) {
  const op = m.operating;
  const mt = m.maintenance;
  const [busy, setBusy] = useState(false);
  if (!op) return null;
  const run = op.running === false ? "Stopped" : op.running === null ? "Unknown (no signal)" : "Running";
  const sensor =
    op.sensor === "live"
      ? "live, reading this cycle"
      : op.sensor === "stale"
        ? `no new reading since ${fmtClock(op.last_ts)}`
        : op.sensor === "lost"
          ? `signal lost; last reading ${fmtClock(op.last_ts)}`
          : "no reading yet";
  const set = (body: { run?: string; sensor?: string }) => {
    setBusy(true);
    post(`/api/machine/${m.id}/operating`, body).finally(() => setBusy(false));
  };
  return (
    <section className="op-block small">
      <div className="row between">
        <b>Sensor and operation</b>
        <span className="muted">{twinWords(m)}</span>
      </div>
      <div>
        Machine: {run} · Sensor: {sensor}
        {op.simulated && <span className="muted"> · SIMULATED state</span>}
      </div>
      {mt && (mt.job_booked || mt.last_repair) && (
        <div className="muted">
          {mt.job_booked && `Mechanic job ${mt.job_status ?? "sent"}. `}
          {mt.last_repair &&
            `Last repair ${fmtClock(mt.last_repair.at)}: ${mt.last_repair.outcome ?? "not verified yet"}.`}
        </div>
      )}
      <div className="row gap">
        <span className="muted">Simulate:</span>
        <button
          className="btn ghost"
          disabled={busy}
          onClick={() => set({ run: op.run === "stopped" ? "running" : "stopped" })}
        >
          {op.run === "stopped" ? "Start machine" : "Stop machine"}
        </button>
        <button
          className="btn ghost"
          disabled={busy}
          onClick={() => set({ sensor: op.sensor === "lost" ? "ok" : "lost" })}
        >
          {op.sensor === "lost" ? "Restore sensor" : "Drop sensor"}
        </button>
      </div>
    </section>
  );
}
