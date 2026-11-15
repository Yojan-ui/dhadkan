// One machine: plain words first, then the time machine, the countdown, what changed, and the numbers.
import { useEffect, useState } from "react";
import type { Explanation, Factory, History, Machine, Meta } from "../api";
import { store, useFrame } from "../api";
import { fmtClock, mechanicLine, pct, SHAPE_WORD, sourceWord, STATUS_WORD } from "../theme";
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
}: {
  m: Machine;
  meta: Meta;
  tickHours: number;
  onClose: () => void;
  flat?: boolean;
  hist: Loaded<History>;
  ex: Loaded<Explanation>;
  tm: TM;
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
          <HealthRing m={m} size={40} />
          <div>
            <div className="title">
              {m.name} <span className="muted mono small">{m.id}</span>
            </div>
            <div className="small muted">
              {f.name} · {f.area} · {SHAPE_WORD[m.shape]}
            </div>
          </div>
        </div>
        <button className="btn ghost" onClick={onClose} aria-label="close" title="Esc">
          Back
        </button>
      </header>

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

      <p className={`say ${m.status === "alarm" || m.status === "early_match" ? "hot" : ""}`}>
        {past && l ? <span className="muted">On {fmtClock(l.ts)}: </span> : null}
        {mechanicLine(m, meta.factories)}
      </p>

      {flat && <Spectro2D m={m} />}
      {!flat && (
        <p className="small muted">
          The surface beside the machine is {past ? "that reading's" : "this reading's"} sound: left = low
          pitch, right = high, height = level against normal; orange = the band that changed.
        </p>
      )}

      <div className="row gap">
        <button className="btn" onClick={() => sound("live")} title="P">
          {past ? "Play that reading" : "Play live sound"}
        </button>
        <button className="btn ghost" onClick={() => sound("normal")}>
          Play normal reference
        </button>
        <button className="btn ghost" onClick={stopClip}>
          Stop
        </button>
      </div>
      {err && <p className="small accent">{err}</p>}

      <ForecastBlock m={m} rows={hist.data?.rows ?? []} tickHours={tickHours} />

      {flagged && !past && <ExplainCard id={m.id} ex={ex} />}

      <table className="kv mono">
        <tbody>
          <tr>
            <th>status</th>
            <td>{STATUS_WORD[m.status]}</td>
          </tr>
          <tr>
            <th>last 3 readings</th>
            <td>
              {pct(m.risk3)} <span className="muted">of its alarm level</span>
            </td>
          </tr>
          <tr>
            <th>this reading</th>
            <td>
              {l ? `${pct(l.risk)} (score ${l.score.toFixed(3)}, alarm ${l.threshold.toFixed(3)})` : "--"}
            </td>
          </tr>
          <tr>
            <th>this second</th>
            <td>{frame ? `${pct(frame.risk)} · ${frame.energy.toFixed(0)} dBFS` : "--"}</td>
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
    </aside>
  );
}
