// One machine, in the right column: the owner's question first, then the live sound, what changed,
// the 14-day trend, the countdown, and the numbers behind them.
import { useEffect, useState } from "react";
import type { Explanation, Factory, History, Machine, Message, Meta, Route } from "../api";
import { store, useFrame } from "../api";
import { fmtClock, mechanicLine, pct, SHAPE_WORD, sourceWord, STATUS_WORD } from "../theme";
import { ApprovalCard } from "./ApprovalBar";
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
